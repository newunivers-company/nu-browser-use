"""Inspect the .moviedetails card block and the account subscription-plan text."""

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

		async def ev(expr: str):
			r = await client.send.Runtime.evaluate(
				params={'expression': expr, 'returnByValue': True, 'awaitPromise': True}, session_id=sid
			)
			return r.get('result', {}).get('value')

		html = await ev("fetch('/browse/searchstillsajax').then(r=>r.text())")
		m = re.search(r"<div class='moviedetails.*?</div>", html or '', re.DOTALL)
		print('--- moviedetails raw ---')
		print(m.group(0)[:700] if m else 'miss')

		# How title parses two ways.
		parsed = await ev(
			"""
			(() => {
				const doc = new DOMParser().parseFromString(arguments0, 'text/html');
				const el = doc.querySelector('.outerimage[data-shotid]');
				const md = el.querySelector('.moviedetails');
				return JSON.stringify({
					mdInner: md ? md.innerText : null,
					mdText: md ? md.textContent.replace(/\\s+/g,' ').trim() : null,
					firstLink: md ? (md.querySelector('a')||{}).textContent : null,
					titleAttr: md ? (md.querySelector('[title]')||{}).getAttribute?.('title') : null,
				});
			})()
			""".replace('arguments0', __import__('json').dumps(html))
		)
		print('\n--- title parses ---')
		print(parsed)

		# Account plan line.
		acct = await ev(
			"""
			fetch('/account').then(r=>r.text()).then(html => {
				const doc = new DOMParser().parseFromString(html, 'text/html');
				const t = doc.body.innerText;
				const i = t.indexOf('Subscription Plan');
				return JSON.stringify({ around: t.slice(i, i+60) });
			})
			"""
		)
		print('\n--- account plan text ---')
		print(acct)


if __name__ == '__main__':
	asyncio.run(main())
