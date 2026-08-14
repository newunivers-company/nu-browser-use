"""Probe how per-option shot totals are computed: inspect loadShotTotal + call the total endpoint."""

from __future__ import annotations

import asyncio
import os

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

		async def ev(expr: str):
			r = await client.send.Runtime.evaluate(
				params={'expression': expr, 'returnByValue': True, 'awaitPromise': True}, session_id=sid
			)
			if 'exceptionDetails' in r:
				return {'__error__': r['exceptionDetails'].get('text')}
			return r.get('result', {}).get('value')

		print('--- loadShotTotal source ---')
		print(await ev('typeof loadShotTotal==="function"?loadShotTotal.toString():"n/a"'))
		print('\n--- loadShotTotals source ---')
		print((await ev('typeof loadShotTotals==="function"?loadShotTotals.toString():"n/a"'))[:1500])

		# Try the totals endpoint a couple of plausible ways.
		for path in (
			'/browse/searchpaneshottotalajax/meta/genre/value/Movie%2FTV+-+Drama',
			'/browse/searchpaneshottotalajax/metatype/genre/value/Movie%2FTV+-+Drama',
			'/browse/searchstillsajax/genre/Movie%2FTV+-+Drama/limit/1/offset/0',
		):
			txt = await ev(f"fetch('{path}').then(r=>r.text()).then(t=>t.slice(0,300)).catch(e=>'ERR '+e)")
			print(f'\n--- {path} ---')
			print(txt)


if __name__ == '__main__':
	asyncio.run(main())
