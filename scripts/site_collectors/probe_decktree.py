"""Extract the deck tree (names, ids, shot counts) from #offcanvasDecks."""

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

		out = await ev(
			r"""
			fetch('/browse/decks').then(r=>r.text()).then(html => {
				const doc = new DOMParser().parseFromString(html, 'text/html');
				const tree = doc.querySelector('#offcanvasDecks');
				const items = [];
				tree.querySelectorAll('li, a[data-deckid], [data-deckid], a[href*="deck"]').forEach(el => {
					const id = el.getAttribute('data-deckid') || el.getAttribute('data-id') || '';
					const onclick = el.getAttribute('onclick') || el.getAttribute('href') || '';
					const txt = el.textContent.replace(/\s+/g,' ').trim().slice(0,80);
					if (txt || id) items.push({ tag: el.tagName, id, txt, onclick: onclick.slice(0,90),
						cls: el.className, attrs: Object.fromEntries([...el.attributes].filter(a=>a.name.startsWith('data-')).map(a=>[a.name,a.value])) });
				});
				return JSON.stringify(items.slice(0,40), null, 1);
			})
			"""
		)
		print(out)


if __name__ == '__main__':
	asyncio.run(main())
