"""Debug Pinterest search scroll: does the window scroll, is the grid virtualized, does it lazy-load?"""

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
		page = next(t for t in targets['targetInfos'] if t['type'] == 'page' and 'pinterest.com' in t.get('url', '') and 'recaptcha' not in t.get('url', ''))
		session = await client.send.Target.attachToTarget(params={'targetId': page['targetId'], 'flatten': True})
		sid = session['sessionId']
		await client.send.Page.enable(session_id=sid)
		await client.send.Runtime.enable(session_id=sid)

		async def ev(expr: str):
			r = await client.send.Runtime.evaluate(
				params={'expression': expr, 'returnByValue': True, 'awaitPromise': True}, session_id=sid
			)
			if 'exceptionDetails' in r:
				return {'__error__': r['exceptionDetails'].get('text')}
			return r.get('result', {}).get('value')

		await client.send.Page.navigate(params={'url': 'https://kr.pinterest.com/search/pins/?q=cinematic%20lighting'}, session_id=sid)
		await asyncio.sleep(5)

		metrics0 = await ev(
			"JSON.stringify({scrollY: window.scrollY, innerH: window.innerHeight, bodyH: document.body.scrollHeight, "
			"docH: document.documentElement.scrollHeight, pins: document.querySelectorAll('[data-test-id=pin]').length, "
			"pinLinks: document.querySelectorAll('a[href*=\"/pin/\"]').length})"
		)
		print('before scroll:', metrics0)

		# Try several scroll strategies and see which moves the page / loads pins.
		for i in range(6):
			await ev("window.scrollBy(0, 1200); window.dispatchEvent(new Event('scroll'));")
			await asyncio.sleep(2.5)
			m = await ev(
				"JSON.stringify({scrollY: Math.round(window.scrollY), bodyH: document.body.scrollHeight, "
				"pins: document.querySelectorAll('[data-test-id=pin]').length, "
				"pinLinks: document.querySelectorAll('a[href*=\"/pin/\"]').length})"
			)
			print(f'scroll {i+1}:', m)

		# Look for a scroll container that isn't the window.
		containers = await ev(
			r"""
			(() => {
				const els = [...document.querySelectorAll('*')].filter(e => {
					const s = getComputedStyle(e);
					return (s.overflowY === 'auto' || s.overflowY === 'scroll') && e.scrollHeight > e.clientHeight + 50;
				}).slice(0, 8).map(e => ({tag:e.tagName, cls:(e.className||'').toString().slice(0,40), sh:e.scrollHeight, ch:e.clientHeight}));
				return JSON.stringify(els);
			})()
			"""
		)
		print('scroll containers:', containers)


if __name__ == '__main__':
	asyncio.run(main())
