"""Confirm the POST totals endpoint returns a count, and the filtered response's total."""

from __future__ import annotations

import asyncio
import os
import re

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
			if 'exceptionDetails' in r:
				return {'__error__': r['exceptionDetails'].get('text')}
			return r.get('result', {}).get('value')

		# POST totals for a few options.
		for meta, val in (('media_type', 'Movie'), ('genre', 'Movie/TV - Drama'), ('shot_type', 'Aerial'), ('lighting', 'Soft light')):
			out = await ev(
				f"""
				fetch('/browse/searchpaneshottotalajax', {{
					method:'POST',
					headers:{{'Content-Type':'application/x-www-form-urlencoded','X-Requested-With':'XMLHttpRequest'}},
					body: new URLSearchParams({{metaname:{meta!r}, metaval:{val!r}}}).toString()
				}}).then(r=>r.text()).then(t=>t.trim().slice(0,80)).catch(e=>'ERR '+e)
				"""
			)
			print(f'{meta}={val!r}: total="{out}"')

		# Filtered list total from results_descrip.
		html = await ev(
			"fetch('/browse/searchstillsajax/media_type/Movie').then(r=>r.text()).catch(e=>'ERR '+e)"
		)
		descrip = re.search(r"results_descrip'\)\.text\(\s*'([^']+)'", html or '')
		cards = len(re.findall(r"class='outerimage", html or ''))
		print(f'\nfiltered media_type/Movie -> cards in batch: {cards}, descrip: {descrip.group(1) if descrip else "n/a"}')


if __name__ == '__main__':
	asyncio.run(main())
