"""Capture Pinterest resource endpoints by navigating to search, a board, and the user's profile,
observing the /resource/ XHR calls and their params (data + bookmarks)."""

from __future__ import annotations

import asyncio
import os
from urllib.parse import unquote

import aiohttp
from cdp_use import CDPClient
from cdp_use.cdp.network import RequestWillBeSentEvent

CDP_HTTP = os.environ.get('BROWSER_USE_CDP_HTTP', 'http://127.0.0.1:9222')


async def main() -> None:
	async with aiohttp.ClientSession() as http:
		async with http.get(f'{CDP_HTTP}/json/version') as response:
			ws_url = (await response.json())['webSocketDebuggerUrl']
	async with CDPClient(ws_url) as client:
		targets = await client.send.Target.getTargets()
		page = next(
			t
			for t in targets['targetInfos']
			if t['type'] == 'page' and 'pinterest.com' in t.get('url', '') and 'recaptcha' not in t.get('url', '')
		)
		session = await client.send.Target.attachToTarget(params={'targetId': page['targetId'], 'flatten': True})
		sid = session['sessionId']
		await client.send.Page.enable(session_id=sid)
		await client.send.Runtime.enable(session_id=sid)
		await client.send.Network.enable(session_id=sid)

		resources: list[str] = []

		def on_request(event: RequestWillBeSentEvent, session_id: str | None) -> None:
			req = event.get('request') or {}
			url = req.get('url', '')
			if '/resource/' in url and 'pinterest.com' in url:
				resources.append(url)

		client.register.Network.requestWillBeSent(on_request)

		async def nav(url: str, wait: float = 7) -> None:
			await client.send.Page.navigate(params={'url': url}, session_id=sid)
			await asyncio.sleep(wait)

		# 1. Search results
		await nav('https://kr.pinterest.com/search/pins/?q=cinematic%20lighting')
		# scroll to trigger more
		await client.send.Runtime.evaluate(
			params={'expression': 'window.scrollTo(0, document.body.scrollHeight)', 'returnByValue': True}, session_id=sid
		)
		await asyncio.sleep(4)

		# 2. The user's own profile (boards)
		await nav('https://kr.pinterest.com/taeoh2026/')

		# Report distinct resource endpoints seen.
		print('=== distinct /resource/ endpoints ===')
		seen = set()
		for url in resources:
			name = url.split('/resource/')[1].split('/')[0]
			base = url.split('?')[0]
			if base in seen:
				continue
			seen.add(base)
			# Decode the data param to show the option shape.
			data_part = ''
			if 'data=' in url:
				try:
					data_part = unquote(url.split('data=')[1].split('&')[0])[:220]
				except Exception:
					data_part = ''
			print(f'{name}')
			print(f'   {base}')
			if data_part:
				print(f'   data: {data_part}')


if __name__ == '__main__':
	asyncio.run(main())
