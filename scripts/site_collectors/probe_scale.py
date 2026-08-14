"""Verify the searchstillsajax list endpoint: batch size, nextbatch pagination, total count."""

from __future__ import annotations

import asyncio
import os
import re

import aiohttp
from cdp_use import CDPClient

CDP_HTTP = os.environ.get('BROWSER_USE_CDP_HTTP', 'http://127.0.0.1:9222')


async def main() -> None:
	async with aiohttp.ClientSession() as http:
		async with http.get(f'{CDP_HTTP}/json/version') as response:
			ws_url = (await response.json())['webSocketDebuggerUrl']

	async with CDPClient(ws_url) as client:
		targets = await client.send.Target.getTargets()
		page = next(t for t in targets['targetInfos'] if t['type'] == 'page' and 'shotdeck.com/browse' in t.get('url', ''))
		session = await client.send.Target.attachToTarget(params={'targetId': page['targetId'], 'flatten': True})
		sid = session['sessionId']
		await client.send.Runtime.enable(session_id=sid)

		async def fetch_text(path: str) -> str:
			r = await client.send.Runtime.evaluate(
				params={
					'expression': f"fetch('{path}').then(r=>r.text()).catch(e=>'ERR '+e)",
					'returnByValue': True, 'awaitPromise': True,
				},
				session_id=sid,
			)
			return r.get('result', {}).get('value') or ''

		html = await fetch_text('/browse/searchstillsajax')
		cards = re.findall(r'data-shotid="([^"]+)"', html)
		nextbatch = re.findall(r'<a[^>]*class="[^"]*nextbatch[^"]*"[^>]*href="([^"]+)"', html)
		nextbatch2 = re.findall(r'href="([^"]*nextbatch[^"]*)"', html, re.IGNORECASE)
		totals = re.findall(r'([\d,]+)\s*(?:shots|results|stills|images)', html, re.IGNORECASE)
		print(f'response bytes: {len(html)}')
		print(f'cards in batch: {len(cards)}  unique: {len(set(cards))}')
		print(f'first cards: {cards[:5]}')
		print(f'nextbatch (class): {nextbatch[:3]}')
		print(f'nextbatch (href-any): {nextbatch2[:3]}')
		print(f'total-ish numbers: {totals[:5]}')

		# Show the tail where the nextbatch link lives.
		idx = html.lower().find('nextbatch')
		print('\n--- around nextbatch ---')
		print(html[max(0, idx - 200):idx + 300] if idx >= 0 else 'no nextbatch token found')

		# Try the searchpane total endpoint for the grand total.
		total_html = await fetch_text('/browse/searchpaneshottotalajax')
		print('\n--- searchpaneshottotalajax (first 400) ---')
		print(total_html[:400])


if __name__ == '__main__':
	asyncio.run(main())
