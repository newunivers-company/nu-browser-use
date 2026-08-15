"""Does a browser actually unlock this source? Measure it, do not score it.

browser_upgrade_review.py ranks sources by what their robots file and declared
channel imply. That is a desk study, and desk studies about this catalogue have
been wrong more than once: `framework_state` was reported as a six-source win
before inspection showed kakuyomu's 556 "items" were feature flags and tapas's
87 were navigation labels. A verdict of `render_required` is a hypothesis.

This tests the hypothesis. Each candidate is read twice — once over plain HTTP,
once from the live DOM after the page has run — and the two are counted the same
way. A browser is worth its Chromium launch here only if the rendered page
carries structured data the HTTP body does not.

WHAT IS COUNTED, AND WHY THESE
  ld_items      JSON-LD objects, flattened through @graph. This is the channel
                the HTTP loop actually collects from, so a gain here converts
                directly into collectable records.
  item_links    hrefs sharing the most common `/segment/<id>`-shaped prefix. A
                catalogue listing produces many; a marketing page produces none.
                Counting the dominant shape rather than all links keeps
                navigation chrome from reading as a catalogue.
  og_props      OpenGraph properties, the fallback channel for a page with no
                JSON-LD.
  text_chars    visible text length. A large rendered/HTTP ratio with no gain in
                the structured counts is the signature of a page that renders
                prose, not data — worth knowing before writing a collector.

CONTROLS
A probe that reports "no gain" everywhere is indistinguishable from a broken
probe, so the run includes sources already known to collect over HTTP. If those
do not show comparable counts on both sides, the measurement is faulty and the
zeros elsewhere mean nothing. This is the same discipline newtoki_watch's
control query enforces, for the same reason.

ROBOTS
The page path is checked before any navigation and the browser is pinned to the
source's own host. A disallowed path is skipped and recorded as skipped, never
silently dropped.

Output (PROBE_OUT, default ~/source_review):
  render_probe.json / .csv - per source: http vs rendered counts, and the verdict
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import datetime as dt
import json
import os
import sys
import tempfile
from collections import Counter
from pathlib import Path
from urllib.parse import urlsplit

if sys.stdout.encoding and sys.stdout.encoding.lower() != 'utf-8':
	sys.stdout.reconfigure(encoding='utf-8', errors='replace')

sys.path.insert(0, str(Path(__file__).resolve().parent))

import aiohttp
from promo_registry_verify import robots_verdict, scalar_verdict

from browser_use.browser.events import NavigateToUrlEvent
from browser_use.browser.profile import BrowserProfile
from browser_use.browser.session import BrowserSession

OUT_DIR = Path(os.environ.get('PROBE_OUT', str(Path.home() / 'source_review')))
REVIEW_FILE = OUT_DIR / 'browser_upgrade_review.json'
UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36'
HEADERS = {'User-Agent': UA, 'Accept': 'text/html,application/xhtml+xml,*/*;q=0.8'}
SETTLE = 6.0
NAV_TIMEOUT = 45.0
HTTP_TIMEOUT = 30

# Verdicts worth spending a browser on, most promising first.
CANDIDATE_VERDICTS = ('api_blocked_web_open', 'render_required', 'no_channel_rendered')
# Sources that already collect over HTTP; they anchor the measurement.
CONTROL_VERDICT = 'http_is_fine'

# One expression, run against the rendered DOM and against the HTTP body parsed
# into the same DOM, so both sides are counted by identical code.
JS_MEASURE = r"""
(() => {
	const out = {ld_items: 0, og_props: 0, item_links: 0, text_chars: 0, has_next: false, has_nuxt: false};
	out.has_next = !!document.getElementById('__NEXT_DATA__');
	out.has_nuxt = !!document.getElementById('__NUXT_DATA__');
	out.og_props = document.querySelectorAll('meta[property^="og:"]').length;
	document.querySelectorAll('script[type="application/ld+json"]').forEach(s => {
		try {
			const parsed = JSON.parse(s.textContent || 'null');
			const flatten = node => {
				if (!node) return 0;
				if (Array.isArray(node)) return node.reduce((n, x) => n + flatten(x), 0);
				if (typeof node === 'object') {
					if (node['@graph']) return flatten(node['@graph']);
					return node['@type'] ? 1 : 0;
				}
				return 0;
			};
			out.ld_items += flatten(parsed);
		} catch (e) { /* malformed block counts as nothing */ }
	});
	// The dominant `/segment/<id>` shape, not every link: navigation chrome
	// otherwise reads as catalogue depth.
	const shapes = {};
	document.querySelectorAll('a[href]').forEach(a => {
		let path;
		try { path = new URL(a.href, location.href).pathname; } catch (e) { return; }
		const m = path.match(/^\/([^/]+)\/[^/]+/);
		if (m) shapes[m[1]] = (shapes[m[1]] || 0) + 1;
	});
	out.item_links = Object.values(shapes).reduce((a, b) => Math.max(a, b), 0);
	// Not every catalogue puts the id in the path. Hacker News links items as
	// `item?id=...`, and counting only path shapes scored its front page at 3
	// links and called it empty — a control catching the metric, which is what
	// controls are for. Query-keyed items are counted separately.
	const q = {};
	document.querySelectorAll('a[href]').forEach(a => {
		let u; try { u = new URL(a.href, location.href); } catch (e) { return; }
		u.searchParams.forEach((v, k) => { if (/^\d+$/.test(v)) q[k] = (q[k] || 0) + 1; });
	});
	out.query_links = Object.values(q).reduce((a, b) => Math.max(a, b), 0);
	out.total_links = document.querySelectorAll('a[href]').length;
	out.text_chars = (document.body ? (document.body.innerText || '') : '').length;
	return JSON.stringify(out);
})()
"""

EMPTY = {
	'ld_items': 0,
	'og_props': 0,
	'item_links': 0,
	'query_links': 0,
	'total_links': 0,
	'text_chars': 0,
	'has_next': False,
	'has_nuxt': False,
}


async def robots_allows(session: aiohttp.ClientSession, url: str, cache: dict[str, str]) -> tuple[bool, str]:
	parts = urlsplit(url)
	origin = f'{parts.scheme}://{parts.netloc}'
	if origin not in cache:
		body = ''
		try:
			async with session.get(f'{origin}/robots.txt', timeout=aiohttp.ClientTimeout(total=20)) as response:
				if response.status == 200:
					body = await response.text(errors='replace')
		except Exception:  # noqa: BLE001
			body = ''
		cache[origin] = body
	body = cache[origin]
	if not body:
		return True, 'no robots.txt published'
	verdict = scalar_verdict(robots_verdict(body, parts.path or '/'))
	return verdict in ('allow', 'unknown'), verdict


async def measure_http(session: aiohttp.ClientSession, url: str, browser: BrowserSession) -> dict:
	"""Fetch the raw body, then count it with the same JS the rendered page gets.

	The body is written to a data: document rather than parsed in Python, because
	two different counters would make any difference between the sides
	uninterpretable — is the browser richer, or is the Python parser poorer?
	"""
	try:
		async with session.get(url, timeout=aiohttp.ClientTimeout(total=HTTP_TIMEOUT)) as response:
			if response.status != 200:
				return {**EMPTY, 'error': f'http_{response.status}'}
			html = await response.text(errors='replace')
	except Exception as exc:  # noqa: BLE001
		return {**EMPTY, 'error': type(exc).__name__}

	cdp = await browser.get_or_create_cdp_session()
	# document.write into a blank page: no network, no script execution.
	#
	# The <base> is not cosmetic. Written into about:blank, every relative href
	# resolves against about:blank, so the HTTP side scored zero item links on
	# any site using relative URLs and the probe called it a browser win. Hacker
	# News — server-rendered, nothing to gain from a browser — came out as
	# "rendering adds 117 item links". With the base tag both sides resolve
	# against the real origin and are actually comparable.
	base_tag = f'<base href="{url}">'
	payload = json.dumps(base_tag + html)
	await cdp.cdp_client.send.Runtime.evaluate(
		params={
			'expression': f'(() => {{ document.open(); document.write({payload}); document.close(); return 1; }})()',
			'returnByValue': True,
		},
		session_id=cdp.session_id,
	)
	response = await cdp.cdp_client.send.Runtime.evaluate(
		params={'expression': JS_MEASURE, 'returnByValue': True}, session_id=cdp.session_id
	)
	raw = response.get('result', {}).get('value')
	return {**(json.loads(raw) if raw else EMPTY), 'error': None, 'bytes': len(html)}


async def measure_rendered(browser: BrowserSession, url: str) -> dict:
	try:
		await asyncio.wait_for(browser.event_bus.dispatch(NavigateToUrlEvent(url=url, new_tab=False)), timeout=NAV_TIMEOUT)
	except Exception as exc:  # noqa: BLE001
		return {**EMPTY, 'error': type(exc).__name__}
	await asyncio.sleep(SETTLE)
	cdp = await browser.get_or_create_cdp_session()
	response = await cdp.cdp_client.send.Runtime.evaluate(
		params={'expression': JS_MEASURE, 'returnByValue': True}, session_id=cdp.session_id
	)
	raw = response.get('result', {}).get('value')
	return {**(json.loads(raw) if raw else EMPTY), 'error': None}


def judge(http: dict, rendered: dict) -> tuple[str, str]:
	"""What the two readings say, in the language of what to build next."""
	if rendered.get('error'):
		return 'render_failed', f'browser could not read the page ({rendered["error"]})'

	# Items may be keyed in the path or in the query; take whichever the site uses.
	def items(side: dict) -> int:
		return max(side.get('item_links', 0), side.get('query_links', 0))

	ld_gain = rendered['ld_items'] - http['ld_items']
	link_gain = items(rendered) - items(http)
	if http.get('error'):
		return (
			'browser_only',
			f'HTTP failed ({http["error"]}) but the browser rendered {rendered["ld_items"]} LD items, {rendered["item_links"]} item links',
		)
	if ld_gain > 0 or link_gain >= 10:
		return 'browser_wins', f'rendering adds {ld_gain} LD items and {link_gain} item links'
	if rendered['text_chars'] > max(1, http['text_chars']) * 3 and rendered['item_links'] < 10:
		return 'renders_prose', 'the page fills in on render but with text, not structured data'
	if rendered['ld_items'] == 0 and items(rendered) < 5 and rendered.get('total_links', 0) < 20:
		return 'nothing_there', 'neither side exposes a catalogue; there is no data to collect here'
	return 'http_is_enough', 'the HTTP body already carries what the rendered page shows'


async def main() -> None:
	parser = argparse.ArgumentParser()
	parser.add_argument('--limit', type=int, default=12, help='candidates to probe')
	parser.add_argument('--controls', type=int, default=3, help='known-good HTTP sources included as a measurement check')
	parser.add_argument('--headful', action='store_true')
	parser.add_argument('--only', nargs='*', help='probe these source ids only')
	args = parser.parse_args()

	if not REVIEW_FILE.exists():
		raise SystemExit(f'no review at {REVIEW_FILE} — run browser_upgrade_review.py first')
	rows = json.loads(REVIEW_FILE.read_text(encoding='utf-8'))['sources']
	by_id = {r['id']: r for r in rows}

	if args.only:
		targets = [by_id[i] for i in args.only if i in by_id]
	else:
		ranked = sorted(rows, key=lambda r: CANDIDATE_VERDICTS.index(r['verdict']) if r['verdict'] in CANDIDATE_VERDICTS else 99)
		targets = [r for r in ranked if r['verdict'] in CANDIDATE_VERDICTS][: args.limit]
		controls = [r for r in rows if r['verdict'] == CONTROL_VERDICT and r['collected_items'] > 20][: args.controls]
		targets += controls
	if not targets:
		raise SystemExit('nothing to probe')
	print(f'probing {len(targets)} sources ({sum(1 for t in targets if t["verdict"] == CONTROL_VERDICT)} of them controls)')

	now = dt.datetime.now(dt.timezone.utc).isoformat()
	results: list[dict] = []
	cache: dict[str, str] = {}

	async with aiohttp.ClientSession(headers=HEADERS) as http_session:
		for source in targets:
			allowed, verdict = await robots_allows(http_session, source['url'], cache)
			if not allowed:
				print(f'  {source["id"]:24} SKIPPED (robots: {verdict})')
				results.append(
					{
						'id': source['id'],
						'url': source['url'],
						'prior_verdict': source['verdict'],
						'probe': 'skipped_robots',
						'why': verdict,
						'observed_at': now,
					}
				)
				continue

			host = urlsplit(source['url']).netloc
			with tempfile.TemporaryDirectory(prefix='render_probe_') as profile_dir:
				profile = BrowserProfile(
					headless=not args.headful,
					keep_alive=False,
					user_data_dir=Path(profile_dir),
					allowed_domains=[host, f'*.{host.removeprefix("www.")}'],
				)
				browser = BrowserSession(browser_profile=profile)
				try:
					await browser.start()
					http = await measure_http(http_session, source['url'], browser)
					rendered = await measure_rendered(browser, source['url'])
				except Exception as exc:  # noqa: BLE001
					http, rendered = {**EMPTY, 'error': 'setup'}, {**EMPTY, 'error': type(exc).__name__}
				finally:
					await browser.kill()

			probe, why = judge(http, rendered)
			results.append(
				{
					'id': source['id'],
					'category': source.get('category'),
					'url': source['url'],
					'prior_verdict': source['verdict'],
					'prior_channel': source.get('channel'),
					'collected_items': source.get('collected_items'),
					'http_ld': http['ld_items'],
					'rendered_ld': rendered['ld_items'],
					'http_links': http['item_links'],
					'rendered_links': rendered['item_links'],
					'http_og': http['og_props'],
					'rendered_og': rendered['og_props'],
					'http_text': http['text_chars'],
					'rendered_text': rendered['text_chars'],
					'rendered_next': rendered['has_next'],
					'rendered_nuxt': rendered['has_nuxt'],
					'http_error': http.get('error'),
					'render_error': rendered.get('error'),
					'probe': probe,
					'why': why,
					'observed_at': now,
				}
			)
			print(
				f'  {source["id"]:24} {probe:16} LD {http["ld_items"]}->{rendered["ld_items"]}  links {http["item_links"]}->{rendered["item_links"]}'
			)

	OUT_DIR.mkdir(parents=True, exist_ok=True)
	(OUT_DIR / 'render_probe.json').write_text(
		json.dumps({'probed_at': now, 'sources': results}, ensure_ascii=False, indent=1), encoding='utf-8'
	)
	columns = list(results[0].keys()) if results else []
	if columns:
		with (OUT_DIR / 'render_probe.csv').open('w', newline='', encoding='utf-8-sig') as handle:
			writer = csv.DictWriter(handle, fieldnames=columns, extrasaction='ignore')
			writer.writeheader()
			writer.writerows(results)

	tally = Counter(r['probe'] for r in results)
	print('\nverdicts:', dict(tally))
	controls = [r for r in results if r.get('prior_verdict') == CONTROL_VERDICT]
	if controls:
		measured = sum(1 for c in controls if (c.get('http_ld') or 0) + (c.get('http_links') or 0) > 0)
		print(f'controls: {measured}/{len(controls)} known-HTTP sources measured non-zero over HTTP', end=' ')
		print('- measurement trusted' if measured else '- MEASUREMENT SUSPECT, treat every zero above as unproven')
	wins = [r for r in results if r['probe'] in ('browser_wins', 'browser_only')]
	if wins:
		print(f'\nworth a browser ({len(wins)}):')
		for row in wins:
			print(f'  {row["id"]:24} {row["why"]}')
	print(f'\nDONE -> {OUT_DIR / "render_probe.csv"}')


if __name__ == '__main__':
	asyncio.run(main())
