"""DramaBoxDB catalog collector — DramaBox's SEO satellite site.

dramaboxdb.com was found by appstore_watch in the DramaBox iOS listing and is
not the same property as dramabox.com. It is the SEO surface, and it is far
more forthcoming: each title page ships a Next.js payload carrying viewCount,
followCount, chapterCount, genre, shelf date, per-episode durations, and a
recommendation rail.

The view counts are an order of magnitude above anything else in the registry —
hundreds of millions rather than the hundreds of thousands GoodShort and My
Drama report. They are almost certainly counted differently (per-episode plays
against per-series views), so they are recorded as the platform states them and
must not be compared across sources without a stated basis. Within DramaBox
they trend fine, which is what a snapshot series is for.

WHAT IS DELIBERATELY DROPPED
`chapterList` hands over signed CloudFront mp4 and m3u8 URLs — direct handles
to the episode video. `docs/collection-policy.md` forbids collecting episode
content outright, HLS segments and download files included, so those fields are
stripped and never written. Episode index, duration and update date are kept:
that is metadata about the work, not the work.

Discovery without a sitemap (404 on every variant): genre listings seed the
frontier, and each title's `recommends` rail supplies more bookIds with the
slugs needed to build their URLs. robots allows `/`; `/search?*` is disallowed
and is never used.

Output (DRAMABOXDB_OUT, default ~/dramaboxdb_export):
  books.json / books.csv
  recommendations.jsonl - title -> recommended-title edges
  snapshots/YYYY-MM-DD/books.json
  observations.jsonl    - VIEW_COUNT RankingObservation rows
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

BASE = 'https://www.dramaboxdb.com'
UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36'
HEADERS = {'User-Agent': UA, 'Accept': 'text/html,application/xhtml+xml,*/*;q=0.8', 'Accept-Language': 'en-US,en;q=0.9'}
OUT_DIR = Path(os.environ.get('DRAMABOXDB_OUT', str(Path.home() / 'dramaboxdb_export')))
NEXT_RE = re.compile(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S)
MOVIE_RE = re.compile(r'/movie/(\d+)/([a-z0-9-]+)')
GENRE_RE = re.compile(r'/genres/(\d+)')
# Signed media handles. Never stored — see the module docstring.
MEDIA_FIELDS = ('mp4', 'm3u8Url', 'videoUrl', 'url')
CONCURRENCY = 5
DELAY = 0.3


async def fetch(session: aiohttp.ClientSession, url: str) -> str | None:
	try:
		async with session.get(url, timeout=aiohttp.ClientTimeout(total=30)) as response:
			return await response.text(errors='replace') if response.status == 200 else None
	except Exception:  # noqa: BLE001
		return None


def page_props(html: str) -> dict:
	match = NEXT_RE.search(html)
	if not match:
		return {}
	try:
		return json.loads(match.group(1)).get('props', {}).get('pageProps', {}) or {}
	except json.JSONDecodeError:
		return {}


def clean_chapters(chapters: object) -> list[dict]:
	"""Episode metadata with every media handle removed."""
	if not isinstance(chapters, list):
		return []
	out = []
	for chapter in chapters:
		if not isinstance(chapter, dict):
			continue
		out.append(
			{
				'index': chapter.get('index'),
				'name': chapter.get('name'),
				'duration_ms': chapter.get('duration'),
				'price': chapter.get('chapterPrice'),
				'updated': chapter.get('utime'),
			}
		)
	assert all(field not in row for row in out for field in MEDIA_FIELDS), 'media URLs must never reach the output'
	return out


def parse_book(props: dict, url: str) -> dict | None:
	book = props.get('bookInfo')
	if not isinstance(book, dict) or not book.get('bookId'):
		return None
	chapters = clean_chapters(props.get('chapterList'))
	durations = [c['duration_ms'] for c in chapters if isinstance(c.get('duration_ms'), int)]
	return {
		'book_id': str(book['bookId']),
		'title': book.get('bookName'),
		'slug': book.get('bookNameLower'),
		'url': url,
		'view_count': book.get('viewCount'),
		'follow_count': book.get('followCount'),
		'chapter_count': book.get('chapterCount'),
		'genre': book.get('typeTwoName'),
		'language': book.get('language'),
		'shelf_time': book.get('shelfTime'),
		'first_shelf_time': book.get('firstShelfTime'),
		'introduction': (book.get('introduction') or '')[:1200],
		'cover': book.get('cover'),
		'episodes_listed': len(chapters),
		'median_duration_ms': sorted(durations)[len(durations) // 2] if durations else None,
		'article_count': len(props.get('articleList') or []),
		'recommends': [
			{
				'book_id': str(r.get('bookId')),
				'title': r.get('bookName'),
				'slug': r.get('bookNameLower'),
				'follow_count': r.get('followCount'),
			}
			for r in (props.get('recommends') or [])
			if isinstance(r, dict) and r.get('bookId')
		],
	}


async def collect_one(session: aiohttp.ClientSession, book_id: str, slug: str, semaphore: asyncio.Semaphore) -> dict | None:
	url = f'{BASE}/movie/{book_id}/{slug}'
	async with semaphore:
		html = await fetch(session, url)
		await asyncio.sleep(DELAY)
	return parse_book(page_props(html), url) if html else None


async def seed_frontier(session: aiohttp.ClientSession) -> dict[str, str]:
	"""{book_id: slug} from the home page and every genre listing."""
	found: dict[str, str] = {}
	home = await fetch(session, BASE + '/') or ''
	for book_id, slug in MOVIE_RE.findall(home):
		found.setdefault(book_id, slug)
	print(f'  home: {len(found)} titles')
	for genre in sorted(set(GENRE_RE.findall(home))):
		page = await fetch(session, f'{BASE}/genres/{genre}')
		if not page:
			continue
		before = len(found)
		for book_id, slug in MOVIE_RE.findall(page):
			found.setdefault(book_id, slug)
		print(f'  genres/{genre}: +{len(found) - before} (total {len(found)})')
		await asyncio.sleep(DELAY)
	return found


def write_outputs(rows: list[dict], now: str) -> None:
	OUT_DIR.mkdir(parents=True, exist_ok=True)
	snap_dir = OUT_DIR / 'snapshots' / dt.date.today().isoformat()
	snap_dir.mkdir(parents=True, exist_ok=True)
	payload = json.dumps(rows, ensure_ascii=False, indent=2)
	(OUT_DIR / 'books.json').write_text(payload, encoding='utf-8')
	(snap_dir / 'books.json').write_text(payload, encoding='utf-8')

	with (OUT_DIR / 'recommendations.jsonl').open('w', encoding='utf-8') as handle:
		for row in rows:
			for rec in row['recommends']:
				handle.write(
					json.dumps(
						{
							'from_id': row['book_id'],
							'from_title': row['title'],
							'to_id': rec['book_id'],
							'to_title': rec['title'],
							'to_follow_count': rec['follow_count'],
							'observed_at': now,
						},
						ensure_ascii=False,
					)
					+ '\n'
				)

	with (OUT_DIR / 'observations.jsonl').open('a', encoding='utf-8') as handle:
		for row in rows:
			if row['view_count'] is None:
				continue
			handle.write(
				json.dumps(
					{
						'source': 'dramaboxdb.com',
						'ranking_name': 'catalog_view_count',
						'rank_type': 'VIEW_COUNT',
						'entity_type': 'work',
						'entity_id': row['book_id'],
						'entity_title': row['title'],
						# Cumulative here, unlike GoodShort's viewCount which decays: over the
						# first two days 874 of 894 titles rose, 2 fell, and both falls were
						# ~0.1% (464,160 -> 463,682) — sharded-counter drift, not a window.
						# A strict-monotonicity assertion downstream would still trip on it.
						'scope': {'type': 'platform', 'platform': 'dramabox'},
						'period': {'type': 'cumulative'},
						'rank': None,
						'raw_metric_name': 'viewCount',
						'raw_score': row['view_count'],
						'views': row['view_count'],
						'follows': row['follow_count'],
						# Basis differs from other sources by orders of magnitude; do not
						# cross-compare without establishing what each platform counts.
						'metric_basis': 'platform_reported_uncalibrated',
						'platform': 'DramaBox',
						'genres': [row['genre']] if row['genre'] else [],
						'episodes': row['chapter_count'],
						'published_at': row['first_shelf_time'],
						'source_url': row['url'],
						'observed_at': now,
					},
					ensure_ascii=False,
				)
				+ '\n'
			)

	columns = [
		'book_id',
		'title',
		'view_count',
		'follow_count',
		'chapter_count',
		'genre',
		'language',
		'first_shelf_time',
		'median_duration_ms',
		'article_count',
		'url',
		'cover',
	]
	with (OUT_DIR / 'books.csv').open('w', newline='', encoding='utf-8-sig') as handle:
		writer = csv.DictWriter(handle, fieldnames=columns, extrasaction='ignore')
		writer.writeheader()
		writer.writerows(rows)


async def main() -> None:
	parser = argparse.ArgumentParser()
	parser.add_argument('--expand', type=int, default=2, help='rounds of recommendation-rail expansion')
	parser.add_argument('--max-titles', type=int, default=600)
	parser.add_argument('--limit', type=int, help='cap fetches (smoke tests)')
	parser.add_argument(
		'--seed-from-union',
		action='store_true',
		help='also fetch details for works in the cumulative union (catalog_state) that the frontier missed',
	)
	args = parser.parse_args()

	now = dt.datetime.now(dt.timezone.utc).isoformat()
	semaphore = asyncio.Semaphore(CONCURRENCY)
	async with aiohttp.ClientSession(headers=HEADERS) as session:
		print('[1/2] seeding from home + genre listings')
		frontier = await seed_frontier(session)
		if args.seed_from_union:
			# Same rationale as goodshort: the frontier walk misses the same works
			# for days (flapped=182), and the union already knows every slug.
			union_path = Path.home() / 'catalog_state_export' / 'dramaboxdb_catalog.json'
			alt = Path(os.environ.get('CATALOG_STATE_OUT', '')) / 'dramaboxdb_catalog.json' if os.environ.get('CATALOG_STATE_OUT') else None
			for candidate in (alt, Path('/mnt/c/Users/USER/catalog_state_export/dramaboxdb_catalog.json'), union_path):
				if candidate and candidate.exists():
					union_path = candidate
					break
			if union_path.exists():
				union = json.load(open(union_path, encoding='utf-8'))
				extra = 0
				for r in union:
					if not isinstance(r, dict):
						continue
					bid, slug = str(r.get('book_id') or r.get('bookId') or ''), r.get('slug') or r.get('bookNameLower') or ''
					if bid and slug and bid not in frontier:
						frontier[bid] = slug
						extra += 1
				print(f'  union seed: +{extra} slugs the frontier did not reach')
			else:
				print('  union catalog not found; frontier slugs only')
		if args.limit:
			frontier = dict(list(frontier.items())[: args.limit])

		print('[2/2] fetching title pages')
		collected: dict[str, dict] = {}
		pending = dict(frontier)
		for round_number in range(args.expand + 1):
			todo = {bid: slug for bid, slug in pending.items() if bid not in collected}
			if not todo or len(collected) >= args.max_titles:
				break
			results = await asyncio.gather(*(collect_one(session, bid, slug, semaphore) for bid, slug in todo.items()))
			new_pending: dict[str, str] = {}
			for row in results:
				if not row:
					continue
				collected[row['book_id']] = row
				for rec in row['recommends']:
					if rec['book_id'] not in collected and rec['slug']:
						new_pending[rec['book_id']] = rec['slug']
			print(f'  round {round_number}: {len(collected)} titles (+{len(new_pending)} discovered via recommendations)')
			pending = new_pending
			if args.limit:
				break

	rows = list(collected.values())
	write_outputs(rows, now)
	with_views = [r for r in rows if r['view_count']]
	edges = sum(len(r['recommends']) for r in rows)
	print(f'DONE -> {OUT_DIR}')
	print(f'  titles: {len(rows)} | with view_count: {len(with_views)} | recommendation edges: {edges}')
	for row in sorted(with_views, key=lambda r: r['view_count'], reverse=True)[:5]:
		print(f'    {row["view_count"]:>14,}  ep{str(row["chapter_count"]):>4}  {str(row["title"])[:44]}')


if __name__ == '__main__':
	asyncio.run(main())
