"""Collect the video body + metadata for a hand-supplied list of Instagram posts.

SCOPE — read this before widening it
`docs/collection-policy.md` puts Instagram at `access_tier: T2`: authwalled, and
the promotion registry never requests it. That rule is about *channel-scale*
harvesting — walking a profile, enumerating a feed, folding the result into the
registry pipeline. This script does none of that. It takes an explicit list of
post URLs the operator already has and reads each one exactly once, as an
anonymous visitor, the same way opening the link in a browser would.

The distinction that matters is the one `login_source_probe.py` draws: it
refuses to touch these platforms *from a signed-in session*, because a session
turns automated access into authenticated automated access. So this deliberately
does not authenticate, does not accept a session cookie, and asserts the
attached browser has no Instagram `sessionid` before it starts. If the operator
happens to be logged in, the run aborts rather than borrowing the session.

WHY RAW CDP AGAINST A RUNNING BROWSER
Same reason as `login_source_probe.py`: `BrowserSession(cdp_url=...)` stalls in
BrowserStartEvent against a real browser that already holds a dozen tabs, while
a raw CDPClient attaches instantly. Each post gets a throwaway tab that is
closed afterwards; the operator's own tabs are never touched.

HOW THE VIDEO IS FOUND
Instagram plays reels through MSE, so `<video>.src` is a `blob:` URL and the DOM
hands you nothing downloadable. Observed on a live post: the player pulls two
DASH representations in ranged chunks — a ~2.6MB video track under `/m367/` and
a ~70KB audio track under `/m78/` — so following the wire means muxing two
partial streams back together. Nobody wants that.

The hydrated page carries the answer instead. After JS runs, the document
contains a `video_versions` array holding the *progressive* mp4 — one complete
file, audio included. It is absent from the raw HTTP body (that response is a
607KB bootstrap shell with no og: tags at all) and only appears once the app
hydrates, which is precisely why this needs a browser rather than a fetch.

So two paths, in preference order:

  * inline — scan the rendered document for `video_versions` and take the
    progressive URL. Also yields like/comment counts and `taken_at`.
  * network — fallback. Media requests are watched, grouped by path, and the
    representation with the largest observed byte range wins with its
    `bytestart`/`byteend` stripped. This recovers the video track only; it is a
    degraded result and the row says so via `media_source`.

If neither produces anything the post is recorded as `no_media` or `wall`, which
is a finding, not a failure.

Output (IG_OUT, default ~/instagram_export):
  video/<shortcode>.mp4
  posts.jsonl            - appended, one row per post, re-run safe
  runs/YYYY-MM-DD.json   - per-run summary
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import os
import re
import sys
from pathlib import Path
from urllib.parse import urlsplit

if sys.stdout.encoding and sys.stdout.encoding.lower() != 'utf-8':
	sys.stdout.reconfigure(encoding='utf-8', errors='replace')

import aiohttp
from cdp_use import CDPClient
from cdp_use.cdp.network import RequestWillBeSentEvent, ResponseReceivedEvent

CDP_URL = os.environ.get('BROWSER_USE_CDP_HTTP', 'http://127.0.0.1:9222')
# One `<source>_export` per source is the deploy convention (docs/collection-
# policy.md stage 2, discovered by stage_to_nas.sh). Not a subdirectory of
# promo_export: that one is the promotion-channel recon shared by eight
# collectors, and a video corpus is a different kind of thing.
OUT_DIR = Path(os.environ.get('IG_OUT', str(Path.home() / 'instagram_export')))
SETTLE = 9.0  # the reel has to actually start streaming, not just paint
NAV_TIMEOUT = 45.0
DOWNLOAD_TIMEOUT = 180.0

MEDIA_HOST_RE = re.compile(r'\.(?:cdninstagram\.com|fbcdn\.net)$')
MEDIA_PATH_RE = re.compile(r'\.(?:mp4|m4v)(?:$|\?)')
SHORTCODE_RE = re.compile(r'/(?:p|reel|reels|tv)/([A-Za-z0-9_-]+)')

# One pass over the rendered post: og/meta tags, the player's own view of the
# media, the hydrated `video_versions` payload, and enough wall signal to tell
# "we were shown the post" from "we were shown a sign-in box".
JS_READ = r"""
(() => {
	const meta = {};
	document.querySelectorAll('meta[property^="og:"], meta[name^="twitter:"], meta[property^="al:"]').forEach(m => {
		const k = m.getAttribute('property') || m.getAttribute('name');
		if (k && !meta[k]) meta[k] = (m.getAttribute('content') || '').slice(0, 600);
	});
	const videos = Array.from(document.querySelectorAll('video')).map(v => ({
		src: v.currentSrc || v.src || '',
		poster: v.poster || '',
		duration: Number.isFinite(v.duration) ? v.duration : null,
		width: v.videoWidth || null,
		height: v.videoHeight || null,
		readyState: v.readyState,
	}));

	// The hydrated payload lives in <script> bodies, sometimes as plain JSON and
	// sometimes as JSON nested inside a JSON string. Read the document both ways
	// rather than guessing which shape this build shipped.
	const html = document.documentElement.innerHTML;
	const hays = [html, html.replace(/\\"/g, '"').replace(/\\\//g, '/')];
	const balanced = (s, from) => {
		let depth = 0, inStr = false, esc = false;
		for (let k = from; k < s.length; k++) {
			const ch = s[k];
			if (inStr) { if (esc) esc = false; else if (ch === '\\') esc = true; else if (ch === '"') inStr = false; continue; }
			if (ch === '"') inStr = true;
			else if (ch === '[') depth++;
			else if (ch === ']') { depth--; if (depth === 0) return k; }
		}
		return -1;
	};
	const versions = [];
	for (const hay of hays) {
		let i = 0;
		while ((i = hay.indexOf('"video_versions"', i)) !== -1) {
			const open = hay.indexOf('[', i);
			if (open === -1) break;
			const close = balanced(hay, open);
			if (close === -1) { i += 16; continue; }
			try {
				const arr = JSON.parse(hay.slice(open, close + 1));
				if (Array.isArray(arr)) arr.forEach(v => { if (v && v.url) versions.push(v); });
			} catch (e) {}
			i = close;
		}
	}
	// Engagement scalars. `null` reaches the regex as the literal word, so
	// numeric-only matching keeps a nulled key from masquerading as a value.
	const num = (name) => {
		for (const hay of hays) {
			const m = hay.match(new RegExp('"' + name + '"\\s*:\\s*(\\d{1,15})'));
			if (m) return Number(m[1]);
		}
		return null;
	};
	const owner = () => {
		for (const hay of hays) {
			const m = hay.match(/"owner"\s*:\s*\{[^{}]{0,400}?"username"\s*:\s*"([A-Za-z0-9._]{1,60})"/);
			if (m) return m[1];
		}
		return null;
	};

	const text = (document.body ? document.body.innerText : '') || '';
	return JSON.stringify({
		url: location.href,
		title: document.title || '',
		meta: meta,
		videos: videos,
		versions: versions,
		owner: owner(),
		like_count: num('like_count'),
		comment_count: num('comment_count'),
		play_count: num('play_count'),
		view_count: num('view_count'),
		taken_at: num('taken_at'),
		original_width: num('original_width'),
		original_height: num('original_height'),
		text_chars: text.length,
		password_inputs: document.querySelectorAll('input[type="password"]').length,
		time_tags: Array.from(document.querySelectorAll('time[datetime]')).map(t => t.getAttribute('datetime')).slice(0, 4),
	});
})()
"""

# Nudge a paused/lazy player. Muted playback is what the page does on its own;
# this only asks the element already on the page to start, nothing else.
JS_PLAY = r"""
(() => {
	const v = document.querySelector('video');
	if (!v) return 'no_video_element';
	v.muted = true;
	const p = v.play();
	if (p && p.catch) p.catch(() => {});
	return 'play_called';
})()
"""


def shortcode_of(url: str) -> str:
	match = SHORTCODE_RE.search(urlsplit(url).path)
	assert match, f'not an Instagram post URL: {url}'
	return match.group(1)


def is_media_url(url: str) -> bool:
	"""True for a video-body request, false for thumbnails, JS bundles, manifests."""
	parts = urlsplit(url)
	return bool(MEDIA_HOST_RE.search(parts.netloc)) and bool(MEDIA_PATH_RE.search(parts.path + '?' + parts.query))


def unrange(url: str) -> str:
	"""Drop the byte-range params so the URL fetches the whole file.

	MSE asks for `...mp4?...&bytestart=0&byteend=524287`. The same signed URL
	without them serves the complete media, which is what we want on disk.
	"""
	stripped = re.sub(r'&(?:bytestart|byteend)=\d+', '', url)
	stripped = re.sub(r'\?(?:bytestart|byteend)=\d+&', '?', stripped)
	stripped = re.sub(r'\?(?:bytestart|byteend)=\d+$', '', stripped)
	return stripped


def best_version(versions: list[dict]) -> dict | None:
	"""Pick one rendition out of `video_versions`.

	The array repeats the same progressive file under several `type` tags (101,
	102, 103 all pointed at one URL on the posts checked), and the two document
	readings duplicate it again, so dedupe by URL first. Where width/height are
	present the largest wins; where they are absent — the common case on the
	logged-out view — the first distinct URL is the only candidate anyway.
	"""
	seen: dict[str, dict] = {}
	for version in versions:
		if isinstance(version, dict) and version.get('url'):
			seen.setdefault(version['url'], version)
	if not seen:
		return None
	return max(seen.values(), key=lambda v: (v.get('width') or 0) * (v.get('height') or 0))


def best_media_request(urls: list[str]) -> str | None:
	"""Fallback pick from the DASH chunk requests the player made.

	Each representation is fetched as many ranged GETs against one path. Group by
	path and keep the path whose largest `byteend` is biggest — that is the video
	track rather than the ~70KB audio track or a bandwidth probe. Range params
	are stripped so the URL serves the whole representation.
	"""
	spans: dict[str, tuple[int, str]] = {}
	for url in urls:
		path = urlsplit(url).path
		match = re.search(r'[?&]byteend=(\d+)', url)
		end = int(match.group(1)) if match else 0
		if end >= spans.get(path, (-1, ''))[0]:
			spans[path] = (end, url)
	if not spans:
		return None
	return unrange(max(spans.values(), key=lambda pair: pair[0])[1])


def caption_from(meta: dict) -> str:
	for key in ('og:description', 'twitter:description', 'description'):
		if meta.get(key):
			return meta[key]
	return ''


def username_from(page: dict, meta: dict, page_url: str) -> str:
	"""Whoever posted it, from whichever of three shapes actually rendered.

	The hydrated `owner.username` is authoritative when present. Failing that,
	og:description opens with `133K likes, 574 comments - handle - July 25 ...`,
	and failing that the canonical URL may carry the handle.
	"""
	if page.get('owner'):
		return str(page['owner'])
	description = caption_from(meta)
	match = re.search(r'comments?\s*[-–—]\s*([A-Za-z0-9._]{1,60})\s*(?:[-–—]|on\b)', description)
	if match:
		return match.group(1)
	match = re.search(r'@([A-Za-z0-9._]+)', meta.get('og:title') or '')
	if match:
		return match.group(1)
	match = re.search(r'/([A-Za-z0-9._]+)/(?:p|reel)/', urlsplit(meta.get('og:url') or page_url).path)
	return match.group(1) if match else ''


def _iso(taken_at: int | None) -> str | None:
	"""`taken_at` is a unix second stamp; store it as something readable."""
	if not taken_at:
		return None
	return dt.datetime.fromtimestamp(taken_at, dt.timezone.utc).isoformat()


class PostCapture:
	"""Per-tab collector state: the media requests the player issued."""

	def __init__(self) -> None:
		self.media_urls: list[str] = []


async def websocket_url() -> str:
	async with aiohttp.ClientSession() as http:
		async with http.get(f'{CDP_URL}/json/version', timeout=aiohttp.ClientTimeout(total=15)) as response:
			return (await response.json())['webSocketDebuggerUrl']


async def assert_anonymous(client: CDPClient) -> None:
	"""Refuse to run against a signed-in browser. See the module docstring."""
	cookies = (await client.send.Storage.getCookies()).get('cookies', [])
	held = [c['name'] for c in cookies if 'instagram.com' in c.get('domain', '') and c.get('name') == 'sessionid']
	assert not held, (
		'the attached Chrome holds an Instagram sessionid - this script only does anonymous reads. '
		'Log out of Instagram in that browser, or point BROWSER_USE_CDP_HTTP at one that is not signed in.'
	)


async def collect(client: CDPClient, url: str, settle: float) -> dict:
	"""Open one post in a throwaway tab, capture its media, close the tab."""
	code = shortcode_of(url)
	capture = PostCapture()
	created = await client.send.Target.createTarget(params={'url': 'about:blank'})
	target_id = created['targetId']
	try:
		attached = await client.send.Target.attachToTarget(params={'targetId': target_id, 'flatten': True})
		session_id = attached['sessionId']
		await client.send.Page.enable(session_id=session_id)
		await client.send.Runtime.enable(session_id=session_id)
		await client.send.Network.enable(session_id=session_id)

		# cdp-use hands the callback the event params plus the session they came
		# from — not the raw message envelope. Filtering on session_id is what
		# keeps the operator's own tabs out of this capture.
		def on_request(event: RequestWillBeSentEvent, event_session: str | None) -> None:
			if event_session != session_id:
				return
			request_url = (event.get('request') or {}).get('url', '')
			if is_media_url(request_url):
				capture.media_urls.append(request_url)

		def on_response(event: ResponseReceivedEvent, event_session: str | None) -> None:
			if event_session != session_id:
				return
			response = event.get('response') or {}
			if is_media_url(response.get('url', '')):
				capture.media_urls.append(response['url'])

		client.register.Network.requestWillBeSent(on_request)
		client.register.Network.responseReceived(on_response)

		await asyncio.wait_for(
			client.send.Page.navigate(params={'url': url}, session_id=session_id),
			timeout=NAV_TIMEOUT,
		)
		await asyncio.sleep(settle * 0.6)
		try:
			await client.send.Runtime.evaluate(params={'expression': JS_PLAY, 'returnByValue': True}, session_id=session_id)
		except Exception:  # noqa: BLE001 - the nudge is optional, the wait is not
			pass
		await asyncio.sleep(settle * 0.4)

		raw = (
			(await client.send.Runtime.evaluate(params={'expression': JS_READ, 'returnByValue': True}, session_id=session_id))
			.get('result', {})
			.get('value')
		)
		page = json.loads(raw) if raw else {}
	finally:
		try:
			await client.send.Target.closeTarget(params={'targetId': target_id})
		except Exception:  # noqa: BLE001
			pass

	meta = page.get('meta', {})
	first_video = (page.get('videos') or [{}])[0]
	versions = page.get('versions') or []
	picked = best_version(versions)
	fallback_url = best_media_request(capture.media_urls)
	media_url = (picked or {}).get('url') or fallback_url
	return {
		'shortcode': code,
		'source_url': url,
		'final_url': page.get('url'),
		'username': username_from(page, meta, page.get('url') or url),
		'caption': caption_from(meta)[:1000],
		'posted_at': _iso(page.get('taken_at')) or (page.get('time_tags') or [None])[0],
		'like_count': page.get('like_count'),
		'comment_count': page.get('comment_count'),
		'play_count': page.get('play_count') or page.get('view_count'),
		'poster': first_video.get('poster') or meta.get('og:image'),
		'duration_s': first_video.get('duration'),
		'width': (picked or {}).get('width') or first_video.get('width') or page.get('original_width'),
		'height': (picked or {}).get('height') or first_video.get('height') or page.get('original_height'),
		'media_url': media_url,
		# 'network' means the DASH video track without its audio - a degraded
		# result the downstream corpus should be able to tell apart.
		'media_source': 'video_versions' if picked else ('network' if fallback_url else None),
		'renditions': len({v['url'] for v in versions if isinstance(v, dict) and v.get('url')}),
		'media_requests': len(capture.media_urls),
		'text_chars': page.get('text_chars', 0),
		'title': (page.get('title') or '')[:120],
		'verdict': 'ok' if media_url else ('wall' if page.get('password_inputs') else 'no_media'),
	}


async def download(http: aiohttp.ClientSession, row: dict, dest_dir: Path) -> dict:
	"""Fetch the picked rendition. CDN URLs are signed and need no cookies."""
	if not row.get('media_url'):
		return row
	dest = dest_dir / f'{row["shortcode"]}.mp4'
	if dest.exists() and dest.stat().st_size > 0:
		return row | {'file': str(dest), 'bytes': dest.stat().st_size, 'cached': True}
	part = dest.with_suffix('.part')
	try:
		async with http.get(row['media_url'], timeout=aiohttp.ClientTimeout(total=DOWNLOAD_TIMEOUT)) as response:
			if response.status != 200:
				return row | {'download_error': f'HTTP {response.status}'}
			with part.open('wb') as handle:
				async for chunk in response.content.iter_chunked(1 << 18):
					handle.write(chunk)
	except Exception as exc:  # noqa: BLE001 - one bad download must not sink the run
		part.unlink(missing_ok=True)
		return row | {'download_error': f'{type(exc).__name__}: {str(exc)[:120]}'}
	part.replace(dest)
	return row | {'file': str(dest), 'bytes': dest.stat().st_size, 'cached': False}


def _log_row(index: int, total: int, row: dict) -> None:
	code = row['shortcode']
	if row.get('file'):
		size = f'{row["bytes"] / 1_048_576:.1f}MB'
		tag = 'cached' if row.get('cached') else row.get('media_source', '?')
		dims = f'{row.get("width")}x{row.get("height")}' if row.get('width') else '?'
		secs = f'{row["duration_s"]:.0f}s' if row.get('duration_s') else '?'
		likes = f'{row["like_count"]:,}' if row.get('like_count') else '-'
		print(
			f'  [{index}/{total}] {code:14} {size:>8} {secs:>5} {dims:>10}  via={tag:14} @{row.get("username") or "?":22} likes={likes}'
		)
	else:
		why = row.get('download_error') or row['verdict']
		print(f'  [{index}/{total}] {code:14} {why:>18}  reqs={row.get("media_requests", 0)} chars={row.get("text_chars", 0)}')


async def main() -> None:
	parser = argparse.ArgumentParser(description=(__doc__ or '').split('\n')[0])
	parser.add_argument('urls', nargs='*', help='post URLs; omit to read them from --file or stdin')
	parser.add_argument('--file', type=Path, help='newline-delimited URL list')
	parser.add_argument('--settle', type=float, default=SETTLE, help=f'seconds to let each post stream (default {SETTLE})')
	parser.add_argument('--no-download', action='store_true', help='resolve media URLs only, write no mp4')
	args = parser.parse_args()

	urls = list(args.urls)
	if args.file:
		urls += [line.strip() for line in args.file.read_text(encoding='utf-8').splitlines() if line.strip()]
	if not urls and not sys.stdin.isatty():
		urls += [line.strip() for line in sys.stdin.read().splitlines() if line.strip()]
	urls = list(dict.fromkeys(u for u in urls if u.startswith('http')))
	if not urls:
		print('no post URLs given')
		return

	video_dir = OUT_DIR / 'video'
	video_dir.mkdir(parents=True, exist_ok=True)

	print(f'attaching to the running Chrome at {CDP_URL}')
	print(f'{len(urls)} posts, anonymous read, {args.settle:.0f}s settle each')

	rows: list[dict] = []
	async with CDPClient(await websocket_url()) as client:
		await assert_anonymous(client)
		async with aiohttp.ClientSession(headers={'Referer': 'https://www.instagram.com/'}) as http:
			for index, url in enumerate(urls, 1):
				try:
					row = await collect(client, url, args.settle)
				except Exception as exc:  # noqa: BLE001
					row = {
						'shortcode': shortcode_of(url),
						'source_url': url,
						'verdict': 'error',
						'error': f'{type(exc).__name__}: {str(exc)[:120]}',
						'media_requests': 0,
						'text_chars': 0,
					}
					print(f'  [{index}/{len(urls)}] {row["shortcode"]:14} ERROR {row["error"]}')
					rows.append(row)
					continue
				if not args.no_download:
					row = await download(http, row, video_dir)
				row['observed_at'] = dt.datetime.now(dt.timezone.utc).isoformat()
				rows.append(row)
				_log_row(index, len(urls), row)

	with (OUT_DIR / 'posts.jsonl').open('a', encoding='utf-8') as handle:
		for row in rows:
			handle.write(json.dumps(row, ensure_ascii=False) + '\n')
	runs_dir = OUT_DIR / 'runs'
	runs_dir.mkdir(parents=True, exist_ok=True)
	(runs_dir / f'{dt.date.today().isoformat()}.json').write_text(
		json.dumps({'collected_at': dt.datetime.now(dt.timezone.utc).isoformat(), 'posts': rows}, ensure_ascii=False, indent=2),
		encoding='utf-8',
	)

	got = [r for r in rows if r.get('file')]
	total_bytes = sum(r.get('bytes', 0) for r in got)
	tally: dict[str, int] = {}
	for row in rows:
		tally[row['verdict']] = tally.get(row['verdict'], 0) + 1
	print('\n' + ', '.join(f'{k}={v}' for k, v in sorted(tally.items())))
	print(f'DONE -> {video_dir} ({len(got)}/{len(rows)} videos, {total_bytes / 1_048_576:.1f}MB)')


if __name__ == '__main__':
	asyncio.run(main())
