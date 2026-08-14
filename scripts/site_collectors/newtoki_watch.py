"""Newtoki infringement watch — OUR TITLES ONLY.

Purpose: rights protection. Searches newtoki for a watchlist of titles WE own
or distribute and logs every match as takedown evidence. This is the
anti-piracy monitoring pattern (cf. MUSO), not a catalog crawler.

Scope guardrails (do not widen):
  - queries come ONLY from the watchlist file
  - matches record title + page URL + observed date; no content download
  - no site cataloging, no episode or image fetching, no listing enumeration

WHY THE CONTROL QUERY EXISTS
The 2026-08-14 run reported 0 hits on all five titles. That reading was not
trustworthy: the site had moved search behind a redirect (`?stx=` now 302s to
`/webtoon/__q/<base64 of the query string>`), so a broken detector and a clean
result were indistinguishable. For a rights-protection tool that is the worst
failure mode — silence read as safety.

Every run therefore fires a control query first (a term that must match on any
Korean webtoon index) plus a nonsense query that must match nothing. If the
control returns nothing, or the nonsense query returns something, the run is
recorded as `detector_status: unverified` and explicitly does NOT claim the
absence of infringement. Verified against the live site: control -> 37 results,
nonsense -> 0.

Runs a real browser via BrowserSession, so no manually opened logged-in tab is
required and navigation is confined to the target host by the SecurityWatchdog.

Usage:
  python newtoki_watch.py --watchlist watchlist.txt   # one of OUR titles per line
  python newtoki_watch.py --self-test                 # control queries only
Output (NEWTOKI_OUT, default ~/newtoki_watch):
  sightings.jsonl      - one record per matched title per run (append)
  runs/YYYY-MM-DD.json - per-query result summary + detector status
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import os
import re
import sys
import tempfile
import unicodedata
from pathlib import Path
from urllib.parse import quote

if sys.stdout.encoding and sys.stdout.encoding.lower() != 'utf-8':
	sys.stdout.reconfigure(encoding='utf-8', errors='replace')

from browser_use.browser.events import NavigateToUrlEvent
from browser_use.browser.profile import BrowserProfile
from browser_use.browser.session import BrowserSession

HOST = os.environ.get('NEWTOKI_HOST', 'https://newtoki1.org')
OUT_DIR = Path(os.environ.get('NEWTOKI_OUT', str(Path.home() / 'newtoki_watch')))
SEARCH_URL = HOST + '/webtoon?stx={query}'
ALLOWED = ['newtoki1.org', '*.newtoki1.org']
QUERY_WAIT = 4.0
NAV_TIMEOUT = 45.0
PAUSE_EVERY = 50
PAUSE_SECONDS = 30.0
# A term that must appear on any Korean webtoon index, and one that must not.
CONTROL_QUERY = '사랑'
NONSENSE_QUERY = 'zzzqqxnotitle'

JS_READ_RESULTS = r"""
(() => {
	const seen = new Set();
	const out = [];
	document.querySelectorAll('a[href*="/webtoon/"]').forEach(a => {
		const m = (a.getAttribute('href') || '').match(/\/webtoon\/(\d+)/);
		if (!m) return;
		const text = (a.textContent || '').replace(/\s+/g, ' ').trim();
		if (!text || seen.has(m[1])) return;
		seen.add(m[1]);
		out.push({id: m[1], url: a.href, title: text.slice(0, 120)});
	});
	return JSON.stringify(out);
})()
"""


def normalize(title: str) -> str:
	"""Fold a title for comparison.

	Curly apostrophes are the practical trap: our catalog writes "Dragon's"
	with U+2019 while a pirate index types an ASCII quote, and a raw substring
	test then silently misses a real hit.
	"""
	folded = unicodedata.normalize('NFKC', title).casefold()
	folded = folded.replace('’', "'").replace('‘', "'").replace('“', '"').replace('”', '"')
	return re.sub(r'[^0-9a-z가-힣]+', '', folded)


async def search(session: BrowserSession, query: str) -> list[dict]:
	"""Run one search and read the result anchors.

	Navigation follows the site's `/webtoon/__q/<base64>` redirect on its own;
	the browser does what a visitor's would.
	"""
	await asyncio.wait_for(
		session.event_bus.dispatch(NavigateToUrlEvent(url=SEARCH_URL.format(query=quote(query)), new_tab=False)),
		timeout=NAV_TIMEOUT,
	)
	await asyncio.sleep(QUERY_WAIT)
	cdp_session = await session.get_or_create_cdp_session()
	response = await cdp_session.cdp_client.send.Runtime.evaluate(
		params={'expression': JS_READ_RESULTS, 'returnByValue': True}, session_id=cdp_session.session_id
	)
	raw = response.get('result', {}).get('value')
	return json.loads(raw) if raw else []


async def check_detector(session: BrowserSession) -> dict:
	"""Prove the search path works before trusting any zero."""
	control = await search(session, CONTROL_QUERY)
	await asyncio.sleep(2.0)
	nonsense = await search(session, NONSENSE_QUERY)
	ok = len(control) > 0 and len(nonsense) == 0
	return {
		'detector_status': 'verified' if ok else 'unverified',
		'control_query': CONTROL_QUERY,
		'control_hits': len(control),
		'nonsense_query': NONSENSE_QUERY,
		'nonsense_hits': len(nonsense),
	}


async def main() -> None:
	parser = argparse.ArgumentParser()
	parser.add_argument('--watchlist', help='text file, one of OUR titles per line')
	parser.add_argument('--self-test', action='store_true', help='run the control queries only')
	parser.add_argument('--headful', action='store_true')
	args = parser.parse_args()

	if not args.watchlist and not args.self_test:
		raise SystemExit('--watchlist is required (rights-protection mode queries our titles only)')

	watchlist: list[str] = []
	if args.watchlist:
		watchlist = [line.strip() for line in Path(args.watchlist).read_text(encoding='utf-8').splitlines() if line.strip() and not line.startswith('#')]
		if not watchlist:
			raise SystemExit('watchlist is empty')
		print(f'watchlist: {len(watchlist)} titles (rights-protection mode, our titles only)')

	today = dt.date.today().isoformat()
	now = dt.datetime.now(dt.timezone.utc).isoformat()
	runs_dir = OUT_DIR / 'runs'
	runs_dir.mkdir(parents=True, exist_ok=True)

	per_query: list[dict] = []
	sightings: list[dict] = []
	with tempfile.TemporaryDirectory(prefix='newtoki_') as profile_dir:
		profile = BrowserProfile(
			headless=not args.headful,
			keep_alive=False,
			user_data_dir=Path(profile_dir),
			allowed_domains=ALLOWED,
		)
		session = BrowserSession(browser_profile=profile)
		try:
			await session.start()
			print('[control] verifying the detector')
			detector = await check_detector(session)
			print(f'  control "{detector["control_query"]}" -> {detector["control_hits"]} hits; nonsense -> {detector["nonsense_hits"]} hits')
			print(f'  detector: {detector["detector_status"].upper()}')
			if detector['detector_status'] == 'unverified':
				print('  WARNING: a zero result this run means nothing — treat it as no observation, not as no infringement')

			for index, title in enumerate(watchlist, 1):
				if index > 1 and (index - 1) % PAUSE_EVERY == 0:
					print(f'  ...cooldown {PAUSE_SECONDS:.0f}s after {index - 1} queries')
					await asyncio.sleep(PAUSE_SECONDS)
				try:
					results = await search(session, title)
				except Exception as exc:  # noqa: BLE001 - record the miss, continue
					per_query.append({'query': title, 'error': type(exc).__name__, 'exact': [], 'near': 0})
					print(f'[{index}/{len(watchlist)}] "{title}": FAILED {type(exc).__name__}')
					continue
				needle = normalize(title)
				matches = [r for r in results if needle and (needle in normalize(r['title']) or normalize(r['title']) in needle)]
				per_query.append({'query': title, 'exact': matches, 'near': len(results) - len(matches)})
				for match in matches:
					sightings.append({
						'our_title': title, 'matched_title': match['title'], 'url': match['url'],
						'match': 'exact', 'detector_status': detector['detector_status'], 'observed_at': now,
					})
				print(f'[{index}/{len(watchlist)}] "{title}": exact={len(matches)} other_results={len(results) - len(matches)}')
				await asyncio.sleep(2.0)
		finally:
			await session.kill()

	if sightings:
		with (OUT_DIR / 'sightings.jsonl').open('a', encoding='utf-8') as handle:
			for record in sightings:
				handle.write(json.dumps(record, ensure_ascii=False) + '\n')
	(runs_dir / f'{today}.json').write_text(
		json.dumps({'date': today, 'host': HOST, **detector, 'queries': per_query, 'sightings': len(sightings)}, ensure_ascii=False, indent=2),
		encoding='utf-8',
	)

	print(f'\nDONE -> {OUT_DIR}')
	print(f'  queries: {len(per_query)} | exact matches (potential infringement): {len(sightings)}')
	if detector['detector_status'] == 'verified':
		print('  detector verified — a zero above is a real absence of matches')
	else:
		print('  detector UNVERIFIED — this run makes no claim about infringement')
	if sightings:
		print(f'  evidence log: {OUT_DIR / "sightings.jsonl"}')


if __name__ == '__main__':
	asyncio.run(main())
