"""Report what the browse page currently shows the user, to diagnose the block precisely."""

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
			(() => ({
				url: location.href,
				descrip: (document.querySelector('.results_descrip')||{}).textContent||'',
				stillsText: (document.querySelector('#stills')||{}).textContent?.replace(/\s+/g,' ').trim().slice(0,200)||'',
				hasLoginLink: !!document.querySelector('a[href*="login"]'),
				hasLogoutLink: !!document.querySelector('a[href*="logout"]'),
				headerUser: (document.querySelector('.username, .account-name, [class*=user]')||{}).textContent?.trim()||'',
			}))()
			"""
		)
		import json as j
		print(j.dumps(out, ensure_ascii=False, indent=2))

		# Does the raw /browse/stills page (server HTML) show shots or a login wall?
		raw = await ev("fetch('/browse/stills').then(r=>r.text()).then(t=>({len:t.length, cards:(t.match(/data-shotid=/g)||[]).length, notLoggedIn: t.includes('not logged in'), loginForm: t.includes('welcome/login')||t.includes('Log In')})).then(JSON.stringify).catch(e=>'ERR '+e)")
		print('raw /browse/stills:', raw)


if __name__ == '__main__':
	asyncio.run(main())
