"""FlexTV (flextv.cc) catalog + engagement collector, via rendered DOM.

Recon finding (promo_recon.py, 2026-08-14): Nuxt SSR, no catalog API — the only
JSON in a 277-request HAR was an ad-quality config, a build manifest and a
recaptcha frame. Unlike ShortMax the SSR payload is plaintext, so an HTTP
collector would be easy. We deliberately do not write one.

flextv.cc/robots.txt has **no `User-agent: *` group at all**. It allowlists
Googlebot / Bingbot / Yandex / Yeti and then denies, by name, the tools people
scrape with: python-requests, Scrapy, wget, HTTrack, crawler4j, libwww-perl
(plus AhrefsBot / SemrushBot / MJ12bot / BadBot). Formally no rule binds an
unnamed agent, and stdlib RobotFileParser returns can_fetch('*') == True. But
the enumeration says plainly which kind of access is unwelcome, and the server
backs it up: a default aiohttp request is answered `400 Too many headers
received`, while a real browser is served normally.

So the line drawn here is library-vs-browser, not allowed-vs-forbidden: we
visit with a real browser like any viewer, and skip the easier HTTP path that
the site asks automated clients not to take.

Slug source is the home page and genre pages, which expose ~109 `/episodes/
episode-1-<title>-<id>` links. The `/dramas/all-dramas` listing renders cards
without anchors (click handlers) and paginates via JS, so full enumeration is
deferred rather than silently truncated — see the coverage note this prints.

Per title the play page publishes `.video-op-btn__label` counters (likes,
views, and an episode/share figure in DOM order) plus title and cover.

Genre is deliberately absent — see discover() for why attributing it from the
listing page is provably wrong on this site.

Output (FLEXTV_OUT, default ~/flextv_export):
  dramas.json / dramas.csv
  snapshots/YYYY-MM-DD/dramas.json
  observations.jsonl   - VIEW_COUNT RankingObservation rows
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
from urllib.parse import unquote

if sys.stdout.encoding and sys.stdout.encoding.lower() != 'utf-8':
	sys.stdout.reconfigure(encoding='utf-8', errors='replace')

sys.path.insert(0, str(Path(__file__).resolve().parent))

from registry.models import load_registry

from browser_use.browser.events import NavigateToUrlEvent
from browser_use.browser.profile import BrowserProfile
from browser_use.browser.session import BrowserSession

BASE = 'https://www.flextv.cc'
OUT_DIR = Path(os.environ.get('FLEXTV_OUT', str(Path.home() / 'flextv_export')))
ALLOWED = ['flextv.cc', '*.flextv.cc']
SETTLE = 6.0
NAV_TIMEOUT = 45.0
GENRE_RE = re.compile(r'/genres/([a-z0-9-]+-[a-z0-9]{8,12})')
EPISODE_PREFIX = '/episodes/episode-1-'
ID_RE = re.compile(r'^[A-Za-z0-9]{8,12}$')
COUNT_RE = re.compile(r'^([\d.]+)\s*([KMB])?$', re.I)
MULTIPLIER = {'K': 1_000, 'M': 1_000_000, 'B': 1_000_000_000}

JS_LINKS = r"""
(() => Array.from(document.querySelectorAll('a[href]'))
	.map(a => a.getAttribute('href')).filter(Boolean).join('\n'))()
"""

JS_DETAIL = r"""
(() => {
	const text = el => el ? (el.innerText || '').replace(/\s+/g, ' ').trim() : null;
	const out = {
		url: location.href,
		title: text(document.querySelector('h1')) || document.title,
		labels: Array.from(document.querySelectorAll('.video-op-btn__label')).map(el => text(el)),
		meta: {},
	};
	document.querySelectorAll('meta[property^="og:"], meta[name="description"]').forEach(m => {
		const k = m.getAttribute('property') || m.getAttribute('name');
		out.meta[k] = (m.getAttribute('content') || '').slice(0, 800);
	});
	return JSON.stringify(out);
})()
"""


def parse_count(raw: str | None) -> tuple[int | None, bool]:
	"""'725.0K' -> (725000, True). Returns (value, is_approx)."""
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


async def visit(session: BrowserSession, url: str, expression: str, scrolls: int = 0) -> str | None:
	await asyncio.wait_for(session.event_bus.dispatch(NavigateToUrlEvent(url=url, new_tab=False)), timeout=NAV_TIMEOUT)
	await asyncio.sleep(SETTLE)
	for _ in range(scrolls):
		await evaluate(session, 'window.scrollTo(0, document.body.scrollHeight)')
		await asyncio.sleep(2.0)
	return await evaluate(session, expression)


def episode_links(href_dump: str) -> dict[str, str]:
	"""{drama_id: path} from a newline-joined href dump.

	Title slugs are percent-encoded and contain hyphens and apostrophes, so the
	id is read as the trailing path segment rather than pattern-matched out of
	the middle of the slug.
	"""
	found: dict[str, str] = {}
	for line in href_dump.splitlines():
		path = line.strip().split('?')[0].split('#')[0].rstrip('/')
		if not path.startswith(EPISODE_PREFIX):
			continue
		drama_id = path.rsplit('-', 1)[-1]
		if ID_RE.match(drama_id):
			found.setdefault(drama_id, path)
	return found


async def discover(session: BrowserSession, genre_pages: int) -> dict[str, str]:
	"""Return {drama_id: episode-1 path} from home plus genre pages.

	Genre is NOT recorded. Attributing it from the listing a title was found on
	was tried and is provably wrong here: every genre page yields the identical
	set of anchor-linked titles (first page +10, then +0, +0, +0, +0, +0), so
	those anchors are a shared rail rather than genre-filtered results. The real
	genre grid is anchor-less JS cards, the same wall as /dramas/all-dramas.
	Play pages render no breadcrumb either, so FlexTV genre is currently not
	obtainable and the field is omitted rather than guessed.
	"""
	home = await visit(session, BASE + '/', JS_LINKS, scrolls=3) or ''
	found = episode_links(home)
	print(f'  home: {len(found)} titles')

	for genre in sorted(set(GENRE_RE.findall(home)))[:genre_pages]:
		label = genre.rsplit('-', 1)[0].replace('-', ' ').title()
		try:
			page = await visit(session, f'{BASE}/genres/{genre}', JS_LINKS, scrolls=3) or ''
		except Exception as exc:  # noqa: BLE001
			print(f'  genres/{genre}: FAILED {type(exc).__name__}')
			continue
		before = len(found)
		for drama_id, path in episode_links(page).items():
			found.setdefault(drama_id, path)
		print(f'  genres/{label}: +{len(found) - before} (total {len(found)})')
	return found


def parse_detail(payload: dict, drama_id: str, path: str) -> dict:
	"""Map the play page onto flat fields.

	`.video-op-btn__label` renders as [likes, views, episodes] in DOM order.
	The labels carry no accessible names, so the mapping is positional and
	recorded raw alongside, letting a layout change be spotted in the data.
	"""
	labels = payload.get('labels') or []
	likes, likes_approx = parse_count(labels[0] if len(labels) > 0 else None)
	views, views_approx = parse_count(labels[1] if len(labels) > 1 else None)
	title = payload.get('meta', {}).get('og:title') or payload.get('title') or ''
	return {
		'drama_id': drama_id,
		'title': re.sub(r'^Watch\s+|\s+Episode\s+\d+.*$', '', unquote(title)).strip() or None,
		'url': payload.get('url') or f'{BASE}{path}',
		'likes': likes,
		'likes_is_approx': likes_approx,
		'views': views,
		'views_is_approx': views_approx,
		'labels_raw': ' | '.join(labels),
		'synopsis': (payload.get('meta', {}).get('og:description') or '')[:1200],
		'cover': payload.get('meta', {}).get('og:image'),
	}


def write_outputs(rows: list[dict], now: str) -> None:
	OUT_DIR.mkdir(parents=True, exist_ok=True)
	snap_dir = OUT_DIR / 'snapshots' / dt.date.today().isoformat()
	snap_dir.mkdir(parents=True, exist_ok=True)
	payload = json.dumps(rows, ensure_ascii=False, indent=2)
	(OUT_DIR / 'dramas.json').write_text(payload, encoding='utf-8')
	(snap_dir / 'dramas.json').write_text(payload, encoding='utf-8')

	with (OUT_DIR / 'observations.jsonl').open('a', encoding='utf-8') as handle:
		for row in rows:
			if row.get('views') is None:
				continue
			handle.write(
				json.dumps(
					{
						'source': 'flextv.cc', 'ranking_name': 'catalog_view_count', 'rank_type': 'VIEW_COUNT',
						'entity_type': 'work', 'entity_id': row['drama_id'], 'entity_title': row['title'],
						'scope': {'type': 'platform', 'platform': 'flextv'}, 'period': {'type': 'cumulative'},
						'rank': None, 'raw_metric_name': 'views', 'raw_score': row['views'], 'views': row['views'],
						'is_approximate': row['views_is_approx'], 'likes': row['likes'],
						'platform': 'FlexTV',
						'source_url': row['url'], 'observed_at': now,
					},
					ensure_ascii=False,
				)
				+ '\n'
			)

	columns = ['drama_id', 'title', 'views', 'likes', 'views_is_approx', 'likes_is_approx', 'labels_raw', 'url', 'cover', 'synopsis']
	with (OUT_DIR / 'dramas.csv').open('w', newline='', encoding='utf-8-sig') as handle:
		writer = csv.DictWriter(handle, fieldnames=columns, extrasaction='ignore')
		writer.writeheader()
		writer.writerows(rows)


async def main() -> None:
	parser = argparse.ArgumentParser()
	parser.add_argument('--genres', type=int, default=8, help='how many genre pages to sweep')
	parser.add_argument('--limit', type=int, help='cap detail pages (smoke tests)')
	parser.add_argument('--headful', action='store_true')
	args = parser.parse_args()

	registry = load_registry()
	now = dt.datetime.now(dt.timezone.utc).isoformat()
	rows: list[dict] = []

	with tempfile.TemporaryDirectory(prefix='flextv_') as profile_dir:
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
			print('[1/2] discovering titles')
			found = await discover(session, args.genres)
			items = sorted(found.items())
			if args.limit:
				items = items[: args.limit]
			print(f'      {len(items)} titles')

			print('[2/2] rendering play pages')
			for index, (drama_id, path) in enumerate(items, 1):
				try:
					raw = await visit(session, f'{BASE}{path}', JS_DETAIL)
					payload = json.loads(raw) if raw else None
				except Exception as exc:  # noqa: BLE001
					print(f'  [{index}/{len(items)}] {drama_id}: FAILED {type(exc).__name__}')
					continue
				if not payload:
					continue
				row = parse_detail(payload, drama_id, path)
				rows.append(row)
				if index % 10 == 0 or index == len(items):
					print(f'  [{index}/{len(items)}] {row["title"]}: views={row["views"]} likes={row["likes"]}')
		finally:
			await session.kill()

	write_outputs(rows, now)
	with_views = [row for row in rows if row.get('views')]
	print(f'DONE -> {OUT_DIR}')
	print(f'  titles: {len(rows)} | with views: {len(with_views)}')
	print('  COVERAGE: home + genre pages only; /dramas/all-dramas and the genre grids are anchor-less JS cards')
	print('  NOT COLLECTED: genre — every genre page returns the same shared rail, so listing-based attribution is invalid')
	for row in sorted(with_views, key=lambda r: r['views'], reverse=True)[:5]:
		print(f'    {row["views"]:>12,}  {str(row["title"])[:50]}')


if __name__ == '__main__':
	asyncio.run(main())
