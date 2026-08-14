"""Test whether the logged-in Civitai browser session returns generation meta, vs anonymous."""

from __future__ import annotations

import asyncio
import json
import os

import aiohttp
from cdp_use import CDPClient

CDP_HTTP = os.environ.get('BROWSER_USE_CDP_HTTP', 'http://127.0.0.1:9222')
BASE = 'https://civitai.com/api/v1'
HEADERS = {'User-Agent': 'Mozilla/5.0 reference-collector', 'Accept': 'application/json'}


async def anon_test(sort: str) -> None:
	async with aiohttp.ClientSession(headers=HEADERS) as s:
		async with s.get(f'{BASE}/images', params={'limit': '40', 'nsfw': 'None', 'sort': sort}) as r:
			data = await r.json()
	items = data.get('items', [])
	hm = sum(1 for im in items if im.get('meta'))
	print(f'ANON sort={sort}: {len(items)} images, {hm} with meta')


async def cdp_test(sort: str) -> None:
	async with aiohttp.ClientSession() as http:
		async with http.get(f'{CDP_HTTP}/json/version') as response:
			ws_url = (await response.json())['webSocketDebuggerUrl']
	async with CDPClient(ws_url) as client:
		targets = await client.send.Target.getTargets()
		page = next((t for t in targets['targetInfos'] if t['type'] == 'page' and 'civitai.com' in t.get('url', '') and 'red' not in t.get('url', '')), None)
		if page is None:
			print('no civitai.com page open')
			return
		session = await client.send.Target.attachToTarget(params={'targetId': page['targetId'], 'flatten': True})
		sid = session['sessionId']
		await client.send.Runtime.enable(session_id=sid)
		r = await client.send.Runtime.evaluate(
			params={
				'expression': f"fetch('/api/v1/images?limit=40&nsfw=None&sort={sort}').then(r=>r.text()).catch(e=>'ERR '+e)",
				'returnByValue': True, 'awaitPromise': True,
			},
			session_id=sid,
		)
		text = r.get('result', {}).get('value') or ''
		try:
			data = json.loads(text)
			items = data.get('items', [])
			hm = sum(1 for im in items if im.get('meta'))
			print(f'CDP  sort={sort}: {len(items)} images, {hm} with meta')
			rich = max(items, key=lambda im: len(im.get('meta') or {}), default={})
			meta = rich.get('meta') or {}
			if meta:
				print(f'   sample meta fields ({len(meta)}):', list(meta.keys())[:20])
				print('   prompt:', str(meta.get('prompt'))[:120])
		except Exception as e:  # noqa: BLE001
			print('CDP parse error:', text[:120])


async def main() -> None:
	for sort in ('Newest', 'Most Reactions'):
		await anon_test(sort)
		await cdp_test(sort)


if __name__ == '__main__':
	asyncio.run(main())
