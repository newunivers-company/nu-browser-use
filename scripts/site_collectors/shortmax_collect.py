"""ShortMax (shorttv.live) catalog + engagement collector, via rendered DOM.

Recon finding (promo_recon.py, 2026-08-14): shorttv.live is Nuxt SSR with no
catalog API — the only JSON endpoints in a 154-request HAR were i18n strings,
a build manifest, an ipify lookup and two telemetry beacons. The catalog ships
inside `__NUXT_DATA__`, but **every value there is encrypted**: the payload is
a list of base64 blocks that all share the prefix `9MePJ5iXm/LfrSEc7YAS7`,
i.e. a fixed-IV symmetric envelope over the whole SSR state.

We do not break that envelope. Recovering its key from the JS bundle to read
data the site deliberately obscured is access-control circumvention, which
`docs/collection-policy.md` forbids; the ReelShort carve-out is explicitly
scoped to ReelShort's anonymous catalog read and is not a general licence.

The browser decrypts it in the ordinary course of being a browser, so we read
the rendered page instead — exactly what any visitor sees, no envelope
touched. That is why this collector is heavier than goodshort_collect.py: the
cost is the price of staying on the right side of the line.

Published per title: two `.stat-item` counters (plays, likes — in that DOM
order, matching the play and heart icons), episode count, category breadcrumb,
tag, synopsis, cover, and the "You Might Like" rail, which is a
platform-authored similarity edge list worth keeping on its own.

Counts are abbreviated in the UI ("447K"); `parse_count` expands them and
`*_is_approx` records that the precision is the site's, not ours.

robots.txt: `User-agent: *` disallows only /search/, which we never touch.

Output (SHORTMAX_OUT, default ~/shortmax_export):
  dramas.json / dramas.csv
  snapshots/YYYY-MM-DD/dramas.json
  observations.jsonl   - VIEW_COUNT RankingObservation rows
  recommendations.jsonl - title -> "You Might Like" edges
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
import tempfile
from pathlib import Path

if sys.stdout.encoding and sys.stdout.encoding.lower() != 'utf-8':
	sys.stdout.reconfigure(encoding='utf-8', errors='replace')

sys.path.insert(0, str(Path(__file__).resolve().parent))

from registry.models import load_registry

from browser_use.browser.events import NavigateToUrlEvent
from browser_use.browser.profile import BrowserProfile
from browser_use.browser.session import BrowserSession

BASE = 'https://www.shorttv.live'
OUT_DIR = Path(os.environ.get('SHORTMAX_OUT', str(Path.home() / 'shortmax_export')))
ALLOWED = ['shorttv.live', '*.shorttv.live']
SETTLE = 5.0
NAV_TIMEOUT = 45.0
DRAMA_RE = re.compile(r'/drama/([a-z0-9][a-z0-9-]*-\d+)')
COUNT_RE = re.compile(r'^([\d.]+)\s*([KMB])?$', re.I)
MULTIPLIER = {'K': 1_000, 'M': 1_000_000, 'B': 1_000_000_000}

JS_LISTING = r"""
(() => Array.from(document.querySelectorAll('a[href*="/drama/"]'))
	.map(a => a.getAttribute('href')).filter(Boolean).join('\n'))()
"""

JS_DETAIL = r"""
(() => {
	const text = el => el ? (el.innerText || '').replace(/\s+/g, ' ').trim() : null;
	const out = {
		url: location.href,
		title: text(document.querySelector('h1')),
		stats: Array.from(document.querySelectorAll('.stat-item')).map(el => text(el)),
		// The episode rail paginates at 24, so counting a.episode undercounts
		// every longer series. The "N Episodes" label states the real total.
		episodes_label: (document.body.innerText.match(/(\d+)\s+Episodes?\b/i) || [])[1] || null,
		episodes_rendered: Array.from(document.querySelectorAll('a.episode')).length,
		breadcrumb: Array.from(document.querySelectorAll('script[type="application/ld+json"]'))
			.flatMap(s => { try { const j = JSON.parse(s.textContent); return j['@type'] === 'BreadcrumbList' ? j.itemListElement : []; } catch (e) { return []; } })
			.map(i => ({name: i.name, item: i.item || null})),
		meta: {},
		recommendations: [],
	};
	document.querySelectorAll('meta[property^="og:"], meta[name="description"]').forEach(m => {
		const k = m.getAttribute('property') || m.getAttribute('name');
		out.meta[k] = (m.getAttribute('content') || '').slice(0, 800);
	});
	// The "You Might Like" rail: every other /drama/ link on a detail page.
	const self = location.pathname;
	const seen = new Set();
	document.querySelectorAll('a[href*="/drama/"]').forEach(a => {
		const href = a.getAttribute('href');
		if (!href || href === self || seen.has(href)) return;
		seen.add(href);
		out.recommendations.push({href: href, title: text(a)});
	});
	return JSON.stringify(out);
})()
"""


def parse_count(raw: str | None) -> tuple[int | None, bool]:
	"""'447K' -> (447000, True). Returns (value, is_approx)."""
	if not raw:
		return None, False
	match = COUNT_RE.match(raw.strip())
	if not match:
		return None, False
	number, suffix = match.groups()
	if not suffix:
		return int(float(number)), False
	return int(float(number) * MULTIPLIER[suffix.upper()]), True


async def evaluate(session: BrowserSession, expression: str) -> str | None:
	cdp_session = await session.get_or_create_cdp_session()
	response = await cdp_session.cdp_client.send.Runtime.evaluate(
		params={'expression': expression, 'returnByValue': True}, session_id=cdp_session.session_id
	)
	return response.get('result', {}).get('value')


async def visit(session: BrowserSession, url: str, expression: str) -> str | None:
	await asyncio.wait_for(session.event_bus.dispatch(NavigateToUrlEvent(url=url, new_tab=False)), timeout=NAV_TIMEOUT)
	await asyncio.sleep(SETTLE)
	return await evaluate(session, expression)


def parse_detail(payload: dict, slug: str) -> dict:
	"""Map the rendered page onto flat catalog fields."""
	crumbs = payload.get('breadcrumb') or []
	plays, plays_approx = parse_count((payload.get('stats') or [None])[0])
	likes, likes_approx = parse_count((payload.get('stats') or [None, None])[1] if len(payload.get('stats') or []) > 1 else None)
	return {
		'slug': slug,
		'drama_id': slug.rsplit('-', 1)[-1],
		'title': payload.get('title'),
		'url': payload.get('url') or f'{BASE}/drama/{slug}',
		# Breadcrumb is [site, All Dramas, <category>, <title>]; the category is
		# the only taxonomy the detail page publishes.
		'category': crumbs[2]['name'] if len(crumbs) > 2 else None,
		'category_url': crumbs[2].get('item') if len(crumbs) > 2 else None,
		'episodes': int(payload['episodes_label']) if payload.get('episodes_label') else (payload.get('episodes_rendered') or None),
		'episodes_rendered': payload.get('episodes_rendered') or None,
		'plays': plays,
		'plays_is_approx': plays_approx,
		'likes': likes,
		'likes_is_approx': likes_approx,
		'stats_raw': ' | '.join(payload.get('stats') or []),
		'synopsis': (payload.get('meta', {}).get('og:description') or payload.get('meta', {}).get('description') or '')[:1200],
		'cover': payload.get('meta', {}).get('og:image'),
		'recommendation_count': len(payload.get('recommendations') or []),
	}


async def discover(session: BrowserSession, listings: list[str]) -> set[str]:
	slugs: set[str] = set()
	for path in listings:
		try:
			raw = await visit(session, BASE + path, JS_LISTING)
		except Exception as exc:  # noqa: BLE001
			print(f'  {path}: FAILED {type(exc).__name__}')
			continue
		found = set(DRAMA_RE.findall(raw or ''))
		print(f'  {path}: +{len(found - slugs)} (total {len(slugs | found)})')
		slugs |= found
	return slugs


def write_outputs(rows: list[dict], edges: list[dict], now: str) -> None:
	OUT_DIR.mkdir(parents=True, exist_ok=True)
	snap_dir = OUT_DIR / 'snapshots' / dt.date.today().isoformat()
	snap_dir.mkdir(parents=True, exist_ok=True)
	payload = json.dumps(rows, ensure_ascii=False, indent=2)
	(OUT_DIR / 'dramas.json').write_text(payload, encoding='utf-8')
	(snap_dir / 'dramas.json').write_text(payload, encoding='utf-8')

	with (OUT_DIR / 'observations.jsonl').open('a', encoding='utf-8') as handle:
		for row in rows:
			if row.get('plays') is None:
				continue
			handle.write(
				json.dumps(
					{
						'source': 'shorttv.live', 'ranking_name': 'catalog_play_count', 'rank_type': 'VIEW_COUNT',
						'entity_type': 'work', 'entity_id': row['drama_id'], 'entity_title': row['title'],
						'scope': {'type': 'platform', 'platform': 'shortmax'}, 'period': {'type': 'cumulative'},
						'rank': None, 'raw_metric_name': 'plays', 'raw_score': row['plays'], 'views': row['plays'],
						'is_approximate': row['plays_is_approx'], 'likes': row['likes'],
						'platform': 'ShortMax', 'genres': [row['category']] if row['category'] else [],
						'episodes': row['episodes'], 'source_url': row['url'], 'observed_at': now,
					},
					ensure_ascii=False,
				)
				+ '\n'
			)
	if edges:
		with (OUT_DIR / 'recommendations.jsonl').open('a', encoding='utf-8') as handle:
			for edge in edges:
				handle.write(json.dumps(edge, ensure_ascii=False) + '\n')

	columns = ['drama_id', 'title', 'category', 'episodes', 'episodes_rendered', 'plays', 'likes', 'plays_is_approx', 'likes_is_approx', 'stats_raw', 'recommendation_count', 'slug', 'url', 'cover', 'category_url', 'synopsis']
	with (OUT_DIR / 'dramas.csv').open('w', newline='', encoding='utf-8-sig') as handle:
		writer = csv.DictWriter(handle, fieldnames=columns, extrasaction='ignore')
		writer.writeheader()
		writer.writerows(rows)


async def main() -> None:
	parser = argparse.ArgumentParser()
	parser.add_argument('--listings', nargs='*', default=['/', '/dramas'], help='listing paths to harvest slugs from')
	parser.add_argument('--limit', type=int, help='cap detail pages (smoke tests)')
	parser.add_argument('--headful', action='store_true')
	args = parser.parse_args()

	registry = load_registry()
	now = dt.datetime.now(dt.timezone.utc).isoformat()
	rows: list[dict] = []
	edges: list[dict] = []

	with tempfile.TemporaryDirectory(prefix='shortmax_') as profile_dir:
		profile = BrowserProfile(
			headless=not args.headful,
			keep_alive=False,
			user_data_dir=Path(profile_dir),
			allowed_domains=ALLOWED,
			prohibited_domains=registry.prohibited_domains(),
		)
		session = BrowserSession(browser_profile=profile)
		try:
			await session.start()
			print('[1/2] discovering slugs')
			slugs = sorted(await discover(session, args.listings))
			if args.limit:
				slugs = slugs[: args.limit]
			print(f'      {len(slugs)} slugs')

			print('[2/2] rendering detail pages')
			for index, slug in enumerate(slugs, 1):
				try:
					raw = await visit(session, f'{BASE}/drama/{slug}', JS_DETAIL)
					payload = json.loads(raw) if raw else None
				except Exception as exc:  # noqa: BLE001
					print(f'  [{index}/{len(slugs)}] {slug}: FAILED {type(exc).__name__}')
					continue
				if not payload or not payload.get('title'):
					print(f'  [{index}/{len(slugs)}] {slug}: no content')
					continue
				row = parse_detail(payload, slug)
				rows.append(row)
				for rec in payload.get('recommendations') or []:
					target = DRAMA_RE.findall(rec.get('href') or '')
					if target:
						edges.append({'from_id': row['drama_id'], 'from_title': row['title'], 'to_slug': target[0], 'to_title': rec.get('title'), 'observed_at': now})
				if index % 10 == 0 or index == len(slugs):
					print(f'  [{index}/{len(slugs)}] {row["title"]}: plays={row["plays"]} likes={row["likes"]} eps={row["episodes"]}')
		finally:
			await session.kill()

	write_outputs(rows, edges, now)
	with_plays = [row for row in rows if row.get('plays')]
	print(f'DONE -> {OUT_DIR}')
	print(f'  dramas: {len(rows)} | with plays: {len(with_plays)} | recommendation edges: {len(edges)}')
	for row in sorted(with_plays, key=lambda r: r['plays'], reverse=True)[:5]:
		print(f'    {row["plays"]:>12,}  {str(row["title"])[:50]}')


if __name__ == '__main__':
	asyncio.run(main())
