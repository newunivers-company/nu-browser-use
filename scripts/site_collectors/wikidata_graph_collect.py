"""Wikidata SPARQL keyless collector — story/adaptation knowledge-graph seeds.

CC0, no key. Builds the Work↔Origin↔Person↔Event graph seeds the Story
Intelligence layer needs (doc §10): recent TV drama/web series + their source
material relationships.

Queries (each small and bounded — endpoint caps 60s/parallel per UA+IP):
  1. recent_series   — TV/web series with a 'based on' (P144) source work
  2. shortdrama_alt  — web series instances (Q68961619 alt) with country+date
  3. trope_films     — films based on novels (sample) for adaptation edges

Output layout (WIKIDATA_OUT, default ~/wikidata_export):
  <name>.json - raw SPARQL result bindings
  <name>.csv  - flattened rows
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import os
from pathlib import Path

import aiohttp

OUT_DIR = Path(os.environ.get('WIKIDATA_OUT', str(Path.home() / 'wikidata_export')))
SPARQL_URL = 'https://query.wikidata.org/sparql'
UA = 'nu-browser-use-collector/1.0 (Wikidata SPARQL seed collection; contact: dev)'
QUERY_SLEEP = 30.0
RETRY_SLEEP = 120.0

QUERIES: dict[str, str] = {
	'recent_series_based_on': """
SELECT ?series ?seriesLabel ?basedOn ?basedOnLabel ?pubDate ?countryLabel WHERE {
  ?series wdt:P31/wdt:P279* wd:Q5398426;
          wdt:P144 ?basedOn.
  OPTIONAL { ?series wdt:P577 ?pubDate. FILTER(YEAR(?pubDate) >= 2015) }
  OPTIONAL { ?series wdt:P495 ?country. }
  SERVICE wikibase:label { bd:serviceParam wikibase:language "en". }
} LIMIT 500
""",
	'web_series': """
SELECT ?series ?seriesLabel ?pubDate ?countryLabel ?genreLabel WHERE {
  ?series wdt:P31 ?subtype.
  ?subtype wdt:P279* wd:Q21191270.
  OPTIONAL { ?series wdt:P577 ?pubDate. }
  OPTIONAL { ?series wdt:P495 ?country. }
  OPTIONAL { ?series wdt:P136 ?genre. }
  SERVICE wikibase:label { bd:serviceParam wikibase:language "en". }
} LIMIT 500
""",
	'novel_to_film_edges': """
SELECT ?film ?filmLabel ?novel ?novelLabel ?novelistLabel ?pubDate WHERE {
  ?film wdt:P31 wd:Q11424;
        wdt:P144 ?novel.
  ?novel wdt:P50 ?novelist.
  OPTIONAL { ?film wdt:P577 ?pubDate. FILTER(YEAR(?pubDate) >= 2010) }
  SERVICE wikibase:label { bd:serviceParam wikibase:language "en". }
} LIMIT 500
""",
}


async def run_query(session: aiohttp.ClientSession, name: str, query: str) -> list[dict]:
	"""Run one SPARQL query with retry on 429/5xx, flattening bindings to rows."""
	for attempt in (1, 2, 3):
		try:
			async with session.get(
				SPARQL_URL,
				params={'query': query, 'format': 'json'},
				headers={'Accept': 'application/sparql-results+json'},
				timeout=aiohttp.ClientTimeout(total=90),
			) as response:
				if response.status == 429 or response.status >= 500:
					print(f'  {name}: HTTP {response.status}, retry {attempt} in {RETRY_SLEEP}s')
					await asyncio.sleep(RETRY_SLEEP)
					continue
				if response.status != 200:
					print(f'  {name}: HTTP {response.status}')
					return []
				doc = await response.json()
		except Exception as exc:  # noqa: BLE001
			print(f'  {name}: {type(exc).__name__}, retry {attempt}')
			await asyncio.sleep(RETRY_SLEEP)
			continue
		break
	else:
		return []
	bindings = doc.get('results', {}).get('bindings', [])
	rows = [{k: v.get('value', '') for k, v in b.items()} for b in bindings]
	(OUT_DIR / f'{name}.json').write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding='utf-8')
	if rows:
		fieldnames = sorted({key for row in rows for key in row})
		with (OUT_DIR / f'{name}.csv').open('w', newline='', encoding='utf-8') as fh:
			writer = csv.DictWriter(fh, fieldnames=fieldnames)
			writer.writeheader()
			writer.writerows(rows)
	print(f'  {name}: {len(rows)} rows')
	return rows


async def main() -> None:
	parser = argparse.ArgumentParser()
	parser.add_argument('--only', default='', help='comma-separated query names')
	args = parser.parse_args()
	OUT_DIR.mkdir(parents=True, exist_ok=True)

	names = [n.strip() for n in args.only.split(',') if n.strip()] or list(QUERIES)
	async with aiohttp.ClientSession(headers={'User-Agent': UA}) as session:
		for name in names:
			await run_query(session, name, QUERIES[name])
			await asyncio.sleep(QUERY_SLEEP)
	print(f'done -> {OUT_DIR}')


if __name__ == '__main__':
	asyncio.run(main())
