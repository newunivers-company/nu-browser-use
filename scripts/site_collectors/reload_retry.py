"""Full-reload the browse page to re-establish the search session, then retry the AJAX endpoint."""

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
		page = next(t for t in targets['targetInfos'] if t['type'] == 'page' and 'shotdeck.com' in t.get('url', ''))
		session = await client.send.Target.attachToTarget(params={'targetId': page['targetId'], 'flatten': True})
		sid = session['sessionId']
		await client.send.Page.enable(session_id=sid)
		await client.send.Runtime.enable(session_id=sid)

		async def ev(expr: str):
			r = await client.send.Runtime.evaluate(
				params={'expression': expr, 'returnByValue': True, 'awaitPromise': True}, session_id=sid
			)
			return r.get('result', {}).get('value')

		print('navigating to a clean browse URL...')
		await client.send.Page.navigate(params={'url': 'https://shotdeck.com/browse/stills'}, session_id=sid)
		await asyncio.sleep(8)

		# How many cards did the server render into the page directly?
		dom_cards = await ev("document.querySelectorAll('.outerimage[data-shotid]').length")
		print('server-rendered cards in DOM:', dom_cards)

		ajax = await ev("fetch('/browse/searchstillsajax').then(r=>r.text()).then(t=>t.slice(0,70)).catch(e=>'ERR '+e)")
		print('ajax after reload:', repr(ajax))

		filt = await ev("fetch('/browse/searchstillsajax/media_type/Movie').then(r=>r.text()).then(t=>t.slice(0,70)).catch(e=>'ERR '+e)")
		print('filtered after reload:', repr(filt))


if __name__ == '__main__':
	asyncio.run(main())
