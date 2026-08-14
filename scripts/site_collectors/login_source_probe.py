"""Verify `login_required` catalog sources against an already-signed-in Chrome.

21 catalogue sources are marked `login_required`, which means the anonymous
checkers cannot say anything useful about them: an isolated profile sees a
sign-in wall and reports the same "reachable" it would report for a healthy
page. Whether an account actually reaches the content is only answerable from a
session that holds one.

This attaches to a Chrome already running with remote debugging
(`BROWSER_USE_CDP_HTTP`, default http://127.0.0.1:9222). Each source is opened
in its own tab which is closed afterwards, and nothing is clicked, typed or
submitted — navigate and read only. The operator's existing tabs are never
touched and the browser is never closed.

WHY RAW CDP RATHER THAN BrowserSession
BrowserSession(cdp_url=...) is the ergonomic path and was tried first, but it
times out in BrowserStartEvent after 30s against a real signed-in browser —
reproducibly, with no competing job running. A raw CDPClient against the same
endpoint attaches instantly and enumerates all 12 page targets, so the browser
is fine and the stall is in the library's attach-to-existing startup, most
likely its handling of a browser that already has a dozen live tabs. Worth
fixing there; until then this uses the same raw-CDP pattern the other
signed-in collectors in this directory already use.

EXCLUDED ON PURPOSE
The six large social platforms in that group — x, instagram, threads, facebook,
linkedin, snapchat — are skipped. Their terms prohibit automated access, and
doing it from a signed-in session makes it authenticated automated access,
which is squarely what LinkedIn's user agreement names. `docs/collection-
policy.md` already bans them and the promotion registry enforces it at the
browser level; a logged-in browser is not a reason to revisit that.

What is reported per source: whether the page was reached, the final URL after
redirects, how much text rendered, and whether the result looks like signed-in
content or a sign-in wall — the distinction the anonymous checkers cannot draw.

Output (PROBE_OUT, default ~/promo_export):
  snapshots/YYYY-MM-DD/login_sources.json
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import os
import sys
from pathlib import Path

if sys.stdout.encoding and sys.stdout.encoding.lower() != 'utf-8':
	sys.stdout.reconfigure(encoding='utf-8', errors='replace')

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

import aiohttp
from cdp_use import CDPClient

from scripts.data_source_catalog import DataSourceAccess, load_data_source_catalog

CDP_URL = os.environ.get('BROWSER_USE_CDP_HTTP', 'http://127.0.0.1:9222')
OUT_DIR = Path(os.environ.get('PROBE_OUT', str(Path.home() / 'promo_export')))
SETTLE = 5.0
NAV_TIMEOUT = 45.0
# Terms prohibit automated access; a signed-in session makes that worse, not
# permissible. See the module docstring.
EXCLUDED = {'x_explore', 'instagram_explore', 'threads', 'facebook', 'linkedin_feed', 'snapchat_web'}

JS_READ = r"""
(() => {
	const text = (document.body ? document.body.innerText : '') || '';
	const lowered = text.toLowerCase();
	const wallWords = ['sign in', 'log in', 'sign up', 'create account', 'login', '로그인'];
	const hits = wallWords.filter(w => lowered.includes(w));
	return JSON.stringify({
		url: location.href,
		title: document.title || '',
		text_chars: text.length,
		// A wall is short AND asks you to sign in; a signed-in page usually
		// still contains "log out" or plenty of body text.
		signin_words: hits,
		has_logout: /log ?out|sign ?out|로그아웃/i.test(text),
		forms: document.querySelectorAll('form').length,
		password_inputs: document.querySelectorAll('input[type="password"]').length,
		links: document.querySelectorAll('a[href]').length,
	});
})()
"""


def verdict(payload: dict) -> str:
	"""signed_in | sign_in_wall | reachable_unclear."""
	if payload.get('password_inputs'):
		return 'sign_in_wall'
	if payload.get('has_logout'):
		return 'signed_in'
	if payload.get('text_chars', 0) < 400 and payload.get('signin_words'):
		return 'sign_in_wall'
	if payload.get('text_chars', 0) >= 1500:
		return 'signed_in'
	return 'reachable_unclear'


async def websocket_url() -> str:
	async with aiohttp.ClientSession() as http:
		async with http.get(f'{CDP_URL}/json/version', timeout=aiohttp.ClientTimeout(total=15)) as response:
			return (await response.json())['webSocketDebuggerUrl']


async def probe(client: CDPClient, url: str) -> dict:
	"""Open one throwaway tab, read it, close it."""
	created = await client.send.Target.createTarget(params={'url': url})
	target_id = created['targetId']
	try:
		attached = await client.send.Target.attachToTarget(params={'targetId': target_id, 'flatten': True})
		session_id = attached['sessionId']
		await asyncio.sleep(SETTLE)
		response = await asyncio.wait_for(
			client.send.Runtime.evaluate(params={'expression': JS_READ, 'returnByValue': True}, session_id=session_id),
			timeout=NAV_TIMEOUT,
		)
		raw = response.get('result', {}).get('value')
		return json.loads(raw) if raw else {}
	finally:
		# Always give the operator their browser back the way we found it.
		try:
			await client.send.Target.closeTarget(params={'targetId': target_id})
		except Exception:  # noqa: BLE001
			pass


async def main() -> None:
	parser = argparse.ArgumentParser()
	parser.add_argument('--only', nargs='*', help='restrict to these source ids')
	parser.add_argument('--include-excluded', action='store_true', help=argparse.SUPPRESS)
	args = parser.parse_args()

	catalog = load_data_source_catalog()
	sources = [s for s in catalog.sources if s.access is DataSourceAccess.LOGIN_REQUIRED]
	skipped = [s for s in sources if s.id in EXCLUDED and not args.include_excluded]
	sources = [s for s in sources if s.id not in EXCLUDED or args.include_excluded]
	if args.only:
		wanted = set(args.only)
		sources = [s for s in sources if s.id in wanted]
	if args.include_excluded:
		print('REFUSING --include-excluded: these platforms prohibit automated access; a signed-in session does not change that')
		return

	print(f'attaching to the running Chrome at {CDP_URL}')
	print(f'{len(sources)} login-required sources to probe, {len(skipped)} skipped by policy ({", ".join(s.id for s in skipped)})')

	rows: list[dict] = []
	async with CDPClient(await websocket_url()) as client:
		for index, source in enumerate(sources, 1):
			try:
				payload = await probe(client, str(source.url))
			except Exception as exc:  # noqa: BLE001
				rows.append({'id': source.id, 'url': str(source.url), 'verdict': 'error', 'error': type(exc).__name__})
				print(f'  [{index}/{len(sources)}] {source.id:22} ERROR {type(exc).__name__}')
				continue
			row = {
				'id': source.id, 'category': source.category.value, 'url': str(source.url),
				'final_url': payload.get('url'), 'title': (payload.get('title') or '')[:90],
				'text_chars': payload.get('text_chars'), 'password_inputs': payload.get('password_inputs'),
				'has_logout': payload.get('has_logout'), 'verdict': verdict(payload),
			}
			rows.append(row)
			print(f'  [{index}/{len(sources)}] {source.id:22} {row["verdict"]:18} chars={row["text_chars"]}')

	snap_dir = OUT_DIR / 'snapshots' / dt.date.today().isoformat()
	snap_dir.mkdir(parents=True, exist_ok=True)
	(snap_dir / 'login_sources.json').write_text(
		json.dumps({'probed_at': dt.datetime.now(dt.timezone.utc).isoformat(), 'cdp_url': CDP_URL,
					'skipped_by_policy': [s.id for s in skipped], 'sources': rows}, ensure_ascii=False, indent=2),
		encoding='utf-8',
	)
	tally: dict[str, int] = {}
	for row in rows:
		tally[row['verdict']] = tally.get(row['verdict'], 0) + 1
	print('\n' + ', '.join(f'{k}={v}' for k, v in sorted(tally.items())))
	print(f'DONE -> {snap_dir / "login_sources.json"}')


if __name__ == '__main__':
	asyncio.run(main())
