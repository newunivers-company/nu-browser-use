"""Check current ShotDeck login state over CDP."""

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
		pages = [t for t in targets['targetInfos'] if t['type'] == 'page' and 'shotdeck' in t.get('url', '')]
		for p in pages:
			print('PAGE:', p.get('url'))
		if not pages:
			print('no shotdeck page open')
			return
		session = await client.send.Target.attachToTarget(params={'targetId': pages[0]['targetId'], 'flatten': True})
		sid = session['sessionId']
		await client.send.Runtime.enable(session_id=sid)

		async def ev(expr: str):
			r = await client.send.Runtime.evaluate(
				params={'expression': expr, 'returnByValue': True, 'awaitPromise': True}, session_id=sid
			)
			return r.get('result', {}).get('value')

		browse = await ev("fetch('/browse/searchstillsajax').then(r=>r.text()).then(t=>t.slice(0,90)).catch(e=>'ERR '+e)")
		print('browse endpoint:', repr(browse))
		acct = await ev("fetch('/account').then(r=>r.text()).then(t=> t.includes('not logged in') ? 'NOT_LOGGED_IN' : (t.match(/name=\"email\"[^>]*value=\"([^\"]*)\"/)||[])[1] || 'logged-in-no-email').catch(e=>'ERR '+e)")
		print('account probe:', repr(acct))
		loc = await ev('location.href')
		print('current location:', loc)


if __name__ == '__main__':
	asyncio.run(main())
