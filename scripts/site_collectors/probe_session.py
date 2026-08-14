"""Probe the authenticated ShotDeck session over CDP: confirm login, capture the DOM of the
browse page, and observe which XHR/fetch endpoints the shot browser calls."""

from __future__ import annotations

import asyncio
import json
import os

from cdp_use import CDPClient

CDP_HTTP = os.environ.get('BROWSER_USE_CDP_HTTP', 'http://127.0.0.1:9222')


async def main() -> None:
	import aiohttp

	async with aiohttp.ClientSession() as http:
		async with http.get(f'{CDP_HTTP}/json/version') as response:
			version = await response.json()
		ws_url = version['webSocketDebuggerUrl']

	async with CDPClient(ws_url) as client:
		targets = await client.send.Target.getTargets()
		page_target = next(
			t for t in targets['targetInfos']
			if t['type'] == 'page' and 'shotdeck.com' in t.get('url', '')
		)
		session = await client.send.Target.attachToTarget(
			params={'targetId': page_target['targetId'], 'flatten': True}
		)
		session_id = session['sessionId']

		await client.send.Page.enable(session_id=session_id)
		await client.send.Runtime.enable(session_id=session_id)
		await client.send.Network.enable(session_id=session_id)

		observed: list[dict] = []

		def on_request(client, message: dict) -> None:
			params = message.get('params', {})
			request = params.get('request', {})
			url = request.get('url', '')
			request_type = params.get('type', '')
			if 'shotdeck.com' in url and request_type in ('XHR', 'Fetch'):
				observed.append({'method': request.get('method'), 'url': url, 'type': request_type})

		client.register.Network.requestWillBeSent(on_request)

		async def evaluate(expression: str) -> object:
			result = await client.send.Runtime.evaluate(
				params={'expression': expression, 'returnByValue': True, 'awaitPromise': True},
				session_id=session_id,
			)
			return result.get('result', {}).get('value')

		# 1. Confirm authenticated identity from the page.
		identity = await evaluate(
			"""
			(() => {
				const grab = (sel) => { const el = document.querySelector(sel); return el ? el.textContent.trim() : null; };
				const bodyClass = document.body.className;
				const links = [...document.querySelectorAll('a[href]')].map(a => a.getAttribute('href'))
					.filter(h => h && (h.includes('account') || h.includes('profile') || h.includes('logout')
						|| h.includes('deck') || h.includes('subscription') || h.includes('membership')));
				return {
					title: document.title,
					url: location.href,
					bodyClass,
					accountLinks: [...new Set(links)].slice(0, 30),
					hasLogout: !!document.querySelector('a[href*="logout"], a[href*="signout"]'),
				};
			})()
			"""
		)
		print('=== identity ===')
		print(json.dumps(identity, ensure_ascii=False, indent=2))

		# 2. Reload the browse page to capture the network calls it makes.
		await client.send.Page.navigate(params={'url': 'https://shotdeck.com/browse/stills'}, session_id=session_id)
		await asyncio.sleep(9)

		print('\n=== observed data endpoints ===')
		seen = set()
		for entry in observed:
			key = (entry['method'], entry['url'].split('?')[0])
			if key in seen:
				continue
			seen.add(key)
			print(f"{entry['method']:5} [{entry['type']}] {entry['url'][:200]}")

		# 3. Sample the shot cards currently in the DOM to learn the metadata fields.
		cards = await evaluate(
			"""
			(() => {
				// Shot thumbnails point at the CDN; find their nearest meaningful container.
				const imgs = [...document.querySelectorAll('img')].filter(img => {
					const s = (img.getAttribute('src') || img.getAttribute('data-src') || '');
					return /shot|still|thumb|cdn|media|upload/i.test(s) && !/logo/i.test(s);
				});
				const sample = [];
				const seenContainers = new Set();
				for (const img of imgs) {
					if (sample.length >= 3) break;
					let el = img;
					for (let i = 0; i < 4 && el.parentElement; i++) {
						el = el.parentElement;
						if (el.hasAttribute('data-id') || el.hasAttribute('data-shot-id')
							|| /shot|card|item|result|grid-cell/i.test(el.className)) break;
					}
					if (seenContainers.has(el)) continue;
					seenContainers.add(el);
					sample.push({
						tag: el.tagName,
						className: el.className,
						dataAttrs: Object.fromEntries([...el.attributes].filter(a => a.name.startsWith('data-')).map(a => [a.name, a.value])),
						imgSrc: img.getAttribute('src') || img.getAttribute('data-src'),
						imgTitle: img.getAttribute('title') || img.getAttribute('alt'),
						outer: el.outerHTML.slice(0, 900),
					});
				}
				return { thumbImgCount: imgs.length, sample };
			})()
			"""
		)
		print('\n=== shot card sample ===')
		print(json.dumps(cards, ensure_ascii=False, indent=2)[:6000])

		# 4. Inspect global JS state ShotDeck may expose (config, filter vocab, user).
		globals_info = await evaluate(
			"""
			(() => {
				const interesting = {};
				for (const k of Object.keys(window)) {
					if (/shot|deck|user|config|app|filter|search|api|csrf/i.test(k)) {
						const v = window[k];
						const t = typeof v;
						interesting[k] = t === 'object' && v ? Object.keys(v).slice(0, 30) : String(v).slice(0, 120);
					}
				}
				const csrf = document.querySelector('meta[name="csrf-token"], meta[name="_token"]');
				return { interesting, csrf: csrf ? csrf.getAttribute('content') ? 'present' : 'empty' : 'absent' };
			})()
			"""
		)
		print('\n=== window globals ===')
		print(json.dumps(globals_info, ensure_ascii=False, indent=2)[:3000])


if __name__ == '__main__':
	asyncio.run(main())
