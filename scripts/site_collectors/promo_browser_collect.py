"""T1 browser harness for registry channels that need a real renderer.

Five of the short-drama platforms ship a JS shell with no sitemap — goodshort,
flextv, flareflow, mydramawave, shorttv.live all return either a few KB of
bootstrap or a page whose catalog never appears in the HTTP body. They are the
only sizeable gap left in the promotion registry.

Rather than a bespoke CDP script per site (the pattern the earlier collectors in
this directory grew into — each one re-implements its own Runtime.evaluate
wrapper), this drives `BrowserSession` from the library this repo actually is.
That buys three things a raw CDPClient does not:

  * SecurityWatchdog navigation limits. `prohibited_domains` is loaded straight
    from the registry's T2 set, so a redirect or an injected link cannot walk the
    browser onto an authwalled channel. The collection policy stops being a rule
    the script remembers to follow and becomes a property of the browser.
  * Managed lifecycle — launch, target attach, and teardown via the event bus,
    including cleanup when a page hangs.
  * An isolated profile per run, so nothing inherits the operator's logged-in
    cookies. These are anonymous-visitor reads by construction.

Extracted per target: framework state blob (__NEXT_DATA__ / __NUXT__), outbound
links grouped by host (channel discovery, same output contract as
appstore_watch), catalog-shaped link families, and og: metadata.

Output (PROMO_OUT, default ~/promo_export):
  snapshots/YYYY-MM-DD/browser/<brand-or-company>.json
  discovered_channels.jsonl   - appended, shared with appstore_watch
"""

from __future__ import annotations

import argparse
import asyncio
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

from registry.models import AccessTier, Channel, PromotionRegistry, load_registry

from browser_use.browser.events import NavigateToUrlEvent
from browser_use.browser.profile import BrowserProfile
from browser_use.browser.session import BrowserSession

OUT_DIR = Path(os.environ.get('PROMO_OUT', str(Path.home() / 'promo_export')))
SETTLE_SECONDS = 6.0  # SPA catalogs paint well after load fires
NAV_TIMEOUT = 45.0

# One pass over the rendered DOM. Returns JSON (Runtime.evaluate gives us back a
# string, which avoids deep-object serialization limits on big __NEXT_DATA__).
JS_EXTRACT = r"""
(() => {
	const out = {
		url: location.href,
		title: document.title || '',
		text_chars: (document.body ? document.body.innerText.length : 0),
		meta: {},
		state: {},
		links: [],
	};
	document.querySelectorAll('meta[property^="og:"], meta[name^="twitter:"]').forEach(m => {
		const k = m.getAttribute('property') || m.getAttribute('name');
		if (k && !out.meta[k]) out.meta[k] = (m.getAttribute('content') || '').slice(0, 300);
	});
	const next = document.getElementById('__NEXT_DATA__');
	if (next) { try { out.state.next = JSON.parse(next.textContent).props || null; } catch (e) {} }
	if (window.__NUXT__) { try { out.state.nuxt = window.__NUXT__.data || window.__NUXT__.state || null; } catch (e) {} }
	const seen = new Set();
	document.querySelectorAll('a[href]').forEach(a => {
		const href = a.href;
		if (!href || !/^https?:/.test(href) || seen.has(href)) return;
		seen.add(href);
		out.links.push({href: href, text: (a.textContent || '').replace(/\s+/g, ' ').trim().slice(0, 120)});
	});
	return JSON.stringify(out);
})()
"""

SOCIAL_HOSTS = ('instagram.com', 'tiktok.com', 'facebook.com', 'youtube.com', 'youtu.be', 'twitter.com', 'x.com', 'linkedin.com', 'linktr.ee', 'lnk.bio', 't.me', 'discord', 'reddit.com')


def catalog_families(links: list[dict], self_host: str) -> list[dict]:
	"""Group same-host links by path shape to find the catalog route.

	A short-drama site's catalog shows up as one path prefix repeated dozens of
	times (/drama/<slug>, /video/<id>). Reporting the shape and its count tells
	the next collector where to page without hardcoding a guess per site.
	"""
	shapes: Counter[str] = Counter()
	for link in links:
		parts = urlsplit(link['href'])
		if parts.netloc.removeprefix('www.') != self_host.removeprefix('www.'):
			continue
		segments = [s for s in parts.path.split('/') if s]
		if not segments:
			continue
		# Collapse the trailing identifier so /drama/foo and /drama/bar share a shape.
		shape = '/' + '/'.join(segments[:-1] + ['<id>']) if len(segments) > 1 else '/' + segments[0]
		shapes[shape] += 1
	return [{'shape': shape, 'count': count} for shape, count in shapes.most_common(12) if count > 1]


async def extract(session: BrowserSession, channel: Channel) -> dict:
	"""Navigate to one channel and read the rendered page."""
	await asyncio.wait_for(
		session.event_bus.dispatch(NavigateToUrlEvent(url=str(channel.url), new_tab=False)),
		timeout=NAV_TIMEOUT,
	)
	await asyncio.sleep(SETTLE_SECONDS)
	cdp_session = await session.get_or_create_cdp_session()
	response = await cdp_session.cdp_client.send.Runtime.evaluate(
		params={'expression': JS_EXTRACT, 'returnByValue': True, 'awaitPromise': False},
		session_id=cdp_session.session_id,
	)
	value = response.get('result', {}).get('value')
	if not value:
		raise RuntimeError(f'no DOM payload: {response.get("exceptionDetails", {}).get("text", "unknown")}')
	return json.loads(value)


def summarize(page: dict, channel: Channel) -> dict:
	"""Reduce the raw page dump to the fields worth diffing between runs."""
	links = page.get('links', [])
	hosts = Counter(urlsplit(link['href']).netloc.removeprefix('www.') for link in links)
	self_host = urlsplit(page.get('url') or str(channel.url)).netloc
	return {
		'url': str(channel.url),
		'final_url': page.get('url'),
		'brand': channel.brand,
		'company': channel.company,
		'title': page.get('title', '')[:200],
		'text_chars': page.get('text_chars', 0),
		'meta': page.get('meta', {}),
		'link_count': len(links),
		'external_hosts': dict(hosts.most_common(20)),
		'catalog_families': catalog_families(links, self_host),
		'has_next_state': bool((page.get('state') or {}).get('next')),
		'has_nuxt_state': bool((page.get('state') or {}).get('nuxt')),
	}


async def run(registry: PromotionRegistry, targets: list[Channel], headless: bool) -> tuple[list[dict], list[dict]]:
	"""Drive one browser over every target; returns (summaries, discoveries)."""
	now = dt.datetime.now(dt.timezone.utc).isoformat()
	known = {str(channel.url).rstrip('/').replace('https://', '').replace('http://', '').removeprefix('www.') for channel in registry.channels}
	snap_dir = OUT_DIR / 'snapshots' / dt.date.today().isoformat() / 'browser'
	snap_dir.mkdir(parents=True, exist_ok=True)

	with tempfile.TemporaryDirectory(prefix='promo_browser_') as profile_dir:
		profile = BrowserProfile(
			headless=headless,
			keep_alive=False,
			user_data_dir=Path(profile_dir),
			# The registry is the navigation policy: reachable targets only, and
			# every T2 host refused outright even if something links to it.
			allowed_domains=registry.allowed_domains(targets),
			prohibited_domains=registry.prohibited_domains(),
		)
		session = BrowserSession(browser_profile=profile)
		summaries: list[dict] = []
		discoveries: list[dict] = []
		try:
			await session.start()
			for channel in targets:
				label = channel.brand or channel.company or channel.host
				try:
					page = await extract(session, channel)
				except Exception as exc:  # noqa: BLE001 - one bad target must not sink the run
					print(f'  {label}: FAILED {type(exc).__name__}: {str(exc)[:120]}')
					summaries.append({'url': str(channel.url), 'brand': channel.brand, 'company': channel.company, 'error': type(exc).__name__})
					continue
				summary = summarize(page, channel) | {'observed_at': now}
				summaries.append(summary)
				(snap_dir / f'{label}.json').write_text(json.dumps(page, ensure_ascii=False, indent=1)[:4_000_000], encoding='utf-8')
				for link in page.get('links', []):
					host = urlsplit(link['href']).netloc.removeprefix('www.')
					normalized = link['href'].rstrip('/').replace('https://', '').replace('http://', '').removeprefix('www.')
					if any(social in host for social in SOCIAL_HOSTS) and normalized not in known:
						discoveries.append({
							'url': link['href'], 'kind': 'social', 'brand': channel.brand, 'company': channel.company,
							'evidence': f'rendered_page:{channel.url}', 'official_status': 'claimed', 'observed_at': now,
						})
				families = ', '.join(f"{f['shape']}×{f['count']}" for f in summary['catalog_families'][:3]) or 'none'
				print(f"  {label}: {summary['text_chars']} chars, {summary['link_count']} links, catalog: {families}")
		finally:
			await session.kill()
	return summaries, discoveries


async def main() -> None:
	parser = argparse.ArgumentParser()
	parser.add_argument('--only', nargs='*', help='restrict to these brand/company ids')
	parser.add_argument('--headful', action='store_true', help='watch the run in a visible window')
	args = parser.parse_args()

	registry = load_registry()
	targets = registry.collectible(AccessTier.BROWSER_REQUIRED)
	if args.only:
		wanted = set(args.only)
		targets = [c for c in targets if c.brand in wanted or c.company in wanted]
	if not targets:
		print('no collectible T1 channels — set collect: true on the rows you want rendered')
		return

	print(f'{len(targets)} T1 targets, {len(registry.prohibited_domains())} prohibited domain patterns')
	summaries, discoveries = await run(registry, targets, headless=not args.headful)

	today = dt.date.today().isoformat()
	(OUT_DIR / 'snapshots' / today / 'browser_summary.json').write_text(
		json.dumps({'collected_at': dt.datetime.now(dt.timezone.utc).isoformat(), 'targets': summaries}, ensure_ascii=False, indent=2),
		encoding='utf-8',
	)
	if discoveries:
		with (OUT_DIR / 'discovered_channels.jsonl').open('a', encoding='utf-8') as handle:
			for discovery in discoveries:
				handle.write(json.dumps(discovery, ensure_ascii=False) + '\n')
		unique = sorted({d['url'] for d in discoveries})
		print(f'  {len(unique)} social URLs not in registry:')
		for url in unique[:25]:
			print(f'    {url}')

	failed = [s for s in summaries if 'error' in s]
	print(f'DONE -> {OUT_DIR / "snapshots" / today / "browser"} ({len(summaries) - len(failed)} ok, {len(failed)} failed)')


if __name__ == '__main__':
	asyncio.run(main())
