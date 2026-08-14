"""Inspect the internal structure of the genre and shade accordion-items to find sub-group
headers and non-checkbox controls, so the menu collector captures the full hierarchy."""

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
		page = next(t for t in targets['targetInfos'] if t['type'] == 'page' and 'shotdeck.com/browse' in t.get('url', ''))
		session = await client.send.Target.attachToTarget(params={'targetId': page['targetId'], 'flatten': True})
		sid = session['sessionId']
		await client.send.Runtime.enable(session_id=sid)

		async def ev(expr: str):
			r = await client.send.Runtime.evaluate(
				params={'expression': expr, 'returnByValue': True, 'awaitPromise': True}, session_id=sid
			)
			return r.get('result', {}).get('value')

		for metatype in ('genre', 'shot_type'):
			out = await ev(
				r"""
				(() => {
					const item = document.querySelector('#filterAccordian > .accordion-item.METATYPE');
					if (!item) return 'no item';
					// Header text
					const header = (item.querySelector('.accordion-header, button, .accordion-button, h3, h4')||{}).textContent||'';
					// Walk the body, listing sub-headers and a few options in order
					const body = item.querySelector('.accordion-collapse, .accordion-body') || item;
					const seq = [];
					body.querySelectorAll('*').forEach(el => {
						if (seq.length > 30) return;
						const cls = el.className && el.className.toString ? el.className.toString() : '';
						if (/subhead|sub-head|group-head|filter-group-header|category|subcat/i.test(cls) && el.textContent.trim()) {
							seq.push({SUBHEAD: el.textContent.replace(/\s+/g,' ').trim().slice(0,40), cls: cls.slice(0,40)});
						}
						if (el.tagName === 'INPUT' && el.getAttribute('name')) {
							const label = el.closest('label')?.textContent?.trim() || el.nextElementSibling?.textContent?.trim() || '';
							seq.push({name: el.getAttribute('name'), value: el.getAttribute('value'), type: el.getAttribute('type'), label: label.slice(0,30)});
						}
					});
					return JSON.stringify({header: header.replace(/\s+/g,' ').trim().slice(0,40), seq}, null, 1);
				})()
				""".replace('METATYPE', metatype)
			)
			print(f'===== {metatype} =====')
			print(out)
			print()


if __name__ == '__main__':
	asyncio.run(main())
