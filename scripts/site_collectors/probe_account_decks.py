"""Inspect the /account and /browse/decks page structure to design their parsers."""

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

		async def ev(expression: str):
			r = await client.send.Runtime.evaluate(
				params={'expression': expression, 'returnByValue': True, 'awaitPromise': True}, session_id=sid
			)
			if 'exceptionDetails' in r:
				return {'__error__': r['exceptionDetails'].get('text', 'js error')}
			return r.get('result', {}).get('value')

		# Parse /account into label:value pairs using a detached DOM.
		account = await ev(
			"""
			fetch('/account').then(r=>r.text()).then(html => {
				const doc = new DOMParser().parseFromString(html, 'text/html');
				const text = doc.body.innerText.replace(/\\n{2,}/g,'\\n').slice(0, 2500);
				const inputs = [...doc.querySelectorAll('input')].map(i => ({name:i.name, value:i.type==='password'?'***':i.value})).filter(i=>i.name).slice(0,40);
				const heads = [...doc.querySelectorAll('h1,h2,h3,.plan,.subscription,.membership,[class*=plan],[class*=subscription]')].map(e=>e.textContent.trim()).filter(Boolean).slice(0,20);
				return JSON.stringify({title: doc.title, heads, inputs, textStart: text}, null, 1);
			}).catch(e=>'ERR '+e)
			"""
		)
		print('===== /account =====')
		print(account)

		# Parse /browse/decks into deck entries.
		decks = await ev(
			"""
			fetch('/browse/decks').then(r=>r.text()).then(html => {
				const doc = new DOMParser().parseFromString(html, 'text/html');
				const deckEls = doc.querySelectorAll('[data-deckid], .deck, [class*=deck-], a[href*="/deck/"]');
				const decks = [];
				const seen = new Set();
				deckEls.forEach(el => {
					const id = el.getAttribute('data-deckid') || (el.getAttribute('href')||'').match(/\\/deck\\/(\\w+)/)?.[1];
					if (!id || seen.has(id)) return; seen.add(id);
					decks.push({ id, text: el.innerText.trim().slice(0,80), href: el.getAttribute('href') });
				});
				return JSON.stringify({title: doc.title, deckCount: decks.length, decks: decks.slice(0,40)}, null, 1);
			}).catch(e=>'ERR '+e)
			"""
		)
		print('\n===== /browse/decks =====')
		print(decks)


if __name__ == '__main__':
	asyncio.run(main())
