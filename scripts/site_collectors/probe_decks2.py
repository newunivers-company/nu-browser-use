"""Inspect the real deck markup on /browse/decks to confirm 0 decks vs a parser miss."""

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
		page = next(t for t in targets['targetInfos'] if t['type'] == 'page' and 'shotdeck.com' in t.get('url', ''))
		session = await client.send.Target.attachToTarget(params={'targetId': page['targetId'], 'flatten': True})
		sid = session['sessionId']
		await client.send.Runtime.enable(session_id=sid)

		async def ev(expr: str):
			r = await client.send.Runtime.evaluate(
				params={'expression': expr, 'returnByValue': True, 'awaitPromise': True}, session_id=sid
			)
			return r.get('result', {}).get('value')

		info = await ev(
			"""
			fetch('/browse/decks').then(r=>r.text()).then(html => {
				const doc = new DOMParser().parseFromString(html, 'text/html');
				const classHits = {};
				['deck','deckname','deck-','mydeck','deckthumb','deckwrap','deckrow','deck_row','decklist','folder'].forEach(c=>{
					classHits[c] = doc.querySelectorAll('[class*="'+c+'"]').length;
				});
				const deckLinks = [...doc.querySelectorAll('a[href*="/deck/"]')].slice(0,15).map(a=>({t:a.textContent.trim().slice(0,50),h:a.getAttribute('href')}));
				const dataDeck = doc.querySelectorAll('[data-deckid]').length;
				const emptyMsg = (doc.body.textContent.match(/(no decks|haven't|create your first|get started|empty)[^.]{0,60}/i)||[])[0] || '';
				const headers = [...doc.querySelectorAll('h1,h2,h3')].map(e=>e.textContent.trim()).filter(Boolean).slice(0,10);
				return JSON.stringify({classHits, dataDeckCount: dataDeck, deckLinks, emptyMsg, headers, bytes: html.length}, null, 1);
			})
			"""
		)
		print(info)


if __name__ == '__main__':
	asyncio.run(main())
