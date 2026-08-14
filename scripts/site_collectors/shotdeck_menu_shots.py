"""Collect a bounded sample of actual shots for each option of ONE left-menu category.

Filter URL pattern:  #/media_type/Movie  ==  GET /browse/searchstillsajax/<metatype>/<value>...
Each option holds tens of thousands to millions of shots, so PER_OPTION bounds the sample.

Usage:
  python shotdeck_menu_shots.py --category media_type --per-option 100 [--thumbs] [--detail]

Reads menu/menu.json (already collected) to enumerate the category's options.
Writes into  menu/<NN>_<metatype>/shots/ :
  <option-slug>.csv         per-option card metadata
  _combined.csv             all options of the category
  thumbs/<shotid>.jpg       if --thumbs
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import csv
import json
import os
import re
from pathlib import Path

import aiohttp
from cdp_use import CDPClient

CDP_HTTP = os.environ.get('BROWSER_USE_CDP_HTTP', 'http://127.0.0.1:9222')
OUT_DIR = Path(os.environ.get('SHOTDECK_OUT', str(Path.home() / 'shotdeck_export')))
MENU_DIR = OUT_DIR / 'menu'
BATCH = 36

# Polite pacing (seconds) — we tripped an abuse guard once with fast/bulk requests.
BATCH_SLEEP = 1.3
OPTION_SLEEP = 2.5
THUMB_SLEEP = 0.6

JS_PARSE_CARDS = r"""
(html => {
	const doc = new DOMParser().parseFromString(html, 'text/html');
	const out = [];
	doc.querySelectorAll('.outerimage[data-shotid]').forEach(el => {
		const a = el.querySelector('a.gallerythumb');
		const titleLink = el.querySelector('.moviedetails .gallerytitle a');
		const movieHref = titleLink ? (titleLink.getAttribute('href') || '') : '';
		const movieMatch = movieHref.match(/\/movie\/(\d+)~/);
		const img = el.querySelector('img.still');
		out.push({
			shotid: el.getAttribute('data-shotid'),
			title: titleLink ? titleLink.textContent.trim() : '',
			movie_id: movieMatch ? movieMatch[1] : '',
			year: el.getAttribute('data-titleyear') || '',
			resolution: a ? (a.getAttribute('data-size') || '') : '',
			filename: a ? (a.getAttribute('data-filename') || '') : '',
			shot_status: el.getAttribute('data-shot-status') || '',
			content_status: el.getAttribute('data-title-content-status') || '',
			thumb: img ? (img.getAttribute('src') || '') : '',
		});
	});
	const m = html.match(/nextbatchClick\([^,]+,\s*'([^']+)'\)/);
	const descrip = html.match(/results_descrip'\)\.text\(\s*'([^']+)'/);
	return JSON.stringify({ cards: out, nextUrl: m ? m[1] : null, descrip: descrip ? descrip[1] : '' });
})(arguments0placeholder)
"""

JS_PARSE_DETAIL = r"""
(html => {
	const doc = new DOMParser().parseFromString(html, 'text/html');
	const fields = {};
	doc.querySelectorAll('.detail-group').forEach(g => {
		const key = (g.querySelector('.detail-type')||{}).textContent;
		const val = g.querySelector('.details');
		if (!key || !val) return;
		const k = key.replace(/:\s*$/,'').trim();
		const links = [...val.querySelectorAll('a')].map(a => a.textContent.trim()).filter(Boolean);
		fields[k] = links.length ? links : val.textContent.trim().replace(/\s+/g,' ');
	});
	const palette = [...doc.querySelectorAll('.palette a')].map(a => {
		const m = (a.getAttribute('style')||'').match(/background-color:\s*(#[0-9a-fA-F]+)/);
		return m ? m[1] : null;
	}).filter(Boolean);
	return JSON.stringify({ fields, palette });
})(arguments0placeholder)
"""


def _slug(text: str) -> str:
	s = ''.join(c if c.isalnum() else '_' for c in text.strip().lower())
	return '_'.join(filter(None, s.split('_'))) or 'opt'


class Page:
	def __init__(self, client: CDPClient, sid: str) -> None:
		self._client, self._sid = client, sid

	async def ev(self, expr: str):
		r = await self._client.send.Runtime.evaluate(
			params={'expression': expr, 'returnByValue': True, 'awaitPromise': True}, session_id=self._sid
		)
		if 'exceptionDetails' in r:
			raise RuntimeError(r['exceptionDetails'].get('text', 'js error'))
		return r.get('result', {}).get('value')

	async def fetch(self, path: str) -> str:
		return await self.ev(f"fetch({json.dumps(path)}).then(r=>r.text()).catch(e=>'ERR '+e)")

	async def navigate(self, url: str) -> None:
		await self._client.send.Page.navigate(params={'url': url}, session_id=self._sid)

	async def parse(self, js: str, html: str):
		return json.loads(await self.ev(js.replace('arguments0placeholder', json.dumps(html))))


def _filter_path(metatype: str, value: str, offset: int) -> str:
	# The hash #/<metatype>/<value> maps to this ajax path; value is URL-encoded per-segment.
	from urllib.parse import quote

	enc = quote(value, safe='')
	return f'/browse/searchstillsajax/{metatype}/{enc}/limit/{BATCH}/offset/{offset}'


async def collect_option(page: Page, metatype: str, value: str, per_option: int) -> tuple[list[dict], str]:
	"""Collect up to per_option cards for one filter option; returns (cards, total_descrip)."""
	cards: list[dict] = []
	seen: set[str] = set()
	descrip = ''
	offset = 0
	while len(cards) < per_option:
		html = await page.fetch(_filter_path(metatype, value, offset))
		parsed = await page.parse(JS_PARSE_CARDS, html)
		descrip = descrip or parsed.get('descrip', '')
		batch = parsed['cards']
		if not batch:
			break
		for card in batch:
			if card['shotid'] in seen:
				continue
			seen.add(card['shotid'])
			cards.append(card)
			if len(cards) >= per_option:
				break
		offset += BATCH
		await asyncio.sleep(BATCH_SLEEP)
	return cards, descrip


async def enrich(page: Page, cards: list[dict]) -> None:
	sem = asyncio.Semaphore(4)

	async def one(card: dict) -> None:
		async with sem:
			try:
				html = await page.fetch(f"/browse/shotdetailsajax/image/{card['shotid']}")
				d = await page.parse(JS_PARSE_DETAIL, html)
				card['metadata'] = d['fields']
				card['palette'] = d['palette']
			except Exception as e:  # noqa: BLE001
				card['metadata'] = {'__error__': str(e)}
				card['palette'] = []

	for i in range(0, len(cards), 4):
		await asyncio.gather(*(one(c) for c in cards[i : i + 4]))


async def save_thumbs(page: Page, cards: list[dict], thumbs_dir: Path) -> int:
	thumbs_dir.mkdir(parents=True, exist_ok=True)
	sem = asyncio.Semaphore(6)
	saved = 0

	async def one(card: dict) -> None:
		nonlocal saved
		dest = thumbs_dir / f"{card['shotid']}.jpg"
		if dest.exists():
			saved += 1
			return
		src = card.get('thumb') or f"/assets/images/stills/smthumb/small_{card['shotid']}.jpg"
		async with sem:
			try:
				data = await page.ev(
					f"fetch({json.dumps(src)}).then(r=>r.blob()).then(b=>new Promise(res=>{{const fr=new FileReader();fr.onload=()=>res(fr.result);fr.readAsDataURL(b);}})).catch(e=>'ERR '+e)"
				)
				if isinstance(data, str) and data.startswith('data:'):
					dest.write_bytes(base64.b64decode(data.split(',', 1)[1]))
					saved += 1
			except Exception:  # noqa: BLE001
				pass

	for i in range(0, len(cards), 4):
		await asyncio.gather(*(one(c) for c in cards[i : i + 4]))
		await asyncio.sleep(THUMB_SLEEP)
	return saved


def _option_target(opt: dict, per_option: int) -> int:
	"""How many shots to aim for: capped by per_option and the option's library total."""
	num = opt.get('shots_num')
	if isinstance(num, int) and num > 0:
		return min(per_option, num)
	return per_option


async def collect_category(page: Page, cat: dict, args) -> str:
	"""Collect one menu category. Returns 'ok', 'logged_out'. Resumable: options whose JSON
	already holds the target count are reused, so a re-run skips finished work."""
	from urllib.parse import quote_plus

	folder = MENU_DIR / f"{cat['order']:02d}_{_slug(cat['metatype'])}"
	shots_dir = folder / 'shots'
	thumbs_dir = shots_dir / 'thumbs'
	shots_dir.mkdir(parents=True, exist_ok=True)

	print(f"\nMENU {cat['order']:02d} {cat['header']} ({cat['metatype']}) - {cat['option_count']} options, up to {args.per_option}/option", flush=True)
	combined: list[dict] = []
	summary: list[dict] = []
	total_saved = 0
	reached = args.start_at is None
	for opt in cat['options']:
		if not reached:
			reached = opt['label'] == args.start_at
			if not reached:
				continue
		name = _slug((opt['subgroup'] + '_' + opt['label']) if opt['subgroup'] else opt['label'])
		json_path = shots_dir / f'{name}.json'
		target = _option_target(opt, args.per_option)

		# Resume: reuse a prior JSON that already meets the target.
		existing: list[dict] | None = None
		if json_path.is_file():
			try:
				prior = json.loads(json_path.read_text(encoding='utf-8'))
				if len(prior) >= target:
					existing = prior
			except (json.JSONDecodeError, OSError):
				existing = None

		if existing is not None:
			cards = existing
		else:
			await page.navigate(f"https://shotdeck.com/browse/stills#/{opt['metatype']}/{quote_plus(opt['value'])}")
			await asyncio.sleep(1.5)
			cards, descrip = await collect_option(page, opt['metatype'], opt['value'], target)
			if descrip and 'not logged in' in descrip:
				print(f"  STOPPED at {opt['label']}: logged out. Re-login, then rerun (auto-resumes).", flush=True)
				if combined:
					_write_category(shots_dir, combined, summary)
				return 'logged_out'
			for c in cards:
				c['_option'] = opt['label']; c['_option_value'] = opt['value']; c['_subgroup'] = opt['subgroup']
			with (shots_dir / f'{name}.csv').open('w', newline='', encoding='utf-8-sig') as h:
				w = csv.writer(h)
				w.writerow(['shotid', 'title', 'movie_id', 'year', 'resolution', 'shot_status', 'content_status', 'thumb'])
				for c in cards:
					w.writerow([c['shotid'], c['title'], c['movie_id'], c['year'], c['resolution'], c['shot_status'], c['content_status'], c['thumb']])
			json_path.write_text(json.dumps(cards, ensure_ascii=False, indent=2), encoding='utf-8')

		for c in cards:
			c.setdefault('_option', opt['label']); c.setdefault('_option_value', opt['value']); c.setdefault('_subgroup', opt['subgroup'])
		combined.extend(cards)
		opt_saved = 0
		if not args.no_thumbs:
			opt_saved = await save_thumbs(page, cards, thumbs_dir)
			total_saved += opt_saved
		summary.append({'option': opt['label'], 'subgroup': opt['subgroup'], 'value': opt['value'],
			'collected': len(cards), 'thumbs': opt_saved, 'library_total': opt.get('shots', '')})
		tag = '(cached)' if existing is not None else ''
		print(f"  {opt['subgroup']+' / ' if opt['subgroup'] else ''}{opt['label']}: {len(cards)} shots, {opt_saved} thumbs (of {opt.get('shots','')}) {tag}", flush=True)
		if existing is None:
			await asyncio.sleep(OPTION_SLEEP)

	_write_category(shots_dir, combined, summary)
	uniq = len({c['shotid'] for c in combined})
	print(f"DONE {cat['metatype']} -> {len(summary)} options, {len(combined)} cards ({uniq} unique), {total_saved} thumbs", flush=True)
	return 'ok'


def _write_category(shots_dir: Path, combined: list[dict], summary: list[dict]) -> None:
	"""Write the category-level combined CSV and summary."""
	with (shots_dir / '_combined.csv').open('w', newline='', encoding='utf-8-sig') as h:
		w = csv.writer(h)
		w.writerow(['option', 'subgroup', 'shotid', 'title', 'movie_id', 'year', 'resolution', 'content_status', 'thumb'])
		for c in combined:
			w.writerow([c.get('_option', ''), c.get('_subgroup', ''), c['shotid'], c['title'], c['movie_id'], c['year'], c['resolution'], c['content_status'], c['thumb']])
	(shots_dir / '_summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')


async def main() -> None:
	parser = argparse.ArgumentParser()
	parser.add_argument('--category', required=True, help="metatype, or 'all' to iterate every menu in order")
	parser.add_argument('--per-option', type=int, default=108)
	parser.add_argument('--no-thumbs', action='store_true', help='skip thumbnail download (on by default)')
	parser.add_argument('--detail', action='store_true')
	parser.add_argument('--start-at', default=None, help='resume from this option label (single category only)')
	parser.add_argument('--start-cat', default=None, help="for --category all: begin from this metatype")
	args = parser.parse_args()

	menu = json.loads((MENU_DIR / 'menu.json').read_text(encoding='utf-8'))
	if args.category == 'all':
		categories = menu['categories']
		if args.start_cat:
			idx = next((i for i, c in enumerate(categories) if c['metatype'] == args.start_cat), 0)
			categories = categories[idx:]
	else:
		cat = next((c for c in menu['categories'] if c['metatype'] == args.category), None)
		if cat is None:
			raise SystemExit(f"category {args.category!r} not found: {[c['metatype'] for c in menu['categories']]}")
		categories = [cat]

	async with aiohttp.ClientSession() as http:
		async with http.get(f'{CDP_HTTP}/json/version') as response:
			ws_url = (await response.json())['webSocketDebuggerUrl']
	async with CDPClient(ws_url) as client:
		targets = await client.send.Target.getTargets()
		target = next(t for t in targets['targetInfos'] if t['type'] == 'page' and 'shotdeck.com' in t.get('url', ''))
		session = await client.send.Target.attachToTarget(params={'targetId': target['targetId'], 'flatten': True})
		page = Page(client, session['sessionId'])
		await client.send.Page.enable(session_id=session['sessionId'])
		await client.send.Runtime.enable(session_id=session['sessionId'])

		state = json.loads(await page.ev("JSON.stringify({login:/welcome\\/login/.test(location.href), logout:!!document.querySelector('a[href*=logout]')})"))
		if state['login']:
			print('BLOCKED: session logged out. Log in in the Chrome window, then rerun (auto-resumes).', flush=True)
			return

		for cat in categories:
			status = await collect_category(page, cat, args)
			if status == 'logged_out':
				print('HALTED: log back in, then rerun the same command to resume.', flush=True)
				return
		print('\nALL DONE.', flush=True)


if __name__ == '__main__':
	asyncio.run(main())
