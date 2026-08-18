"""DramaBox keyless catalog collector.

Pulls the DramaBox short-drama catalog with no login and no browser, from public web data:

  1. home page           -> current Next.js buildId
  2. /browse/page/N       -> bookList (12/page) across all `pages` -> every bookId
  3. /drama/<bookId>      -> __NEXT_DATA__ bookInfo: title(s), genres/labels, chapterCount,
                             viewCount, followCount, introduction, cast, cover, language, dates
  4. cover images         -> promotional poster per drama (CDN, unauthenticated)

Scope follows docs/collection-policy.md: catalog metadata + promotional assets only.
Episode video originals are NOT collected (원문 비보관 원칙).

Output (DRAMABOX_OUT, default ~/dramabox_export):
  dramas.json / dramas.csv     - one record per bookId
  covers/<bookId>.jpg          - poster images
  _seen_ids.txt                - resume marker
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import os
import re
from pathlib import Path

import aiohttp

BASE = 'https://www.dramabox.com'
OUT_DIR = Path(os.environ.get('DRAMABOX_OUT', str(Path.home() / 'dramabox_export')))
UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36'
NEXT_RE = re.compile(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', re.S)
LOCALE = 'en'
DETAIL_CONCURRENCY = 8
COVER_CONCURRENCY = 8
PAGE_SLEEP = 0.3


async def fetch_text(session: aiohttp.ClientSession, url: str, sem: asyncio.Semaphore) -> str | None:
	"""GET text with a browser UA, tolerating transient failures."""
	async with sem:
		for attempt in range(3):
			try:
				async with session.get(url, timeout=aiohttp.ClientTimeout(total=30)) as r:
					if r.status == 200:
						return await r.text()
					if r.status in (429, 500, 502, 503):
						await asyncio.sleep(2 * (attempt + 1))
						continue
					return None
			except Exception:  # noqa: BLE001
				await asyncio.sleep(1)
	return None


async def fetch_json(session: aiohttp.ClientSession, url: str, sem: asyncio.Semaphore) -> dict | None:
	"""GET and parse a _next/data JSON document."""
	text = await fetch_text(session, url, sem)
	if not text:
		return None
	try:
		return json.loads(text)
	except json.JSONDecodeError:
		return None


def parse_next_data(html: str) -> dict | None:
	"""Return props.pageProps from an HTML page's __NEXT_DATA__."""
	m = NEXT_RE.search(html)
	if not m:
		return None
	try:
		return json.loads(m.group(1))['props']['pageProps']
	except (json.JSONDecodeError, KeyError):
		return None


async def get_build_id(session: aiohttp.ClientSession, sem: asyncio.Semaphore) -> str:
	"""Read the current Next.js buildId from the home page (avoids hardcoding)."""
	html = await fetch_text(session, f'{BASE}/', sem)
	m = re.search(r'"buildId":"([^"]+)"', html or '')
	if not m:
		raise RuntimeError('could not read DramaBox buildId')
	return m.group(1)


async def enumerate_book_ids(session: aiohttp.ClientSession, build_id: str, sem: asyncio.Semaphore, max_pages: int) -> list[str]:
	"""Page through the /browse/page/N HTML pages, collecting every bookId in order."""
	first_html = await fetch_text(session, f'{BASE}/browse/page/1', sem)
	first_pp = parse_next_data(first_html or '') or {}
	pages = int(first_pp.get('pages') or 1)
	if max_pages > 0:
		pages = min(pages, max_pages)
	print(f'  browse: {pages} pages', flush=True)

	ordered: list[str] = []
	seen: set[str] = set()

	def take(pp: dict) -> None:
		for book in (pp or {}).get('bookList', []) or []:
			bid = str(book.get('bookId') or '')
			if bid and bid not in seen:
				seen.add(bid)
				ordered.append(bid)

	take(first_pp)
	for page in range(2, pages + 1):
		html = await fetch_text(session, f'{BASE}/browse/page/{page}', sem)
		if html:
			take(parse_next_data(html) or {})
		if page % 25 == 0:
			print(f'  ...browse page {page}/{pages} -> {len(ordered)} bookIds', flush=True)
		await asyncio.sleep(PAGE_SLEEP)
	return ordered


def extract_book(pp: dict) -> dict | None:
	"""Pull the bookInfo record (with a few nested lists flattened) from a detail page."""
	book = pp.get('bookInfo')
	if not isinstance(book, dict) or not book.get('bookId'):
		return None
	record = dict(book)
	record['typeTwoNames'] = book.get('typeTwoNames') or [t.get('typeTwoName') for t in (book.get('typeTwoList') or []) if isinstance(t, dict)]
	record['performers'] = [p.get('name') for p in (book.get('performerList') or []) if isinstance(p, dict)]
	record['chapterCountReported'] = book.get('chapterCount')
	record['recommendIds'] = [r.get('bookId') for r in (pp.get('recommends') or []) if isinstance(r, dict)]
	return record


async def download_cover(session: aiohttp.ClientSession, book: dict, covers: Path, sem: asyncio.Semaphore) -> bool:
	"""Download a drama's poster/cover image (promotional asset)."""
	url = book.get('cover')
	if not url:
		return False
	dest = covers / f"{book['bookId']}.jpg"
	if dest.exists() and dest.stat().st_size > 0:
		return True
	async with sem:
		try:
			async with session.get(url, timeout=aiohttp.ClientTimeout(total=60)) as r:
				if r.status == 200:
					dest.write_bytes(await r.read())
					return True
		except Exception:  # noqa: BLE001
			return False
	return False


def write_outputs(dramas: list[dict]) -> None:
	"""Persist dramas.json + a flat CSV of headline fields."""
	(OUT_DIR / 'dramas.json').write_text(json.dumps(dramas, ensure_ascii=False, indent=2), encoding='utf-8')
	cols = ['bookId', 'bookName', 'bookNameEn', 'chapterCount', 'viewCount', 'followCount',
		'labels', 'tags', 'typeTwoNames', 'performers', 'language', 'shelfTime', 'introduction', 'cover']
	with (OUT_DIR / 'dramas.csv').open('w', newline='', encoding='utf-8-sig') as h:
		w = csv.writer(h)
		w.writerow(cols)
		for d in dramas:
			row = []
			for c in cols:
				v = d.get(c, '')
				row.append(' | '.join(str(x) for x in v) if isinstance(v, list) else v)
			w.writerow(row)


async def main() -> None:
	parser = argparse.ArgumentParser()
	parser.add_argument('--max-pages', type=int, default=0, help='cap browse pages (0 = all)')
	parser.add_argument('--limit', type=int, default=0, help='cap dramas fetched (0 = all)')
	parser.add_argument('--no-covers', action='store_true')
	args = parser.parse_args()

	OUT_DIR.mkdir(parents=True, exist_ok=True)
	covers = OUT_DIR / 'covers'
	covers.mkdir(exist_ok=True)
	seen_file = OUT_DIR / '_seen_ids.txt'
	already = set(seen_file.read_text(encoding='utf-8').split()) if seen_file.is_file() else set()
	existing = []
	if (OUT_DIR / 'dramas.json').is_file():
		try:
			existing = json.loads((OUT_DIR / 'dramas.json').read_text(encoding='utf-8'))
		except json.JSONDecodeError:
			existing = []

	sem = asyncio.Semaphore(DETAIL_CONCURRENCY)
	cover_sem = asyncio.Semaphore(COVER_CONCURRENCY)
	async with aiohttp.ClientSession(headers={'User-Agent': UA}) as session:
		build_id = await get_build_id(session, sem)
		print(f'[1/3] buildId {build_id}', flush=True)
		print('[2/3] enumerating catalog', flush=True)
		book_ids = await enumerate_book_ids(session, build_id, sem, args.max_pages)
		todo = [b for b in book_ids if b not in already]
		if args.limit > 0:
			todo = todo[: args.limit]
		print(f'[3/3] fetching {len(todo)} drama details ({len(already)} already done)', flush=True)

		dramas = list(existing)
		done = 0

		async def one(bid: str) -> dict | None:
			nonlocal done
			html = await fetch_text(session, f'{BASE}/drama/{bid}', sem)
			done += 1
			if done % 100 == 0:
				print(f'  ...{done}/{len(todo)} details', flush=True)
			if not html:
				return None
			pp = parse_next_data(html)
			return extract_book(pp) if pp else None

		# Process in batches so we can checkpoint + download covers as we go.
		for i in range(0, len(todo), 200):
			batch = todo[i : i + 200]
			results = [r for r in await asyncio.gather(*(one(b) for b in batch)) if r]
			dramas.extend(results)
			already.update(r['bookId'] for r in results)
			if not args.no_covers:
				await asyncio.gather(*(download_cover(session, r, covers, cover_sem) for r in results))
			write_outputs(dramas)
			seen_file.write_text(' '.join(already), encoding='utf-8')
			print(f'  checkpoint: {len(dramas)} dramas', flush=True)

	print(f'DONE -> {OUT_DIR} ({len(dramas)} dramas)', flush=True)


if __name__ == '__main__':
	asyncio.run(main())
