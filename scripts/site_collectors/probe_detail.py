"""Fetch a shot-detail AJAX response and the full getSearches source from the authenticated
page context, to learn the metadata schema and the list/pagination endpoint."""

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

		# Full getSearches source to find the real results endpoint + pagination.
		src = await ev('getSearches.toString()')
		print('=== getSearches source ===')
		print(src if isinstance(src, str) else json.dumps(src))
		print('\n\n=== showMoreShots source ===')
		print(await ev('showMoreShots.toString()'))

		# Fetch one shot's detail HTML/JSON via the authenticated session.
		shotid = await ev("document.querySelector('.outerimage[data-shotid]')?.getAttribute('data-shotid')")
		print(f'\n\n=== sample shotid: {shotid} ===')
		detail = await ev(
			f"""
			fetch('/browse/shotdetailsajax/image/{shotid}', {{headers: {{'X-Requested-With':'XMLHttpRequest'}}}})
				.then(r => r.text()).then(t => t.slice(0, 8000)).catch(e => 'ERR: ' + e)
			"""
		)
		print(detail if isinstance(detail, str) else json.dumps(detail))


if __name__ == '__main__':
	asyncio.run(main())
