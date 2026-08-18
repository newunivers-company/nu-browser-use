"""My Drama (HOLYWATER) catalog collector — schema.org TVSeries.

my-drama.com was not in the source research doc at all; appstore_watch found it
in the iOS listing's description while the doc covered only My Drama's social
and Linktree presence. It turned out to be the richest structured source in the
registry.

Every series page publishes a full schema.org `TVSeries` node in JSON-LD —
markup a site emits precisely so machines can read it. That yields, per title:
episode and season counts, publish and modify dates, language, a genre array
that doubles as a trope vocabulary, `aggregateRating` (value plus rating
count), an `interactionStatistic` WatchAction counter, a trailer VideoObject,
and a cast list of `Person` nodes.

The cast is the part nothing else gave us. Every other platform publishes
titles and numbers; this one publishes who is in them, which turns the catalog
into an actor-to-title graph and makes questions like "which faces is a
competitor betting on, and are they shared with anyone else" answerable.

robots.txt is `Allow: /` for `*`, disallowing only tracking-parameter URLs and
infra paths, none of which are touched. sitemap.xml enumerates the catalog
(395 URLs: 191 series, 191 video, 12 other), so discovery needs no crawling —
the site states its own inventory.

Output (MYDRAMA_OUT, default ~/mydrama_export):
  series.json / series.csv
  cast.jsonl            - actor <-> title edges
  snapshots/YYYY-MM-DD/series.json
  observations.jsonl    - VIEW_COUNT / rating RankingObservation rows
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

BASE = 'https://my-drama.com'
UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36'
HEADERS = {'User-Agent': UA, 'Accept': 'text/html,application/xhtml+xml,*/*;q=0.8', 'Accept-Language': 'en-US,en;q=0.9'}
OUT_DIR = Path(os.environ.get('MYDRAMA_OUT', str(Path.home() / 'mydrama_export')))
LD_RE = re.compile(r'<script[^>]*application/ld\+json[^>]*>(.*?)</script>', re.S | re.I)
LOC_RE = re.compile(r'<loc>([^<]+)</loc>')
# Trailing UUID identifies the work; the slug ahead of it is the display title.
SERIES_ID_RE = re.compile(r'/series/(.+)-([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$')
CONCURRENCY = 5
DELAY = 0.3


async def fetch(session: aiohttp.ClientSession, url: str) -> str | None:
	try:
		async with session.get(url, timeout=aiohttp.ClientTimeout(total=30)) as response:
			return await response.text(errors='replace') if response.status == 200 else None
	except Exception:  # noqa: BLE001
		return None


def ld_nodes(html: str) -> list[dict]:
	"""Every JSON-LD node, flattening @graph wrappers."""
	nodes: list[dict] = []
	for block in LD_RE.findall(html):
		try:
			payload = json.loads(block)
		except json.JSONDecodeError:
			continue
		for candidate in payload if isinstance(payload, list) else [payload]:
			if not isinstance(candidate, dict):
				continue
			graph = candidate.get('@graph')
			nodes.extend(node for node in (graph if isinstance(graph, list) else [candidate]) if isinstance(node, dict))
	return nodes


def _number(value: object) -> int | float | None:
	if isinstance(value, (int, float)):
		return value
	if isinstance(value, str):
		try:
			return float(value) if '.' in value else int(value)
		except ValueError:
			return None
	return None


def watch_count(series: dict) -> int | None:
	"""Pull the WatchAction counter out of interactionStatistic.

	The field is sometimes a single node and sometimes a list of counters for
	different interaction types, so the WatchAction one is selected by name
	rather than by position.
	"""
	stat = series.get('interactionStatistic')
	candidates = stat if isinstance(stat, list) else [stat] if isinstance(stat, dict) else []
	for candidate in candidates:
		interaction = candidate.get('interactionType')
		name = interaction.get('@type') if isinstance(interaction, dict) else str(interaction or '')
		if 'Watch' in str(name):
			value = _number(candidate.get('userInteractionCount'))
			return int(value) if value is not None else None
	# Only one counter and it is unlabelled: take it, but only if unambiguous.
	if len(candidates) == 1:
		value = _number(candidates[0].get('userInteractionCount'))
		return int(value) if value is not None else None
	return None


def parse_series(html: str, url: str) -> dict | None:
	series = next((node for node in ld_nodes(html) if node.get('@type') == 'TVSeries'), None)
	if not series:
		return None
	match = SERIES_ID_RE.search(url)
	rating = series.get('aggregateRating') if isinstance(series.get('aggregateRating'), dict) else {}
	genres = series.get('genre')
	actors = [a for a in (series.get('actor') or []) if isinstance(a, dict)]
	trailer = series.get('trailer') if isinstance(series.get('trailer'), dict) else {}
	return {
		'series_id': match.group(2) if match else None,
		'slug': match.group(1) if match else None,
		'title': series.get('name'),
		'url': url,
		'description': (series.get('description') or '')[:1200],
		'language': series.get('inLanguage'),
		'seasons': _number(series.get('numberOfSeasons')),
		'episodes': _number(series.get('numberOfEpisodes')),
		'date_published': series.get('datePublished'),
		'date_modified': series.get('dateModified'),
		'genres': ' | '.join(genres) if isinstance(genres, list) else (genres or ''),
		'keywords': series.get('keywords') or '',
		'rating_value': _number(rating.get('ratingValue') or series.get('contentRating')),
		'rating_count': _number(rating.get('ratingCount')),
		'watch_count': watch_count(series),
		'thumbnail': series.get('thumbnailUrl'),
		'trailer_name': trailer.get('name'),
		'cast': [{'name': a.get('name'), 'id': a.get('@id')} for a in actors if a.get('name')],
	}


async def collect_one(session: aiohttp.ClientSession, url: str, semaphore: asyncio.Semaphore) -> dict | None:
	async with semaphore:
		html = await fetch(session, url)
		await asyncio.sleep(DELAY)
	return parse_series(html, url) if html else None


def write_outputs(rows: list[dict], now: str) -> None:
	OUT_DIR.mkdir(parents=True, exist_ok=True)
	snap_dir = OUT_DIR / 'snapshots' / dt.date.today().isoformat()
	snap_dir.mkdir(parents=True, exist_ok=True)
	payload = json.dumps(rows, ensure_ascii=False, indent=2)
	(OUT_DIR / 'series.json').write_text(payload, encoding='utf-8')
	(snap_dir / 'series.json').write_text(payload, encoding='utf-8')

	with (OUT_DIR / 'cast.jsonl').open('w', encoding='utf-8') as handle:
		for row in rows:
			for person in row['cast']:
				handle.write(json.dumps({
					'series_id': row['series_id'], 'title': row['title'],
					'actor_name': person['name'], 'actor_id': person['id'], 'observed_at': now,
				}, ensure_ascii=False) + '\n')

	with (OUT_DIR / 'observations.jsonl').open('a', encoding='utf-8') as handle:
		for row in rows:
			if row['watch_count'] is None and row['rating_value'] is None:
				continue
			handle.write(json.dumps({
				'source': 'my-drama.com', 'ranking_name': 'catalog_watch_count',
				'rank_type': 'VIEW_COUNT' if row['watch_count'] is not None else 'RATING',
				'entity_type': 'work', 'entity_id': row['series_id'], 'entity_title': row['title'],
				'scope': {'type': 'platform', 'platform': 'my_drama'}, 'period': {'type': 'cumulative'},
				'rank': None, 'raw_metric_name': 'userInteractionCount' if row['watch_count'] is not None else 'ratingValue',
				'raw_score': row['watch_count'] if row['watch_count'] is not None else row['rating_value'],
				'views': row['watch_count'], 'rating': row['rating_value'], 'rating_count': row['rating_count'],
				'platform': 'My Drama', 'genres': [g for g in row['genres'].split(' | ') if g],
				'episodes': row['episodes'], 'cast_size': len(row['cast']),
				'source_url': row['url'], 'observed_at': now,
			}, ensure_ascii=False) + '\n')

	columns = ['series_id', 'title', 'episodes', 'seasons', 'watch_count', 'rating_value', 'rating_count', 'genres', 'language', 'date_published', 'date_modified', 'cast_names', 'url', 'thumbnail']
	with (OUT_DIR / 'series.csv').open('w', newline='', encoding='utf-8-sig') as handle:
		writer = csv.DictWriter(handle, fieldnames=columns, extrasaction='ignore')
		writer.writeheader()
		for row in rows:
			writer.writerow({**row, 'cast_names': ' | '.join(p['name'] for p in row['cast'])})


async def main() -> None:
	parser = argparse.ArgumentParser()
	parser.add_argument('--limit', type=int, help='cap series fetched (smoke tests)')
	args = parser.parse_args()

	now = dt.datetime.now(dt.timezone.utc).isoformat()
	async with aiohttp.ClientSession(headers=HEADERS) as session:
		print('[1/2] reading sitemap')
		sitemap = await fetch(session, f'{BASE}/sitemap.xml')
		if not sitemap:
			print('sitemap unreachable — aborting rather than guessing the catalog')
			return
		urls = [u for u in LOC_RE.findall(sitemap) if '/series/' in u]
		if args.limit:
			urls = urls[: args.limit]
		print(f'      {len(urls)} series URLs')

		print('[2/2] fetching series pages')
		semaphore = asyncio.Semaphore(CONCURRENCY)
		results = await asyncio.gather(*(collect_one(session, url, semaphore) for url in urls))

	rows = [row for row in results if row]
	write_outputs(rows, now)
	with_watch = [r for r in rows if r['watch_count'] is not None]
	with_cast = [r for r in rows if r['cast']]
	actors = {p['name'] for r in rows for p in r['cast']}
	print(f'DONE -> {OUT_DIR}')
	print(f'  series: {len(rows)}/{len(urls)} | with watch_count: {len(with_watch)} | with cast: {len(with_cast)} | distinct actors: {len(actors)}')
	for row in sorted(with_watch, key=lambda r: r['watch_count'], reverse=True)[:5]:
		print(f'    {row["watch_count"]:>12,}  ep{str(row["episodes"]):>4}  {str(row["title"])[:44]}')


if __name__ == '__main__':
	asyncio.run(main())
