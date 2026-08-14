"""Confirm pins are readable from the DOM on search + profile pages, and list the user's boards."""

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
			if 'exceptionDetails' in r:
				return {'__error__': r['exceptionDetails'].get('text')}
			return r.get('result', {}).get('value')

		async def nav(url: str, wait: float = 7) -> None:
			await client.send.Page.navigate(params={'url': url}, session_id=sid)
			await asyncio.sleep(wait)

		# Search page: read pin cards from the DOM.
		await nav('https://kr.pinterest.com/search/pins/?q=cinematic%20lighting')
		search = await ev(
			r"""
			(() => {
				const pins = [...document.querySelectorAll('div[data-test-id="pin"], [data-test-pin-id]')];
				const sample = pins.slice(0, 3).map(el => {
					const a = el.querySelector('a[href*="/pin/"]');
					const img = el.querySelector('img');
					return {
						pinid: (el.getAttribute('data-test-pin-id')) || (a ? (a.getAttribute('href')||'').match(/\/pin\/(\d+)/)?.[1] : ''),
						href: a ? a.getAttribute('href') : '',
						img: img ? img.getAttribute('src') : '',
						alt: img ? (img.getAttribute('alt')||'').slice(0,60) : '',
					};
				});
				return JSON.stringify({ url: location.href, pinCount: pins.length, sample,
					anyPinLinks: document.querySelectorAll('a[href*="/pin/"]').length });
			})()
			"""
		)
		print('=== search DOM ===')
		print(search)

		# Profile/boards page: list boards.
		await nav('https://kr.pinterest.com/taeoh2026/')
		boards = await ev(
			r"""
			(() => {
				const boardLinks = [...document.querySelectorAll('a[href^="/taeoh2026/"]')]
					.map(a => ({ href: a.getAttribute('href'), text: (a.textContent||'').trim().slice(0,50) }))
					.filter(b => b.href && b.href !== '/taeoh2026/' && !b.href.includes('/_'));
				const uniq = [...new Map(boardLinks.map(b=>[b.href,b])).values()];
				return JSON.stringify({ url: location.href, boardCount: uniq.length, boards: uniq.slice(0,25) });
			})()
			"""
		)
		print('\n=== profile boards DOM ===')
		print(boards)


if __name__ == '__main__':
	asyncio.run(main())
