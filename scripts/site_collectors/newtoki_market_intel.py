"""Newtoki piracy-market intelligence — INVENTORY METADATA ONLY.

Question this answers (user, 2026-08-14): "어떤 작품들이 암흑의 시장에서
유통되는지 확인하고 싶다" — what circulates on the black market.

What is collected, and what is not:
  YES  listing inventory: titles, series ids, update recency, section,
       platform/genre attribution via the site's own filter taxonomy
  YES  category totals and daily snapshot deltas (supply-side time series)
  NO   content of any kind — no episode pages, no images, no text bodies.
       A title and its listing position are facts, not expression; the
       episode files are the infringement itself and copying them would make
       us a party to it.

This is the MUSO-style supply view: which genres, which source platforms,
how fast inventory turns over. Demand proxies (views) are not exposed by
this site's listing and are not chased.

Runs a real browser (same pattern as newtoki_watch.py), headless, host-locked.

Output (NEWTOKI_MI_OUT, default ~/newtoki_market):
  snapshots/YYYY-MM-DD/inventory_<section>.json
  snapshots/YYYY-MM-DD/taxonomy.json
  observations.jsonl - one row per listed title per run
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import os
import sys
from pathlib import Path
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).resolve().parent))
import newtoki_watch as nw  # reuse visit/pick_host/browser helpers

from browser_use.browser.profile import BrowserProfile
from browser_use.browser.session import BrowserSession

OUT_DIR = Path(os.environ.get('NEWTOKI_MI_OUT', str(Path.home() / 'newtoki_market')))
SECTIONS = [s for s in os.environ.get('NEWTOKI_SECTIONS', 'webtoon,manhwa,novel,anime').split(',') if s]
MAX_PAGES = int(os.environ.get('NEWTOKI_MI_PAGES', '10'))
PAGE_WAIT = 3.0

# The site's own filter vocabulary, read off the listing UI (2026-08-14).
PLATFORMS = ['naver', 'kakao', 'lezhin', 'toomics', 'toptoon', 'comica', 'battlecomics', 'keitoon', 'pinetoon', 'bomtoon', 'comic', 'ridibooks']
GENRES_KO = ['판타지', '액션', '개그', '미스터리', '로맨스', '드라마', '무협', '스포츠', '일상', '학원', '성인', 'BLGL', '한국', '중국']

JS_READ_LISTING = r"""
(() => {
	const seen = new Map();
	document.querySelectorAll('a[href*="/SECTION/"]').forEach(a => {
		const m = (a.getAttribute('href') || '').match(/\/SECTION\/(\d+)$/);
		if (!m) return;
		const text = (a.textContent || '').replace(/\s+/g, ' ').trim();
		if (!text || seen.has(m[1])) return;
		seen.set(m[1], text.slice(0, 120));
	});
	return JSON.stringify({count: seen.size, titles: [...seen.entries()].map(([id, title]) => ({id, title}))});
})()
""".replace('SECTION', '__SECTION__')


async def read_listing(session, host: str, section: str, query: str) -> list[dict]:
	"""One listing page -> title entries."""
	expression = JS_READ_LISTING.replace('__SECTION__', section)
	raw = await nw.visit(session, f'{host}/{section}{query}', expression)
	if not raw:
		return []
	try:
		return json.loads(raw).get('titles', [])
	except json.JSONDecodeError:
		return []


async def sweep_section(session, host: str, section: str) -> tuple[list[dict], int]:
	"""Paginate the listing; stop early when a page repeats page-1 ids."""
	entries: dict[str, dict] = {}
	for page in range(1, MAX_PAGES + 1):
		query = '' if page == 1 else f'?page={page}'
		titles = await read_listing(session, host, section, query)
		if not titles:
			break
		new_before = len(entries)
		for item in titles:
			entries.setdefault(item['id'], item)
		print(f'  {section} p{page}: {len(titles)} titles (total {len(entries)})', flush=True)
		if len(entries) == new_before:  # page served the same ids — stop
			break
		await asyncio.sleep(PAGE_WAIT)
	return list(entries.values()), len(entries)


async def count_filter(session, host: str, section: str, param: str, value: str) -> int:
	"""First-page count under one filter — cheap category sizing."""
	titles = await read_listing(session, host, section, f'?{param}={quote(value)}')
	await asyncio.sleep(PAGE_WAIT)
	return len(titles)


async def main() -> None:
	parser = argparse.ArgumentParser()
	parser.add_argument('--sections', nargs='*', default=SECTIONS)
	parser.add_argument('--skip-filters', action='store_true', help='pagination only, no category sizing')
	args = parser.parse_args()

	today = dt.date.today().isoformat()
	now = dt.datetime.now(dt.timezone.utc).isoformat()
	snap_dir = OUT_DIR / 'snapshots' / today
	snap_dir.mkdir(parents=True, exist_ok=True)

	profile = BrowserProfile(headless=True, keep_alive=False, allowed_domains=['newtoki1.org', '*.newtoki1.org'])
	session = BrowserSession(browser_profile=profile)
	try:
		await session.start()
		host, _mirrors = await nw.pick_host(session)
		if not host:
			raise SystemExit('no live mirror found')
		print(f'host: {host}')

		with (OUT_DIR / 'observations.jsonl').open('a', encoding='utf-8') as obs:
			for section in args.sections:
				print(f'[{section}]')
				entries, total = await sweep_section(session, host, section)
				(snap_dir / f'inventory_{section}.json').write_text(
					json.dumps({'section': section, 'date': today, 'count': total, 'titles': entries}, ensure_ascii=False, indent=2),
					encoding='utf-8',
				)
				for position, item in enumerate(entries, 1):
					obs.write(json.dumps({
						'source': 'newtoki1.org', 'record_type': 'piracy_market_inventory',
						'section': section, 'entity_type': 'work', 'entity_id': item['id'],
						'entity_title': item['title'], 'listing_position': position,
						'observed_at': now,
					}, ensure_ascii=False) + '\n')

			if not args.skip_filters:
				print('[taxonomy sizing]')
				taxonomy = {}
				for platform in PLATFORMS[:6]:
					taxonomy[f'platform:{platform}'] = await count_filter(session, host, 'webtoon', 'plat', platform)
					print(f'  plat={platform}: {taxonomy[f"platform:{platform}"]}')
				for genre in GENRES_KO[:8]:
					taxonomy[f'genre:{genre}'] = await count_filter(session, host, 'webtoon', 'tag', genre)
					print(f'  tag={genre}: {taxonomy[f"genre:{genre}"]}')
				(snap_dir / 'taxonomy.json').write_text(json.dumps({'date': today, 'counts': taxonomy}, ensure_ascii=False, indent=2), encoding='utf-8')
	finally:
		await session.stop()

	print(f'DONE -> {snap_dir}')


if __name__ == '__main__':
	asyncio.run(main())
