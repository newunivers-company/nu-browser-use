"""Dump the .deck / .deckname element markup to design the deck parser."""

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
		await client.send.Runtime.enable(session_id=sid)

		async def ev(expr: str):
			r = await client.send.Runtime.evaluate(
				params={'expression': expr, 'returnByValue': True, 'awaitPromise': True}, session_id=sid
			)
			return r.get('result', {}).get('value')

		out = await ev(
			"""
			fetch('/browse/decks').then(r=>r.text()).then(html => {
				const doc = new DOMParser().parseFromString(html, 'text/html');
				const namers = [...doc.querySelectorAll('[class*=deckname], [class*=deck]')].slice(0,8).map(el => ({
					tag: el.tagName, cls: el.className,
					attrs: Object.fromEntries([...el.attributes].map(a=>[a.name,a.value])),
					text: el.textContent.replace(/\\s+/g,' ').trim().slice(0,100),
					outer: el.outerHTML.slice(0,300),
				}));
				return JSON.stringify(namers, null, 1);
			})
			"""
		)
		print(out)


if __name__ == '__main__':
	asyncio.run(main())
