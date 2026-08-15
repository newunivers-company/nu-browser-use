"""Per-source collection dossiers, walked in catalog order.

The catalog says which sources exist and whether they are reachable. It does
not say how any of them could actually be collected, and with 232 entries that
question cannot be answered by writing 232 collectors. So this answers it once
per source, mechanically.

A sample of the first dozen public sources showed what to expect: OpenGraph is
effectively universal (13/13), JSON-LD is rare (2/13), feeds appear on roughly
a third. Item-level catalogs are therefore NOT harvestable in bulk — the honest
deliverable is a dossier per source recording which machine-readable surface it
offers, so the sources worth a real collector can be picked on evidence instead
of by guessing.

Recorded per source: robots stance for the path, declared identity (title,
description, OpenGraph), the @type values of any JSON-LD (which entities the
site models), discovered RSS/Atom feed URLs, and whether a sitemap exists.

METADATA ONLY
Feeds and sitemaps are discovered and their URLs recorded; they are not
fetched for content. JSON-LD is reduced to its @type values and top-level keys,
never its text. Nothing here copies article bodies, images, or any other
expression — this maps collection surfaces, it does not collect works.

Sources marked login_required are skipped (login_source_probe.py covers those
against a signed-in browser), as are the platforms whose terms prohibit
automated access.

Output (HARVEST_OUT, default ~/source_harvest):
  dossiers.json               - one record per source
  dossiers.csv                - flat view for triage
  collectable.csv             - sources with a real machine-readable channel
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import datetime as dt
import json
import os
import re
import sys
from pathlib import Path
from urllib.parse import urljoin, urlsplit

if sys.stdout.encoding and sys.stdout.encoding.lower() != 'utf-8':
	sys.stdout.reconfigure(encoding='utf-8', errors='replace')

import aiohttp

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from promo_registry_verify import robots_verdict, scalar_verdict

from scripts.data_source_catalog import DataSourceAccess, load_data_source_catalog

OUT_DIR = Path(os.environ.get('HARVEST_OUT', str(Path.home() / 'source_harvest')))
UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36'
HEADERS = {
	'User-Agent': UA,
	'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
	'Accept-Language': 'en-US,en;q=0.9,ko;q=0.8',
	'Upgrade-Insecure-Requests': '1',
}
# Terms prohibit automated access; see collection-policy.md and the promotion registry.
BANNED = {'x_explore', 'instagram_explore', 'threads', 'facebook', 'linkedin_feed', 'snapchat_web', 'tvtropes'}
CONCURRENCY = 6
LD_RE = re.compile(r'<script[^>]*application/ld\+json[^>]*>(.*?)</script>', re.S | re.I)
FEED_RE = re.compile(r'<link[^>]+type="application/(?:rss|atom)\+xml"[^>]*>', re.I)
HREF_RE = re.compile(r'href="([^"]+)"', re.I)
TITLE_RE = re.compile(r'<title[^>]*>(.*?)</title>', re.S | re.I)
META_RE = re.compile(r'<meta[^>]+(?:property|name)="((?:og:|twitter:)?[a-z:_-]+)"[^>]+content="([^"]*)"', re.I)


def ld_types(html: str) -> tuple[list[str], list[str]]:
	"""(@type values, top-level keys) — which entities the site models, not their text."""
	types: list[str] = []
	keys: list[str] = []
	for block in LD_RE.findall(html)[:6]:
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
			if isinstance(node_type, list):
				types.extend(str(t) for t in node_type)
			elif node_type:
				types.append(str(node_type))
			keys.extend(k for k in node if not k.startswith('@'))
	return sorted(dict.fromkeys(types))[:12], sorted(dict.fromkeys(keys))[:25]


def feeds(html: str, base: str) -> list[str]:
	found: list[str] = []
	for tag in FEED_RE.findall(html)[:8]:
		href = HREF_RE.search(tag)
		if href:
			url = urljoin(base, href.group(1))
			if url not in found:
				found.append(url)
	return found


async def get(session: aiohttp.ClientSession, url: str, *, head: bool = False) -> tuple[int | None, str]:
	try:
		method = session.head if head else session.get
		async with method(url, timeout=aiohttp.ClientTimeout(total=25), allow_redirects=True) as response:
			return response.status, ('' if head else await response.text(errors='replace'))
	except Exception:  # noqa: BLE001
		return None, ''


async def dossier(session: aiohttp.ClientSession, source, semaphore: asyncio.Semaphore) -> dict:
	url = str(source.url)
	parts = urlsplit(url)
	row: dict = {
		'id': source.id,
		'category': source.category.value,
		'access': source.access.value,
		'url': url,
		'host': parts.netloc,
	}
	async with semaphore:
		robots_status, robots_body = await get(session, f'{parts.scheme}://{parts.netloc}/robots.txt')
		if robots_status == 200 and robots_body:
			verdict = robots_verdict(robots_body, parts.path or '/')
			row['robots'] = scalar_verdict(verdict)
			row['robots_ai_named'] = verdict['ai_named']
		else:
			row['robots'] = 'unknown'
			row['robots_ai_named'] = []

		status, html = await get(session, url)
		row['status'] = status
		if not html:
			row['result'] = 'unreachable' if status is None else 'no_body'
			return row

		metas = {name.lower(): content for name, content in META_RE.findall(html)}
		title = TITLE_RE.search(html)
		types, keys = ld_types(html)
		feed_urls = feeds(html, url)
		sitemap_status, _ = await get(session, f'{parts.scheme}://{parts.netloc}/sitemap.xml', head=True)
		row |= {
			'result': 'ok',
			'title': (re.sub(r'\s+', ' ', title.group(1)).strip()[:160]) if title else None,
			'description': (metas.get('description') or metas.get('og:description') or '')[:300],
			'og_count': sum(1 for k in metas if k.startswith('og:')),
			'ld_types': types,
			'ld_keys': keys,
			'feeds': feed_urls,
			'sitemap': sitemap_status == 200,
			'next_data': '__NEXT_DATA__' in html,
			'nuxt_data': '__NUXT_DATA__' in html or '__NUXT__' in html,
			'bytes': len(html),
		}
	return row


def channel(row: dict) -> str:
	"""The strongest machine-readable surface this source offers."""
	if row.get('result') != 'ok':
		return 'none'
	if row.get('ld_types'):
		return 'json_ld'
	if row.get('feeds'):
		return 'feed'
	if row.get('next_data') or row.get('nuxt_data'):
		return 'framework_state'
	if row.get('sitemap'):
		return 'sitemap'
	if row.get('og_count'):
		return 'opengraph_only'
	return 'html_only'


async def main() -> None:
	parser = argparse.ArgumentParser()
	parser.add_argument('--start', type=int, default=0, help='catalog offset, for batching')
	parser.add_argument('--limit', type=int, help='how many sources to walk')
	parser.add_argument('--category')
	args = parser.parse_args()

	catalog = load_data_source_catalog()
	sources = [s for s in catalog.sources if s.access is not DataSourceAccess.LOGIN_REQUIRED and s.id not in BANNED]
	if args.category:
		sources = [s for s in sources if s.category.value == args.category]
	skipped = [s.id for s in catalog.sources if s.id in BANNED]
	sources = sources[args.start : args.start + args.limit if args.limit else None]
	print(f'walking {len(sources)} sources in catalog order (skipping {len(skipped)} banned, login_required excluded)')

	semaphore = asyncio.Semaphore(CONCURRENCY)
	async with aiohttp.ClientSession(headers=HEADERS) as session:
		rows = list(await asyncio.gather(*(dossier(session, s, semaphore) for s in sources)))

	for row in rows:
		row['channel'] = channel(row)

	OUT_DIR.mkdir(parents=True, exist_ok=True)
	now = dt.datetime.now(dt.timezone.utc).isoformat()
	(OUT_DIR / 'dossiers.json').write_text(
		json.dumps({'built_at': now, 'sources': rows}, ensure_ascii=False, indent=2), encoding='utf-8'
	)

	columns = [
		'id',
		'category',
		'access',
		'channel',
		'robots',
		'status',
		'sitemap',
		'og_count',
		'feeds',
		'ld_types',
		'next_data',
		'nuxt_data',
		'title',
		'url',
	]
	with (OUT_DIR / 'dossiers.csv').open('w', newline='', encoding='utf-8-sig') as handle:
		writer = csv.writer(handle)
		writer.writerow(columns)
		for row in rows:
			writer.writerow([' | '.join(row.get(c) or []) if c in ('feeds', 'ld_types') else row.get(c) for c in columns])

	strong = [r for r in rows if r['channel'] in ('json_ld', 'feed', 'framework_state', 'sitemap')]
	with (OUT_DIR / 'collectable.csv').open('w', newline='', encoding='utf-8-sig') as handle:
		writer = csv.writer(handle)
		writer.writerow(['id', 'category', 'channel', 'robots', 'feeds', 'ld_types', 'url'])
		for row in sorted(strong, key=lambda r: (r['channel'], r['id'])):
			writer.writerow(
				[
					row['id'],
					row['category'],
					row['channel'],
					row['robots'],
					' | '.join(row.get('feeds') or []),
					' | '.join(row.get('ld_types') or []),
					row['url'],
				]
			)

	tally: dict[str, int] = {}
	for row in rows:
		tally[row['channel']] = tally.get(row['channel'], 0) + 1
	print('\ncollection channel available, by source:')
	for name, count in sorted(tally.items(), key=lambda kv: -kv[1]):
		print(f'  {name:18} {count}')
	ai_named = [r for r in rows if r.get('robots_ai_named')]
	if ai_named:
		# "Names" is not "blocks": news.coupang.com names ClaudeBot in order to
		# write `Allow: /`. The verdict column beside each row is what decides
		# collection, and docs/collection-policy.md has already ruled on it. The
		# earlier wording here — "human ruling needed" — described a settled
		# question as an open one and kept it on the open list for days.
		restricted = sum(1 for r in ai_named if r.get('robots') in ('named_ai_block', 'disallow', 'ai_train_reserved'))
		print(f'\n{len(ai_named)} sources name AI crawlers in robots; {restricted} of them actually restrict us:')
		for row in ai_named[:15]:
			print(f'  {row["id"]:24} {row["robots"]:16} {", ".join(row["robots_ai_named"][:4])}')
	print(f'\n{len(strong)} sources offer a real machine-readable channel -> {OUT_DIR / "collectable.csv"}')


if __name__ == '__main__':
	asyncio.run(main())
