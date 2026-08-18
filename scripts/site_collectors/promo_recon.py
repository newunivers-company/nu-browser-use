"""Per-source API recon via HAR capture.

`promo_browser_collect.py` answers "what does this page render". This answers
the question that actually decides how to collect a source: *what does the page
call to get its data*. An SPA's catalog is served by a JSON endpoint, and
reading that endpoint is cheaper, more complete, and more stable than scraping
rendered DOM — it is how `reelshort_collect.py` ended up on the API instead of
the page.

Uses BrowserProfile's built-in `record_har_path` rather than wiring the CDP
Network domain by hand (the pattern the older recon scripts in this directory
use). The HarRecordingWatchdog captures request/response bodies and writes
HAR 1.2 on shutdown, so recon is a profile flag instead of a script. Those
bodies are read for their shapes and then dropped — see prune_har_bodies.

Run against ONE source at a time and read the report before writing its
collector — the point is to look at each data source deliberately, not to
sweep. Navigation stays inside the registry's allow/deny sets, so recon cannot
wander onto a T2 channel.

  python promo_recon.py goodshort
  python promo_recon.py flextv --paths / /genres --headful

Output (PROMO_OUT, default ~/promo_export):
  recon/<brand>/har.json        - HAR capture, response bodies stripped once
                                  their shapes have been read into endpoints.json
  recon/<brand>/endpoints.json  - ranked JSON endpoints with payload shapes
"""

from __future__ import annotations

import argparse
import asyncio
import base64
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
SETTLE_SECONDS = 7.0
NAV_TIMEOUT = 45.0
# Asset traffic drowns the API calls we are looking for.
ASSET_SUFFIXES = (
	'.js',
	'.css',
	'.png',
	'.jpg',
	'.jpeg',
	'.webp',
	'.gif',
	'.svg',
	'.woff',
	'.woff2',
	'.ttf',
	'.ico',
	'.mp4',
	'.m3u8',
	'.ts',
)
SCROLL_JS = 'window.scrollTo(0, document.body.scrollHeight)'


def resolve(base: str, path: str) -> str:
	"""Join a CLI path onto the source URL, rejecting anything that isn't one.

	Git Bash rewrites a bare `/` argument into a Windows path (`C:/Program
	Files/Git/`), which then reads as an absolute URL and sends the browser
	somewhere absurd. The SecurityWatchdog catches it, but failing here says
	what actually went wrong.
	"""
	if path.startswith(('http://', 'https://')):
		return path
	if not path.startswith('/'):
		raise ValueError(
			f'path must start with / or be an http(s) URL, got {path!r} (MSYS path conversion? prefix the command with MSYS_NO_PATHCONV=1)'
		)
	return base + path


def json_shape(value: object, depth: int = 0) -> object:
	"""Describe a payload's structure without keeping the payload.

	Recon output goes in the repo's export dir and gets read by a human; the
	shape is the actionable part and the content is someone's catalog.
	"""
	if depth > 3:
		return '...'
	if isinstance(value, dict):
		return {key: json_shape(val, depth + 1) for key, val in list(value.items())[:25]}
	if isinstance(value, list):
		return [json_shape(value[0], depth + 1), f'... x{len(value)}'] if value else []
	return type(value).__name__


def har_entries(har: dict) -> list[dict]:
	return har.get('log', {}).get('entries', [])


def prune_har_bodies(har: dict, path: Path) -> None:
	"""Rewrite the capture without the response bodies, once shapes are extracted.

	`record_har_path` writes everything the browser saw, which for three sources
	came to 21.8MB of which 19.2MB was third-party HTML, JavaScript, CSS and
	base64 images. That is the same thing json_shape() above refuses to keep in
	endpoints.json, sitting in the export dir and shipping to the NAS because it
	arrived by a different route.

	What recon needs survives: URL, method, status, mimeType and size per entry,
	so the traffic shape is still readable. The bytes are dropped and the count
	kept, because "how big was it" is a fact about the request and the content is
	someone's page.

	Ordering matters — summarize_endpoints() reads the bodies to infer payload
	shapes, so this runs after endpoints.json is written, never before.
	"""
	stripped = 0
	for entry in har_entries(har):
		content = entry.get('response', {}).get('content')
		if isinstance(content, dict) and content.get('text') is not None:
			content['_body_chars'] = len(content['text'])
			del content['text']
			stripped += 1
		post = entry.get('request', {}).get('postData')
		if isinstance(post, dict) and post.get('text') is not None:
			post['_body_chars'] = len(post['text'])
			del post['text']
	har.setdefault('log', {})['_pruned'] = {
		'bodies_stripped': stripped,
		'why': 'third-party response content is not collection metadata',
	}
	path.write_text(json.dumps(har, ensure_ascii=False), encoding='utf-8')
	print(f'  har: {stripped} response bodies stripped, {path.stat().st_size / 1e6:.1f}MB kept')


def interesting(entry: dict) -> bool:
	"""Keep JSON-ish responses that are not static assets."""
	url = entry.get('request', {}).get('url', '')
	path = urlsplit(url).path.lower()
	if path.endswith(ASSET_SUFFIXES):
		return False
	mime = (entry.get('response', {}).get('content', {}) or {}).get('mimeType', '') or ''
	return 'json' in mime.lower() or '/api' in path or path.endswith('.json')


def decode_body(content: dict) -> object | None:
	text = content.get('text')
	if text is None:
		return None
	if content.get('encoding') == 'base64':
		try:
			text = base64.b64decode(text).decode('utf-8', 'replace')
		except Exception:  # noqa: BLE001
			return None
	try:
		return json.loads(text)
	except Exception:  # noqa: BLE001
		return None


def summarize_endpoints(har: dict) -> list[dict]:
	"""Rank captured JSON endpoints by payload size — the catalog is the big one."""
	rows: list[dict] = []
	for entry in har_entries(har):
		if not interesting(entry):
			continue
		request = entry.get('request', {})
		response = entry.get('response', {})
		content = response.get('content', {}) or {}
		payload = decode_body(content)
		parts = urlsplit(request.get('url', ''))
		rows.append(
			{
				'method': request.get('method'),
				'host': parts.netloc,
				'path': parts.path,
				'query_keys': sorted({kv.split('=')[0] for kv in parts.query.split('&') if kv}),
				'status': response.get('status'),
				'mime': content.get('mimeType'),
				'bytes': content.get('size') or 0,
				'post_data': (request.get('postData', {}) or {}).get('text', '')[:400] or None,
				'request_headers': {
					header['name']: header['value'][:80]
					for header in request.get('headers', [])
					if header['name'].lower()
					not in (
						'cookie',
						'user-agent',
						'accept-encoding',
						'accept-language',
						'referer',
						'origin',
						'sec-fetch-dest',
						'sec-fetch-mode',
						'sec-fetch-site',
						'sec-ch-ua',
						'sec-ch-ua-mobile',
						'sec-ch-ua-platform',
						'connection',
						'host',
						'content-length',
						'accept',
					)
				},
				'shape': json_shape(payload) if payload is not None else None,
			}
		)
	# Deduplicate by (method, path); keep the largest payload seen for each.
	best: dict[tuple, dict] = {}
	for row in rows:
		key = (row['method'], row['host'], row['path'])
		if key not in best or (row['bytes'] or 0) > (best[key]['bytes'] or 0):
			best[key] = row
	return sorted(best.values(), key=lambda r: r['bytes'] or 0, reverse=True)


async def recon(registry: PromotionRegistry, channel: Channel, paths: list[str], headless: bool, scrolls: int) -> dict:
	"""Drive one source through a few paths while HAR captures everything."""
	out_dir = OUT_DIR / 'recon' / (channel.brand or channel.company or channel.host)
	out_dir.mkdir(parents=True, exist_ok=True)
	har_path = out_dir / 'har.json'

	with tempfile.TemporaryDirectory(prefix='promo_recon_') as profile_dir:
		profile = BrowserProfile(
			headless=headless,
			keep_alive=False,
			user_data_dir=Path(profile_dir),
			allowed_domains=registry.allowed_domains([channel]),
			prohibited_domains=registry.prohibited_domains(),
			record_har_path=har_path,
		)
		session = BrowserSession(browser_profile=profile)
		visited: list[dict] = []
		try:
			await session.start()
			base = str(channel.url).rstrip('/')
			for path in paths:
				url = resolve(base, path)
				try:
					await asyncio.wait_for(
						session.event_bus.dispatch(NavigateToUrlEvent(url=url, new_tab=False)), timeout=NAV_TIMEOUT
					)
					await asyncio.sleep(SETTLE_SECONDS)
					cdp_session = await session.get_or_create_cdp_session()
					for _ in range(scrolls):  # lazy-loaded rails only fire on scroll
						await cdp_session.cdp_client.send.Runtime.evaluate(
							params={'expression': SCROLL_JS, 'returnByValue': True}, session_id=cdp_session.session_id
						)
						await asyncio.sleep(2.0)
					visited.append({'url': url, 'ok': True})
					print(f'  visited {url}')
				except Exception as exc:  # noqa: BLE001
					visited.append({'url': url, 'ok': False, 'error': type(exc).__name__})
					print(f'  FAILED {url}: {type(exc).__name__}')
		finally:
			await session.kill()  # HAR is written on shutdown

	if not har_path.exists():
		print('  no HAR produced')
		return {'visited': visited, 'endpoints': []}
	har = json.loads(har_path.read_text(encoding='utf-8'))
	endpoints = summarize_endpoints(har)
	report = {
		'source': str(channel.url),
		'brand': channel.brand,
		'observed_at': dt.datetime.now(dt.timezone.utc).isoformat(),
		'visited': visited,
		'total_requests': len(har_entries(har)),
		'hosts': dict(Counter(urlsplit(e.get('request', {}).get('url', '')).netloc for e in har_entries(har)).most_common(12)),
		'endpoints': endpoints,
	}
	(out_dir / 'endpoints.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
	prune_har_bodies(har, har_path)
	return report


async def main() -> None:
	parser = argparse.ArgumentParser()
	parser.add_argument('target', help='brand or company id from the registry')
	parser.add_argument('--paths', nargs='*', default=['/'], help='paths to visit, relative to the channel url')
	parser.add_argument('--scrolls', type=int, default=3)
	parser.add_argument('--headful', action='store_true')
	args = parser.parse_args()

	registry = load_registry()
	candidates = [c for c in registry.collectible() if c.brand == args.target or c.company == args.target]
	# Prefer the browser-tier row; a brand can own both a T0 site and a T1 app shell.
	candidates.sort(key=lambda c: c.access_tier is not AccessTier.BROWSER_REQUIRED)
	if not candidates:
		print(f'no collectible channel for {args.target!r}')
		return
	channel = candidates[0]
	print(f'recon {channel.url} ({channel.access_tier.value}) over {len(args.paths)} path(s)')

	report = await recon(registry, channel, args.paths, headless=not args.headful, scrolls=args.scrolls)
	endpoints = report.get('endpoints', [])
	print(f'\n{report.get("total_requests", 0)} requests, {len(endpoints)} distinct JSON endpoints')
	for row in endpoints[:15]:
		keys = ','.join(row['query_keys'][:6])
		print(f'  {row["bytes"]:>9} B  {row["method"]:4} {row["host"]}{row["path"][:60]:60} [{keys}]')
	if endpoints:
		print(f'\nfull shapes -> {OUT_DIR / "recon" / (channel.brand or channel.host) / "endpoints.json"}')


if __name__ == '__main__':
	asyncio.run(main())
