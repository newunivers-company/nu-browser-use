"""Resumable collection loop over the catalog, in order.

source_harvest.py answered "how is each source collectable". This acts on that:
it walks the sources that are both robots-clean and offer a real machine-
readable channel, collects the item metadata that channel exposes, and
checkpoints after every source so the run can be stopped and resumed.

Per-source channel handling:
  feed              RSS/Atom entries -> title, link, dates, author, categories
  sitemap           URL inventory    -> loc, lastmod, changefreq, priority
  json_ld           typed entities   -> name, url, dates, numeric properties
  framework_state   recorded as needing a bespoke collector; not guessed at

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
ITEM_CAP = 300
SNIPPET = 200
COLLECTABLE = {'feed', 'sitemap', 'json_ld'}
# Full-text carriers. Dropped before write — see the module docstring.
BODY_FIELDS = {'content', 'content:encoded', 'encoded', 'articlebody', 'text', 'body', 'summary_detail'}
LD_RE = re.compile(r'<script[^>]*application/ld\+json[^>]*>(.*?)</script>', re.S | re.I)
NS = {'atom': 'http://www.w3.org/2005/Atom', 'sm': 'http://www.sitemaps.org/schemas/sitemap/0.9'}


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


def parse_sitemap(body: str) -> tuple[list[dict], list[str]]:
	"""(url entries, nested sitemap urls)."""
	try:
		root = ET.fromstring(body.encode('utf-8', 'replace'))
	except ET.ParseError:
		return [], []
	nested = [_text(node.find('sm:loc', NS)) for node in root.findall('sm:sitemap', NS)]
	entries = []
	for node in root.findall('sm:url', NS):
		loc = _text(node.find('sm:loc', NS))
		if not loc:
			continue
		entries.append({
			'url': loc,
			'published': _text(node.find('sm:lastmod', NS)),
			'changefreq': _text(node.find('sm:changefreq', NS)),
			'priority': _text(node.find('sm:priority', NS)),
		})
	return entries, [n for n in nested if n]


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
		targets = source.get('feeds', [])[:3]
	elif channel == 'sitemap':
		parts = urlsplit(base)
		targets = [f'{parts.scheme}://{parts.netloc}/sitemap.xml']
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
		elif channel == 'sitemap':
			entries, nested = parse_sitemap(body)
			items.extend(entries)
			# One level of nesting only; a full sitemap tree is a crawl, not a read.
			for nested_url in nested[:2]:
				nested_status, nested_body, _ = await fetch(session, nested_url, {})
				if nested_status == 200 and nested_body:
					items.extend(parse_sitemap(nested_body)[0])
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
	args = parser.parse_args()

	dossiers_path = HARVEST_DIR / 'dossiers.json'
	if not dossiers_path.exists():
		print(f'no dossiers at {dossiers_path} — run source_harvest.py first')
		return
	dossiers = json.loads(dossiers_path.read_text(encoding='utf-8'))['sources']

	# Catalog order is preserved; only robots-clean sources with a real channel.
	queue = [d for d in dossiers if d.get('channel') in COLLECTABLE and d.get('robots') == 'allow']
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
