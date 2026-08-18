"""duanju007.com weekly Chinese short-drama ranking collector.

WordPress SSR site publishing weekly tables. The home page table is the
抖音周榜 (Douyin weekly); sister pages carry 红果周榜 (Hongguo) and 出海榜单
(overseas). Each row:

  rank | title | genres(/) | platform | distributor | production | minutes
  | new_views(万) | cumulative_views(万)

Views are in 万 (10k units). New vs cumulative weekly views are the most
interesting columns — they give an actual weekly-velocity signal, which none
of the Western sources expose.

Output (DUANJU_OUT, default ~/ranking_export):
  snapshots/YYYY-MM-DD/duanju007_<kind>.json
  observations.jsonl (appended, VIEW_COUNT type)
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import os
import re
import sys
from html import unescape
from pathlib import Path

# Windows console (cp949) can't print Chinese; force utf-8 stdout.
if sys.stdout.encoding and sys.stdout.encoding.lower() != 'utf-8':
	sys.stdout.reconfigure(encoding='utf-8', errors='replace')

import aiohttp

BASE = 'https://duanju007.com'
UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36'
OUT_DIR = Path(os.environ.get('DUANJU_OUT', str(Path.home() / 'ranking_export')))
# page path -> (kind, chinese name, scope platform)
PAGES = {
	'/': ('douyin_weekly', '抖音周榜', 'douyin'),
	'/anime_hg/': ('hongguo_weekly', '红果周榜', 'hongguo'),
	'/oversea/': ('overseas', '出海榜单', None),
}
WAN = 10_000


async def fetch(session: aiohttp.ClientSession, path: str) -> str | None:
	try:
		async with session.get(f'{BASE}{path}', timeout=aiohttp.ClientTimeout(total=40)) as response:
			if response.status != 200:
				return None
			return await response.text()
	except Exception:  # noqa: BLE001
		return None


def parse_table(html: str) -> tuple[str | None, list[dict]]:
	"""Extract (period label, rows) from the first ranking table."""
	match = re.search(r'<table[^>]*>(.*?)</table>', html, re.S)
	if not match:
		return None, []
	table = match.group(1)
	rows = re.findall(r'<tr[^>]*>(.*?)</tr>', table, re.S)
	period = None
	parsed: list[dict] = []
	for row in rows:
		cells = [re.sub(r'<[^>]+>', '', c).strip() for c in re.findall(r'<t[dh][^>]*>(.*?)</t[dh]>', row, re.S)]
		cells = [unescape(c) for c in cells]
		if not cells:
			continue
		period_m = re.match(r'(\d{4}-\d{2}-\d{2})\s*-\s*(\d{4}-\d{2}-\d{2})', cells[0])
		if period_m and len(cells) == 1:
			period = period_m.group(0)
			continue
		if len(cells) >= 9 and cells[0].isdigit():
			parsed.append({
				'rank': int(cells[0]),
				'title': cells[1],
				'genres': [g for g in cells[2].split('/') if g],
				'platform': cells[3],
				'distributor': '' if cells[4] == '我要认领' else cells[4],
				'production': '' if cells[5] == '我要认领' else cells[5],
				'minutes': int(cells[6]) if cells[6].isdigit() else None,
				'new_views': int(cells[7]) * WAN if cells[7].isdigit() else None,
				'cumulative_views': int(cells[8]) * WAN if cells[8].isdigit() else None,
			})
	return period, parsed


async def main() -> None:
	parser = argparse.ArgumentParser()
	parser.add_argument('--pages', nargs='*', default=list(PAGES))
	args = parser.parse_args()

	today = dt.date.today().isoformat()
	now = dt.datetime.now(dt.timezone.utc).isoformat()
	snap_dir = OUT_DIR / 'snapshots' / today
	snap_dir.mkdir(parents=True, exist_ok=True)

	async with aiohttp.ClientSession(headers={'User-Agent': UA}) as session:
		for path in args.pages:
			if path not in PAGES:
				continue
			kind, chinese, platform = PAGES[path]
			html = await fetch(session, path)
			if not html:
				print(f'{chinese}: fetch failed')
				continue
			period, rows = parse_table(html)
			(snap_dir / f'duanju007_{kind}.json').write_text(
				json.dumps({'kind': kind, 'chinese': chinese, 'period': period, 'rows': rows}, ensure_ascii=False, indent=2),
				encoding='utf-8',
			)
			scope = {'type': 'platform', 'platform': platform} if platform else {'type': 'global'}
			with (OUT_DIR / 'observations.jsonl').open('a', encoding='utf-8') as handle:
				for row in rows:
					handle.write(json.dumps({
						'source': 'duanju007.com', 'ranking_name': chinese, 'rank_type': 'VIEW_COUNT',
						'entity_type': 'work', 'entity_id': row['title'], 'entity_title': row['title'],
						'scope': scope, 'period': {'type': 'weekly', 'label': period},
						'rank': row['rank'], 'raw_metric_name': 'new_views',
						'raw_score': row['new_views'], 'views': row['cumulative_views'],
						'platform': row['platform'], 'genres': row['genres'], 'episodes': row['minutes'],
						'source_url': f'{BASE}{path}', 'observed_at': now,
					}, ensure_ascii=False) + '\n')
			print(f'{chinese}: period={period} rows={len(rows)}')
			await asyncio.sleep(1.0)

	print(f'DONE -> {snap_dir}')


if __name__ == '__main__':
	asyncio.run(main())
