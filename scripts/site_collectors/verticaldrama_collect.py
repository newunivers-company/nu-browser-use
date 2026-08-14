"""VerticalDrama.tv ranking collector.

verticaldrama.tv is the friendliest cross-platform short-drama ranking source:
SSR HTML, robots.txt allows all crawlers (and explicitly welcomes AI crawlers),
and it publishes a sitemap + weekly VDS ranking.

Collected surfaces:
  /top/                 - weekly Top 20 by VDS score (rank, title, score,
                          hit/contender signal, show slug)
  / (home phone-cards)  - current №01… carousel (platform, genre, ep count,
                          views, rating) for the global weekly chart
  /apps/<platform>/     - per-platform pages, ordinal phone-card order

Output (VD_OUT, default ~/ranking_export):
  snapshots/YYYY-MM-DD/verticaldrama.json   - parsed rankings
  observations.jsonl                         - appended RankingObservation rows
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import os
import re
from html import unescape
from pathlib import Path

import aiohttp

BASE = 'https://www.verticaldrama.tv'
UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36'
OUT_DIR = Path(os.environ.get('VD_OUT', str(Path.home() / 'ranking_export')))
PLATFORMS = ['reelshort', 'dramabox', 'netshort', 'shortmax', 'vigloo', 'flareflow', 'hongguo', 'dramawave', 'goodshort', 'shotbook']


async def fetch(session: aiohttp.ClientSession, path: str) -> str | None:
	"""GET one page, None on any failure."""
	try:
		async with session.get(f'{BASE}{path}', timeout=aiohttp.ClientTimeout(total=30)) as response:
			if response.status != 200:
				return None
			return await response.text()
	except Exception:  # noqa: BLE001
		return None


def parse_top_table(html: str) -> list[dict]:
	"""Parse /top/: rank / title / VDS score / signal / slug."""
	table = re.search(r'<table.*?</table>', html, re.S)
	if not table:
		return []
	rows = []
	for row_html in re.findall(r'<tr[^>]*>(.*?)</tr>', table.group(0), re.S)[1:]:
		cells = [re.sub(r'<[^>]+>', '', c).strip() for c in re.findall(r'<t[dh][^>]*>(.*?)</t[dh]>', row_html, re.S)]
		if len(cells) < 3 or not cells[0].isdigit():
			continue
		link = re.search(r'<a href="(/shows/[^"]+)"', row_html)
		rows.append({
			'rank': int(cells[0]),
			'title': unescape(cells[1]),
			'score': cells[2],
			'signal': cells[3] if len(cells) > 3 else '',
			'slug': link.group(1) if link else '',
		})
	return rows


def parse_phone_cards(html: str, scope: str) -> list[dict]:
	"""Parse the home/platform phone-card carousel: №NN, platform, genre+eps, title, views+rating."""
	cards = []
	for block in re.findall(r'<div class="phone-card".*?(?=<div class="phone-card"|$)', html, re.S):
		rank = re.search(r'№\s*(\d+)', block)
		if not rank:
			continue
		def grab(cls: str) -> str:
			m = re.search(rf'<div class="{cls}">([^<]*)</div>', block)
			return unescape(m.group(1)).strip() if m else ''
		platform = grab('phone-platform')
		genre_ep = grab('phone-genre')
		genre_m = re.match(r'(.+?)\s*·\s*(\d+)\s*EP', genre_ep)
		meta = grab('phone-meta')
		views_m = re.search(r'([\d.]+[KM]?)$', meta)
		rating_m = re.search(r'★\s*([\d.]+|—)', meta)
		href = re.search(r'href="(/shows/[^"]+)"', block)
		cards.append({
			'rank': int(rank.group(1)),
			'title': grab('phone-title'),
			'platform': platform,
			'genres': [genre_m.group(1)] if genre_m else ([genre_ep] if genre_ep else []),
			'episodes': int(genre_m.group(2)) if genre_m else None,
			'views': views_m.group(1) if views_m else None,
			'rating': rating_m.group(1) if rating_m else None,
			'slug': href.group(1) if href else '',
			'scope': scope,
		})
	return cards


def parse_poster_grid(html: str, scope: str) -> list[dict]:
	"""Parse /apps/<platform>/ poster grids: DOM order is the ordinal ranking."""
	cards = []
	for block in re.findall(r'<a class="poster-card"[^>]*>(.*?)</a>', html, re.S):
		slug_m = re.search(r'href="(/shows/[^"]+)"', block)
		title_m = re.search(r'<div class="title">([^<]*)</div>', block)
		genre_m = re.search(r'<div class="genre">([^<]*)</div>', block)
		rating_m = re.search(r'★\s*([\d.]+)', block)
		if not title_m:
			continue
		cards.append({
			'rank': len(cards) + 1,  # ordinal = exposure order on the page
			'title': unescape(title_m.group(1)).strip(),
			'platform': scope.split(':', 1)[1] if ':' in scope else '',
			'genres': [genre_m.group(1)] if genre_m else [],
			'episodes': None,
			'views': None,
			'rating': rating_m.group(1) if rating_m else None,
			'slug': slug_m.group(1) if slug_m else '',
			'scope': scope,
		})
	return cards


def views_to_int(views: str | None) -> int | None:
	"""'1.5M'/'820K' -> int."""
	if not views:
		return None
	m = re.match(r'([\d.]+)([KM]?)', views)
	if not m:
		return None
	value = float(m.group(1))
	return int(value * {'K': 1e3, 'M': 1e6}.get(m.group(2), 1))


def observation(row: dict, ranking_name: str, rank_type: str, scope: dict, period: dict, url: str, observed_at: str) -> dict:
	"""Build one RankingObservation record from a parsed row."""
	return {
		'source': 'verticaldrama.tv',
		'ranking_name': ranking_name,
		'rank_type': rank_type,
		'entity_type': 'work',
		'entity_id': row.get('slug') or row['title'],
		'entity_title': row['title'],
		'scope': scope,
		'period': period,
		'rank': row['rank'],
		'raw_metric_name': 'VDS' if row.get('score') else ('views' if row.get('views') else None),
		'raw_score': row.get('score'),
		'views': views_to_int(row.get('views')),
		'rating': row.get('rating'),
		'platform': row.get('platform'),
		'genres': row.get('genres'),
		'episodes': row.get('episodes'),
		'source_url': url,
		'observed_at': observed_at,
	}


async def main() -> None:
	parser = argparse.ArgumentParser()
	parser.add_argument('--platforms', nargs='*', default=PLATFORMS)
	args = parser.parse_args()

	today = dt.date.today().isoformat()
	now = dt.datetime.now(dt.timezone.utc).isoformat()
	snap_dir = OUT_DIR / 'snapshots' / today
	snap_dir.mkdir(parents=True, exist_ok=True)

	async with aiohttp.ClientSession(headers={'User-Agent': UA}) as session:
		print('[1/3] /top/ weekly VDS table')
		top_html = await fetch(session, '/top/')
		top_rows = parse_top_table(top_html) if top_html else []
		print(f'  top: {len(top_rows)} rows')

		print('[2/3] home global carousel')
		home_html = await fetch(session, '/')
		global_cards = parse_phone_cards(home_html, 'global') if home_html else []
		print(f'  global cards: {len(global_cards)}')

		print(f'[3/3] platform pages: {len(args.platforms)}')
		platform_cards: list[dict] = []
		for platform in args.platforms:
			html = await fetch(session, f'/apps/{platform}/')
			if html:
				# Platform pages render poster grids (DOM order = ordinal), not phone cards.
				cards = parse_poster_grid(html, f'platform:{platform}') or parse_phone_cards(html, f'platform:{platform}')
				platform_cards.extend(cards)
				print(f'  {platform}: {len(cards)}')
			await asyncio.sleep(0.5)

	# Snapshot file.
	payload = {
		'date': today,
		'top_weekly': top_rows,
		'global_cards': global_cards,
		'platform_cards': platform_cards,
	}
	(snap_dir / 'verticaldrama.json').write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')

	# RankingObservation appends.
	obs = []
	for row in top_rows:
		obs.append(observation(row, 'VDS Weekly Top', 'CROSS_PLATFORM', {'type': 'global'}, {'type': 'weekly'}, f'{BASE}/top/', now))
	for row in global_cards:
		obs.append(observation(row, 'Global Carousel', 'CROSS_PLATFORM', {'type': 'global'}, {'type': 'weekly'}, f'{BASE}/', now))
	for row in platform_cards:
		platform = row['scope'].split(':', 1)[1]
		obs.append(observation(row, f'{platform} Carousel', 'PLATFORM_INTERNAL', {'type': 'platform', 'platform': platform}, {'type': 'weekly'}, f'{BASE}/apps/{platform}/', now))
	with (OUT_DIR / 'observations.jsonl').open('a', encoding='utf-8') as handle:
		for record in obs:
			handle.write(json.dumps(record, ensure_ascii=False) + '\n')

	print(f'DONE -> {snap_dir} | snapshot rows: top={len(top_rows)} global={len(global_cards)} platform={len(platform_cards)}')
	print(f'observations appended: {len(obs)} -> {OUT_DIR / "observations.jsonl"}')


if __name__ == '__main__':
	asyncio.run(main())
