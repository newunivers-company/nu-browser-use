"""GoodShort catalog + engagement collector.

Recon finding (promo_recon.py, 2026-08-14): goodshort.com looks like an SPA but
is server-rendered. The HAR capture showed 73 requests and exactly zero catalog
JSON endpoints — `acf.goodshort.com` is the poster CDN, `acfs3` serves the JS
bundle, `api.xintaicz.cn/sa.gif` is Sensors Analytics telemetry. Everything the
page displays is already in the HTML inside `window.__INITIAL_STATE__`.

So this needs no browser: the registry row moves from T1 to T0 and collection is
plain HTTP. That is the whole point of doing recon per source rather than
assuming a JS shell means a JS collector.

Detail pages carry `BookInfoModule.book`, which includes **viewCount** — a real
engagement number, not a rank ordinal. That makes GoodShort one of the few
sources exposing an absolute metric we can trend, comparable to duanju007's
weekly view counts rather than to a rail position.

robots.txt allows `/` for `*`; only /subscription, /results and /pay are
disallowed, none of which we touch. No sitemap exists (404), so slugs come from
a breadth-first walk of the tag and
genre listings, which name each other transitively.

Output (GOODSHORT_OUT, default ~/goodshort_export):
  books.json / books.csv
  snapshots/YYYY-MM-DD/books.json
  observations.jsonl   - VIEW_COUNT RankingObservation rows
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import datetime as dt
import json
import os
import re
import sys
from pathlib import Path

if sys.stdout.encoding and sys.stdout.encoding.lower() != 'utf-8':
	sys.stdout.reconfigure(encoding='utf-8', errors='replace')

import aiohttp

BASE = 'https://www.goodshort.com'
UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36'
HEADERS = {'User-Agent': UA, 'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8', 'Accept-Language': 'en-US,en;q=0.9'}
OUT_DIR = Path(os.environ.get('GOODSHORT_OUT', str(Path.home() / 'goodshort_export')))
STATE_KEY = 'window.__INITIAL_STATE__='
DRAMA_RE = re.compile(r'/drama/([a-z0-9][a-z0-9-]*-\d+)')
TAG_RE = re.compile(r'/tag/([a-z0-9][a-z0-9-]*)')
# Genre listings are a second frontier alongside tags, and every tag page names
# ~38 further tags, so the tag frontier is transitive rather than flat: reading
# only the home page's tags (the original behaviour) sampled one hop of it.
GENRE_RE = re.compile(r'/dramas/([a-z0-9][a-z0-9-]*)')
CONCURRENCY = 5
DELAY = 0.35


def extract_state(html: str) -> dict | None:
	"""Pull `window.__INITIAL_STATE__` out of the page.

	Brace-matching rather than a regex: the blob is ~150KB of nested JSON with
	embedded braces inside strings, so a non-greedy `\\{.*?\\}` matches nothing
	useful and a greedy one swallows the rest of the document.
	"""
	start = html.find(STATE_KEY)
	if start < 0:
		return None
	start += len(STATE_KEY)
	depth = 0
	in_string = False
	escaped = False
	for index in range(start, len(html)):
		char = html[index]
		if escaped:
			escaped = False
			continue
		if char == '\\':
			escaped = True
			continue
		if char == '"':
			in_string = not in_string
			continue
		if in_string:
			continue
		if char == '{':
			depth += 1
		elif char == '}':
			depth -= 1
			if depth == 0:
				try:
					return json.loads(html[start : index + 1])
				except json.JSONDecodeError:
					return None
	return None


async def fetch(session: aiohttp.ClientSession, url: str) -> str | None:
	try:
		async with session.get(url, timeout=aiohttp.ClientTimeout(total=30)) as response:
			return await response.text(errors='replace') if response.status == 200 else None
	except Exception:  # noqa: BLE001
		return None


async def discover_slugs(session: aiohttp.ClientSession, page_budget: int) -> set[str]:
	"""Breadth-first over tag and genre listings until the budget is spent.

	There is no sitemap (404), so the catalog has to be walked. Listing pages
	name further listing pages, so the frontier is explored transitively and
	the budget caps total requests rather than capping the first hop.
	"""
	slugs: set[str] = set()
	home = await fetch(session, BASE + '/')
	if not home:
		return slugs
	slugs |= set(DRAMA_RE.findall(home))
	print(f'  home: {len(slugs)} slugs')

	seen_pages: set[str] = set()
	frontier: list[str] = [f'/dramas/{g}' for g in sorted(set(GENRE_RE.findall(home)))]
	frontier += [f'/tag/{t}' for t in sorted(set(TAG_RE.findall(home)))]
	while frontier and len(seen_pages) < page_budget:
		path = frontier.pop(0)
		if path in seen_pages:
			continue
		seen_pages.add(path)
		page = await fetch(session, BASE + path)
		await asyncio.sleep(DELAY)
		if not page:
			continue
		before = len(slugs)
		slugs |= set(DRAMA_RE.findall(page))
		for tag in sorted(set(TAG_RE.findall(page))):
			candidate = f'/tag/{tag}'
			if candidate not in seen_pages:
				frontier.append(candidate)
		if len(slugs) != before:
			print(f'  {path}: +{len(slugs) - before} (total {len(slugs)}, {len(seen_pages)}/{page_budget} pages, frontier {len(frontier)})')
	print(f'  walked {len(seen_pages)} listing pages')
	return slugs


def _names(node: object) -> list[str]:
	"""Read a list that holds either plain strings or {id, name, resourceUrl}."""
	if not isinstance(node, list):
		return []
	return [str(item.get('name')) if isinstance(item, dict) else str(item) for item in node if item]


def flatten_book(book: dict, slug: str) -> dict:
	"""Scalar catalog fields, plus the genre and trope taxonomies.

	`tagsList` is the interesting one: GoodShort tags each title with its tropes
	(Regret, Chasing Love, Marriage...), which is the vocabulary the ranking
	plan wants for Trope Rank and which most platforms do not publish.
	"""
	row = {key: value for key, value in book.items() if not isinstance(value, (dict, list))}
	row['slug'] = slug
	row['url'] = f'{BASE}/drama/{slug}'
	row['genres'] = ' | '.join(_names(book.get('genreList')))
	row['subgenres'] = ' | '.join(_names(book.get('typeTwoNames')))
	row['tropes'] = ' | '.join(_names(book.get('tagsList')))
	return row


async def collect_book(session: aiohttp.ClientSession, slug: str, semaphore: asyncio.Semaphore) -> dict | None:
	async with semaphore:
		html = await fetch(session, f'{BASE}/drama/{slug}')
		await asyncio.sleep(DELAY)
	if not html:
		return None
	state = extract_state(html)
	book = ((state or {}).get('BookInfoModule') or {}).get('book')
	if not isinstance(book, dict) or not book.get('bookId'):
		return None
	return flatten_book(book, slug)


def write_outputs(rows: list[dict], now: str) -> None:
	OUT_DIR.mkdir(parents=True, exist_ok=True)
	snap_dir = OUT_DIR / 'snapshots' / dt.date.today().isoformat()
	snap_dir.mkdir(parents=True, exist_ok=True)
	payload = json.dumps(rows, ensure_ascii=False, indent=2)
	(OUT_DIR / 'books.json').write_text(payload, encoding='utf-8')
	(snap_dir / 'books.json').write_text(payload, encoding='utf-8')

	with (OUT_DIR / 'observations.jsonl').open('a', encoding='utf-8') as handle:
		for row in rows:
			views = row.get('viewCount')
			if views in (None, ''):
				continue
			handle.write(
				json.dumps(
					{
						'source': 'goodshort.com', 'ranking_name': 'catalog_view_count', 'rank_type': 'VIEW_COUNT',
						'entity_type': 'work', 'entity_id': str(row['bookId']), 'entity_title': row.get('bookName'),
						'scope': {'type': 'platform', 'platform': 'goodshort'}, 'period': {'type': 'cumulative'},
						'rank': None, 'raw_metric_name': 'viewCount', 'raw_score': views, 'views': views,
						'platform': 'GoodShort',
						'genres': [g for g in row.get('genres', '').split(' | ') if g],
						'tropes': [t for t in row.get('tropes', '').split(' | ') if t],
						'episodes': row.get('chapterCount'),
						'source_url': row['url'], 'observed_at': now,
					},
					ensure_ascii=False,
				)
				+ '\n'
			)

	preferred = ['bookId', 'bookName', 'slug', 'viewCount', 'likeCount', 'followCount', 'commentCount', 'inLibraryNum', 'ratings', 'chapterCount', 'genres', 'subgenres', 'tropes', 'pseudonym', 'writeStatus', 'lastChapterTime', 'url']
	columns = preferred + sorted({key for row in rows for key in row} - set(preferred))
	with (OUT_DIR / 'books.csv').open('w', newline='', encoding='utf-8-sig') as handle:
		writer = csv.DictWriter(handle, fieldnames=columns, extrasaction='ignore')
		writer.writeheader()
		writer.writerows(rows)


async def main() -> None:
	parser = argparse.ArgumentParser()
	parser.add_argument('--pages', type=int, default=60, help='listing-page budget for the breadth-first walk')
	parser.add_argument('--limit', type=int, help='cap detail fetches (smoke tests)')
	args = parser.parse_args()

	now = dt.datetime.now(dt.timezone.utc).isoformat()
	async with aiohttp.ClientSession(headers=HEADERS) as session:
		print('[1/2] discovering slugs')
		slugs = sorted(await discover_slugs(session, args.pages))
		if args.limit:
			slugs = slugs[: args.limit]
		print(f'      {len(slugs)} slugs')
		print('[2/2] fetching detail pages')
		semaphore = asyncio.Semaphore(CONCURRENCY)
		results = await asyncio.gather(*(collect_book(session, slug, semaphore) for slug in slugs))

	rows = [row for row in results if row]
	write_outputs(rows, now)
	with_views = [row for row in rows if row.get('viewCount')]
	print(f'DONE -> {OUT_DIR}')
	print(f'  books: {len(rows)}/{len(slugs)} | with viewCount: {len(with_views)}')
	for row in sorted(with_views, key=lambda r: r['viewCount'], reverse=True)[:5]:
		print(f"    {row['viewCount']:>10,}  {str(row.get('bookName'))[:52]}")


if __name__ == '__main__':
	asyncio.run(main())
