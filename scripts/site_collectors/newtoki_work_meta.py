"""Newtoki work-metadata enrichment — PUBLIC WORK FACTS ONLY.

Extends the market-intel inventory with each listed work's public facts:
original platform (네이버/카카오/...), author, genre, synopsis (the site's own
og:description copy), serialization status, hit counter, update day. This is
the "what circulates and where it came from" layer — author, genre and a
synopsis blurb are catalog facts any bookstore page shows.

NOT collected, ever: episode images/pages/text. The episode files ARE the
infringement; copying them would make us a party to it. This script never
follows an episode link.

Input:  inventory_*.json from newtoki_market_intel.py
Output: snapshots/YYYY-MM-DD/works_<section>.json + observations.jsonl rows
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import newtoki_watch as nw

from browser_use.browser.profile import BrowserProfile
from browser_use.browser.session import BrowserSession

OUT_DIR = Path(os.environ.get('NEWTOKI_MI_OUT', str(Path.home() / 'newtoki_market')))
FIELDS = ['작가', '장르', '플랫폼', '연재', '조회', '추천', '최신글']
DETAIL_WAIT = 3.0
BATCH_PAUSE_EVERY = 40
BATCH_PAUSE_SECONDS = 25.0

JS_READ_WORK_META = r"""
(() => {
	const meta = {};
	const ogd = document.querySelector('meta[property="og:description"]');
	// 200, per the snippet cap in docs/collection-policy.md. Observed synopses
	// top out at 155 chars so this binds nothing today; it stops the cap
	// drifting upward later, which is the only way a blurb becomes a body.
	if (ogd) meta.synopsis = (ogd.getAttribute('content') || '').slice(0, 200);
	const ogt = document.querySelector('meta[property="og:title"]');
	if (ogt) meta.og_title = (ogt.getAttribute('content') || '').slice(0, 160);
	const text = document.body.innerText.replace(/\s+/g, ' ');
	for (const key of ['작가', '장르', '플랫폼', '연재', '조회', '추천', '최신글']) {
		const m = text.match(new RegExp(key + '\\s*:?\\s*([^\\s,]{1,40})'));
		if (m) meta[key] = m[1];
	}
	return JSON.stringify(meta);
})()
"""


async def main() -> None:
	parser = argparse.ArgumentParser()
	parser.add_argument('--sections', nargs='*', default=['webtoon'])
	parser.add_argument('--inventory-dir', type=Path, default=None, help='dir holding inventory_<section>.json (default: latest snapshot)')
	parser.add_argument('--limit', type=int, default=0, help='cap works per section (smoke tests)')
	args = parser.parse_args()

	snap_root = OUT_DIR / 'snapshots'
	inv_dir = args.inventory_dir or sorted(snap_root.glob('????-??-??'))[-1]
	today = dt.date.today().isoformat()
	now = dt.datetime.now(dt.timezone.utc).isoformat()
	out_dir = snap_root / today
	out_dir.mkdir(parents=True, exist_ok=True)

	profile = BrowserProfile(headless=True, keep_alive=False, allowed_domains=['newtoki1.org', '*.newtoki1.org'])
	session = BrowserSession(browser_profile=profile)
	try:
		await session.start()
		host, _mirrors = await nw.pick_host(session)
		if not host:
			raise SystemExit('no live mirror')
		print(f'host: {host} | inventory: {inv_dir}')

		with (OUT_DIR / 'observations.jsonl').open('a', encoding='utf-8') as obs:
			for section in args.sections:
				inventory = json.loads((inv_dir / f'inventory_{section}.json').read_text(encoding='utf-8'))
				works = inventory['titles'] if args.limit <= 0 else inventory['titles'][: args.limit]
				print(f'[{section}] enriching {len(works)} works')
				enriched = []
				for index, work in enumerate(works, 1):
					if index > 1 and (index - 1) % BATCH_PAUSE_EVERY == 0:
						print(f'  ...pause {BATCH_PAUSE_SECONDS:.0f}s after {index - 1}')
						await asyncio.sleep(BATCH_PAUSE_SECONDS)
					try:
						raw = await nw.visit(session, f"{host}/{section}/{work['id']}", JS_READ_WORK_META)
						meta = json.loads(raw) if raw else {}
					except nw.RobotsRefusal:
						# Systematic, not per-work: if this path is disallowed then
						# every remaining one is too. Stopping is the honest response;
						# logging it per title would bury a policy failure in 1,600 rows.
						raise
					except Exception as exc:  # noqa: BLE001 - record and continue
						meta = {'error': type(exc).__name__}
					record = {**work, **meta, 'section': section}
					enriched.append(record)
					obs.write(json.dumps({
						'source': 'newtoki1.org', 'record_type': 'piracy_market_work_meta',
						'section': section, 'entity_type': 'work', 'entity_id': work['id'],
						'entity_title': work['title'],
						'author': meta.get('작가'), 'genre': meta.get('장르'),
						'origin_platform': meta.get('플랫폼'), 'status': meta.get('연재'),
						'hit_count': meta.get('조회'), 'synopsis_chars': len(meta.get('synopsis') or ''),
						'observed_at': now,
					}, ensure_ascii=False) + '\n')
					if index % 25 == 0:
						print(f'  {index}/{len(works)}')
					await asyncio.sleep(DETAIL_WAIT)
				(out_dir / f'works_{section}.json').write_text(
					json.dumps({'section': section, 'date': today, 'count': len(enriched), 'works': enriched}, ensure_ascii=False, indent=2),
					encoding='utf-8',
				)
				print(f'[{section}] DONE {len(enriched)} -> {out_dir / f"works_{section}.json"}')
	finally:
		await session.stop()


if __name__ == '__main__':
	asyncio.run(main())
