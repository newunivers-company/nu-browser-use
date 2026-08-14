"""Determine Pinterest login state from __PWS_DATA__ and an authenticated resource call."""

from __future__ import annotations

import asyncio
import json
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
		page = next(t for t in targets['targetInfos'] if t['type'] == 'page' and 'pinterest.com' in t.get('url', '') and 'recaptcha' not in t.get('url', ''))
		session = await client.send.Target.attachToTarget(params={'targetId': page['targetId'], 'flatten': True})
		sid = session['sessionId']
		await client.send.Runtime.enable(session_id=sid)

		async def ev(expr: str):
			r = await client.send.Runtime.evaluate(
				params={'expression': expr, 'returnByValue': True, 'awaitPromise': True}, session_id=sid
			)
			if 'exceptionDetails' in r:
				return {'__error__': r['exceptionDetails'].get('text')}
			return r.get('result', {}).get('value')

		# Read the logged-in viewer from the embedded initial state.
		viewer = await ev(
			r"""
			(() => {
				try {
					const el = document.querySelector('script#__PWS_DATA__');
					if (!el) return JSON.stringify({error: 'no __PWS_DATA__'});
					const data = JSON.parse(el.textContent);
					// Walk for a viewer/user object.
					const ctx = data?.props?.context || data?.context || {};
					const user = ctx?.user || data?.props?.initialReduxState?.viewer || null;
					const findUser = (o, d=0) => {
						if (!o || d > 6 || typeof o !== 'object') return null;
						if (o.username && (o.full_name !== undefined || o.email !== undefined)) return o;
						for (const k in o) { const r = findUser(o[k], d+1); if (r) return r; }
						return null;
					};
					const u = user && user.username ? user : findUser(data);
					return JSON.stringify({
						found: !!u,
						username: u?.username || '',
						full_name: u?.full_name || '',
						email: u?.email || '',
						id: u?.id || '',
						board_count: u?.board_count,
						pin_count: u?.pin_count,
						follower_count: u?.follower_count,
					});
				} catch(e) { return JSON.stringify({error: String(e)}); }
			})()
			"""
		)
		print('=== viewer from __PWS_DATA__ ===')
		print(viewer)

		# Hit an authenticated resource endpoint to confirm session works.
		probe = await ev(
			r"""
			fetch('/resource/UserSettingsResource/get/?source_url=%2Fsettings%2F&data=' + encodeURIComponent(JSON.stringify({options:{}, context:{}})), {headers:{'X-Requested-With':'XMLHttpRequest','X-APP-VERSION':'', 'Accept':'application/json'}})
				.then(r => r.text()).then(t => t.slice(0, 200)).catch(e => 'ERR ' + e)
			"""
		)
		print('\n=== UserSettingsResource probe ===')
		print(probe)


if __name__ == '__main__':
	asyncio.run(main())
