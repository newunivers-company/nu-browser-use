"""Munpia keyless web-novel ranking collector.

robots.txt: Allow all (only /novel/viewer/ disallowed); sitemap offered. The PC
site is a JS shell, but the underlying JSON API is unauthenticated:

  1. /api/v1/main/layouts          -> home ranking rails (SideNovelRank* sections)
  2. /api/v1/pc/novel-detail/{id}  -> full metadata per novel:
     title, authorName, genres[], introduction, viewCount, preferenceCount,
     likeCount, chapterCount, serialize status (finish/pause/free/paidSerial),
     exclusive, createdAt/updatedAt, coverUrl

Metadata only — no episode text, no cover downloads by default. Web-novel
titles are themselves trend data (doc: story layer §29 "작품 제목도 데이터").

Output layout (MUNPIA_OUT, default ~/munpia_export):
  rankings.json  - ranking rails snapshot with per-novel detail merged
  rankings.csv   - flat
  snapshots/     - dated raw layout responses for velocity tracking
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

OUT_DIR = Path(os.environ.get('MUNPIA_OUT', str(Path.home() / 'munpia_export')))
BASE = 'https://www.munpia.com'
UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36'
DETAIL_SLEEP = 0.7
GENRE_SLEEP = 1.0

# Genre code map from the pc-novel JS bundle (Ij constant)
GENRES = {
	'ALL': '전체 장르',
	'NEWFANTASY': '현대판타지',
	'FANTASY': '판타지',
	'HEROISM': '무협',
	'HISTORY': '대체역사',
	'SPORTS': '스포츠',
	'FUSION': '퓨전',
	'DRAMA': '드라마',
	'MILIWAR': '전쟁·밀리터리',
	'ROMANCE': '로맨스',
	'GAME': '게임',
	'SCIENCE': 'SF',
	'ETC': '기타',
}


def extract_rank_rows(layout_result: dict) -> list[dict]:
	"""Pull every ranking row (novelId + rail context) from the layout payload."""
	rows: list[dict] = []

	def walk(o: object, rail: str = '') -> None:
		if isinstance(o, dict):
			if 'novelId' in o and isinstance(o.get('novelId'), int):
				rows.append(
					{
						'rail': rail,
						'novel_id': o['novelId'],
						'title': (o.get('title') or '')[:200],
						'author': (o.get('author') or o.get('authorName') or '')[:80],
					}
				)
			for v in o.values():
				walk(v, rail)
		elif isinstance(o, list):
			for item in o:
				walk(item, rail)

	for section in layout_result.get('layout', []):
		name = section.get('componentTypeKey') or ''
		walk(section.get('data') or {}, name)
	return rows


async def main() -> None:
	parser = argparse.ArgumentParser()
	parser.add_argument('--no-details', action='store_true', help='ranking rails only')
	args = parser.parse_args()

	snap_dir = OUT_DIR / 'snapshots'
	snap_dir.mkdir(parents=True, exist_ok=True)
	stamp = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H%M%SZ')

	async with aiohttp.ClientSession(headers={'User-Agent': UA, 'Accept': 'application/json'}) as session:
		async with session.get(f'{BASE}/api/v1/main/layouts', timeout=aiohttp.ClientTimeout(total=30)) as resp:
			layouts = await resp.json()
		(snap_dir / f'{stamp}.json').write_text(json.dumps(layouts, ensure_ascii=False), encoding='utf-8')
		rows = extract_rank_rows(layouts.get('result') or {})

		# --- genre-best rails: top-25 per genre (codes from the JS bundle) ------
		genre_rows: list[dict] = []
		genre_snap: dict[str, list] = {}
		for code, label in GENRES.items():
			if code == 'ALL':
				continue
			params = {'genre': code, 'adultMode': 'false', 'platinum': 'false'}
			try:
				async with session.get(
					f'{BASE}/api/v1/main/novel-detail/genre-best',
					params=params,
					timeout=aiohttp.ClientTimeout(total=20),
				) as resp:
					doc = await resp.json()
				novels = (doc.get('result') or {}).get('novels') or []
				genre_snap[code] = novels
				for n in novels:
					genre_rows.append(
						{
							'rail': f'genre_best:{label}',
							'novel_id': n.get('novelId'),
							'title': (n.get('title') or '')[:200],
							'author': (n.get('author') or '')[:80],
							'rank': n.get('rank'),
							'sub_genre': n.get('subGenre'),
						}
					)
				print(f'  genre {label}: {len(novels)} novels')
			except Exception:  # noqa: BLE001 - a failed genre is skipped
				print(f'  genre {label}: failed')
			await asyncio.sleep(GENRE_SLEEP)
		(snap_dir / f'{stamp}_genre_best.json').write_text(json.dumps(genre_snap, ensure_ascii=False), encoding='utf-8')
		rows.extend(genre_rows)

		# dedupe per rail+id, keep first
		seen: set[tuple[str, int]] = set()
		deduped: list[dict] = []
		for row in rows:
			key = (row['rail'], row['novel_id'])
			if key not in seen:
				seen.add(key)
				deduped.append(row)
		rows = deduped
		print(f'ranking rows: {len(rows)} across rails')

		if not args.no_details:
			ids = sorted({r['novel_id'] for r in rows})
			details: dict[int, dict] = {}
			for n, nid in enumerate(ids, 1):
				try:
					async with session.get(f'{BASE}/api/v1/pc/novel-detail/{nid}', timeout=aiohttp.ClientTimeout(total=20)) as resp:
						doc = await resp.json()
					ni = (doc.get('result') or {}).get('novelInfo') or {}
					if ni.get('id'):
						details[nid] = ni
				except Exception:  # noqa: BLE001 - record miss, continue
					pass
				if n % 10 == 0:
					print(f'  details {n}/{len(ids)}')
				await asyncio.sleep(DETAIL_SLEEP)
			for row in rows:
				ni = details.get(row['novel_id'], {})
				row.update(
					{
						'genres': '|'.join(ni.get('genres') or []),
						'author_name': ni.get('authorName'),
						'view_count': ni.get('viewCount'),
						'preference_count': ni.get('preferenceCount'),
						'like_count': ni.get('likeCount'),
						'chapter_count': ni.get('chapterCount'),
						'finish': ni.get('finish'),
						'pause': ni.get('pause'),
						'free': ni.get('free'),
						'paid_serial': ni.get('paidSerial'),
						'exclusive': ni.get('exclusive'),
						'adult': ni.get('adult'),
						'introduction': (ni.get('introduction') or '')[:400],
						'created_at': ni.get('createdAt'),
						'updated_at': ni.get('updatedAt'),
					}
				)
			print(f'details fetched: {len(details)}/{len(ids)}')

	(OUT_DIR / 'rankings.json').write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding='utf-8')
	fieldnames = ['rail', 'rank', 'novel_id', 'title', 'author', 'author_name', 'genres', 'sub_genre', 'view_count', 'preference_count', 'like_count', 'chapter_count', 'finish', 'pause', 'free', 'paid_serial', 'exclusive', 'adult', 'introduction', 'created_at', 'updated_at']
	with (OUT_DIR / 'rankings.csv').open('w', newline='', encoding='utf-8') as fh:
		writer = csv.DictWriter(fh, fieldnames=fieldnames, extrasaction='ignore')
		writer.writeheader()
		writer.writerows(rows)
	print(f'done: {len(rows)} rows -> {OUT_DIR}')


if __name__ == '__main__':
	asyncio.run(main())
