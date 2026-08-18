"""Google Trends daily search-trends collector (keyless RSS).

trends.google.com/trending/rss is the public daily-trends feed, no key, no
login — the only major trend surface still fully keyless. Works per-region.

Instagram and TikTok were probed the same day (2026-08-19): the Instagram
tag page returns a login wall (no og metadata without a session) and TikTok
Creative Center is a JS shell (0 hashtags server-side) — both need a browser
session or official API keys, so they are NOT collected here. Google Trends
carries the "what are people searching" axis those two cannot give keylessly.

Output (GTRENDS_OUT, default ~/gtrends_export):
  snapshots/YYYY-MM-DD.json - per geo: ranked trending queries + traffic approx
  trends.csv               - appended rows: date, geo, rank, query, traffic
"""

from __future__ import annotations

import asyncio
import csv
import json
import os
import re
import xml.etree.ElementTree as ET
from datetime import date, datetime, timezone
from pathlib import Path

import aiohttp

OUT_DIR = Path(os.environ.get('GTRENDS_OUT', str(Path.home() / 'gtrends_export')))
RSS_URL = 'https://trends.google.com/trending/rss'
GEOS = ('US', 'KR', 'JP')
UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36'
REQUEST_DELAY = 1.5


def parse_rss(xml_text: str) -> list[dict]:
	"""Ranked items with title, approximate traffic, and news titles."""
	try:
		root = ET.fromstring(xml_text)
	except ET.ParseError:
		return []
	items = []
	ns = {'ht': 'https://trends.google.com/trending/rss'}
	for rank, item in enumerate(root.findall('.//item'), 1):
		title = (item.findtext('title') or '').strip()
		traffic = (item.findtext('ht:approx_traffic', namespaces=ns) or '').strip()
		news_title = (item.findtext('ht:news_item/ht:news_item_title', namespaces=ns) or '').strip()[:160]
		if title:
			items.append({'rank': rank, 'query': title, 'traffic': traffic, 'news_title': news_title})
	return items


async def main() -> None:
	snap_dir = OUT_DIR / 'snapshots' / date.today().isoformat()
	snap_dir.mkdir(parents=True, exist_ok=True)
	stamp = datetime.now(timezone.utc).isoformat()

	rows: list[dict] = []
	snapshot: dict = {'collected_at': stamp}
	async with aiohttp.ClientSession(headers={'User-Agent': UA}) as session:
		for geo in GEOS:
			try:
				async with session.get(RSS_URL, params={'geo': geo}, timeout=aiohttp.ClientTimeout(total=30)) as resp:
					xml_text = await resp.text() if resp.status == 200 else ''
			except Exception:  # noqa: BLE001 - record the miss, keep other geos
				xml_text = ''
			items = parse_rss(xml_text)
			snapshot[geo] = items
			for it in items:
				rows.append({'collected_at': stamp[:10], 'geo': geo, **it})
			print(f'  {geo}: {len(items)} trends')
			await asyncio.sleep(REQUEST_DELAY)

	(snap_dir / 'trends.json').write_text(json.dumps(snapshot, ensure_ascii=False, indent=1), encoding='utf-8')
	csv_path = OUT_DIR / 'trends.csv'
	new_file = not csv_path.exists()
	with csv_path.open('a', newline='', encoding='utf-8') as fh:
		writer = csv.DictWriter(fh, fieldnames=['collected_at', 'geo', 'rank', 'query', 'traffic', 'news_title'])
		if new_file:
			writer.writeheader()
		writer.writerows(rows)
	print(f'done: {len(rows)} rows ({len(GEOS)} geos) -> {OUT_DIR}')


if __name__ == '__main__':
	asyncio.run(main())
