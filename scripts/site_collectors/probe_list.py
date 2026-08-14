"""Discover the browse-list / pagination endpoint by scrolling the gallery and capturing XHR,
and read the account page + decks list for account/subscription/deck info."""

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
		await client.send.Network.enable(session_id=sid)

		xhr: list[dict] = []

		def on_request(client, message: dict) -> None:
			p = message.get('params', {})
			req = p.get('request', {})
			if p.get('type') in ('XHR', 'Fetch') and 'shotdeck.com' in req.get('url', ''):
				xhr.append({'method': req.get('method'), 'url': req.get('url')})

		client.register.Network.requestWillBeSent(on_request)

		async def ev(expression: str):
			r = await client.send.Runtime.evaluate(
				params={'expression': expression, 'returnByValue': True, 'awaitPromise': True}, session_id=sid
			)
			if 'exceptionDetails' in r:
				return {'__error__': r['exceptionDetails'].get('text', 'js error')}
			return r.get('result', {}).get('value')

		# Scroll the gallery to force the next batch to load.
		for _ in range(6):
			await ev(
				"(() => { window.scrollTo(0, document.body.scrollHeight); "
				"const s = document.getElementById('stills'); if (s) s.scrollTop = s.scrollHeight; return true; })()"
			)
			await asyncio.sleep(1.5)

		print('=== XHR during scroll ===')
		seen = set()
		for e in xhr:
			base = e['url'].split('?')[0]
			if base in seen:
				continue
			seen.add(base)
			print(f"{e['method']:5} {e['url'][:180]}")

		count_now = await ev("document.querySelectorAll('.outerimage[data-shotid]').length")
		print(f'\ncards after scroll: {count_now}')

		# Look for the pagination function / infinite scroll handler in globals.
		fns = await ev(
			"""
			Object.keys(window).filter(k => /still|scroll|paginate|loadmore|nextpage|batch|getshot|browse/i.test(k))
			"""
		)
		print(f'\npagination-ish globals: {json.dumps(fns)}')

		# Fetch decks page and account page HTML (authenticated).
		for path in ('/browse/decks', '/account'):
			html = await ev(f"fetch('{path}').then(r=>r.text()).then(t=>t.length + ' bytes').catch(e=>'ERR '+e)")
			print(f'{path}: {html}')


if __name__ == '__main__':
	asyncio.run(main())
