"""Find the Civitai tRPC endpoint that returns image generation data (prompt/resources),
by observing network calls when opening an image page, and probing candidate procedures."""

from __future__ import annotations

import asyncio
import json
import os
from urllib.parse import quote

import aiohttp
from cdp_use import CDPClient

CDP_HTTP = os.environ.get('BROWSER_USE_CDP_HTTP', 'http://127.0.0.1:9222')
BASE = 'https://civitai.com/api/v1'
HEADERS = {'User-Agent': 'Mozilla/5.0 reference-collector', 'Accept': 'application/json'}


async def main() -> None:
	# Grab a SFW image id.
	async with aiohttp.ClientSession(headers=HEADERS) as s:
		async with s.get(f'{BASE}/images', params={'limit': '5', 'nsfw': 'None', 'sort': 'Newest'}) as r:
			img_id = (await r.json())['items'][0]['id']
	print('test image id:', img_id)

	async with aiohttp.ClientSession() as http:
		async with http.get(f'{CDP_HTTP}/json/version') as response:
			ws_url = (await response.json())['webSocketDebuggerUrl']
	async with CDPClient(ws_url) as client:
		targets = await client.send.Target.getTargets()
		page = next(t for t in targets['targetInfos'] if t['type'] == 'page' and 'civitai.com' in t.get('url', '') and 'red' not in t.get('url', ''))
		session = await client.send.Target.attachToTarget(params={'targetId': page['targetId'], 'flatten': True})
		sid = session['sessionId']
		await client.send.Page.enable(session_id=sid)
		await client.send.Runtime.enable(session_id=sid)
		await client.send.Network.enable(session_id=sid)

		trpc_calls: list[str] = []

		def on_req(client, message: dict) -> None:
			url = message.get('params', {}).get('request', {}).get('url', '')
			if '/api/trpc/' in url and ('image' in url.lower() or 'generation' in url.lower()):
				trpc_calls.append(url)

		client.register.Network.requestWillBeSent(on_req)

		# Open the image page so the app fetches its generation data.
		await client.send.Page.navigate(params={'url': f'https://civitai.com/images/{img_id}'}, session_id=sid)
		await asyncio.sleep(8)

		print('\n=== observed image/generation tRPC calls ===')
		seen = set()
		for u in trpc_calls:
			proc = u.split('/api/trpc/')[1].split('?')[0]
			if proc in seen:
				continue
			seen.add(proc)
			print(' ', proc)

		# Directly probe the standard generation-data procedure via the authenticated session.
		async def ev(expr: str):
			r = await client.send.Runtime.evaluate(
				params={'expression': expr, 'returnByValue': True, 'awaitPromise': True}, session_id=sid
			)
			return r.get('result', {}).get('value')

		for proc in ('image.getGenerationData', 'image.getResources'):
			inp = quote(json.dumps({'json': {'id': img_id}}))
			out = await ev(f"fetch('/api/trpc/{proc}?input={inp}').then(r=>r.text()).then(t=>t.slice(0,600)).catch(e=>'ERR '+e)")
			print(f'\n--- {proc} ---')
			print(out)


if __name__ == '__main__':
	asyncio.run(main())
