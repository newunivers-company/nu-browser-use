"""Which catalogue sources would a browser actually unlock?

Two things make this worth re-deriving rather than reasoning about from memory.
The robots parser was under-blocking wildcard rules until recently, so every
earlier verdict is suspect in both directions. And CivitAI turned out to be a
shape worth looking for deliberately: the API path is disallowed while the web
pages are not, so a source written off as closed is reachable by a browser and
only by a browser.

Each source is scored against what is already known about it — the channel the
harvest found, how many items the HTTP loop actually produced — and re-checked
against robots on two paths: the page path itself, and the site's likely API
prefix. That second check is what surfaces the CivitAI shape.

Verdicts, most actionable first:

  api_blocked_web_open   robots disallows the API path, allows the page, and the
                         HTTP loop produced nothing. That combination is the
                         CivitAI shape: a browser is the only compliant route,
                         so these are the highest-value upgrades. A source that
                         already collects over HTTP never lands here, however
                         its API is ruled.
  render_required        permitted, but the HTTP loop produced nothing from a
                         channel that should have yielded (framework state, or
                         a page whose content only exists after JS runs).
  no_channel_rendered    permitted with no machine-readable channel at all;
                         a browser is the only way to see whether there is
                         anything worth taking.
  http_is_fine           already producing over HTTP. A browser would cost a
                         Chromium launch and add nothing.
  blocked                robots disallows the page path, or the source reserves
                         ai-train, or it names Claude crawlers. Not a candidate
                         at any tier — a browser does not change permission.

Output (REVIEW_OUT, default ~/source_review):
  browser_upgrade_review.csv / .json
"""

from __future__ import annotations

import argparse
import asyncio
import collections
import csv
import datetime as dt
import json
import os
import sys
from pathlib import Path
from urllib.parse import urlsplit

if sys.stdout.encoding and sys.stdout.encoding.lower() != 'utf-8':
	sys.stdout.reconfigure(encoding='utf-8', errors='replace')

import aiohttp

sys.path.insert(0, str(Path(__file__).resolve().parent))
from promo_registry_verify import robots_verdict, scalar_verdict  # noqa: E402

OUT_DIR = Path(os.environ.get('REVIEW_OUT', str(Path.home() / 'source_review')))
HARVEST_DIR = Path(os.environ.get('HARVEST_OUT', str(Path.home() / 'source_harvest')))
LOOP_DIR = Path(os.environ.get('LOOP_OUT', str(Path.home() / 'source_loop')))
UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36'
HEADERS = {'User-Agent': UA, 'Accept': 'text/plain,*/*;q=0.8'}
CLAUDE_TOKENS = {'anthropic-ai', 'claudebot', 'claude-user', 'claude-searchbot'}
API_PROBE_PATHS = ('/api/v1/items', '/api/items')
CONCURRENCY = 6


def collected_counts() -> dict[str, int]:
	items = LOOP_DIR / 'items'
	if not items.exists():
		return {}
	return {path.stem: sum(1 for _ in path.open(encoding='utf-8')) for path in items.glob('*.jsonl')}


async def robots_for(session: aiohttp.ClientSession, host_url: str, cache: dict[str, str]) -> str:
	parts = urlsplit(host_url)
	origin = f'{parts.scheme}://{parts.netloc}'
	if origin in cache:
		return cache[origin]
	body = ''
	try:
		async with session.get(f'{origin}/robots.txt', timeout=aiohttp.ClientTimeout(total=20)) as response:
			if response.status == 200:
				body = await response.text(errors='replace')
	except Exception:  # noqa: BLE001
		body = ''
	cache[origin] = body
	return body


def classify(source: dict, body: str, collected: int) -> dict:
	parts = urlsplit(source['url'])
	page_path = parts.path or '/'
	if not body:
		page = {'star': 'unknown', 'ai_named': [], 'ai_named_disallow': False, 'content_signal': {}}
		api_blocked = False
	else:
		page = robots_verdict(body, page_path)
		api_blocked = any(robots_verdict(body, probe)['star'] == 'disallow' for probe in API_PROBE_PATHS)

	page_verdict = scalar_verdict(page)
	names_us = bool(set(page['ai_named']) & CLAUDE_TOKENS and page['ai_named_disallow'])
	reserved = page.get('content_signal', {}).get('ai-train') == 'no'

	if page_verdict == 'disallow' or reserved or names_us:
		verdict, why = 'blocked', ('robots disallows the page' if page_verdict == 'disallow' else 'ai-train reserved' if reserved else 'names Claude crawlers')
	elif collected > 0:
		# A source already producing over HTTP is not a browser candidate, whatever
		# its API rules say — the first cut of this check ran before the collected
		# test and promoted sources with 2,900 rows already banked.
		verdict, why = 'http_is_fine', f'{collected} items already collected over HTTP'
	elif api_blocked and page_verdict in ('allow', 'unknown'):
		verdict, why = 'api_blocked_web_open', 'API path disallowed, page allowed, and HTTP yielded nothing'
	elif source['channel'] in ('framework_state',):
		verdict, why = 'render_required', 'framework state the generic miner cannot resolve'
	elif source['channel'] in ('json_ld', 'feed', 'sitemap') and collected == 0:
		verdict, why = 'render_required', f'{source["channel"]} channel yielded nothing over HTTP'
	elif source['channel'] in ('html_only', 'opengraph_only', 'none'):
		verdict, why = 'no_channel_rendered', 'no machine-readable channel; only a render can tell'
	else:
		verdict, why = 'http_is_fine', 'HTTP path is adequate'

	return {
		'id': source['id'],
		'category': source['category'],
		'url': source['url'],
		'channel': source['channel'],
		'collected_items': collected,
		'robots_page': page_verdict,
		'api_path_blocked': api_blocked,
		'ai_named': ' | '.join(page['ai_named'][:4]),
		'content_signal': json.dumps(page.get('content_signal') or {}, ensure_ascii=False),
		'verdict': verdict,
		'why': why,
	}


async def main() -> None:
	parser = argparse.ArgumentParser()
	parser.add_argument('--limit', type=int)
	args = parser.parse_args()

	dossiers_path = HARVEST_DIR / 'dossiers.json'
	if not dossiers_path.exists():
		print(f'no dossiers at {dossiers_path} — run source_harvest.py first')
		return
	sources = json.loads(dossiers_path.read_text(encoding='utf-8'))['sources']
	if args.limit:
		sources = sources[: args.limit]
	counts = collected_counts()
	print(f'reviewing {len(sources)} sources against the corrected robots parser')

	cache: dict[str, str] = {}
	semaphore = asyncio.Semaphore(CONCURRENCY)

	async def one(source: dict) -> dict:
		async with semaphore:
			body = await robots_for(session, source['url'], cache)
		return classify(source, body, counts.get(source['id'], 0))

	async with aiohttp.ClientSession(headers=HEADERS) as session:
		rows = list(await asyncio.gather(*(one(s) for s in sources)))

	OUT_DIR.mkdir(parents=True, exist_ok=True)
	now = dt.datetime.now(dt.timezone.utc).isoformat()
	(OUT_DIR / 'browser_upgrade_review.json').write_text(json.dumps({'reviewed_at': now, 'sources': rows}, ensure_ascii=False, indent=2), encoding='utf-8')
	columns = ['verdict', 'id', 'category', 'channel', 'collected_items', 'robots_page', 'api_path_blocked', 'ai_named', 'content_signal', 'why', 'url']
	order = {'api_blocked_web_open': 0, 'render_required': 1, 'no_channel_rendered': 2, 'http_is_fine': 3, 'blocked': 4}
	rows.sort(key=lambda r: (order.get(r['verdict'], 9), -r['collected_items'], r['id']))
	with (OUT_DIR / 'browser_upgrade_review.csv').open('w', newline='', encoding='utf-8-sig') as handle:
		writer = csv.DictWriter(handle, fieldnames=columns, extrasaction='ignore')
		writer.writeheader()
		writer.writerows(rows)

	tally = collections.Counter(row['verdict'] for row in rows)
	print('\nverdicts:')
	for verdict, count in sorted(tally.items(), key=lambda kv: order.get(kv[0], 9)):
		print(f'  {verdict:22} {count}')
	for verdict in ('api_blocked_web_open', 'render_required'):
		picked = [r for r in rows if r['verdict'] == verdict]
		if not picked:
			continue
		print(f'\n{verdict} ({len(picked)}):')
		for row in picked[:14]:
			print(f'  {row["id"]:26} {row["category"]:14} {row["channel"]:16} {row["why"]}')
	print(f'\nDONE -> {OUT_DIR / "browser_upgrade_review.csv"}')


if __name__ == '__main__':
	asyncio.run(main())
