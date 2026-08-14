"""Confirm ajax offset pagination works while logged in (one option -> 108 cards), paced slowly."""

from __future__ import annotations

import asyncio
import json
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
		page = next(t for t in targets['targetInfos'] if t['type'] == 'page' and 'shotdeck.com' in t.get('url', ''))
		session = await client.send.Target.attachToTarget(params={'targetId': page['targetId'], 'flatten': True})
		sid = session['sessionId']
		await client.send.Runtime.enable(session_id=sid)

		async def fetch(path: str) -> str:
			r = await client.send.Runtime.evaluate(
				params={'expression': f"fetch({json.dumps(path)}).then(r=>r.text()).catch(e=>'ERR '+e)", 'returnByValue': True, 'awaitPromise': True},
				session_id=sid,
			)
			return r.get('result', {}).get('value') or ''

		value = quote('TV', safe='')
		seen = set()
		for offset in (0, 36, 72):
			html = await fetch(f'/browse/searchstillsajax/media_type/{value}/limit/36/offset/{offset}')
			ids = re.findall(r"data-shotid='([^']+)'", html)
			uniq = set(ids)
			seen |= uniq
			blocked = 'not logged in' in html
			print(f'offset {offset}: {len(uniq)} unique ids in batch, blocked={blocked}, total unique so far={len(seen)}')
			await asyncio.sleep(1.5)
		print('final unique:', len(seen))


if __name__ == '__main__':
	asyncio.run(main())
