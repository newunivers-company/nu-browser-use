"""Re-verify the Instagram/TikTok keyless verdicts with a real Chromium render.

The HTTP probes said: Instagram tag page = login wall, TikTok Creative
Center = JS shell. Both verdicts were made on raw HTML; a rendered page is
what an anonymous human visitor sees, so that is the standard (policy
principle 2). Uses the same CDP evaluate pattern as browser_catalog_collect.

Read-only: navigate, settle, one DOM measurement per target. No scrolling
loops, no clicking, fresh temp profile per run.

Usage (repo venv python):
  python social_render_check.py            (all targets)
  python social_render_check.py --only tiktok_hashtags
Output: printed summary + RENDER_CHECK_OUT/report.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

if sys.stdout.encoding and sys.stdout.encoding.lower() != 'utf-8':
	sys.stdout.reconfigure(encoding='utf-8', errors='replace')

from browser_use.browser.events import NavigateToUrlEvent
from browser_use.browser.profile import BrowserProfile
from browser_use.browser.session import BrowserSession

OUT = Path(os.environ.get('RENDER_CHECK_OUT', str(Path.home() / 'social_render_check'))) / 'report.json'
SETTLE = 8.0
NAV_TIMEOUT = 45

TARGETS: dict[str, dict] = {
	'tiktok_hashtags': {
		'url': 'https://ads.tiktok.com/business/creativecenter/inspiration/popular/hashtag/pc/en',
		'probe': """
(() => {
	const body = document.body.innerText || '';
	const rows = [...document.querySelectorAll('table tr, [class*="HashTag"], [class*="hashtag"]')]
		.map(r => r.innerText).filter(t => t && t.trim().length > 3);
	return JSON.stringify({rows: rows.length, samples: rows.slice(0, 6).map(t => t.slice(0, 120)),
		loginWall: /log in|sign in/i.test(body.slice(0, 3000)), bodyLen: body.length});
})()
""",
	},
	'instagram_tag': {
		'url': 'https://www.instagram.com/explore/tags/cinematography/',
		'probe': """
(() => {
	window.scrollTo(0, document.body.scrollHeight);
	const posts = document.querySelectorAll('a[href*="/p/"]').length;
	const meta = document.querySelector('meta[property="og:description"]');
	const body = document.body.innerText || '';
	const counts = (meta && meta.content.match(/([\d.,KM]+)\s*posts/i)) || null;
	return JSON.stringify({posts, ogDesc: meta ? meta.content.slice(0, 120) : null,
		postCount: counts ? counts[1] : null,
		loginWall: /log in|sign up/i.test(body.slice(0, 2000)), bodyLen: body.length});
})()
""",
	},
	'gtrends_page': {
		'url': 'https://trends.google.com/trends/trendingsearches/daily?geo=KR',
		'probe': """
(() => {
	const rows = document.querySelectorAll('[role="listitem"], a[href*="/trends/explore"]').length;
	return JSON.stringify({rows, title: document.title.slice(0, 80), bodyLen: (document.body.innerText||'').length});
})()
""",
	},
}


async def evaluate(session: BrowserSession, expression: str) -> str | None:
	cdp = await session.get_or_create_cdp_session()
	response = await cdp.cdp_client.send.Runtime.evaluate(
		params={'expression': expression, 'returnByValue': True}, session_id=cdp.session_id
	)
	return response.get('result', {}).get('value')


async def measure(session: BrowserSession, name: str, spec: dict) -> dict:
	await asyncio.wait_for(session.event_bus.dispatch(NavigateToUrlEvent(url=spec['url'], new_tab=False)), timeout=NAV_TIMEOUT)
	await asyncio.sleep(SETTLE)
	title = (await evaluate(session, 'document.title')) or ''
	raw = await evaluate(session, spec['probe'])
	try:
		probe = json.loads(raw) if isinstance(raw, str) else (raw or {})
	except json.JSONDecodeError:
		probe = {'raw': str(raw)[:200]}
	return {'target': name, 'url': spec['url'], 'title': title[:80], 'probe': probe}


async def main() -> None:
	parser = argparse.ArgumentParser()
	parser.add_argument('--only', nargs='*', help='target ids to probe')
	parser.add_argument('--headful', action='store_true')
	args = parser.parse_args()
	targets = {k: v for k, v in TARGETS.items() if not args.only or k in args.only}

	results: list[dict] = []
	with tempfile.TemporaryDirectory(prefix='social_check_') as profile_dir:
		profile = BrowserProfile(headless=not args.headful, keep_alive=False, user_data_dir=Path(profile_dir))
		session = BrowserSession(browser_profile=profile)
		try:
			await session.start()
			for name, spec in targets.items():
				try:
					res = await measure(session, name, spec)
				except Exception as exc:  # noqa: BLE001 - record and continue
					res = {'target': name, 'url': spec['url'], 'error': f'{type(exc).__name__}: {exc}'[:200]}
				results.append(res)
				p = res.get('probe') or {}
				print(f"[{res['target']}] {res.get('title') or res.get('error', '')[:70]}")
				if 'rows' in p:
					print(f"   rows={p['rows']} loginWall={p.get('loginWall')}")
					for s in p.get('samples', [])[:4]:
						print(f"   · {s[:110]}")
		finally:
			await session.stop()

	OUT.parent.mkdir(parents=True, exist_ok=True)
	OUT.write_text(json.dumps(results, ensure_ascii=False, indent=1), encoding='utf-8')
	print(f'-> {OUT}')


if __name__ == '__main__':
	asyncio.run(main())
