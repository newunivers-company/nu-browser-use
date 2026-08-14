"""Render-based per-menu shot collector.

Instead of hitting /browse/searchstillsajax directly (which the site rate-limited into a logout),
this NAVIGATES the real page to each filter hash URL — e.g.

    https://shotdeck.com/browse/stills#/media_type/Movie

— lets ShotDeck's own JS populate the gallery, then reads the rendered .outerimage cards from the
DOM, scrolling to trigger the site's nextbatch loader until PER_OPTION cards are gathered. This is
the legitimate browse flow and is paced politely to avoid re-tripping the abuse guard.

Usage:
  python shotdeck_render_collect.py --category media_type --per-option 108 [--start-at LABEL]
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import os
from pathlib import Path
from urllib.parse import quote_plus

import aiohttp
from cdp_use import CDPClient

CDP_HTTP = os.environ.get('BROWSER_USE_CDP_HTTP', 'http://127.0.0.1:9222')
OUT_DIR = Path(os.environ.get('SHOTDECK_OUT', str(Path.home() / 'shotdeck_export')))
MENU_DIR = OUT_DIR / 'menu'

# Politeness pacing (seconds). Generous on purpose — we already tripped a block once.
NAV_SETTLE = 4.0
SCROLL_WAIT = 2.0
BETWEEN_OPTIONS = 3.0


def _slug(text: str) -> str:
	s = ''.join(c if c.isalnum() else '_' for c in text.strip().lower())
	return '_'.join(filter(None, s.split('_'))) or 'opt'


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


JS_READ_CARDS = r"""
(() => {
	const out = [];
	document.querySelectorAll('.outerimage[data-shotid]').forEach(el => {
		const a = el.querySelector('a.gallerythumb');
		const titleLink = el.querySelector('.moviedetails .gallerytitle a');
		const movieHref = titleLink ? (titleLink.getAttribute('href') || '') : '';
		const movieMatch = movieHref.match(/\/movie\/(\d+)~/);
		const img = el.querySelector('img.still');
		out.push({
			shotid: el.getAttribute('data-shotid'),
			title: titleLink ? titleLink.textContent.trim() : '',
			movie_id: movieMatch ? movieMatch[1] : '',
			year: el.getAttribute('data-titleyear') || '',
			resolution: a ? (a.getAttribute('data-size') || '') : '',
			shot_status: el.getAttribute('data-shot-status') || '',
			content_status: el.getAttribute('data-title-content-status') || '',
			thumb: img ? (img.getAttribute('src') || '') : '',
		});
	});
	const descrip = (document.querySelector('.results_descrip')||{}).textContent || '';
	const loggedOut = /welcome\/login/.test(location.href) || document.body.textContent.includes('not logged in');
	return JSON.stringify({ cards: out, descrip, url: location.href, loggedOut });
})()
"""


async def logged_in(page: Page) -> bool:
	state = await page.ev(
		"JSON.stringify({login: /welcome\\/login/.test(location.href), "
		"logout: !!document.querySelector('a[href*=logout]')})"
	)
	s = json.loads(state)
	return s['logout'] and not s['login']


async def collect_option(page: Page, metatype: str, value: str, per_option: int) -> tuple[list[dict], str]:
	"""Navigate to the filter hash URL, let the site render, scroll to load up to per_option cards."""
	hash_value = quote_plus(value)
	url = f'https://shotdeck.com/browse/stills#/{metatype}/{hash_value}'
	await page.navigate(url)
	await asyncio.sleep(NAV_SETTLE)
	# A hash change on an already-loaded page may not trigger a full reload; nudge the app.
	await page.ev("window.dispatchEvent(new HashChangeEvent('hashchange')); true")
	await asyncio.sleep(NAV_SETTLE)

	seen: dict[str, dict] = {}
	descrip = ''
	stagnant = 0
	while len(seen) < per_option and stagnant < 3:
		parsed = json.loads(await page.ev(JS_READ_CARDS))
		if parsed['loggedOut']:
			return [], '__LOGGED_OUT__'
		descrip = parsed['descrip'] or descrip
		before = len(seen)
		for card in parsed['cards']:
			seen.setdefault(card['shotid'], card)
		if len(seen) >= per_option:
			break
		# Scroll to trigger the site's nextbatch auto-loader.
		await page.ev(
			"(() => { window.scrollTo(0, document.body.scrollHeight); "
			"const s = document.getElementById('stills'); if (s) s.scrollTop = s.scrollHeight; "
			"if (typeof nextbatchCheck === 'function') nextbatchCheck(); return true; })()"
		)
		await asyncio.sleep(SCROLL_WAIT)
		stagnant = stagnant + 1 if len(seen) == before else 0
	return list(seen.values())[:per_option], descrip


async def main() -> None:
	parser = argparse.ArgumentParser()
	parser.add_argument('--category', required=True)
	parser.add_argument('--per-option', type=int, default=108)
	parser.add_argument('--start-at', default=None, help='resume from this option label')
	args = parser.parse_args()

	menu = json.loads((MENU_DIR / 'menu.json').read_text(encoding='utf-8'))
	cat = next((c for c in menu['categories'] if c['metatype'] == args.category), None)
	if cat is None:
		raise SystemExit(f"category {args.category!r} not found: {[c['metatype'] for c in menu['categories']]}")

	folder = MENU_DIR / f"{cat['order']:02d}_{_slug(cat['metatype'])}"
	shots_dir = folder / 'shots'
	shots_dir.mkdir(parents=True, exist_ok=True)

	async with aiohttp.ClientSession() as http:
		async with http.get(f'{CDP_HTTP}/json/version') as response:
			ws_url = (await response.json())['webSocketDebuggerUrl']
	async with CDPClient(ws_url) as client:
		targets = await client.send.Target.getTargets()
		target = next(t for t in targets['targetInfos'] if t['type'] == 'page' and 'shotdeck.com' in t.get('url', ''))
		session = await client.send.Target.attachToTarget(params={'targetId': target['targetId'], 'flatten': True})
		page = Page(client, session['sessionId'])
		await client.send.Page.enable(session_id=session['sessionId'])
		await client.send.Runtime.enable(session_id=session['sessionId'])

		if not await logged_in(page):
			print('BLOCKED: session is logged out. Please log in in the Chrome window, then rerun.')
			return

		print(f"MENU {cat['order']:02d} {cat['header']} ({cat['metatype']}) - {cat['option_count']} options, up to {args.per_option}/option (render mode)")
		combined: list[dict] = []
		summary: list[dict] = []
		reached = args.start_at is None
		for opt in cat['options']:
			if not reached:
				if opt['label'] == args.start_at:
					reached = True
				else:
					continue
			cards, descrip = await collect_option(page, opt['metatype'], opt['value'], args.per_option)
			if descrip == '__LOGGED_OUT__':
				print(f"  STOPPED at {opt['label']}: session logged out again. Re-login and resume with --start-at '{opt['label']}'.")
				break
			for c in cards:
				c['_option'] = opt['label']; c['_subgroup'] = opt['subgroup']; c['_option_value'] = opt['value']
			name = _slug((opt['subgroup'] + '_' + opt['label']) if opt['subgroup'] else opt['label'])
			with (shots_dir / f'{name}.csv').open('w', newline='', encoding='utf-8-sig') as h:
				w = csv.writer(h)
				w.writerow(['shotid', 'title', 'movie_id', 'year', 'resolution', 'content_status', 'thumb'])
				for c in cards:
					w.writerow([c['shotid'], c['title'], c['movie_id'], c['year'], c['resolution'], c['content_status'], c['thumb']])
			(shots_dir / f'{name}.json').write_text(json.dumps(cards, ensure_ascii=False, indent=2), encoding='utf-8')
			combined.extend(cards)
			import re
			total = re.search(r'of ([\d,]+) shots', descrip)
			summary.append({'option': opt['label'], 'subgroup': opt['subgroup'], 'collected': len(cards),
				'library_total': total.group(1) if total else opt.get('shots', '')})
			print(f"  {opt['subgroup']+' / ' if opt['subgroup'] else ''}{opt['label']}: {len(cards)} collected (of {summary[-1]['library_total']})")
			await asyncio.sleep(BETWEEN_OPTIONS)

		if combined:
			with (shots_dir / '_combined.csv').open('w', newline='', encoding='utf-8-sig') as h:
				w = csv.writer(h)
				w.writerow(['option', 'subgroup', 'shotid', 'title', 'movie_id', 'year', 'resolution', 'content_status', 'thumb'])
				for c in combined:
					w.writerow([c['_option'], c['_subgroup'], c['shotid'], c['title'], c['movie_id'], c['year'], c['resolution'], c['content_status'], c['thumb']])
			(shots_dir / '_summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')
		print(f"\nDONE -> {shots_dir}  |  cards: {len(combined)} ({len({c['shotid'] for c in combined})} unique)")


if __name__ == '__main__':
	asyncio.run(main())
