"""Resumable collection loop over the catalog, in order.

source_harvest.py answered "how is each source collectable". This acts on that:
it walks the sources that are both robots-clean and offer a real machine-
readable channel, collects the item metadata that channel exposes, and
checkpoints after every source so the run can be stopped and resumed.

Per-source channel handling:
  feed              RSS/Atom entries -> title, link, dates, author, categories
  sitemap           URL inventory    -> loc, lastmod, changefreq, priority
  json_ld           typed entities   -> name, url, dates, numeric properties
  framework_state   __NEXT_DATA__ / __NUXT_DATA__ mined for item-shaped nodes

METADATA ONLY — WHAT IS DROPPED AND WHY
Feeds routinely carry the whole article in `content:encoded` or `<content>`,
and many of these sources are news organisations, publishers and prompt
libraries. Those fields are discarded before anything is written; so are
JSON-LD `articleBody`, `text` and `description` beyond a short identifying
snippet. What is kept is what a catalog needs to name and locate an item —
title, URL, timestamp, author, category. The loop indexes works; it does not
reproduce them.

Politeness: one host at a time, a delay between requests, a per-source item cap,
and conditional requests via stored ETag/Last-Modified so a resumed run asks
for changes rather than refetching.

Output (LOOP_OUT, default ~/source_loop):
  state.json                 - checkpoint: per-source cursor, etag, counts
  items/<source_id>.jsonl    - collected item metadata, appended, deduped by URL
  run_log.jsonl              - one record per source per pass
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import gzip
import json
import os
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.parse import urljoin, urlsplit

if sys.stdout.encoding and sys.stdout.encoding.lower() != 'utf-8':
	sys.stdout.reconfigure(encoding='utf-8', errors='replace')

import aiohttp

OUT_DIR = Path(os.environ.get('LOOP_OUT', str(Path.home() / 'source_loop')))
HARVEST_DIR = Path(os.environ.get('HARVEST_OUT', str(Path.home() / 'source_harvest')))
UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36'
HEADERS = {
	'User-Agent': UA,
	'Accept': 'application/rss+xml, application/atom+xml, application/xml, text/html;q=0.9, */*;q=0.8',
	'Accept-Language': 'en-US,en;q=0.9,ko;q=0.8',
}
DELAY = 1.0
ITEM_CAP = 20000
SNIPPET = 200
SITEMAP_CHILDREN = 40  # bounded: a whole sitemap tree is a crawl, not a read
FEEDS_PER_SOURCE = 6
# Framework state blobs can embed article bodies, so any string longer than this
# is dropped outright rather than snipped — a body has no business in a catalog.
STATE_STRING_MAX = 200
COLLECTABLE = {'feed', 'sitemap', 'json_ld', 'framework_state'}
# robots.txt absent means no rule was published, which under RFC 9309 is not a
# restriction. Treating that as a block excluded sources nobody asked us to skip.
PERMITTED_ROBOTS = {'allow', 'unknown'}
# Full-text carriers. Dropped before write — see the module docstring.
BODY_FIELDS = {'content', 'content:encoded', 'encoded', 'articlebody', 'text', 'body', 'summary_detail'}
LD_RE = re.compile(r'<script[^>]*application/ld\+json[^>]*>(.*?)</script>', re.S | re.I)
NEXT_RE = re.compile(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S)
NUXT_RE = re.compile(r'<script[^>]*id="__NUXT_DATA__"[^>]*>(.*?)</script>', re.S)
TITLE_KEYS = ('title', 'name', 'headline', 'bookName', 'seriesName', 'displayName')
URL_KEYS = ('url', 'link', 'href', 'permalink', 'slug', 'canonicalUrl')
NS = {'atom': 'http://www.w3.org/2005/Atom', 'sm': 'http://www.sitemaps.org/schemas/sitemap/0.9'}
SITEMAP_DIRECTIVE_RE = re.compile(r'(?im)^\s*sitemap:\s*(\S+)')


async def declared_sitemaps(session: aiohttp.ClientSession, base: str) -> list[str]:
	"""Sitemaps the site points at from robots.txt.

	Guessing /sitemap.xml and giving up on a 404 was leaving inventories on the
	table: 14 otherwise channel-less sources publish a `Sitemap:` directive
	naming a path we never tried. Reading the pointer the site published is
	both more complete and more respectful than probing for one.
	"""
	parts = urlsplit(base)
	try:
		async with session.get(f'{parts.scheme}://{parts.netloc}/robots.txt', timeout=aiohttp.ClientTimeout(total=20)) as response:
			if response.status != 200:
				return []
			body = await response.text(errors='replace')
	except Exception:  # noqa: BLE001
		return []
	return list(dict.fromkeys(SITEMAP_DIRECTIVE_RE.findall(body)))[:4]


def snippet(value: object) -> str | None:
	if not isinstance(value, str):
		return None
	cleaned = re.sub(r'<[^>]+>', ' ', value)
	cleaned = re.sub(r'\s+', ' ', cleaned).strip()
	return cleaned[:SNIPPET] or None


def _text(node: ET.Element | None) -> str | None:
	return (node.text or '').strip() or None if node is not None else None


def parse_feed(body: str, base: str) -> list[dict]:
	"""RSS or Atom entries, reduced to identifying metadata."""
	try:
		root = ET.fromstring(body.encode('utf-8', 'replace'))
	except ET.ParseError:
		return []
	items: list[dict] = []
	# RSS
	for node in root.iter('item'):
		link = _text(node.find('link'))
		items.append({
			'title': _text(node.find('title')),
			'url': urljoin(base, link) if link else None,
			'published': _text(node.find('pubDate')),
			'author': _text(node.find('author')) or _text(node.find('{http://purl.org/dc/elements/1.1/}creator')),
			'categories': [c.text.strip() for c in node.findall('category') if c.text],
			'snippet': snippet(_text(node.find('description'))),
		})
	# Atom
	for node in root.findall('atom:entry', NS):
		link_node = node.find('atom:link', NS)
		href = link_node.get('href') if link_node is not None else None
		items.append({
			'title': _text(node.find('atom:title', NS)),
			'url': urljoin(base, href) if href else None,
			'published': _text(node.find('atom:published', NS)) or _text(node.find('atom:updated', NS)),
			'author': _text(node.find('atom:author/atom:name', NS)),
			'categories': [c.get('term') for c in node.findall('atom:category', NS) if c.get('term')],
			'snippet': snippet(_text(node.find('atom:summary', NS))),
		})
	return [i for i in items if i.get('url')]


def decompress(payload: bytes) -> bytes:
	"""Gunzip when the bytes are a gzip member.

	Sitemap indexes routinely point at `.xml.gz` children — geonames alone has
	268 of them. Those arrive as gzip *files*, not gzip transfer-encoding, so
	the HTTP layer hands them over compressed, the XML parse fails, and the
	source silently reports zero. Detected by magic number rather than by file
	extension, since the extension lies often enough.
	"""
	if payload[:2] == bytes((0x1F, 0x8B)):
		try:
			return gzip.decompress(payload)
		except (OSError, EOFError):
			return payload
	return payload


def _local(element: ET.Element) -> str:
	"""Tag without its namespace — some sitemaps declare none, or a private one."""
	return element.tag.rsplit('}', 1)[-1]


def _child_text(node: ET.Element, name: str) -> str | None:
	for child in node:
		if _local(child) == name:
			return (child.text or '').strip() or None
	return None


def parse_sitemap(payload: bytes | str) -> tuple[list[dict], list[str]]:
	"""(url entries, nested sitemap urls). Namespace-agnostic, gzip-aware."""
	raw = payload.encode('utf-8', 'replace') if isinstance(payload, str) else decompress(payload)
	try:
		root = ET.fromstring(raw)
	except ET.ParseError:
		return [], []
	nested: list[str] = []
	entries: list[dict] = []
	for node in root:
		tag = _local(node)
		if tag == 'sitemap':
			loc = _child_text(node, 'loc')
			if loc:
				nested.append(loc)
		elif tag == 'url':
			loc = _child_text(node, 'loc')
			if not loc:
				continue
			entries.append({
				'url': loc,
				'published': _child_text(node, 'lastmod'),
				'changefreq': _child_text(node, 'changefreq'),
				'priority': _child_text(node, 'priority'),
			})
	return entries, nested


def parse_json_ld(body: str, base: str) -> list[dict]:
	"""Typed entities, with body-bearing fields dropped."""
	items: list[dict] = []
	for block in LD_RE.findall(body)[:8]:
		try:
			payload = json.loads(block)
		except json.JSONDecodeError:
			continue
		nodes = payload if isinstance(payload, list) else [payload]
		flat: list[dict] = []
		for node in nodes:
			if isinstance(node, dict):
				graph = node.get('@graph')
				flat.extend(g for g in (graph if isinstance(graph, list) else [node]) if isinstance(g, dict))
		for node in flat:
			node_type = node.get('@type')
			url = node.get('url') or node.get('@id')
			if not isinstance(url, str):
				continue
			numeric = {
				key: value for key, value in node.items()
				if isinstance(value, (int, float)) and key.lower() not in BODY_FIELDS
			}
			items.append({
				'title': node.get('name') if isinstance(node.get('name'), str) else None,
				'url': urljoin(base, url),
				'type': node_type if isinstance(node_type, str) else None,
				'published': node.get('datePublished') if isinstance(node.get('datePublished'), str) else None,
				'updated': node.get('dateModified') if isinstance(node.get('dateModified'), str) else None,
				'snippet': snippet(node.get('description')),
				'numeric': numeric or None,
			})
	return items


def mine_state(blob: object, base: str, out: list[dict], depth: int = 0) -> None:
	"""Walk a framework state blob for item-shaped nodes.

	A node counts as an item when it carries both a title-ish and a url-ish
	key. Long strings are dropped rather than truncated: news and publisher
	sites embed whole articles in these blobs, and a catalog has no use for
	them.
	"""
	if depth > 8 or len(out) >= ITEM_CAP:
		return
	if isinstance(blob, list):
		for node in blob[:200]:
			mine_state(node, base, out, depth + 1)
		return
	if not isinstance(blob, dict):
		return
	title = next((blob[k] for k in TITLE_KEYS if isinstance(blob.get(k), str) and blob[k].strip()), None)
	url = next((blob[k] for k in URL_KEYS if isinstance(blob.get(k), str) and blob[k].strip()), None)
	if title and url and len(title) <= STATE_STRING_MAX:
		out.append({
			'title': title,
			'url': urljoin(base, url),
			'type': blob.get('@type') if isinstance(blob.get('@type'), str) else blob.get('type') if isinstance(blob.get('type'), str) else None,
			'published': next((blob[k] for k in ('datePublished', 'publishedAt', 'createdAt', 'shelfTime', 'date') if isinstance(blob.get(k), str)), None),
			'numeric': {k: v for k, v in blob.items() if isinstance(v, (int, float)) and k.lower() not in BODY_FIELDS} or None,
		})
	for value in blob.values():
		mine_state(value, base, out, depth + 1)


def parse_framework_state(body: str, base: str) -> list[dict]:
	items: list[dict] = []
	for pattern in (NEXT_RE, NUXT_RE):
		match = pattern.search(body)
		if not match:
			continue
		try:
			blob = json.loads(match.group(1))
		except json.JSONDecodeError:
			continue
		mine_state(blob, base, items)
	# Deduplicate within the page; the caller dedupes against what is on disk.
	seen: set[str] = set()
	unique = []
	for item in items:
		if item['url'] in seen:
			continue
		seen.add(item['url'])
		unique.append(item)
	return unique


def scrub(item: dict) -> dict:
	"""Last line of defence: no body-bearing key ever reaches disk."""
	clean = {k: v for k, v in item.items() if k.lower() not in BODY_FIELDS}
	assert not (set(k.lower() for k in clean) & BODY_FIELDS), 'body field leaked into output'
	return clean


def load_state() -> dict:
	path = OUT_DIR / 'state.json'
	return json.loads(path.read_text(encoding='utf-8')) if path.exists() else {'sources': {}}


def save_state(state: dict) -> None:
	OUT_DIR.mkdir(parents=True, exist_ok=True)
	(OUT_DIR / 'state.json').write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding='utf-8')


def existing_urls(source_id: str) -> set[str]:
	path = OUT_DIR / 'items' / f'{source_id}.jsonl'
	if not path.exists():
		return set()
	urls = set()
	for line in path.open(encoding='utf-8'):
		try:
			urls.add(json.loads(line)['url'])
		except Exception:  # noqa: BLE001
			continue
	return urls


async def fetch_bytes(session: aiohttp.ClientSession, url: str) -> tuple[int | None, bytes]:
	"""Raw bytes, so a gzip member is not mangled by text decoding."""
	try:
		async with session.get(url, timeout=aiohttp.ClientTimeout(total=40)) as response:
			return response.status, await response.read()
	except Exception:  # noqa: BLE001
		return None, b''


async def fetch(session: aiohttp.ClientSession, url: str, cursor: dict) -> tuple[int | None, str, dict]:
	"""Conditional GET so a resumed pass asks for changes, not the whole thing."""
	headers = {}
	if cursor.get('etag'):
		headers['If-None-Match'] = cursor['etag']
	if cursor.get('last_modified'):
		headers['If-Modified-Since'] = cursor['last_modified']
	try:
		async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=30)) as response:
			validators = {'etag': response.headers.get('ETag'), 'last_modified': response.headers.get('Last-Modified')}
			if response.status == 304:
				return 304, '', validators
			return response.status, await response.text(errors='replace'), validators
	except Exception as exc:  # noqa: BLE001
		return None, f'{type(exc).__name__}', {}


async def collect_source(session: aiohttp.ClientSession, source: dict, state: dict) -> dict:
	source_id = source['id']
	cursor = state['sources'].setdefault(source_id, {})
	channel = source['channel']
	base = source['url']
	targets: list[str]
	if channel == 'feed':
		targets = source.get('feeds', [])[:FEEDS_PER_SOURCE]
	elif channel == 'sitemap':
		parts = urlsplit(base)
		declared = await declared_sitemaps(session, base)
		targets = declared or [f'{parts.scheme}://{parts.netloc}/sitemap.xml']
	else:
		targets = [base]

	items: list[dict] = []
	statuses: list[int | None] = []
	for target in targets:
		status, body, validators = await fetch(session, target, cursor)
		statuses.append(status)
		if status == 304 or not body:
			await asyncio.sleep(DELAY)
			continue
		if status != 200:
			await asyncio.sleep(DELAY)
			continue
		if channel == 'feed':
			items.extend(parse_feed(body, target))
		elif channel == 'framework_state':
			items.extend(parse_framework_state(body, target))
		elif channel == 'sitemap':
			entries, nested = parse_sitemap(body)
			items.extend(entries)
			# One level of nesting, bounded child count: enough to reach a real
			# inventory without walking an entire site.
			for nested_url in nested[:SITEMAP_CHILDREN]:
				nested_status, nested_bytes = await fetch_bytes(session, nested_url)
				if nested_status == 200 and nested_bytes:
					items.extend(parse_sitemap(nested_bytes)[0])
				if len(items) >= ITEM_CAP:
					break
				await asyncio.sleep(DELAY)
		else:
			items.extend(parse_json_ld(body, target))
		cursor.update({k: v for k, v in validators.items() if v})
		await asyncio.sleep(DELAY)

	seen = existing_urls(source_id)
	fresh = []
	for item in items[:ITEM_CAP]:
		url = item.get('url')
		if not url or url in seen:
			continue
		seen.add(url)
		fresh.append(scrub({**item, 'source_id': source_id, 'observed_at': dt.datetime.now(dt.timezone.utc).isoformat()}))

	if fresh:
		(OUT_DIR / 'items').mkdir(parents=True, exist_ok=True)
		with (OUT_DIR / 'items' / f'{source_id}.jsonl').open('a', encoding='utf-8') as handle:
			for item in fresh:
				handle.write(json.dumps(item, ensure_ascii=False) + '\n')

	cursor.update({
		'channel': channel,
		'last_pass': dt.datetime.now(dt.timezone.utc).isoformat(),
		'last_statuses': statuses,
		'total_items': cursor.get('total_items', 0) + len(fresh),
		'passes': cursor.get('passes', 0) + 1,
	})
	return {'id': source_id, 'channel': channel, 'new_items': len(fresh), 'statuses': statuses, 'total': cursor['total_items']}


async def main() -> None:
	parser = argparse.ArgumentParser()
	parser.add_argument('--limit', type=int, help='sources per pass (the loop resumes where it stopped)')
	parser.add_argument('--only-channel', choices=sorted(COLLECTABLE))
	parser.add_argument('--reset', action='store_true', help='forget checkpoints and start the walk over')
	parser.add_argument('--no-promote', action='store_true', help='skip the robots Sitemap: promotion pass')
	args = parser.parse_args()

	dossiers_path = HARVEST_DIR / 'dossiers.json'
	if not dossiers_path.exists():
		print(f'no dossiers at {dossiers_path} — run source_harvest.py first')
		return
	dossiers = json.loads(dossiers_path.read_text(encoding='utf-8'))['sources']

	# Catalog order is preserved; only robots-clean sources with a real channel.
	queue = [d for d in dossiers if d.get('channel') in COLLECTABLE and d.get('robots') in PERMITTED_ROBOTS]
	# Sources with no channel today may still publish a Sitemap: directive; the
	# harvest only probed /sitemap.xml. Promote the ones that name a real map.
	channel_less = [d for d in dossiers if d.get('robots') in PERMITTED_ROBOTS and d.get('channel') in ('opengraph_only', 'html_only')]
	promoted: list[dict] = []
	if channel_less and not args.no_promote:
		async with aiohttp.ClientSession(headers=HEADERS) as session:
			semaphore = asyncio.Semaphore(6)

			async def check(source: dict) -> dict | None:
				async with semaphore:
					maps = await declared_sitemaps(session, source['url'])
				return {**source, 'channel': 'sitemap'} if maps else None

			promoted = [r for r in await asyncio.gather(*(check(d) for d in channel_less)) if r]
		known = {d['id'] for d in queue}
		queue = queue + [p for p in promoted if p['id'] not in known]
		print(f'promoted {len(promoted)} channel-less sources that declare a Sitemap: in robots')
	if args.only_channel:
		queue = [d for d in queue if d['channel'] == args.only_channel]
	skipped_named = [d['id'] for d in dossiers if d.get('robots') == 'named_ai_block']
	skipped_block = [d['id'] for d in dossiers if d.get('robots') == 'disallow']

	state = {'sources': {}} if args.reset else load_state()
	done = {sid for sid, cur in state['sources'].items() if cur.get('passes')}
	pending = [d for d in queue if d['id'] not in done]
	if args.limit:
		pending = pending[: args.limit]

	print(f'queue {len(queue)} robots-clean collectable sources | already walked {len(done)} | this pass {len(pending)}')
	print(f'excluded: {len(skipped_block)} robots-disallow, {len(skipped_named)} AI-crawler-named (awaiting ruling)')

	results = []
	async with aiohttp.ClientSession(headers=HEADERS) as session:
		for index, source in enumerate(pending, 1):
			try:
				result = await collect_source(session, source, state)
			except Exception as exc:  # noqa: BLE001
				result = {'id': source['id'], 'channel': source['channel'], 'new_items': 0, 'error': type(exc).__name__}
				print(f'  [{index}/{len(pending)}] {source["id"]:24} ERROR {type(exc).__name__}')
			else:
				print(f'  [{index}/{len(pending)}] {source["id"]:24} {result["channel"]:10} +{result["new_items"]:<4} total={result["total"]}')
			results.append(result)
			save_state(state)  # checkpoint after every source, so a stop loses nothing

	OUT_DIR.mkdir(parents=True, exist_ok=True)
	with (OUT_DIR / 'run_log.jsonl').open('a', encoding='utf-8') as handle:
		stamp = dt.datetime.now(dt.timezone.utc).isoformat()
		for result in results:
			handle.write(json.dumps({**result, 'run_at': stamp}, ensure_ascii=False) + '\n')

	total_new = sum(r['new_items'] for r in results)
	errors = [r for r in results if r.get('error')]
	remaining = len(queue) - len({sid for sid, cur in state['sources'].items() if cur.get('passes')})
	print(f'\nnew items {total_new} | sources with errors {len(errors)} | remaining in queue {remaining}')
	print(f'DONE -> {OUT_DIR}')


if __name__ == '__main__':
	asyncio.run(main())
