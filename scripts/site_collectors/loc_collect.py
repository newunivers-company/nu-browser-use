"""Library of Congress keyless collector — film & storytelling seed metadata.

www.loc.gov serves JSON for any search or item page with ?fo=json, no API key.
This collector builds the STORY INTELLIGENCE seed layer:

  1. National Film Registry announcements/catalog  (culturally canonized works)
  2. Film & Video format search (with subject filters for drama/romance/noir)
  3. Film-photo collections (still photographs — cinematic reference metadata)

Each result keeps only derived metadata (id, title, date, subjects, creators,
description, URLs) — no media downloads. LoC text is US Government Work, but
underlying items vary; rights field is kept per record.

Output layout (LOC_OUT, default ~/loc_export):
  film_registry.json/.csv   - registry announcement records
  film_subject.json         - film/video search per subject query
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import os
from pathlib import Path

import aiohttp

OUT_DIR = Path(os.environ.get('LOC_OUT', str(Path.home() / 'loc_export')))
SEARCH_URL = 'https://www.loc.gov/search/'
UA = 'nu-browser-use collector/1.0 (data collection for internal research; contact: dev)'
PAGE_SIZE = 50
PAGE_SLEEP = 2.0
MAX_RETRIES = 2

SUBJECT_QUERIES = {
	'film_registry': {'q': '"national film registry"', 'fa': 'format:film and video'},
	'noir': {'q': 'film noir photograph', 'fa': 'format:photos,prints and drawings'},
	'cinema_history': {'q': 'motion pictures history', 'fa': ''},
}


def slim(result: dict) -> dict:
	"""Keep the derived-metadata fields the Story/Cinematic registry wants."""
	return {
		'loc_id': result.get('id'),
		'title': (result.get('title') or '')[:300],
		'date': result.get('date'),
		'creators': result.get('contributor') or [],
		'subjects': result.get('subject') or [],
		'formats': result.get('format') or [],
		'partof': result.get('partof') or [],
		'description': (result.get('description') or [])[:1] if isinstance(result.get('description'), list) else (result.get('description') or '')[:400],
		'rights': (result.get('rights') or '')[:200] if isinstance(result.get('rights'), str) else None,
		'url': result.get('id'),
	}


async def search_all_pages(session: aiohttp.ClientSession, query: dict, max_pages: int) -> list[dict]:
	"""Walk the pagination links of one LoC search, retrying transient payload errors."""
	rows: list[dict] = []
	page = 1
	while page <= max_pages:
		params = {'fo': 'json', 'c': PAGE_SIZE, 'sp': page, 'at': 'results,pagination', 'q': query['q']}
		if query.get('fa'):
			params['fa'] = query['fa']
		doc = None
		for attempt in range(1, MAX_RETRIES + 1):
			try:
				async with session.get(SEARCH_URL, params=params, timeout=aiohttp.ClientTimeout(total=60)) as response:
					if response.status != 200:
						print(f'  page {page}: http {response.status}, attempt {attempt}')
					else:
						raw = await response.read()
						doc = json.loads(raw)
						break
			except Exception as exc:  # noqa: BLE001 - retry once, then give up on this query
				print(f'  page {page}: {type(exc).__name__}, attempt {attempt}')
				await asyncio.sleep(PAGE_SLEEP * attempt)
		if doc is None:
			break
		results = doc.get('results') or []
		rows.extend(slim(r) for r in results)
		total_pages = doc.get('pagination', {}).get('of')
		if not results or (total_pages and page >= total_pages):
			break
		page += 1
		await asyncio.sleep(PAGE_SLEEP)
	return rows


async def main() -> None:
	parser = argparse.ArgumentParser()
	parser.add_argument('--max-pages', type=int, default=4, help='pages per subject query (50/page)')
	args = parser.parse_args()
	OUT_DIR.mkdir(parents=True, exist_ok=True)

	seen_ids: set[str] = set()
	async with aiohttp.ClientSession(headers={'User-Agent': UA}) as session:
		for name, query in SUBJECT_QUERIES.items():
			print(f'collecting: {name}')
			rows = await search_all_pages(session, query, args.max_pages)
			deduped = [r for r in rows if r['loc_id'] and not (r['loc_id'] in seen_ids or seen_ids.add(r['loc_id']))]
			(OUT_DIR / f'{name}.json').write_text(json.dumps(deduped, ensure_ascii=False, indent=1), encoding='utf-8')
			if name == 'film_registry':
				with (OUT_DIR / 'film_registry.csv').open('w', newline='', encoding='utf-8') as fh:
					writer = csv.DictWriter(fh, fieldnames=['loc_id', 'title', 'date', 'subjects', 'url'], extrasaction='ignore')
					writer.writeheader()
					for r in deduped:
						row = dict(r)
						row['subjects'] = '|'.join(r['subjects'][:10])
						writer.writerow(row)
			print(f'  {name}: {len(deduped)} records')

	print(f'done -> {OUT_DIR}')


if __name__ == '__main__':
	asyncio.run(main())
