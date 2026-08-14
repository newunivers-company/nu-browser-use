"""Extract civitai cookies via CDP and call the tRPC generation-data endpoints from Python."""

from __future__ import annotations

import asyncio
import json
import os
from urllib.parse import quote

import aiohttp
from cdp_use import CDPClient

CDP_HTTP = os.environ.get('BROWSER_USE_CDP_HTTP', 'http://127.0.0.1:9222')
BASE = 'https://civitai.com/api/v1'
HEADERS = {'User-Agent': 'Mozilla/5.0', 'Accept': 'application/json', 'Referer': 'https://civitai.com/'}


async def get_cookies() -> str:
	"""Read civitai.com cookies (incl. httpOnly) via CDP and return a Cookie header string."""
	async with aiohttp.ClientSession() as http:
		async with http.get(f'{CDP_HTTP}/json/version') as response:
			ws_url = (await response.json())['webSocketDebuggerUrl']
	async with CDPClient(ws_url) as client:
		targets = await client.send.Target.getTargets()
		page = next(t for t in targets['targetInfos'] if t['type'] == 'page' and 'civitai.com' in t.get('url', '') and 'red' not in t.get('url', ''))
		session = await client.send.Target.attachToTarget(params={'targetId': page['targetId'], 'flatten': True})
		sid = session['sessionId']
		await client.send.Network.enable(session_id=sid)
		result = await client.send.Network.getCookies(params={'urls': ['https://civitai.com']}, session_id=sid)
		cookies = result.get('cookies', [])
		names = [c['name'] for c in cookies]
		print('cookies found:', names)
		return '; '.join(f"{c['name']}={c['value']}" for c in cookies)


async def main() -> None:
	cookie_header = await get_cookies()
	auth_headers = dict(HEADERS, Cookie=cookie_header)

	async with aiohttp.ClientSession(headers=auth_headers) as s:
		# Get a SFW image id.
		async with s.get(f'{BASE}/images', params={'limit': '5', 'nsfw': 'None', 'sort': 'Newest'}) as r:
			img_id = (await r.json())['items'][0]['id']
		print('test image id:', img_id)

		inp = quote(json.dumps({'json': {'id': img_id}}))
		async with s.get(f'https://civitai.com/api/trpc/image.getGenerationData?input={inp}') as r:
			print('getGenerationData status:', r.status)
			gd = await r.json()
		meta = (((gd.get('result') or {}).get('data') or {}).get('json') or {}).get('meta') or {}
		print('meta fields:', list(meta.keys())[:20])
		print('prompt:', str(meta.get('prompt'))[:100])

		async with s.get(f'https://civitai.com/api/trpc/image.getResources?input={inp}') as r:
			res = await r.json()
		resources = ((res.get('result') or {}).get('data') or {}).get('json') or []
		print('resources:', [(x.get('modelName'), x.get('modelType')) for x in resources][:5])


if __name__ == '__main__':
	asyncio.run(main())
