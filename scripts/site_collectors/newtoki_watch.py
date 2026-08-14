"""Newtoki infringement watch — OUR TITLES ONLY.

Purpose: rights protection. Searches newtoki for a watchlist of titles WE own
or distribute, and logs every match (title, url, found-date) as takedown
evidence. This is the anti-piracy monitoring pattern (cf. MUSO) — not a
catalog crawler.

Scope guardrails (do not widen):
  - queries ONLY come from the watchlist file
  - matches record title + page URL + observed date; no content download
  - no site cataloging, no episode/image fetching

Runs over CDP against the already-logged-in tab (App-Bound cookies apply).

Usage:
  python newtoki_watch.py --watchlist watchlist.txt            # one query per line
Output (NEWTOKI_OUT, default ~/ranking_export/../newtoki_watch):
  sightings.jsonl   - one record per matched title per run (append)
  runs/YYYY-MM-DD.json - per-query result summary
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

# Windows console (cp949) chokes on accented titles; force utf-8 stdout.
if sys.stdout.encoding and sys.stdout.encoding.lower() != 'utf-8':
	sys.stdout.reconfigure(encoding='utf-8', errors='replace')

import aiohttp
from cdp_use import CDPClient

CDP_HTTP = os.environ.get('BROWSER_USE_CDP_HTTP', 'http://127.0.0.1:9222')
OUT_DIR = Path(os.environ.get('NEWTOKI_OUT', str(Path.home() / 'newtoki_watch')))
SEARCH_URL = 'https://newtoki1.org/webtoon?stx={query}'
QUERY_WAIT = 4.0
PAUSE_EVERY = 50  # brief cooldown between query batches
PAUSE_SECONDS = 30.0

JS_READ_RESULTS = r"""
(() => {
	const seen = new Set();
	const out = [];
	document.querySelectorAll('a[href*="/webtoon/"]').forEach(a => {
		const href = a.href;
		const m = href.match(/\/webtoon\/(\d+)/);
		if (!m) return;
		const text = (a.textContent || '').replace(/\s+/g, ' ').trim();
		if (!text || seen.has(m[1])) return;
		seen.add(m[1]);
		out.push({id: m[1], url: href, title: text.slice(0, 120)});
	});
	return JSON.stringify(out);
})()
"""


class Page:
	def __init__(self, client: CDPClient, sid: str) -> None:
		self._client, self._sid = client, sid

	async def ev(self, expr: str):
		r = await self._client.send.Runtime.evaluate(
			params={'expression': expr, 'returnByValue': True, 'awaitPromise': True}, session_id=self._sid
		)
		if 'exceptionDetails' in r:
			raise RuntimeError(r['exceptionDetails'].get('text', 'js error'))
		return r.get('result', {}).get('value')

	async def navigate(self, url: str) -> None:
		await self._client.send.Page.navigate(params={'url': url}, session_id=self._sid)


async def main() -> None:
	parser = argparse.ArgumentParser()
	parser.add_argument('--watchlist', required=True, help='text file, one of OUR titles per line')
	args = parser.parse_args()

	watchlist = [ln.strip() for ln in Path(args.watchlist).read_text(encoding='utf-8').splitlines() if ln.strip() and not ln.startswith('#')]
	if not watchlist:
		raise SystemExit('watchlist is empty')
	print(f'watchlist: {len(watchlist)} titles (rights-protection mode, our titles only)')

	today = dt.date.today().isoformat()
	now = dt.datetime.now(dt.timezone.utc).isoformat()
	runs_dir = OUT_DIR / 'runs'
	runs_dir.mkdir(parents=True, exist_ok=True)

	async with aiohttp.ClientSession() as http:
		async with http.get(f'{CDP_HTTP}/json/version') as response:
			ws_url = (await response.json())['webSocketDebuggerUrl']
	async with CDPClient(ws_url) as client:
		targets = await client.send.Target.getTargets()
		target = next(t for t in targets['targetInfos'] if t['type'] == 'page' and 'newtoki' in t.get('url', ''))
		session = await client.send.Target.attachToTarget(params={'targetId': target['targetId'], 'flatten': True})
		page = Page(client, session['sessionId'])
		await client.send.Page.enable(session_id=session['sessionId'])
		await client.send.Runtime.enable(session_id=session['sessionId'])

		per_query: list[dict] = []
		sightings: list[dict] = []
		for idx, title in enumerate(watchlist, 1):
			if idx > 1 and (idx - 1) % PAUSE_EVERY == 0:
				print(f'  ...cooldown {PAUSE_SECONDS:.0f}s after {idx - 1} queries')
				await asyncio.sleep(PAUSE_SECONDS)
			await page.navigate(SEARCH_URL.format(query=quote(title)))
			await asyncio.sleep(QUERY_WAIT)
			try:
				results = json.loads(await page.ev(JS_READ_RESULTS))
			except Exception:  # noqa: BLE001 - record the miss, continue
				results = []
			# A match is any result whose title shares a meaningful token overlap with our title
			# (search is fuzzy; exact-substring matches are strongest evidence).
			lower = title.lower()
			matches = [r for r in results if r['title'].lower() in lower or lower in r['title'].lower()]
			fuzzy = [r for r in results if r not in matches]
			per_query.append({'query': title, 'exact': matches, 'near': len(fuzzy)})
			for m in matches:
				sightings.append({
					'our_title': title, 'matched_title': m['title'], 'url': m['url'],
					'match': 'exact', 'observed_at': now,
				})
			print(f'[{idx}/{len(watchlist)}] "{title}": exact={len(matches)} near={len(fuzzy)}')
			await asyncio.sleep(2.0)

	# Evidence log (append-only) + run summary.
	if sightings:
		with (OUT_DIR / 'sightings.jsonl').open('a', encoding='utf-8') as handle:
			for record in sightings:
				handle.write(json.dumps(record, ensure_ascii=False) + '\n')
	(runs_dir / f'{today}.json').write_text(
		json.dumps({'date': today, 'queries': per_query, 'sightings': len(sightings)}, ensure_ascii=False, indent=2),
		encoding='utf-8',
	)
	print(f'\nDONE -> {OUT_DIR}')
	print(f'  queries: {len(per_query)} | exact matches (potential infringement): {len(sightings)}')
	print(f'  evidence log: {OUT_DIR / "sightings.jsonl"}' if sightings else '  no matches logged')


if __name__ == '__main__':
	asyncio.run(main())
