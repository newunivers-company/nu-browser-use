"""Extract ShotDeck's real AJAX endpoints, shot-card data attributes, and filter vocabulary
by reading the live page's JS functions and DOM over CDP."""

from __future__ import annotations

import asyncio
import json
import os
import re

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

		async def ev(expression: str):
			r = await client.send.Runtime.evaluate(
				params={'expression': expression, 'returnByValue': True, 'awaitPromise': True}, session_id=sid
			)
			if 'exceptionDetails' in r:
				return {'__error__': r['exceptionDetails'].get('text', 'js error')}
			return r.get('result', {}).get('value')

		# Full source of the AJAX-driving functions, to read the endpoint URLs.
		for fn in ('getSearches', 'showShotModal', 'showMoreShots', 'loadShotTotal', 'searchSubmit'):
			src = await ev(f'typeof {fn} === "function" ? {fn}.toString() : "n/a"')
			urls = re.findall(r'["\'/][\w/.-]*(?:ajax|browse|search|shot|deck|get)[\w/.-]*', src or '') if isinstance(src, str) else []
			print(f'--- {fn} url-ish tokens ---')
			print(sorted(set(u for u in urls if '/' in u))[:20])

		# Every data attribute present on a shot card, plus the CSRF/base config.
		card = await ev(
			"""
			(() => {
				const el = document.querySelector('.outerimage[data-shotid]');
				if (!el) return {none:true};
				const thumb = el.querySelector('img');
				const detailLink = el.querySelector('a[href]');
				return {
					dataAttrs: Object.fromEntries([...el.attributes].map(a => [a.name, a.value])),
					innerText: el.innerText.slice(0, 300),
					thumbSrc: thumb ? (thumb.getAttribute('src') || thumb.getAttribute('data-src')) : null,
					linkHref: detailLink ? detailLink.getAttribute('href') : null,
					totalCards: document.querySelectorAll('.outerimage[data-shotid]').length,
				};
			})()
			"""
		)
		print('\n=== shot card full attrs ===')
		print(json.dumps(card, ensure_ascii=False, indent=2)[:2500])

		# Filter accordion: every facet (metatype) and a few option values each.
		filters = await ev(
			"""
			(() => {
				const groups = [];
				document.querySelectorAll('#filterAccordian .filter-group, .filter-group').forEach(g => {
					const name = g.querySelector("input[type='checkbox']")?.getAttribute('name')
						|| g.getAttribute('data-metatype') || null;
					const heading = g.closest('[id]')?.querySelector('.accordion-header, h3, .heading, label')?.textContent?.trim();
					const opts = [...g.querySelectorAll("input[type='checkbox']")].slice(0, 6).map(i => ({
						name: i.getAttribute('name'), value: i.getAttribute('value'),
						label: i.closest('label')?.textContent?.trim()?.slice(0,40) || i.nextElementSibling?.textContent?.trim()?.slice(0,40)
					}));
					if (opts.length) groups.push({ metatype: name, heading, sampleOptions: opts });
				});
				// Also grab the filter accordion section headers (the facet categories)
				const sections = [...document.querySelectorAll('#filterAccordian [data-toggle], #filterAccordian .accordion-toggle, .search-pane h3, .search-pane .filter-heading')]
					.map(e => e.textContent.trim()).filter(Boolean).slice(0, 40);
				return { groupCount: groups.length, groups: groups.slice(0, 25), sectionHeaders: [...new Set(sections)] };
			})()
			"""
		)
		print('\n=== filters ===')
		print(json.dumps(filters, ensure_ascii=False, indent=2)[:4000])

		# The search form input names reveal the full metatype vocabulary.
		metatypes = await ev(
			"""
			(() => {
				const names = new Set();
				document.querySelectorAll('#filterAccordian input[name], .search-pane input[name], form input[name]').forEach(i => names.add(i.getAttribute('name')));
				const ids = [...document.querySelectorAll('#filterAccordian [id]')].map(e => e.id).filter(id => /search_opt|filter/.test(id)).slice(0,40);
				return { inputNames: [...names].filter(Boolean), optionContainerIds: ids };
			})()
			"""
		)
		print('\n=== metatypes ===')
		print(json.dumps(metatypes, ensure_ascii=False, indent=2)[:2500])


if __name__ == '__main__':
	asyncio.run(main())
