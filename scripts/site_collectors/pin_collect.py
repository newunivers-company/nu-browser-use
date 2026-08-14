"""Pinterest keyword pin collector (render + scroll).

Navigates to /search/pins/?q=<keyword>, scrolls to lazy-load pins, reads them from the DOM
(pinid, link, description, image), and downloads images. Detects captcha / login walls and stops.

Usage:
  python pin_collect.py --keyword "cinematic lighting" --target 500 [--no-images]
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import csv
import json
import os
import re
from pathlib import Path
from urllib.parse import quote

import aiohttp
from cdp_use import CDPClient

CDP_HTTP = os.environ.get('BROWSER_USE_CDP_HTTP', 'http://127.0.0.1:9222')
OUT_DIR = Path(os.environ.get('PINTEREST_OUT', str(Path.home() / 'pinterest_export')))
SCROLL_WAIT = 2.0
IMG_SLEEP = 0.5


def _slug(text: str) -> str:
	s = ''.join(c if (c.isalnum() or ord(c) > 127) else '_' for c in text.strip().lower())
	return '_'.join(filter(None, s.split('_'))) or 'kw'


JS_READ_PINS = r"""
(() => {
	const out = [];
	document.querySelectorAll('div[data-test-id="pin"], [data-test-pin-id]').forEach(el => {
		const a = el.querySelector('a[href*="/pin/"]');
		const img = el.querySelector('img');
		const pinid = el.getAttribute('data-test-pin-id') || (a ? ((a.getAttribute('href')||'').match(/\/pin\/(\d+)/)||[])[1] : '');
		if (!pinid) return;
		let src = img ? (img.getAttribute('src') || img.getAttribute('data-src') || '') : '';
		out.push({
			pinid,
			href: a ? a.getAttribute('href') : '',
			description: img ? (img.getAttribute('alt')||'').replace(/\s+/g,' ').trim() : '',
			img: src,
		});
	});
	const blocked = /login|captcha|challenge/i.test(location.pathname)
		|| !!document.querySelector('iframe[src*="captcha"], iframe[title*="challenge"]')
		|| /\/login\//.test(location.href);
	return JSON.stringify({ pins: out, url: location.href, blocked });
})()
"""


def _upgrade_img(src: str) -> str:
	"""Upgrade an i.pinimg.com thumbnail URL to 736x for better quality."""
	return re.sub(r'/(\d+x|\d+x\d+)/', '/736x/', src) if 'i.pinimg.com' in src else src


class Page:
	def __init__(self, client: CDPClient, sid: str) -> None:
		self._client, self._sid = client, sid

	async def ev(self, expr: str):
		r = await self._client.send.Runtime.evaluate(
			params={'expression': expr, 'returnByValue': True, 'awaitPromise': True}, session_id=self._sid
		)
		if 'exceptionDetails' in r:
			raise RuntimeError(r['exceptionDetails'].get('text', 'js error'))
		return r.get('result', {}).get('value')

	async def navigate(self, url: str) -> None:
		await self._client.send.Page.navigate(params={'url': url}, session_id=self._sid)


async def collect_keyword(page: Page, keyword: str, target: int) -> tuple[list[dict], bool]:
	"""Scroll the search page to gather up to `target` unique pins. Returns (pins, blocked)."""
	await page.navigate(f'https://kr.pinterest.com/search/pins/?q={quote(keyword)}')
	await asyncio.sleep(4)
	seen: dict[str, dict] = {}
	stagnant = 0
	# Pinterest virtualizes the masonry grid: off-screen pins are removed from the DOM. So we
	# scroll incrementally (by ~85% of the viewport) and read at every step, catching each pin
	# while it is rendered, before it recycles.
	while len(seen) < target and stagnant < 10:
		parsed = json.loads(await page.ev(JS_READ_PINS))
		if parsed['blocked']:
			return list(seen.values()), True
		before = len(seen)
		for pin in parsed['pins']:
			if pin['pinid'] not in seen:
				pin['img_hi'] = _upgrade_img(pin['img'])
				seen[pin['pinid']] = pin
		if len(seen) >= target:
			break
		at_bottom = await page.ev(
			"(() => { const y = window.scrollY; window.scrollBy(0, Math.round(window.innerHeight*0.85)); "
			"return (window.innerHeight + window.scrollY) >= (document.body.scrollHeight - 5); })()"
		)
		await asyncio.sleep(SCROLL_WAIT)
		# Progress = new pins found this step; reaching the bottom with no growth counts toward stagnation.
		stagnant = stagnant + 1 if len(seen) == before else 0
	return list(seen.values())[:target], False


async def download_images(page: Page, pins: list[dict], img_dir: Path) -> int:
	img_dir.mkdir(parents=True, exist_ok=True)
	sem = asyncio.Semaphore(5)
	saved = 0

	async def one(pin: dict) -> None:
		nonlocal saved
		src = pin.get('img_hi') or pin.get('img')
		if not src:
			return
		dest = img_dir / f"{pin['pinid']}.jpg"
		if dest.exists():
			saved += 1
			return
		async with sem:
			try:
				data = await page.ev(
					f"fetch({json.dumps(src)}).then(r=>r.blob()).then(b=>new Promise(res=>{{const fr=new FileReader();fr.onload=()=>res(fr.result);fr.readAsDataURL(b);}})).catch(e=>'ERR '+e)"
				)
				if isinstance(data, str) and data.startswith('data:'):
					dest.write_bytes(base64.b64decode(data.split(',', 1)[1]))
					saved += 1
			except Exception:  # noqa: BLE001
				pass

	for i in range(0, len(pins), 5):
		await asyncio.gather(*(one(p) for p in pins[i : i + 5]))
		await asyncio.sleep(IMG_SLEEP)
	return saved


async def main() -> None:
	parser = argparse.ArgumentParser()
	parser.add_argument('--keyword', default=None)
	parser.add_argument('--keywords-file', default=None, help='iterate one keyword per line')
	parser.add_argument('--target', type=int, default=500)
	parser.add_argument('--no-images', action='store_true')
	args = parser.parse_args()

	if args.keywords_file:
		keywords = [ln.strip() for ln in Path(args.keywords_file).read_text(encoding='utf-8').splitlines() if ln.strip()]
	elif args.keyword:
		keywords = [args.keyword]
	else:
		raise SystemExit('provide --keyword or --keywords-file')

	async with aiohttp.ClientSession() as http:
		async with http.get(f'{CDP_HTTP}/json/version') as response:
			ws_url = (await response.json())['webSocketDebuggerUrl']
	async with CDPClient(ws_url) as client:
		targets = await client.send.Target.getTargets()
		target = next(t for t in targets['targetInfos'] if t['type'] == 'page' and 'pinterest.com' in t.get('url', '') and 'recaptcha' not in t.get('url', ''))
		session = await client.send.Target.attachToTarget(params={'targetId': target['targetId'], 'flatten': True})
		page = Page(client, session['sessionId'])
		await client.send.Page.enable(session_id=session['sessionId'])
		await client.send.Runtime.enable(session_id=session['sessionId'])

		summary: list[dict] = []
		consecutive_empty = 0
		for idx, keyword in enumerate(keywords, 1):
			kw_dir = OUT_DIR / 'keywords' / _slug(keyword)
			pins_json = kw_dir / 'pins.json'
			# Resume: skip keywords already collected.
			if pins_json.is_file():
				try:
					prior = json.loads(pins_json.read_text(encoding='utf-8'))
					if prior:
						print(f'[{idx}/{len(keywords)}] "{keyword}": {len(prior)} pins (cached)', flush=True)
						summary.append({'keyword': keyword, 'pins': len(prior), 'cached': True})
						consecutive_empty = 0
						continue
				except (json.JSONDecodeError, OSError):
					pass
			kw_dir.mkdir(parents=True, exist_ok=True)
			pins, blocked = await collect_keyword(page, keyword, args.target)
			with (kw_dir / 'pins.csv').open('w', newline='', encoding='utf-8-sig') as h:
				w = csv.writer(h)
				w.writerow(['pinid', 'href', 'description', 'img', 'img_hi'])
				for p in pins:
					w.writerow([p['pinid'], p['href'], p['description'], p['img'], p.get('img_hi', '')])
			pins_json.write_text(json.dumps(pins, ensure_ascii=False, indent=2), encoding='utf-8')
			saved = 0
			if not args.no_images and pins:
				saved = await download_images(page, pins, kw_dir / 'images')
			summary.append({'keyword': keyword, 'pins': len(pins), 'images': saved, 'blocked': blocked})
			print(f'[{idx}/{len(keywords)}] "{keyword}": {len(pins)} pins, {saved} images{" [BLOCKED]" if blocked else ""}', flush=True)

			# Stop if the session appears globally blocked (many consecutive empties).
			consecutive_empty = consecutive_empty + 1 if len(pins) == 0 else 0
			if consecutive_empty >= 8:
				print(f'HALTED: {consecutive_empty} consecutive empty keywords — session likely blocked. Resume after warming up.', flush=True)
				break
			await asyncio.sleep(2.0)

		(OUT_DIR / '_summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')
		total_pins = sum(s['pins'] for s in summary)
		total_imgs = sum(s.get('images', 0) for s in summary)
		print(f'\nALL DONE: {len(summary)} keywords, {total_pins} pins, {total_imgs} images', flush=True)


if __name__ == '__main__':
	asyncio.run(main())
