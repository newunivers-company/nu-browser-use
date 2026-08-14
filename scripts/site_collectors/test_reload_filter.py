"""Verify that a full reload of a hash-filter URL applies the filter and loads >1 batch."""

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

		# Navigate with hash, then force a full reload so the app re-inits with the filter.
		await client.send.Page.navigate(params={'url': 'https://shotdeck.com/browse/stills#/media_type/TV'}, session_id=sid)
		await asyncio.sleep(2)
		await client.send.Page.reload(session_id=sid)
		await asyncio.sleep(7)

		info = await ev(
			"JSON.stringify({url: location.href, cards: document.querySelectorAll('.outerimage[data-shotid]').length, "
			"descrip: (document.querySelector('.results_descrip')||{}).textContent||'', "
			"nextbatch: document.querySelectorAll('a.nextbatch').length})"
		)
		print('after reload:', info)

		# Scroll a few times to trigger nextbatch.
		for i in range(4):
			await ev(
				"(() => { window.scrollTo(0, document.body.scrollHeight); "
				"const s=document.getElementById('stills'); if(s) s.scrollTop=s.scrollHeight; "
				"if (typeof nextbatchCheck==='function') nextbatchCheck(); return true; })()"
			)
			await asyncio.sleep(2)
			c = await ev("document.querySelectorAll('.outerimage[data-shotid]').length")
			print(f'  scroll {i+1}: cards={c}')


if __name__ == '__main__':
	asyncio.run(main())
