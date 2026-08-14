"""Check whether the Pinterest home feed scrolls/loads (to tell a global bot-block from a search cap)."""

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
		page = next(t for t in targets['targetInfos'] if t['type'] == 'page' and 'pinterest.com' in t.get('url', '') and 'recaptcha' not in t.get('url', ''))
		session = await client.send.Target.attachToTarget(params={'targetId': page['targetId'], 'flatten': True})
		sid = session['sessionId']
		await client.send.Page.enable(session_id=sid)
		await client.send.Runtime.enable(session_id=sid)

		async def ev(expr: str):
			r = await client.send.Runtime.evaluate(
				params={'expression': expr, 'returnByValue': True, 'awaitPromise': True}, session_id=sid
			)
			return r.get('result', {}).get('value')

		for label, url in (('HOME', 'https://kr.pinterest.com/'), ('PIN DETAIL', 'https://kr.pinterest.com/pin/55802482886758955/')):
			await client.send.Page.navigate(params={'url': url}, session_id=sid)
			await asyncio.sleep(6)
			m0 = await ev("JSON.stringify({bodyH: document.body.scrollHeight, innerH: window.innerHeight, pins: document.querySelectorAll('a[href*=\"/pin/\"]').length})")
			await ev("window.scrollBy(0, 2000);")
			await asyncio.sleep(3)
			m1 = await ev("JSON.stringify({scrollY: Math.round(window.scrollY), bodyH: document.body.scrollHeight, pins: document.querySelectorAll('a[href*=\"/pin/\"]').length})")
			# Detect captcha / challenge / login redirect
			state = await ev("JSON.stringify({url: location.href, hasCaptcha: !!document.querySelector('iframe[src*=captcha],iframe[src*=recaptcha]'), bodyTextHint: (document.body.innerText||'').slice(0,120).replace(/\\s+/g,' ')})")
			print(f'--- {label} ---')
			print('  before:', m0)
			print('  after scroll:', m1)
			print('  state:', state)


if __name__ == '__main__':
	asyncio.run(main())
