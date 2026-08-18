"""Reconnaissance of the authenticated Pinterest session over CDP: login state, page structure,
and the XHR/resource endpoints the SPA calls."""

from __future__ import annotations

import asyncio
import os

import aiohttp
from cdp_use import CDPClient
from cdp_use.cdp.network import RequestWillBeSentEvent

CDP_HTTP = os.environ.get('BROWSER_USE_CDP_HTTP', 'http://127.0.0.1:9222')


async def main() -> None:
	async with aiohttp.ClientSession() as http:
		async with http.get(f'{CDP_HTTP}/json/version') as response:
			ws_url = (await response.json())['webSocketDebuggerUrl']

	async with CDPClient(ws_url) as client:
		targets = await client.send.Target.getTargets()
		page = next(
			t
			for t in targets['targetInfos']
			if t['type'] == 'page' and 'pinterest.com' in t.get('url', '') and 'recaptcha' not in t.get('url', '')
		)
		session = await client.send.Target.attachToTarget(params={'targetId': page['targetId'], 'flatten': True})
		sid = session['sessionId']
		await client.send.Runtime.enable(session_id=sid)
		await client.send.Network.enable(session_id=sid)

		xhr: list[dict] = []

		def on_request(event: RequestWillBeSentEvent, session_id: str | None) -> None:
			req = event.get('request') or {}
			url = req.get('url', '')
			if event.get('type') in ('XHR', 'Fetch') and 'pinterest.com' in url:
				xhr.append({'method': req.get('method'), 'url': url})

		client.register.Network.requestWillBeSent(on_request)

		async def ev(expr: str):
			r = await client.send.Runtime.evaluate(
				params={'expression': expr, 'returnByValue': True, 'awaitPromise': True}, session_id=sid
			)
			if 'exceptionDetails' in r:
				return {'__error__': r['exceptionDetails'].get('text')}
			return r.get('result', {}).get('value')

		# Login state + basic identity.
		identity = await ev(
			r"""
			(() => {
				const cookies = document.cookie;
				const loggedIn = /_pinterest_sess/.test(cookies) && !/\/login/.test(location.pathname);
				// Pinterest embeds initial state JSON in scripts.
				let user = '';
				const relay = document.querySelector('script#__PWS_DATA__, script#initial-state, script[data-relay-response]');
				const meta = document.querySelector('meta[name="pinterest-user"]');
				const links = [...document.querySelectorAll('a[href^="/"]')].map(a=>a.getAttribute('href'))
					.filter(h => /\/[^/]+\/$/.test(h) || h.includes('board') || h.includes('pin'));
				return JSON.stringify({
					url: location.href,
					loggedIn,
					hasSessCookie: /_pinterest_sess/.test(cookies),
					title: document.title,
					sampleLinks: [...new Set(links)].slice(0, 20),
					hasPwsData: !!document.querySelector('script#__PWS_DATA__'),
				});
			})()
			"""
		)
		print('=== identity ===')
		print(identity)

		# Reload home feed to observe resource endpoints.
		await client.send.Page.enable(session_id=sid)
		await client.send.Page.navigate(params={'url': 'https://kr.pinterest.com/'}, session_id=sid)
		await asyncio.sleep(8)

		print('\n=== observed pinterest XHR/resource endpoints ===')
		seen = set()
		for e in xhr:
			base = e['url'].split('?')[0]
			if base in seen:
				continue
			seen.add(base)
			print(f'{e["method"]:5} {e["url"][:170]}')


if __name__ == '__main__':
	asyncio.run(main())
