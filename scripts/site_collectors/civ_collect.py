"""Civitai collector via the public REST API (no browser needed).

Collects model metadata + preview images and/or image posts + generation metadata, following the
cursor pagination, with rate-limit backoff, resume (cursor files + skip existing files), and
NSFW included. Writes JSONL metadata + downloaded image files.

Usage:
  python civ_collect.py --mode both --model-target 5000 --image-target 10000
  python civ_collect.py --mode images --image-target 20000 --no-files
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
from urllib.parse import quote

import aiohttp
from cdp_use import CDPClient

BASE = 'https://civitai.com/api/v1'
CDP_HTTP = os.environ.get('BROWSER_USE_CDP_HTTP', 'http://127.0.0.1:9222')
OUT_DIR = Path(os.environ.get('CIVITAI_OUT', str(Path.home() / 'civitai_export')))
HEADERS = {'User-Agent': 'Mozilla/5.0 reference-collector', 'Accept': 'application/json', 'Referer': 'https://civitai.com/'}
API_SLEEP = 0.8          # between metadata pages (respect rate limits)
IMG_CONCURRENCY = 6
GEN_CONCURRENCY = 6      # concurrent generation-data tRPC calls
PAGE_LIMIT_MODELS = 100
PAGE_LIMIT_IMAGES = 200


async def get_civitai_cookies() -> str:
	"""Read civitai.com cookies (incl. httpOnly session token) via CDP for authenticated tRPC."""
	async with aiohttp.ClientSession() as http:
		async with http.get(f'{CDP_HTTP}/json/version') as response:
			ws_url = (await response.json())['webSocketDebuggerUrl']
	async with CDPClient(ws_url) as client:
		targets = await client.send.Target.getTargets()
		page = next(t for t in targets['targetInfos'] if t['type'] == 'page' and 'civitai.com' in t.get('url', '') and 'red' not in t.get('url', ''))
		session = await client.send.Target.attachToTarget(params={'targetId': page['targetId'], 'flatten': True})
		sid = session['sessionId']
		await client.send.Network.enable(session_id=sid)
		result = await client.send.Network.getCookies(params={'urls': ['https://civitai.com']}, session_id=sid)
		return '; '.join(f"{c['name']}={c['value']}" for c in result.get('cookies', []))


async def fetch_generation(session: aiohttp.ClientSession, image_id: int, sem: asyncio.Semaphore) -> dict | None:
	"""Fetch full generation input (prompt/params/resources) for one image via the tRPC endpoint."""
	inp = quote(json.dumps({'json': {'id': image_id}}))
	url = f'https://civitai.com/api/trpc/image.getGenerationData?input={inp}'
	async with sem:
		for attempt in range(4):
			try:
				async with session.get(url) as r:
					if r.status == 200:
						payload = await r.json()
						data = (((payload.get('result') or {}).get('data') or {}).get('json') or {})
						if data.get('meta') or data.get('resources'):
							return {'process': data.get('process'), 'type': data.get('type'),
								'onSite': data.get('onSite'), 'meta': data.get('meta'), 'resources': data.get('resources')}
						return None
					if r.status in (429, 500, 502, 503):
						await asyncio.sleep(2 * (attempt + 1))
						continue
					return None
			except Exception:  # noqa: BLE001
				await asyncio.sleep(1)
	return None


async def _get_json(session: aiohttp.ClientSession, url: str, params: dict | None = None, tries: int = 6) -> dict:
	"""GET JSON with exponential backoff on 429/5xx."""
	delay = 2.0
	for attempt in range(tries):
		async with session.get(url, params=params) as r:
			if r.status == 200:
				return await r.json()
			if r.status in (429, 500, 502, 503, 504):
				await asyncio.sleep(delay)
				delay = min(delay * 2, 60)
				params = None  # nextPage url already carries params
				continue
			text = (await r.text())[:200]
			raise RuntimeError(f'HTTP {r.status} for {url}: {text}')
	raise RuntimeError(f'giving up after {tries} tries: {url}')


async def _download(session: aiohttp.ClientSession, url: str, dest: Path, sem: asyncio.Semaphore) -> bool:
	"""Download one image to dest (skip if present)."""
	if dest.exists() and dest.stat().st_size > 0:
		return True
	async with sem:
		for attempt in range(4):
			try:
				async with session.get(url) as r:
					if r.status == 200:
						dest.write_bytes(await r.read())
						return True
					if r.status == 429:
						await asyncio.sleep(3)
						continue
					return False
			except Exception:  # noqa: BLE001
				await asyncio.sleep(1)
	return False


def _level_is_safe(level) -> bool:
	"""Safe if the browsing level is None/'None' or the numeric safe level (1). Civitai encodes
	image nsfwLevel as the string 'None' or as an int bitmask where 1 == safe (PG)."""
	if level in (None, 'None'):
		return True
	if isinstance(level, int):
		return level <= 1
	return False


# The model `nsfw` boolean is unreliable (explicit models still report false). Exclude only the
# overtly-explicit ones: name/tag hints or the XXX bit (16) in the nsfwLevel bitmask. Mainstream
# checkpoints whose galleries top out at X (level 15) are kept; their previews are SFW-filtered.
_NSFW_HINTS = (
	'nsfw', 'lust', 'porn', 'sex', 'hentai', 'nude', 'naked', 'ntr', 'xxx', 'erotic', 'uncensored',
	'boob', 'milf', 'cum', 'pussy', 'cock', 'futa', 'bdsm', 'fetish', 'r18', 'r-18', '18+',
)
_NSFW_XXX_BIT = 16


def _model_is_sfw(model: dict) -> bool:
	"""Keep all but overtly-explicit models (moderate policy)."""
	if model.get('nsfw') is True:
		return False
	text = ((model.get('name') or '') + ' ' + ' '.join(model.get('tags') or [])).lower()
	if any(hint in text for hint in _NSFW_HINTS):
		return False
	level = model.get('nsfwLevel')
	if isinstance(level, int) and (level & _NSFW_XXX_BIT):
		return False
	return True


def _image_is_sfw(image: dict) -> bool:
	"""Images use a string nsfwLevel ('None' == safe) plus a boolean nsfw flag."""
	if image.get('nsfw') is True:
		return False
	return _level_is_safe(image.get('nsfwLevel'))


def _first_preview(model: dict) -> str | None:
	"""Return the first SFW preview image URL for a model (skip NSFW example images)."""
	for mv in model.get('modelVersions') or []:
		for img in mv.get('images') or []:
			if img.get('url') and _level_is_safe(img.get('nsfwLevel')):
				return img['url']
	return None


async def collect_models(session: aiohttp.ClientSession, target: int, download_files: bool) -> int:
	"""Cursor-paginate models; append metadata JSONL and download one preview per model."""
	d = OUT_DIR / 'models'
	d.mkdir(parents=True, exist_ok=True)
	previews = d / 'previews'
	previews.mkdir(exist_ok=True)
	cursor_file = d / '_cursor.txt'
	jsonl = d / 'models.jsonl'

	seen_file = d / '_seen_ids.txt'
	seen = set(seen_file.read_text(encoding='utf-8').split()) if seen_file.is_file() else set()
	sem = asyncio.Semaphore(IMG_CONCURRENCY)

	url = f'{BASE}/models'
	base_params = {'limit': str(PAGE_LIMIT_MODELS), 'nsfw': 'false', 'sort': 'Most Downloaded'}
	params: dict | None = dict(base_params)
	if cursor_file.is_file() and cursor_file.read_text(encoding='utf-8').strip():
		params = dict(base_params, cursor=cursor_file.read_text(encoding='utf-8').strip())

	count = len(seen)
	with jsonl.open('a', encoding='utf-8') as out:
		while count < target:
			data = await _get_json(session, url, params)
			items = data.get('items', [])
			if not items:
				break
			dl_tasks = []
			for m in items:
				mid = str(m['id'])
				if mid in seen:
					continue
				if not _model_is_sfw(m):  # belt-and-suspenders SFW filter
					continue
				seen.add(mid)
				out.write(json.dumps(m, ensure_ascii=False) + '\n')
				count += 1
				if download_files:
					preview = _first_preview(m)
					if preview:
						dl_tasks.append(_download(session, preview, previews / f'{mid}.jpg', sem))
				if count >= target:
					break
			if dl_tasks:
				await asyncio.gather(*dl_tasks)
			out.flush()
			cursor = (data.get('metadata') or {}).get('nextCursor')
			next_page = (data.get('metadata') or {}).get('nextPage')
			if cursor:
				cursor_file.write_text(str(cursor), encoding='utf-8')
			seen_file.write_text(' '.join(seen), encoding='utf-8')
			print(f'  models: {count} collected', flush=True)
			if not next_page:
				break
			url, params = next_page, None
			await asyncio.sleep(API_SLEEP)
	print(f'DONE models -> {count}', flush=True)
	return count


async def collect_images(session: aiohttp.ClientSession, target: int, download_files: bool) -> int:
	"""Cursor-paginate images; append metadata JSONL and download each image file."""
	d = OUT_DIR / 'images'
	d.mkdir(parents=True, exist_ok=True)
	files = d / 'files'
	files.mkdir(exist_ok=True)
	cursor_file = d / '_cursor.txt'
	jsonl = d / 'images.jsonl'

	seen_file = d / '_seen_ids.txt'
	seen = set(seen_file.read_text(encoding='utf-8').split()) if seen_file.is_file() else set()
	sem = asyncio.Semaphore(IMG_CONCURRENCY)

	url = f'{BASE}/images'
	base_params = {'limit': str(PAGE_LIMIT_IMAGES), 'nsfw': 'None', 'sort': 'Most Reactions'}
	params: dict | None = dict(base_params)
	if cursor_file.is_file() and cursor_file.read_text(encoding='utf-8').strip():
		params = dict(base_params, cursor=cursor_file.read_text(encoding='utf-8').strip())

	gen_sem = asyncio.Semaphore(GEN_CONCURRENCY)
	count = len(seen)
	with_gen = 0
	with jsonl.open('a', encoding='utf-8') as out:
		while count < target:
			data = await _get_json(session, url, params)
			items = data.get('items', [])
			if not items:
				break
			# Select the new SFW images on this page (up to the remaining target), then enrich each.
			new_items = []
			for im in items:
				iid = str(im['id'])
				if iid in seen or not _image_is_sfw(im):
					continue
				if count + len(new_items) >= target:
					break
				seen.add(iid)
				new_items.append(im)

			gens = await asyncio.gather(*(fetch_generation(session, im['id'], gen_sem) for im in new_items))
			dl_tasks = []
			for im, gen in zip(new_items, gens):
				im['generation'] = gen  # full prompt/params/resources, or None if the uploader hid it
				if gen:
					with_gen += 1
				out.write(json.dumps(im, ensure_ascii=False) + '\n')
				count += 1
				if download_files and im.get('url'):
					ext = '.jpeg' if '.jpeg' in im['url'] else '.jpg'
					dl_tasks.append(_download(session, im['url'], files / f"{im['id']}{ext}", sem))
			if dl_tasks:
				await asyncio.gather(*dl_tasks)
			out.flush()
			cursor = (data.get('metadata') or {}).get('nextCursor')
			next_page = (data.get('metadata') or {}).get('nextPage')
			if cursor:
				cursor_file.write_text(str(cursor), encoding='utf-8')
			seen_file.write_text(' '.join(seen), encoding='utf-8')
			print(f'  images: {count} collected ({with_gen} with generation data)', flush=True)
			if not next_page or count >= target:
				break
			url, params = next_page, None
			await asyncio.sleep(API_SLEEP)
	print(f'DONE images -> {count} ({with_gen} with generation data)', flush=True)
	return count


async def main() -> None:
	parser = argparse.ArgumentParser()
	parser.add_argument('--mode', choices=['models', 'images', 'both'], default='both')
	parser.add_argument('--model-target', type=int, default=5000)
	parser.add_argument('--image-target', type=int, default=10000)
	parser.add_argument('--no-files', action='store_true', help='skip image downloads (metadata only)')
	args = parser.parse_args()

	OUT_DIR.mkdir(parents=True, exist_ok=True)
	timeout = aiohttp.ClientTimeout(total=120)

	if args.mode in ('models',):
		async with aiohttp.ClientSession(headers=HEADERS, timeout=timeout) as session:
			print(f'=== MODELS (target {args.model_target}) ===', flush=True)
			await collect_models(session, args.model_target, not args.no_files)
		print('ALL DONE.', flush=True)
		return

	# Images need the authenticated session (cookies) to pull generation data via tRPC.
	cookie_header = await get_civitai_cookies()
	auth_headers = dict(HEADERS, Cookie=cookie_header)
	print(f'auth cookies loaded ({len(cookie_header)} chars)', flush=True)
	async with aiohttp.ClientSession(headers=auth_headers, timeout=timeout) as session:
		if args.mode == 'both':
			print(f'=== MODELS (target {args.model_target}) ===', flush=True)
			await collect_models(session, args.model_target, not args.no_files)
		print(f'=== IMAGES (target {args.image_target}) ===', flush=True)
		await collect_images(session, args.image_target, not args.no_files)
	print('ALL DONE.', flush=True)


if __name__ == '__main__':
	asyncio.run(main())
