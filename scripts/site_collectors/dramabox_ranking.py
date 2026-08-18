"""DramaBox homepage ranking collector (keyless).

dramaboxapp.com SSRs its homepage rails into __NEXT_DATA__:
  bigList   - 3 featured works (banner cards, viewCount included)
  smallData - editorial rails (必看好剧/当前热播/精彩剧集...), each with 6 items
              carrying bookId/bookName/viewCount

Rail DOM order is the ordinal ranking — PLATFORM_INTERNAL observations.
Complements dramabox_collect.py (catalog) with a daily ranking surface.

NetShort note: netshort.com geo-redirects to a /marketing stub from this
network (2026-08-14), so it is not collectible here right now.

Output (DRAMABOX_OUT, default ~/ranking_export):
  snapshots/YYYY-MM-DD/dramabox_<locale>.json
  observations.jsonl (appended)
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import os
import re
from pathlib import Path

import aiohttp

UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36'
OUT_DIR = Path(os.environ.get('DRAMABOX_RANK_OUT', str(Path.home() / 'ranking_export')))
NEXT_DATA_RE = re.compile(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', re.S)
HOME_URL = 'https://www.dramaboxapp.com/{locale}'
LOCALES = ['ko', 'en']


def extract_rails(html: str) -> dict:
	"""Pull featured works + editorial rails from __NEXT_DATA__."""
	match = NEXT_DATA_RE.search(html)
	if not match:
		return {}
	try:
		payload = json.loads(match.group(1))
		props = payload['props']['pageProps']
	except (json.JSONDecodeError, KeyError):
		return {}
	# The page is statically generated, so viewCount is fixed at build time, not a
	# live counter: two fresh fetches 20 hours apart returned identical figures
	# under buildId dramaboxapp_prod_20260703 — a build six weeks old. Without the
	# build id, "the rails did not change" and "the site has not been rebuilt" are
	# the same flat series and mean different things.
	build_id = payload.get('buildId')
	featured = [
		{'bookId': w.get('bookId'), 'bookName': w.get('bookName'), 'viewCount': w.get('viewCount'), 'tags': w.get('tags') or []}
		for w in props.get('bigList') or []
		if w.get('bookId')
	]
	rails = []
	for rail in props.get('smallData') or []:
		items = [
			{'bookId': i.get('bookId'), 'bookName': i.get('bookName'), 'viewCount': i.get('viewCount')}
			for i in rail.get('items') or []
			if i.get('bookId')
		]
		if items:
			rails.append({'name': rail.get('name'), 'items': items})
	return {'build_id': build_id, 'featured': featured, 'rails': rails}


def _observation(rail_name: str, rank: int, work: dict, locale: str, now: str, build_id: str | None) -> dict:
	"""One PLATFORM_INTERNAL observation row."""
	return {
		'source': 'dramaboxapp.com',
		'ranking_name': rail_name,
		'rank_type': 'PLATFORM_INTERNAL',
		# Which build these figures were baked into; identical build means the
		# reading is a repeat, not a confirmation that nothing moved.
		'build_id': build_id,
		'entity_type': 'work',
		'entity_id': str(work.get('bookId')),
		'entity_title': work.get('bookName'),
		'scope': {'type': 'platform', 'platform': 'dramabox', 'locale': locale},
		'period': {'type': 'daily'},
		'rank': rank,
		'raw_metric_name': 'view_count',
		'raw_score': work.get('viewCount'),
		'source_url': HOME_URL.format(locale=locale),
		'observed_at': now,
	}


async def collect_locale(session: aiohttp.ClientSession, locale: str, today: str, now: str) -> dict:
	"""Collect one locale homepage; write snapshot + observations."""
	try:
		async with session.get(HOME_URL.format(locale=locale), timeout=aiohttp.ClientTimeout(total=40)) as response:
			if response.status != 200:
				return {}
			html = await response.text()
	except Exception:  # noqa: BLE001
		return {}
	data = extract_rails(html)
	if not data:
		return data

	observations = []
	build_id = data.get('build_id')
	for position, work in enumerate(data.get('featured') or [], 1):
		observations.append(_observation('featured', position, work, locale, now, build_id))
	for rail in data.get('rails') or []:
		for position, work in enumerate(rail['items'], 1):
			observations.append(_observation(f'rail:{rail["name"]}', position, work, locale, now, build_id))
	data['observations'] = observations

	snap_dir = OUT_DIR / 'snapshots' / today
	snap_dir.mkdir(parents=True, exist_ok=True)
	(snap_dir / f'dramabox_{locale}.json').write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
	with (OUT_DIR / 'observations.jsonl').open('a', encoding='utf-8') as handle:
		for record in observations:
			handle.write(json.dumps(record, ensure_ascii=False) + '\n')
	return data


async def main() -> None:
	parser = argparse.ArgumentParser()
	parser.add_argument('--locales', nargs='*', default=LOCALES)
	args = parser.parse_args()

	today = dt.date.today().isoformat()
	now = dt.datetime.now(dt.timezone.utc).isoformat()
	async with aiohttp.ClientSession(headers={'User-Agent': UA}) as session:
		for locale in args.locales:
			data = await collect_locale(session, locale, today, now)
			featured = len(data.get('featured') or [])
			rails = len(data.get('rails') or [])
			print(f'{locale}: featured={featured} rails={rails} observations={len(data.get("observations") or [])}')
			await asyncio.sleep(1.0)
	print(f'DONE -> {OUT_DIR / "snapshots" / today}')


if __name__ == '__main__':
	asyncio.run(main())
