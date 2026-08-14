"""Inspect the left-side filter menu (search accordion) category structure so we can collect
the whole menu, one folder per category, with nesting and option values."""

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

		# Structure of the live left menu in the current DOM.
		out = await ev(
			r"""
			(() => {
				const acc = document.querySelector('#filterAccordian') || document.querySelector('.search-pane') || document.querySelector('[class*=filter]');
				if (!acc) return JSON.stringify({error:'no accordion'});
				// Category headers (the collapsible section titles = folders)
				const cats = [];
				acc.querySelectorAll('.card, .accordion-item, .filter-section, [class*=accordion]').forEach(sec => {
					const head = sec.querySelector('.card-header, .accordion-header, .accordion-toggle, [data-bs-toggle], h3, h4, .filter-heading, a[href^="#"]');
					const headText = head ? head.textContent.replace(/\s+/g,' ').trim().slice(0,50) : '';
					if (!headText) return;
					const inputs = sec.querySelectorAll('input[name]');
					const names = [...new Set([...inputs].map(i=>i.getAttribute('name')))];
					cats.push({ header: headText, metatypes: names, optionCount: inputs.length,
						controlTypes: [...new Set([...inputs].map(i=>i.getAttribute('type')))] });
				});
				// Also list the top-level structure: direct children ids/classes
				const topKids = [...acc.children].map(c => ({tag:c.tagName, id:c.id, cls:c.className.slice(0,60)})).slice(0,40);
				return JSON.stringify({ accId: acc.id, catCount: cats.length, cats: cats.slice(0,50), topKids }, null, 1);
			})()
			"""
		)
		print(out)


if __name__ == '__main__':
	asyncio.run(main())
