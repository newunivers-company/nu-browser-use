"""Try forcing a very tall viewport so Pinterest's masonry renders many pins at once,
and also read pins straight from __PWS_DATA__ / resource bookmarks."""

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
		await client.send.Page.enable(session_id=sid)
		await client.send.Runtime.enable(session_id=sid)

		async def ev(expr: str):
			r = await client.send.Runtime.evaluate(
				params={'expression': expr, 'returnByValue': True, 'awaitPromise': True}, session_id=sid
			)
			if 'exceptionDetails' in r:
				return {'__error__': r['exceptionDetails'].get('text')}
			return r.get('result', {}).get('value')

		# Force a very tall viewport.
		await client.send.Emulation.setDeviceMetricsOverride(
			params={'width': 1600, 'height': 12000, 'deviceScaleFactor': 1, 'mobile': False}, session_id=sid
		)
		await client.send.Page.navigate(params={'url': 'https://kr.pinterest.com/search/pins/?q=cinematic%20lighting'}, session_id=sid)
		await asyncio.sleep(7)

		for i in range(5):
			m = await ev("JSON.stringify({innerH: window.innerHeight, bodyH: document.body.scrollHeight, pins: document.querySelectorAll('a[href*=\"/pin/\"]').length})")
			print(f'tall-view step {i}:', m)
			await ev("window.scrollBy(0, window.innerHeight);")
			await asyncio.sleep(2.5)

		# Also inspect __PWS_DATA__ for a results array + bookmark (pagination cursor).
		pws = await ev(
			r"""
			(() => {
				try {
					const el = document.querySelector('script#__PWS_DATA__');
					if (!el) return JSON.stringify({error:'no pws'});
					const data = JSON.parse(el.textContent);
					let pinIds = new Set(); let bookmark = null;
					const walk = (o, d=0) => {
						if (!o || d>8 || typeof o!=='object') return;
						if (o.id && (o.images || o.grid_title || o.description!==undefined) && /^\d+$/.test(String(o.id))) pinIds.add(String(o.id));
						if (Array.isArray(o.bookmarks) && o.bookmarks.length) bookmark = o.bookmarks[0];
						for (const k in o) walk(o[k], d+1);
					};
					walk(data);
					return JSON.stringify({ pinIdsInPWS: pinIds.size, bookmark: bookmark ? String(bookmark).slice(0,40) : null });
				} catch(e){ return JSON.stringify({error:String(e)}); }
			})()
			"""
		)
		print('PWS pins/bookmark:', pws)

		await client.send.Emulation.clearDeviceMetricsOverride(session_id=sid)


if __name__ == '__main__':
	asyncio.run(main())
