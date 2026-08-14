"""GDELT free DOC API collector — news event seeds for story material.

api.gdeltproject.org is the free classic API (5-second minimum between
requests, enforced hard — backoff is mandatory). GDELT Cloud is a separate
paid product and is NOT used.

Queries track the story-intelligence keyword set (trope/event vocabulary).
Each run appends dated snapshots; news is time-sensitive so snapshots
accumulate rather than overwrite.

Output layout (GDELT_OUT, default ~/gdelt_export):
  snapshots/YYYY-MM-DD/<query>.json - article lists
  articles.csv                      - appended flat rows
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import os
from datetime import datetime, timezone
from pathlib import Path

import aiohttp

OUT_DIR = Path(os.environ.get('GDELT_OUT', str(Path.home() / 'gdelt_export')))
API_URL = 'https://api.gdeltproject.org/api/v2/doc/doc'
UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36'
MIN_INTERVAL = 65.0  # this IP sees 429 well past the documented 5s limit — wait a full minute between queries
RETRY_WAIT = 180.0

QUERIES = {
	'short_drama_market': '"short drama" OR "micro drama" OR "vertical drama"',
	'ai_video_production': '"AI video" OR "AI film" OR "generative video"',
	'webnovel_adaptation': '"web novel" adaptation OR "webtoon drama"',
}


async def fetch_query(session: aiohttp.ClientSession, name: str, query: str, timespan: str) -> list[dict]:
	"""One DOC API call with rate-limit backoff; returns flat article rows."""
	params = {'query': f'{query} sourcelang:english', 'mode': 'artlist', 'maxrecords': '75', 'format': 'json', 'timespan': timespan}
	for attempt in (1, 2, 3):
		try:
			async with session.get(API_URL, params=params, timeout=aiohttp.ClientTimeout(total=45)) as response:
				if response.status == 429:
					print(f'  {name}: 429 rate limited, retry {attempt} in {RETRY_WAIT}s')
					await asyncio.sleep(RETRY_WAIT)
					continue
				if response.status != 200:
					print(f'  {name}: HTTP {response.status}')
					return []
				doc = await response.json(content_type=None)
		except Exception as exc:  # noqa: BLE001
			print(f'  {name}: {type(exc).__name__}, retry {attempt}')
			await asyncio.sleep(RETRY_WAIT)
			continue
		break
	else:
		return []
	rows = []
	for art in doc.get('articles', []):
		rows.append(
			{
				'query': name,
				'url': art.get('url'),
				'title': (art.get('title') or '')[:250],
				'domain': art.get('domain'),
				'seendate': art.get('seendate'),
				'language': art.get('language'),
				'sourcecountry': art.get('sourcecountry'),
			}
		)
	return rows


async def main() -> None:
	parser = argparse.ArgumentParser()
	parser.add_argument('--timespan', default='7d')
	args = parser.parse_args()

	stamp_dir = OUT_DIR / 'snapshots' / datetime.now(timezone.utc).strftime('%Y-%m-%d')
	stamp_dir.mkdir(parents=True, exist_ok=True)

	all_rows: list[dict] = []
	async with aiohttp.ClientSession(headers={'User-Agent': UA}) as session:
		for name, query in QUERIES.items():
			rows = await fetch_query(session, name, query, args.timespan)
			(stamp_dir / f'{name}.json').write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding='utf-8')
			print(f'  {name}: {len(rows)} articles')
			all_rows.extend(rows)
			await asyncio.sleep(MIN_INTERVAL)

	if all_rows:
		new_file = not (OUT_DIR / 'articles.csv').exists()
		with (OUT_DIR / 'articles.csv').open('a', newline='', encoding='utf-8') as fh:
			writer = csv.DictWriter(fh, fieldnames=list(all_rows[0].keys()))
			if new_file:
				writer.writeheader()
			writer.writerows(all_rows)
	print(f'done: {len(all_rows)} articles -> {OUT_DIR}')


if __name__ == '__main__':
	asyncio.run(main())
