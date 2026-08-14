"""Read updateStills / nextbatch* sources to find the list endpoint, then call it directly."""

from __future__ import annotations

import asyncio
import json
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

		async def ev(expression: str):
			r = await client.send.Runtime.evaluate(
				params={'expression': expression, 'returnByValue': True, 'awaitPromise': True}, session_id=sid
			)
			if 'exceptionDetails' in r:
				return {'__error__': r['exceptionDetails'].get('text', 'js error')}
			return r.get('result', {}).get('value')

		for fn in ('updateStills', 'nextbatchClick', 'nextbatchCheck'):
			print(f'\n===== {fn} =====')
			print(await ev(f'typeof {fn}==="function" ? {fn}.toString() : "n/a"'))

		# Current search state vars.
		print('\n===== state vars =====')
		for v in ('search_opts', 'current_batch', 'batch', 'total_shots', 'shots_per_batch', 'batchsize'):
			print(f'{v} = {json.dumps(await ev(f"typeof {v} !== \"undefined\" ? {v} : \"undef\""))}')


if __name__ == '__main__':
	asyncio.run(main())
