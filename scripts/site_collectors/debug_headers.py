"""Test which headers make /browse/searchstillsajax accept the request again."""

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
		page = next(t for t in targets['targetInfos'] if t['type'] == 'page' and 'shotdeck.com/browse' in t.get('url', ''))
		session = await client.send.Target.attachToTarget(params={'targetId': page['targetId'], 'flatten': True})
		sid = session['sessionId']
		await client.send.Runtime.enable(session_id=sid)

		async def ev(expr: str):
			r = await client.send.Runtime.evaluate(
				params={'expression': expr, 'returnByValue': True, 'awaitPromise': True}, session_id=sid
			)
			return r.get('result', {}).get('value')

		tests = {
			'plain': "fetch('/browse/searchstillsajax')",
			'xrw': "fetch('/browse/searchstillsajax',{headers:{'X-Requested-With':'XMLHttpRequest'}})",
			'credentials': "fetch('/browse/searchstillsajax',{credentials:'include'})",
			'xrw+cred': "fetch('/browse/searchstillsajax',{credentials:'include',headers:{'X-Requested-With':'XMLHttpRequest'}})",
			'jquery': "$.get('/browse/searchstillsajax')",  # use the site's own jQuery ajax
		}
		for name, call in tests.items():
			expr = (
				f"Promise.resolve({call}).then(r => (r && r.text) ? r.text() : r)"
				".then(t => (t||'').slice(0,70)).catch(e => 'ERR '+e)"
			)
			out = await ev(expr)
			print(f'{name:14}: {out!r}')


if __name__ == '__main__':
	asyncio.run(main())
