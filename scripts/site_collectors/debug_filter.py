"""Debug why the filtered+limit search path returns no cards."""

from __future__ import annotations

import asyncio
import os
import re
from urllib.parse import quote

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

		async def fetch(path: str) -> str:
			r = await client.send.Runtime.evaluate(
				params={'expression': f"fetch({path!r}).then(r=>r.text()).catch(e=>'ERR '+e)", 'returnByValue': True, 'awaitPromise': True},
				session_id=sid,
			)
			return r.get('result', {}).get('value') or ''

		paths = [
			'/browse/searchstillsajax/media_type/Movie',
			'/browse/searchstillsajax/media_type/Movie/limit/36/offset/0',
			'/browse/searchstillsajax/limit/36/offset/0/media_type/Movie',
			f'/browse/searchstillsajax/media_type/{quote("Movie", safe="")}/limit/36/offset/0',
		]
		for p in paths:
			html = await fetch(p)
			cards = len(re.findall(r"data-shotid='", html))
			descrip = re.search(r"results_descrip'\)\.text\(\s*'([^']+)'", html)
			print(f'cards={cards:3}  len={len(html):6}  {p}')
			print(f'          descrip: {descrip.group(1) if descrip else "n/a"}')
			if cards == 0 and len(html) < 4000:
				print('          BODY:', html[:600].replace(chr(10), ' '))


if __name__ == '__main__':
	asyncio.run(main())
