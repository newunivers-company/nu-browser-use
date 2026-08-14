"""Corporate and production-studio site watch — change detection over time.

When the promotion registry ruled LinkedIn out (robots prohibits automated
access, and company pages sit behind an authwall), the corporate-strategy
signal had to come from somewhere else. This is that substitute: the companies'
own sites, which state partnerships, launches, slate additions and positioning
first-party, and which are plain HTML.

Nine registry channels of type `corporate` or `blog` were marked collectible
and had no collector at all — the largest uncovered block in the registry.

WHAT IS DIFFED, AND WHY NOT THE WHOLE PAGE
A raw body hash flags something on nearly every run: rotating hero copy, a
cache-busted asset name, a cookie-banner nonce. That trains you to ignore the
alert. Instead this extracts the parts that carry meaning — headings, anchor
text, meta description, and the paths of same-site links — and diffs those.
A new heading or a new internal page is a real editorial act; a changed script
hash is not. The raw hash is still recorded, so a page that changed *only*
outside the meaningful set is visible as `cosmetic_only` rather than silently
dropped.

Output (PROMO_OUT, default ~/promo_export):
  snapshots/YYYY-MM-DD/corpsites.json - per-site extraction
  corpsite_changes.jsonl              - appended, only meaningful diffs
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import hashlib
import html
import json
import os
import re
import sys
from pathlib import Path
from urllib.parse import urlsplit

if sys.stdout.encoding and sys.stdout.encoding.lower() != 'utf-8':
	sys.stdout.reconfigure(encoding='utf-8', errors='replace')

import aiohttp

sys.path.insert(0, str(Path(__file__).resolve().parent))
from registry.models import AccessTier, ChannelType, load_registry  # noqa: E402

OUT_DIR = Path(os.environ.get('PROMO_OUT', str(Path.home() / 'promo_export')))
UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36'
HEADERS = {
	'User-Agent': UA,
	'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
	'Accept-Language': 'en-US,en;q=0.9,ko;q=0.8',
	'Upgrade-Insecure-Requests': '1',
}
WATCHED_TYPES = {ChannelType.CORPORATE, ChannelType.BLOG, ChannelType.AFFILIATE}
CONCURRENCY = 5
TAG_RE = re.compile(r'<[^>]+>')
SCRIPT_RE = re.compile(r'<(script|style|noscript)\b.*?</\1>', re.S | re.I)
HEADING_RE = re.compile(r'<h([1-3])\b[^>]*>(.*?)</h\1>', re.S | re.I)
ANCHOR_RE = re.compile(r'<a\b[^>]*href="([^"]+)"[^>]*>(.*?)</a>', re.S | re.I)
TITLE_RE = re.compile(r'<title[^>]*>(.*?)</title>', re.S | re.I)
DESC_RE = re.compile(r'<meta[^>]+name="description"[^>]+content="([^"]*)"', re.I)
# Pages that read as announcements rather than furniture.
NEWS_HINT_RE = re.compile(r'(news|press|blog|announce|release|insight|stories|updates|media|careers|jobs)', re.I)


def clean(fragment: str) -> str:
	return html.unescape(TAG_RE.sub(' ', fragment)).replace('\xa0', ' ').strip()


def extract(body: str, base_url: str) -> dict:
	"""Pull the meaningful surface out of one page."""
	stripped = SCRIPT_RE.sub(' ', body)
	host = urlsplit(base_url).netloc.removeprefix('www.')

	headings: list[str] = []
	for _, fragment in HEADING_RE.findall(stripped):
		text = re.sub(r'\s+', ' ', clean(fragment))
		if 2 < len(text) <= 160 and text not in headings:
			headings.append(text)

	internal: set[str] = set()
	news_links: list[dict] = []
	for href, fragment in ANCHOR_RE.findall(stripped):
		parts = urlsplit(href)
		if parts.netloc and parts.netloc.removeprefix('www.') != host:
			continue
		path = (parts.path or '/').rstrip('/') or '/'
		if len(path) > 120:
			continue
		internal.add(path)
		label = re.sub(r'\s+', ' ', clean(fragment))
		if label and NEWS_HINT_RE.search(path + ' ' + label) and len(label) <= 120:
			entry = {'path': path, 'label': label}
			if entry not in news_links:
				news_links.append(entry)

	title = TITLE_RE.search(stripped)
	description = DESC_RE.search(body)
	text = re.sub(r'\s+', ' ', clean(stripped))
	return {
		'title': clean(title.group(1))[:200] if title else None,
		'description': html.unescape(description.group(1))[:400] if description else None,
		'headings': headings[:60],
		'internal_paths': sorted(internal)[:250],
		'news_links': news_links[:40],
		'text_chars': len(text),
		# Hash of the meaningful surface only — this is what "changed" means here.
		'signal_sha256': hashlib.sha256(json.dumps({'h': headings[:60], 'p': sorted(internal)[:250], 'd': description.group(1) if description else None}, ensure_ascii=False).encode('utf-8')).hexdigest(),
		'raw_sha256': hashlib.sha256(body.encode('utf-8', 'replace')).hexdigest(),
	}


async def fetch(session: aiohttp.ClientSession, url: str, semaphore: asyncio.Semaphore) -> dict:
	async with semaphore:
		try:
			async with session.get(url, timeout=aiohttp.ClientTimeout(total=30), allow_redirects=True) as response:
				body = await response.text(errors='replace')
				if response.status != 200:
					return {'result': 'http_error', 'status': response.status, 'final_url': str(response.url)}
				return {'result': 'ok', 'status': 200, 'final_url': str(response.url), **extract(body, str(response.url))}
		except Exception as exc:  # noqa: BLE001
			return {'result': 'unreachable', 'error': type(exc).__name__}


def previous(today: str) -> dict[str, dict]:
	root = OUT_DIR / 'snapshots'
	if not root.exists():
		return {}
	earlier = sorted(p for p in root.iterdir() if p.is_dir() and p.name < today and (p / 'corpsites.json').exists())
	if not earlier:
		return {}
	rows = json.loads((earlier[-1] / 'corpsites.json').read_text(encoding='utf-8'))['sites']
	return {row['url']: row for row in rows}


def diff(before: dict | None, row: dict, now: str) -> list[dict]:
	"""Only report editorial change; cosmetic churn is labelled, not alerted."""
	url = row['url']
	if before is None:
		return [{'url': url, 'owner': row['owner'], 'change': 'first_seen', 'observed_at': now}]
	if row.get('result') != 'ok' or before.get('result') != 'ok':
		if row.get('result') != before.get('result'):
			return [{'url': url, 'owner': row['owner'], 'change': 'availability', 'from': before.get('result'), 'to': row.get('result'), 'observed_at': now}]
		return []

	changes: list[dict] = []
	new_headings = [h for h in row['headings'] if h not in before.get('headings', [])]
	new_paths = [p for p in row['internal_paths'] if p not in before.get('internal_paths', [])]
	gone_paths = [p for p in before.get('internal_paths', []) if p not in row['internal_paths']]
	if new_headings:
		changes.append({'url': url, 'owner': row['owner'], 'change': 'new_headings', 'values': new_headings[:12], 'observed_at': now})
	if new_paths:
		changes.append({'url': url, 'owner': row['owner'], 'change': 'new_pages', 'values': new_paths[:20], 'observed_at': now})
	if gone_paths:
		changes.append({'url': url, 'owner': row['owner'], 'change': 'removed_pages', 'values': gone_paths[:20], 'observed_at': now})
	if before.get('description') != row.get('description'):
		changes.append({'url': url, 'owner': row['owner'], 'change': 'description', 'from': before.get('description'), 'to': row.get('description'), 'observed_at': now})
	if not changes and before.get('raw_sha256') != row.get('raw_sha256'):
		changes.append({'url': url, 'owner': row['owner'], 'change': 'cosmetic_only', 'observed_at': now})
	return changes


async def main() -> None:
	parser = argparse.ArgumentParser()
	parser.add_argument('--only', nargs='*', help='restrict to these brand/company ids')
	args = parser.parse_args()

	registry = load_registry()
	channels = [
		c for c in registry.collectible()
		if c.channel_type in WATCHED_TYPES and c.access_tier is AccessTier.KEYLESS_HTTP
	]
	if args.only:
		wanted = set(args.only)
		channels = [c for c in channels if c.brand in wanted or c.company in wanted]
	if not channels:
		print('no collectible corporate/blog channels in the registry')
		return

	today = dt.date.today().isoformat()
	now = dt.datetime.now(dt.timezone.utc).isoformat()
	snap_dir = OUT_DIR / 'snapshots' / today
	snap_dir.mkdir(parents=True, exist_ok=True)

	semaphore = asyncio.Semaphore(CONCURRENCY)
	async with aiohttp.ClientSession(headers=HEADERS) as session:
		fetched = await asyncio.gather(*(fetch(session, str(c.url), semaphore) for c in channels))

	rows = [
		{'url': str(c.url), 'owner': c.brand or c.company, 'channel_type': c.channel_type.value, **payload}
		for c, payload in zip(channels, fetched)
	]
	before = previous(today)
	changes = [change for row in rows for change in diff(before.get(row['url']), row, now)]

	(snap_dir / 'corpsites.json').write_text(json.dumps({'collected_at': now, 'sites': rows}, ensure_ascii=False, indent=2), encoding='utf-8')
	if changes:
		with (OUT_DIR / 'corpsite_changes.jsonl').open('a', encoding='utf-8') as handle:
			for change in changes:
				handle.write(json.dumps(change, ensure_ascii=False) + '\n')

	ok = [r for r in rows if r.get('result') == 'ok']
	print(f'{len(ok)}/{len(rows)} sites read')
	for row in sorted(rows, key=lambda r: r['owner'] or ''):
		if row.get('result') != 'ok':
			print(f'  {row["owner"]:22} {row.get("result")} {row.get("status") or row.get("error")}')
			continue
		print(f'  {row["owner"]:22} headings={len(row["headings"]):<3} pages={len(row["internal_paths"]):<3} news_links={len(row["news_links"]):<3} {str(row["title"])[:44]}')

	editorial = [c for c in changes if c['change'] not in ('first_seen', 'cosmetic_only')]
	print(f'\nchanges: {len(editorial)} editorial, {sum(1 for c in changes if c["change"] == "cosmetic_only")} cosmetic-only, {sum(1 for c in changes if c["change"] == "first_seen")} first-seen')
	for change in editorial[:15]:
		print(f'  {change["owner"]}: {change["change"]} {str(change.get("values") or change.get("to"))[:90]}')
	print(f'DONE -> {snap_dir / "corpsites.json"}')


if __name__ == '__main__':
	asyncio.run(main())
