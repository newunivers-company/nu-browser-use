"""Ranking boards that only exist after the page runs.

browser_render_probe flagged a set of sources as browser_wins; DOM recon then
threw most of them out. What survived is two ranking surfaces that serve an
empty shell over HTTP and a real board once rendered:

  goodnovel   /rankings plus its per-genre boards. Works link as
              /book/<slug>_<numeric id>, and the id is stable across locales.
  kuaikan     /ranking/<n>, one board per chart (人气榜, 新作榜, ...). Works link
              as /web/comic/<id>; /web/topic/<id> is the series page for the
              same work, so both are kept and tagged.

WHY THESE TWO AND NOT THE OTHER THREE
piccoma, Google's prompt gallery and Microsoft's measured 125, 48 and 68 links
respectively and have no catalogue at all — piccoma's are genre filters, Google's
are the page linking to itself. The probe's counts ranked navigation, so every
candidate here was confirmed by reading the DOM before a line of this was
written. See browser_render_probe's own docstring for that limitation.

RANK IS THE POINT
These are ordered boards, so DOM order is the rank and the observation carries
it. A title's position today means nothing; its movement between two runs is the
signal `ranking-collection-plan.md` asks for, and it needs the ordinal recorded
at each observation to exist at all.

METADATA ONLY
Title, site id, board, rank, and whatever label the card already shows (latest
chapter, for instance). No chapter pages are opened, no images fetched, no
synopsis taken. These are catalogue facts about works, not the works.

ROBOTS
Every URL is checked before navigation, not once per host. kuaikan disallows
`/*?*` — every URL carrying a query string — so pagination there must stay on
clean paths, and a refusal raises rather than returning an empty page.

Output (BROWSER_CATALOG_OUT, default ~/browser_catalog_export):
  <source>/items.jsonl        - appended, deduped on (board, entity_id)
  <source>/observations.jsonl - one ranked observation per item per run
  snapshots/YYYY-MM-DD/<source>.json
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

import aiohttp
from promo_registry_verify import robots_verdict, scalar_verdict

from browser_use.browser.events import NavigateToUrlEvent
from browser_use.browser.profile import BrowserProfile
from browser_use.browser.session import BrowserSession

OUT_DIR = Path(os.environ.get('BROWSER_CATALOG_OUT', str(Path.home() / 'browser_catalog_export')))
UA = 'nu-browser-use/1.0 (+https://newunivers.com; nu@newunivers.com)'
SETTLE = 7.0
SCROLLS = 4
SCROLL_PAUSE = 2.5
NAV_TIMEOUT = 60.0
BOARD_PAUSE = 2.0

SOURCES: dict[str, dict] = {
	'goodnovel': {
		'host': 'www.goodnovel.com',
		'entry': 'https://www.goodnovel.com/rankings',
		# /book/<slug>_<id>; the trailing digits are the work id.
		'item': r'^/book/(?:.*_)?(\d+)$',
		'kind': 'novel',
		# Genre boards linked from the entry page. Kept to a bounded set: this is
		# a ranking snapshot, not a catalogue walk.
		'board_link': r'^/rankings/[^/]+$',
		'max_boards': 8,
	},
	'kuaikan': {
		'host': 'www.kuaikanmanhua.com',
		'entry': 'https://www.kuaikanmanhua.com/ranking/',
		# /web/topic/<id> is the work; /web/comic/<id> is its latest chapter and
		# lives in the same card, so matching both counted every work twice and
		# gave half the rows a chapter label where a title belongs.
		'item': r'^/web/topic/(\d+)$',
		'kind': 'webtoon',
		# The card renders the title in its own node. Reading the anchor's flat
		# innerText instead produced "2 炮灰闺女的生存方式 乌里丑丑（原著）..." —
		# rank, title, author and synopsis run together — and the cover's alt is
		# the lazy-load placeholder "blank".
		'title_selector': '.title .text',
		'rank_selector': '.RankIcon',
		# /ranking/ (全部) renders a different card layout with no .title node, so
		# 65 of its 65 rows came back as the placeholder alt or a chapter label.
		# It is read for the board list only; every numbered board parses cleanly.
		'collect_entry': False,
		'board_link': r'^/ranking/\d+$',
		'max_boards': 8,
	},
}

JS_EXTRACT = r"""
(() => {
	const pattern = new RegExp(__PATTERN__);
	const seen = new Map();
	document.querySelectorAll('a[href]').forEach(a => {
		let u;
		try { u = new URL(a.href, location.href); } catch (e) { return; }
		if (u.host !== location.host) return;
		const m = u.pathname.match(pattern);
		if (!m) return;
		const id = m[1];
		if (seen.has(id)) return;
		const img = a.querySelector('img');
		const clean = s => (s || '').replace(/\s+/g, ' ').trim();
		const text = clean(a.innerText);
		// "blank" is kuaikan's lazy-load placeholder alt, not a title.
		const alt = img ? clean(img.getAttribute('alt')) : '';
		const altOk = alt && alt.toLowerCase() !== 'blank';

		let title = '';
		if (__TITLE_SEL__) {
			const node = a.querySelector(__TITLE_SEL__);
			title = node ? clean(node.innerText) : '';
		}
		let rank = 0;
		if (__RANK_SEL__) {
			const node = a.querySelector(__RANK_SEL__);
			const parsed = node ? parseInt(clean(node.innerText), 10) : NaN;
			if (!isNaN(parsed)) rank = parsed;
		}
		// The title node carries the rank as a prefix on every card but the first.
		if (rank && title.startsWith(String(rank))) title = title.slice(String(rank).length).trim();
		if (!title) title = altOk ? alt : text;

		seen.set(id, {
			id: id,
			path: u.pathname,
			title: title.slice(0, 200),
			label: (text && text !== title) ? text.slice(0, 160) : null,
			rank: rank || seen.size + 1,
			rank_from: rank ? 'card' : 'dom_order',
			dom_position: seen.size + 1,
			// kuaikan stamps the movement it displays itself ("上升4名"). The
			// card number has been observed frozen across days while the DOM
			// order moves — this is the site's own velocity signal, so keep it.
			rise: (text.match(/上升\s*(\d+)\s*名/) || [])[1] || null,
		});
	});
	return JSON.stringify([...seen.values()]);
})()
"""

JS_BOARDS = r"""
(() => {
	const pattern = new RegExp(__PATTERN__);
	const out = new Map();
	document.querySelectorAll('a[href]').forEach(a => {
		let u;
		try { u = new URL(a.href, location.href); } catch (e) { return; }
		if (u.host !== location.host || !pattern.test(u.pathname)) return;
		if (!out.has(u.pathname)) out.set(u.pathname, (a.innerText || '').replace(/\s+/g, ' ').trim().slice(0, 60));
	});
	return JSON.stringify([...out.entries()].map(([path, name]) => ({path, name})));
})()
"""


class RobotsRefusal(RuntimeError):
	"""A URL this collector must not request."""


_ROBOTS: dict[str, str] = {}


async def robots_body(origin: str) -> str:
	if origin not in _ROBOTS:
		body = ''
		try:
			async with aiohttp.ClientSession(headers={'User-Agent': UA}) as http:
				async with http.get(f'{origin}/robots.txt', timeout=aiohttp.ClientTimeout(total=20)) as response:
					if response.status == 200:
						body = await response.text(errors='replace')
		except Exception:  # noqa: BLE001
			body = ''
		_ROBOTS[origin] = body
	return _ROBOTS[origin]


async def assert_allowed(url: str) -> None:
	"""Per URL, not per host: kuaikan allows /ranking/ and forbids /ranking/9?x=1."""
	parts = urlsplit(url)
	body = await robots_body(f'{parts.scheme}://{parts.netloc}')
	if not body:
		return
	path = parts.path + (f'?{parts.query}' if parts.query else '')
	if scalar_verdict(robots_verdict(body, path or '/')) == 'disallow':
		raise RobotsRefusal(f'robots.txt disallows {path}')


async def evaluate(session: BrowserSession, expression: str) -> str | None:
	cdp = await session.get_or_create_cdp_session()
	response = await cdp.cdp_client.send.Runtime.evaluate(
		params={'expression': expression, 'returnByValue': True}, session_id=cdp.session_id
	)
	return response.get('result', {}).get('value')


async def open_and_settle(session: BrowserSession, url: str) -> None:
	await assert_allowed(url)
	await asyncio.wait_for(session.event_bus.dispatch(NavigateToUrlEvent(url=url, new_tab=False)), timeout=NAV_TIMEOUT)
	await asyncio.sleep(SETTLE)
	cdp = await session.get_or_create_cdp_session()
	for _ in range(SCROLLS):
		await cdp.cdp_client.send.Runtime.evaluate(
			params={'expression': 'window.scrollTo(0, document.body.scrollHeight)', 'returnByValue': True},
			session_id=cdp.session_id,
		)
		await asyncio.sleep(SCROLL_PAUSE)


async def read_board(session: BrowserSession, url: str, item_pattern: str, spec: dict) -> list[dict]:
	await open_and_settle(session, url)
	expression = (
		JS_EXTRACT.replace('__PATTERN__', json.dumps(item_pattern))
		.replace('__TITLE_SEL__', json.dumps(spec.get('title_selector')) if spec.get('title_selector') else 'null')
		.replace('__RANK_SEL__', json.dumps(spec.get('rank_selector')) if spec.get('rank_selector') else 'null')
	)
	raw = await evaluate(session, expression)
	return json.loads(raw) if raw else []


async def collect_source(name: str, spec: dict, args) -> list[dict]:
	base = f'https://{spec["host"]}'
	rows: list[dict] = []
	now = dt.datetime.now(dt.timezone.utc).isoformat()

	with tempfile.TemporaryDirectory(prefix=f'{name}_cat_') as profile_dir:
		profile = BrowserProfile(
			headless=not args.headful,
			keep_alive=False,
			user_data_dir=Path(profile_dir),
			allowed_domains=[spec['host'], f'*.{spec["host"].removeprefix("www.")}'],
		)
		session = BrowserSession(browser_profile=profile)
		try:
			await session.start()
			items = await read_board(session, spec['entry'], spec['item'], spec)
			boards_raw = await evaluate(session, JS_BOARDS.replace('__PATTERN__', json.dumps(spec['board_link'])))
			boards = json.loads(boards_raw) if boards_raw else []
			keep_entry = spec.get('collect_entry', True)
			print(f'  {name}: entry {len(items)} items ({"kept" if keep_entry else "board list only"}), {len(boards)} boards')
			if keep_entry:
				for item in items:
					rows.append({**item, 'board': 'entry', 'board_name': 'entry'})

			for board in boards[: args.boards or spec['max_boards']]:
				url = base + board['path']
				try:
					found = await read_board(session, url, spec['item'], spec)
				except RobotsRefusal as exc:
					print(f'    {board["path"]:28} REFUSED ({exc})')
					continue
				except Exception as exc:  # noqa: BLE001
					print(f'    {board["path"]:28} failed ({type(exc).__name__})')
					continue
				for item in found:
					rows.append({**item, 'board': board['path'], 'board_name': board['name'] or board['path']})
				print(f'    {board["path"]:28} {len(found):>4} items  {board["name"][:24]}')
				await asyncio.sleep(BOARD_PAUSE)
		finally:
			await session.kill()

	for row in rows:
		row |= {'source': spec['host'], 'kind': spec['kind'], 'url': base + row['path'], 'observed_at': now}
	return rows


def append_deduped(path: Path, rows: list[dict], key) -> int:
	seen: set = set()
	if path.exists():
		for line in path.open(encoding='utf-8'):
			try:
				seen.add(key(json.loads(line)))
			except Exception:  # noqa: BLE001
				continue
	fresh = []
	for row in rows:
		k = key(row)
		if k in seen:
			continue
		seen.add(k)
		fresh.append(row)
	if fresh:
		path.parent.mkdir(parents=True, exist_ok=True)
		with path.open('a', encoding='utf-8') as handle:
			for row in fresh:
				handle.write(json.dumps(row, ensure_ascii=False) + '\n')
	return len(fresh)


async def main() -> None:
	parser = argparse.ArgumentParser()
	parser.add_argument('--sources', nargs='*', default=list(SOURCES), choices=list(SOURCES))
	parser.add_argument('--boards', type=int, help='boards per source (default: the source cap)')
	parser.add_argument('--headful', action='store_true')
	args = parser.parse_args()

	today = dt.date.today().isoformat()
	snap_dir = OUT_DIR / 'snapshots' / today
	snap_dir.mkdir(parents=True, exist_ok=True)
	totals: dict[str, int] = {}

	for name in args.sources:
		spec = SOURCES[name]
		try:
			rows = await collect_source(name, spec, args)
		except RobotsRefusal as exc:
			print(f'  {name}: REFUSED before any request ({exc})')
			continue
		except Exception as exc:  # noqa: BLE001
			print(f'  {name}: FAILED {type(exc).__name__}: {exc}')
			continue
		if not rows:
			print(f'  {name}: no items — treat as a detector failure until proven otherwise')
			continue

		source_dir = OUT_DIR / name
		(snap_dir / f'{name}.json').write_text(
			json.dumps({'source': name, 'date': today, 'count': len(rows), 'items': rows}, ensure_ascii=False, indent=1),
			encoding='utf-8',
		)
		unique = {(r['board'], r['id']): r for r in rows}
		new_items = append_deduped(source_dir / 'items.jsonl', list(unique.values()), lambda r: r['id'])
		observations = [
			{
				'source': r['source'],
				'ranking_name': r['board_name'],
				'rank_type': 'PLATFORM_INTERNAL',
				'entity_type': 'work',
				'entity_id': r['id'],
				'entity_title': r['title'],
				'scope': {'type': 'platform', 'platform': name, 'board': r['board']},
				'period': {'type': 'daily'},
				'rank': r['rank'],
				'raw_metric_name': None,
				'raw_score': None,
				'label': r.get('label'),
				'source_url': r['url'],
				'observed_at': r['observed_at'],
			}
			for r in rows
		]
		source_dir.mkdir(parents=True, exist_ok=True)
		with (source_dir / 'observations.jsonl').open('a', encoding='utf-8') as handle:
			for observation in observations:
				handle.write(json.dumps(observation, ensure_ascii=False) + '\n')

		totals[name] = len(unique)
		boards = Counter(r['board_name'] for r in rows)
		print(f'  {name}: {len(rows)} ranked rows, {len(unique)} unique (board,id), +{new_items} new works')
		for board, count in boards.most_common(5):
			print(f'      {str(board)[:28]:30} {count}')

	print(f'\nDONE -> {OUT_DIR} ({", ".join(f"{k}={v}" for k, v in totals.items()) or "nothing collected"})')


if __name__ == '__main__':
	asyncio.run(main())
