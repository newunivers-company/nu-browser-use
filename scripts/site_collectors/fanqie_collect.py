"""Fanqie (番茄小说) keyless catalog collector.

Byte's Fanqie is China's largest free webnovel platform and the source IP
pool for a large share of Chinese short drama. Recon (2026-08-17):

  robots.txt: `User-agent: * Allow: /` — fully open.
  SSR surfaces (plain HTTP, no signing):
    /            home rails: editor/week/boy/girl lists with bookIds
    /rank        top-10 by rank + full category taxonomy (male 19 / female 18)
    /page/<id>   full work record — readCount, wordNumber, categoryV2, abstract
  Client-only (NOT collected — guarded by Byte security SDK):
    search, category-filtered ranks.

Collection path: gather bookIds from home rails + rank, then fetch each
/page/<id> for absolute metrics. Output follows the standard export layout.

Output (FANQIE_OUT, default ~/fanqie_export):
  books.json / books.csv          - all works with full metrics
  taxonomy.json                   - rank category taxonomy
  snapshots/YYYY-MM-DD/books.json - dated copy for the time series
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import datetime as dt
import json
import os
import re
from pathlib import Path

import aiohttp

BASE = 'https://fanqienovel.com'
UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36'
OUT_DIR = Path(os.environ.get('FANQIE_OUT', str(Path.home() / 'fanqie_export')))
STATE_RE = re.compile(r'window\.__INITIAL_STATE__=(\{.*)', re.S)
# Home rails that carry book entries, with the rail name to stamp on each work.
HOME_RAILS = {
	'editorList': 'editor_pick',
	'weekList': 'weekly',
	'boyList': 'male_rail',
	'girlList': 'female_rail',
	'updateList': 'latest_update',
}
DETAIL_WAIT = 1.5
PAUSE_EVERY = 40
PAUSE_SECONDS = 20.0


def parse_state(html: str) -> dict | None:
	"""Decode window.__INITIAL_STATE__, tolerating JS `undefined` literals."""
	match = STATE_RE.search(html)
	if not match:
		return None
	text = match.group(1).rstrip().rstrip(';')
	text = re.sub(r'\bundefined\b', 'null', text)
	try:
		return json.JSONDecoder().raw_decode(text)[0]
	except json.JSONDecodeError:
		return None


async def fetch(session: aiohttp.ClientSession, path: str) -> str | None:
	try:
		async with session.get(f'{BASE}{path}', timeout=aiohttp.ClientTimeout(total=30)) as response:
			if response.status != 200:
				return None
			return await response.text()
	except Exception:  # noqa: BLE001
		return None


async def gather_ids(session: aiohttp.ClientSession) -> tuple[dict[str, dict], dict]:
	"""Collect bookIds from the home rails and the rank page."""
	works: dict[str, dict] = {}

	home_html = await fetch(session, '/')
	if home_html:
		state = parse_state(home_html)
		home = (state or {}).get('home') or {}
		for rail_key, rail_name in HOME_RAILS.items():
			for item in home.get(rail_key) or []:
				book_id = item.get('bookId')
				if not book_id:
					continue
				entry = works.setdefault(book_id, {'bookId': book_id})
				entry.setdefault('rails', []).append(rail_name)

	rank_html = await fetch(session, '/rank')
	taxonomy = {}
	if rank_html:
		state = parse_state(rank_html)
		rank = (state or {}).get('rank') or {}
		taxonomy = rank.get('rankCategoryTypeList') or {}
		for position, item in enumerate(rank.get('book_list') or [], 1):
			book_id = item.get('bookId')
			if not book_id:
				continue
			entry = works.setdefault(book_id, {'bookId': book_id})
			entry['rank_position'] = position
			entry['rank_pos_diff'] = item.get('rankPosDiff')

	print(
		f'ids: {len(works)} from home+rank | taxonomy: male {len(taxonomy.get("male", []))} / female {len(taxonomy.get("female", []))}'
	)
	return works, taxonomy


async def enrich(session: aiohttp.ClientSession, works: dict[str, dict]) -> int:
	"""Fetch /page/<id> for every work; merge the full record."""
	done = 0
	for index, (book_id, entry) in enumerate(works.items(), 1):
		if index > 1 and (index - 1) % PAUSE_EVERY == 0:
			print(f'  ...pause {PAUSE_SECONDS:.0f}s after {index - 1}')
			await asyncio.sleep(PAUSE_SECONDS)
		html = await fetch(session, f'/page/{book_id}')
		if html:
			state = parse_state(html)
			page = (state or {}).get('page') or {}
			if page.get('bookId'):
				entry.update(
					{
						k: page.get(k)
						for k in (
							'bookName',
							'author',
							'readCount',
							'wordNumber',
							'creationStatus',
							'abstract',
							'thumbUri',
							'categoryV2',
						)
					}
				)
				done += 1
		if index % 20 == 0:
			print(f'  {index}/{len(works)} ({done} enriched)')
		await asyncio.sleep(DETAIL_WAIT)
	return done


def write_outputs(works: dict[str, dict], taxonomy: dict) -> None:
	OUT_DIR.mkdir(parents=True, exist_ok=True)
	records = list(works.values())
	(OUT_DIR / 'books.json').write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding='utf-8')
	(OUT_DIR / 'taxonomy.json').write_text(json.dumps(taxonomy, ensure_ascii=False, indent=2), encoding='utf-8')
	snap_dir = OUT_DIR / 'snapshots' / dt.date.today().isoformat()
	snap_dir.mkdir(parents=True, exist_ok=True)
	(snap_dir / 'books.json').write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding='utf-8')

	with (OUT_DIR / 'books.csv').open('w', newline='', encoding='utf-8-sig') as handle:
		writer = csv.writer(handle)
		writer.writerow(['bookId', 'bookName', 'author', 'readCount', 'wordNumber', 'rails', 'rank_position', 'rank_pos_diff'])
		for w in records:
			writer.writerow(
				[
					w.get('bookId'),
					w.get('bookName', ''),
					w.get('author', ''),
					w.get('readCount', ''),
					w.get('wordNumber', ''),
					'|'.join(w.get('rails', [])),
					w.get('rank_position', ''),
					w.get('rank_pos_diff', ''),
				]
			)


async def main() -> None:
	parser = argparse.ArgumentParser()
	parser.add_argument('--limit', type=int, default=0, help='cap detail fetches (smoke tests)')
	args = parser.parse_args()

	async with aiohttp.ClientSession(headers={'User-Agent': UA}) as session:
		works, taxonomy = await gather_ids(session)
		if args.limit > 0:
			works = dict(list(works.items())[: args.limit])
			print(f'limit: {len(works)} works')
		print(f'[2/2] enriching {len(works)} detail pages')
		done = await enrich(session, works)
		write_outputs(works, taxonomy)
	print(f'DONE -> {OUT_DIR} | enriched {done}/{len(works)}')


if __name__ == '__main__':
	asyncio.run(main())
