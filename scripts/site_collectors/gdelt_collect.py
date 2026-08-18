"""GDELT free DOC API collector — news event seeds for story material.

STATUS 2026-08-16: THIS SOURCE HAS NEVER RETURNED A ROW
Every snapshot on disk is a 2-byte empty array and articles.csv was never
created. The step nonetheless reported `ok` in every cadence run, because
exiting zero after collecting nothing is indistinguishable from exiting zero
after collecting something — which is why it now exits non-zero instead.

The block is not our pacing. A single isolated request, with no burst around it,
answers HTTP 429 with "Please limit requests to one every 5 seconds or contact
kalev.leetaru5@gmail.com for larger queries. All high-traffic users should
switch to our ngrams dataset". So retrying is pointless and the collector is
unscheduled; the operator paths GDELT itself names — the ngrams dataset, or
contacting them — are what would change the answer.

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
from datetime import date
from pathlib import Path

import aiohttp

OUT_DIR = Path(os.environ.get('GDELT_OUT', str(Path.home() / 'gdelt_export')))
API_URL = 'https://api.gdeltproject.org/api/v2/doc/doc'
UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36'
MIN_INTERVAL = 65.0  # this IP sees 429 well past the documented 5s limit — wait a full minute between queries
# Three attempts at 180s put the worst case at roughly 28 minutes for three
# queries, which is structurally over the 20-minute slot the cadence allows, and
# the 2026-08-16 weekly run duly timed out. Two attempts at 150s bound it at
# about 15 minutes. The retry budget is what has to fit the schedule, not the
# other way round.
RETRY_WAIT = 150.0
ATTEMPTS = 2

QUERIES = {
	'short_drama_market': '"short drama" OR "micro drama" OR "vertical drama"',
	'ai_video_production': '"AI video" OR "AI film" OR "generative video"',
	'webnovel_adaptation': '"web novel" adaptation OR "webtoon drama"',
}


async def fetch_query(session: aiohttp.ClientSession, name: str, query: str, timespan: str) -> list[dict]:
	"""One DOC API call with rate-limit backoff; returns flat article rows."""
	params = {
		'query': f'{query} sourcelang:english',
		'mode': 'artlist',
		'maxrecords': '75',
		'format': 'json',
		'timespan': timespan,
	}
	for attempt in range(1, ATTEMPTS + 1):
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


def append_csv(rows: list[dict]) -> None:
	if not rows:
		return
	path = OUT_DIR / 'articles.csv'
	path.parent.mkdir(parents=True, exist_ok=True)
	new_file = not path.exists()
	with path.open('a', newline='', encoding='utf-8') as handle:
		writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
		if new_file:
			writer.writeheader()
		writer.writerows(rows)


async def main() -> int:
	parser = argparse.ArgumentParser()
	parser.add_argument('--timespan', default='7d')
	args = parser.parse_args()

	# Local date, matching every other collector. This one used UTC, so the 06:00
	# KST weekly run filed its output under the previous day and looked like a run
	# that had not happened.
	stamp_dir = OUT_DIR / 'snapshots' / date.today().isoformat()
	stamp_dir.mkdir(parents=True, exist_ok=True)

	all_rows: list[dict] = []
	async with aiohttp.ClientSession(headers={'User-Agent': UA}) as session:
		for name, query in QUERIES.items():
			rows = await fetch_query(session, name, query, args.timespan)
			(stamp_dir / f'{name}.json').write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding='utf-8')
			print(f'  {name}: {len(rows)} articles')
			all_rows.extend(rows)
			# Appended per query rather than at the end: this step has timed out
			# mid-run, and a timeout should cost the remaining queries, not the
			# ones already answered.
			append_csv(rows)
			await asyncio.sleep(MIN_INTERVAL)

	print(f'done: {len(all_rows)} articles -> {OUT_DIR}')
	if not all_rows:
		# A collector that collected nothing must not report success. This one did,
		# for its entire life, and the cadence recorded `ok` every time.
		print('NO ROWS from any query — treat as a failure, not an empty week')
		return 1
	return 0


if __name__ == '__main__':
	raise SystemExit(asyncio.run(main()))
