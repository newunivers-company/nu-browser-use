"""Nail the card markup and the nextbatch pagination URL by reading the raw AJAX HTML and the
click handler bound to a.nextbatch."""

from __future__ import annotations

import asyncio
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

		html = await ev("fetch('/browse/searchstillsajax').then(r=>r.text()).catch(e=>'ERR '+e)")
		cards = re.findall(r"data-shotid='([^']+)'", html)
		print(f'cards (single-quote): {len(cards)}  first: {cards[:5]}')

		# One full card block.
		m = re.search(r"<div id='image[^>]*data-shotid='[^']+'.*?</div>\s*</div>", html, re.DOTALL)
		print('\n--- one card block ---')
		print((m.group(0)[:1200]) if m else 'card block regex miss; raw slice:')
		if not m:
			i = html.find("data-shotid")
			print(html[max(0, i - 300):i + 500])

		# The click handler that turns id="36-36" into a fetch URL.
		for token in ('a.nextbatch', 'nextbatch', 'searchstillsajax'):
			print(f'\n--- scripts mentioning {token!r} ---')
			hits = await ev(
				f"""
				(() => {{
					const out = [];
					document.querySelectorAll('script').forEach(s => {{
						const t = s.textContent || '';
						let i = t.indexOf('{token}');
						while (i >= 0 && out.length < 3) {{ out.push(t.slice(Math.max(0,i-120), i+200)); i = t.indexOf('{token}', i+1); }}
					}});
					return out;
				}})()
				"""
			)
			for h in (hits or [])[:3]:
				print('  ...' + h.replace('\n', ' ').replace('\t', '') + '...')


if __name__ == '__main__':
	asyncio.run(main())
