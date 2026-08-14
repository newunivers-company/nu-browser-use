"""Newtoki infringement watch — OUR TITLES ONLY.

Purpose: rights protection. Searches newtoki for a watchlist of titles WE own
or distribute and logs every match as takedown evidence. This is the
anti-piracy monitoring pattern (cf. MUSO), not a catalog crawler.

Scope guardrails (do not widen):
  - queries come ONLY from the watchlist file
  - a series page is fetched ONLY for a title that matched ours, to size the
    infringement; nothing is fetched for anything else
  - evidence is counts, dates and URLs — no episode content, no images
  - no site cataloging, no listing enumeration, no index building

WHY THE CONTROL QUERY EXISTS
The 2026-08-14 run reported 0 hits on all five titles. That reading was not
trustworthy: the site had moved search behind a redirect (`?stx=` now 302s to
`/webtoon/__q/<base64 of the query string>`), so a broken detector and a clean
result were indistinguishable. For a rights-protection tool that is the worst
failure mode — silence read as safety. Every run now proves the search path
works before any zero is believed.

DEPTH (why each layer exists)
  aliases     A Korean webtoon index will not carry our English title. Each
              watchlist line may list localized and alternate names, all
              queried, because searching only the English name is close to
              guaranteeing a false negative.
  near-match  Pirates rename. Exact substring matching misses "Dragons Good
              Girl" or a partial. Character-bigram Jaccard is used because it
              degrades sensibly on Korean, where whitespace tokenization does
              not.
  fragments   The site matches titles by substring, so a rename that mangles
              the middle escapes a whole-title query. Querying head, centre and
              tail windows of our own title recovers those: any single mid-title
              edit leaves at least one window intact. Fragment hits only ever
              land in the near tier — a piece of our title matching some other
              work is the expected outcome of a crowded fragment query, not a
              sighting.
  evidence    A bare URL does not support a takedown. On a match the series
              page is read for episode count, episode numbering, upload dates,
              the author/status fields, and the URLs and hosts serving the
              images — that is what sizes the harm and tells a notice where to
              aim. Asset URLs and counts only: no image bytes are fetched, and
              no copy of the infringing work is made or stored.
  recurrence  first_seen / last_seen / times_seen per (our title, series id)
              makes re-upload after a takedown visible, which a per-run zero
              never would.
  mirror      Piracy hosts rotate domains. The host that actually answered is
              recorded per run, so a dead watch is not mistaken for a clean one.
  sections    The site runs four independent indexes — /webtoon, /manhwa,
              /novel, /anime — each searchable and each verified separately.
              Querying only /webtoon, as this did originally, made infringement
              of our webnovel IP in /novel structurally invisible; that is a
              blind spot no matcher tuning would ever have surfaced.

Usage:
  python newtoki_watch.py --watchlist watchlist.txt
  python newtoki_watch.py --self-test
Watchlist format — one work per line, aliases after `|`:
  The Dragon's Good Girl | 용의 착한 소녀
Output (NEWTOKI_OUT, default ~/newtoki_watch):
  sightings.jsonl      - append-only evidence records
  recurrence.json      - first/last seen per (our title, series id)
  runs/YYYY-MM-DD.json - per-query summary + detector status + host
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import os
import re
import sys
import tempfile
from pathlib import Path
from urllib.parse import quote

if sys.stdout.encoding and sys.stdout.encoding.lower() != 'utf-8':
	sys.stdout.reconfigure(encoding='utf-8', errors='replace')

from textmatch import classify as _classify
from textmatch import normalize

from browser_use.browser.events import NavigateToUrlEvent
from browser_use.browser.profile import BrowserProfile
from browser_use.browser.session import BrowserSession

# Known mirrors, most recent first. Piracy hosts rotate; the watch picks the
# first that answers and records which one, so a dead domain reads as "not
# checked" rather than "nothing found".
MIRRORS = [h for h in os.environ.get('NEWTOKI_HOSTS', 'https://newtoki1.org').split(',') if h]
OUT_DIR = Path(os.environ.get('NEWTOKI_OUT', str(Path.home() / 'newtoki_watch')))
ALLOWED = ['newtoki1.org', '*.newtoki1.org', 'newtoki.org', '*.newtoki.org']
QUERY_WAIT = 4.0
NAV_TIMEOUT = 45.0
PAUSE_EVERY = 50
PAUSE_SECONDS = 30.0
NEAR_THRESHOLD = 0.55  # bigram Jaccard above this is worth a human look
CONTROL_QUERY = '사랑'
NONSENSE_QUERY = 'zzzqqxnotitle'
# The site carries four independent content sections, each with its own working
# search index (controls measured 2026-08-14: 37/10/14/5 hits, 0 on nonsense).
# The watch previously queried only /webtoon, so infringement of our webnovel
# IP in /novel could not have been seen at all — a blind spot no amount of
# matcher tuning would have surfaced.
SECTIONS = [s for s in os.environ.get('NEWTOKI_SECTIONS', 'webtoon,manhwa,novel,anime').split(',') if s]

JS_READ_RESULTS = r"""
(() => {
	const section = '__SECTION__';
	const seen = new Set();
	const out = [];
	document.querySelectorAll('a[href*="/' + section + '/"]').forEach(a => {
		// Double-escaped on purpose: this is a JS *string* compiled to a regex,
		// so a single backslash would be eaten by the string literal.
		const m = (a.getAttribute('href') || '').match(new RegExp('/' + section + '/(\\d+)$'));
		if (!m) return;
		const text = (a.textContent || '').replace(/\s+/g, ' ').trim();
		if (!text || seen.has(m[1])) return;
		seen.add(m[1]);
		out.push({id: m[1], url: a.href, title: text.slice(0, 120)});
	});
	return JSON.stringify(out);
})()
"""

# Read ONLY on a confirmed match, to size the infringement of our own work.
JS_READ_EVIDENCE = r"""
(() => {
	const text = el => el ? (el.innerText || '').replace(/\s+/g, ' ').trim() : null;
	const episodes = Array.from(document.querySelectorAll('a.item-subject'))
		.map(a => (a.getAttribute('href') || '').match(/nv-\d+-(\d+)/))
		.filter(Boolean).map(m => parseInt(m[1], 10));
	// The listing's header cell carries the same class as the data cells, so an
	// unfiltered read makes the column label ("날짜") the latest upload date.
	const dates = Array.from(document.querySelectorAll('.wr-date'))
		.map(el => text(el)).filter(v => v && /\d{2,4}[.\-/]\d{1,2}/.test(v));
	const body = document.body.innerText;
	const views = (body.match(/조회\s*([\d,]+)/g) || []).slice(0, 5);
	return JSON.stringify({
		url: location.href,
		series_title: text(document.querySelector('.view-title, .toon-title, h1')),
		episode_count: episodes.length,
		episode_min: episodes.length ? Math.min.apply(null, episodes) : null,
		episode_max: episodes.length ? Math.max.apply(null, episodes) : null,
		upload_dates: dates.slice(0, 12),
		latest_upload: dates.length ? dates[0] : null,
		view_markers: views,
		author_line: (body.match(/작가[^\n]{0,60}/) || [])[0] || null,
		status_line: (body.match(/연재[^\n]{0,40}/) || [])[0] || null,
		// Asset URLs, hosts and counts only. A notice cites the URLs that serve
		// the infringing copies; it does not require, and this never makes, a
		// copy of them. No image bytes are fetched or stored at any point.
		cover_url: (document.querySelector('meta[property="og:image"]') || {}).content || null,
		image_hosts: Array.from(new Set(Array.from(document.querySelectorAll('img[src]'))
			.map(i => { try { return new URL(i.src, location.href).host; } catch (e) { return null; } })
			.filter(Boolean))).slice(0, 8),
		image_asset_count: document.querySelectorAll('img[src]').length,
	});
})()
"""


def classify(our_normalized: str, candidate_title: str) -> tuple[str, float]:
	"""exact | near | none, at this watch's near threshold."""
	return _classify(our_normalized, candidate_title, NEAR_THRESHOLD)


# Fragment probing. Site search is substring-based (measured: whitespace
# removal, 60% prefix and a dropped final character all resolve at 100%, a
# character deleted mid-title at ~60%). So a rename that mangles the middle
# escapes a whole-title query but is still caught by a query made of a piece
# that survived. Fragments are deliberately long — short ones return crowded
# result sets that cost review time without adding reach.
FRAGMENT_MIN_CJK = 5
FRAGMENT_MIN_LATIN = 10


def fragments(title: str) -> list[str]:
	"""Distinctive substrings of a title, longest first.

	Windows are taken over the raw title rather than the normalized form
	because the query goes to the site, which does its own folding; we only
	need the piece to be long enough to be distinctive.
	"""
	compact = re.sub(r'\s+', ' ', title).strip()
	if not compact:
		return []
	is_cjk = bool(re.search(r'[가-힣]', compact))
	minimum = FRAGMENT_MIN_CJK if is_cjk else FRAGMENT_MIN_LATIN
	if len(compact) <= minimum:
		return []
	window = max(minimum, int(len(compact) * 0.6))
	if window >= len(compact):
		window = len(compact) - 1
	out: list[str] = []
	# Head, tail and centre: between them, any single mid-title edit leaves at
	# least one window intact.
	for start in (0, len(compact) - window, max(0, (len(compact) - window) // 2)):
		piece = compact[start : start + window].strip()
		if len(piece) >= minimum and piece not in out and piece != compact:
			out.append(piece)
	return out


def parse_watchlist(path: Path) -> list[dict]:
	"""One work per line; `|`-separated aliases are all searched."""
	works: list[dict] = []
	for line in path.read_text(encoding='utf-8').splitlines():
		line = line.strip()
		if not line or line.startswith('#'):
			continue
		names = [part.strip() for part in line.split('|') if part.strip()]
		if names:
			probes: list[str] = []
			for name in names:
				for piece in fragments(name):
					if piece not in names and piece not in probes:
						probes.append(piece)
			works.append({'title': names[0], 'aliases': names[1:], 'queries': names, 'fragments': probes})
	return works


async def evaluate(session: BrowserSession, expression: str) -> str | None:
	cdp_session = await session.get_or_create_cdp_session()
	response = await cdp_session.cdp_client.send.Runtime.evaluate(
		params={'expression': expression, 'returnByValue': True}, session_id=cdp_session.session_id
	)
	return response.get('result', {}).get('value')


async def visit(session: BrowserSession, url: str, expression: str) -> str | None:
	await asyncio.wait_for(session.event_bus.dispatch(NavigateToUrlEvent(url=url, new_tab=False)), timeout=NAV_TIMEOUT)
	await asyncio.sleep(QUERY_WAIT)
	return await evaluate(session, expression)


async def search(session: BrowserSession, host: str, query: str, section: str = 'webtoon') -> list[dict]:
	"""One search in one section. Navigation follows the site's __q/<base64> redirect."""
	raw = await visit(session, f'{host}/{section}?stx={quote(query)}', JS_READ_RESULTS.replace('__SECTION__', section))
	return json.loads(raw) if raw else []


async def search_all(session: BrowserSession, host: str, query: str, sections: list[str]) -> list[dict]:
	"""Same query across every section; results tagged with where they came from."""
	found: list[dict] = []
	for section in sections:
		try:
			for row in await search(session, host, query, section):
				found.append({**row, 'section': section})
		except Exception as exc:  # noqa: BLE001
			print(f'  section {section}: query failed ({type(exc).__name__})')
		await asyncio.sleep(1.2)
	return found


async def pick_host(session: BrowserSession) -> tuple[str | None, list[dict]]:
	"""First mirror that answers with a usable index."""
	tried: list[dict] = []
	for host in MIRRORS:
		try:
			results = await search(session, host, CONTROL_QUERY)
		except Exception as exc:  # noqa: BLE001
			tried.append({'host': host, 'ok': False, 'error': type(exc).__name__})
			continue
		tried.append({'host': host, 'ok': bool(results), 'control_hits': len(results)})
		if results:
			return host, tried
	return None, tried


async def check_detector(session: BrowserSession, host: str, sections: list[str]) -> dict:
	"""Prove the search path works in EVERY section before trusting any zero.

	Verified per section, not once overall: a section whose index is down would
	otherwise hide behind a healthy sibling and contribute a silent zero.
	"""
	per_section: dict[str, dict] = {}
	for section in sections:
		control = await search(session, host, CONTROL_QUERY, section)
		await asyncio.sleep(1.5)
		nonsense = await search(session, host, NONSENSE_QUERY, section)
		per_section[section] = {
			'control_hits': len(control), 'nonsense_hits': len(nonsense),
			'status': 'verified' if control and not nonsense else 'unverified',
		}
		await asyncio.sleep(1.5)
	verified = [s for s, v in per_section.items() if v['status'] == 'verified']
	return {
		'detector_status': 'verified' if len(verified) == len(sections) else ('partial' if verified else 'unverified'),
		'control_query': CONTROL_QUERY, 'nonsense_query': NONSENSE_QUERY,
		'sections': per_section, 'sections_verified': verified,
		'control_hits': sum(v['control_hits'] for v in per_section.values()),
		'nonsense_hits': sum(v['nonsense_hits'] for v in per_section.values()),
	}


async def collect_evidence(session: BrowserSession, url: str) -> dict:
	"""Size the infringement of one matched work. Called only on a match."""
	try:
		raw = await visit(session, url, JS_READ_EVIDENCE)
		return json.loads(raw) if raw else {}
	except Exception as exc:  # noqa: BLE001
		return {'error': type(exc).__name__}


def load_recurrence() -> dict:
	path = OUT_DIR / 'recurrence.json'
	return json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}


def update_recurrence(store: dict, our_title: str, series_id: str, today: str) -> dict:
	"""Track re-appearance, which a per-run count cannot show."""
	key = f'{our_title}::{series_id}'
	entry = store.get(key)
	if entry is None:
		entry = {'our_title': our_title, 'series_id': series_id, 'first_seen': today, 'last_seen': today, 'times_seen': 1}
		store[key] = entry
	else:
		if entry['last_seen'] != today:
			entry['times_seen'] += 1
		entry['last_seen'] = today
	return entry


async def main() -> None:
	parser = argparse.ArgumentParser()
	parser.add_argument('--watchlist', help='text file, one of OUR titles per line; `|` separates aliases')
	parser.add_argument('--self-test', action='store_true', help='run the control queries only')
	parser.add_argument('--headful', action='store_true')
	parser.add_argument('--no-fragments', action='store_true', help='whole titles only; skip substring probes')
	args = parser.parse_args()
	use_fragments = not args.no_fragments

	if not args.watchlist and not args.self_test:
		raise SystemExit('--watchlist is required (rights-protection mode queries our titles only)')

	works = parse_watchlist(Path(args.watchlist)) if args.watchlist else []
	if args.watchlist and not works:
		raise SystemExit('watchlist is empty')
	if works:
		alias_count = sum(len(w['aliases']) for w in works)
		fragment_count = sum(len(w['fragments']) for w in works) if use_fragments else 0
		print(f'watchlist: {len(works)} works, {alias_count} aliases, {fragment_count} fragment probes (rights-protection mode, our titles only)')

	today = dt.date.today().isoformat()
	now = dt.datetime.now(dt.timezone.utc).isoformat()
	runs_dir = OUT_DIR / 'runs'
	runs_dir.mkdir(parents=True, exist_ok=True)
	recurrence = load_recurrence()

	per_work: list[dict] = []
	sightings: list[dict] = []
	with tempfile.TemporaryDirectory(prefix='newtoki_') as profile_dir:
		profile = BrowserProfile(headless=not args.headful, keep_alive=False, user_data_dir=Path(profile_dir), allowed_domains=ALLOWED)
		session = BrowserSession(browser_profile=profile)
		detector: dict = {'detector_status': 'unverified'}
		host = None
		try:
			await session.start()
			print('[host] locating a live mirror')
			host, tried = await pick_host(session)
			for attempt in tried:
				print(f'  {attempt["host"]}: ' + ('ok' if attempt.get('ok') else attempt.get('error', 'no results')))
			if host is None:
				print('  NO LIVE MIRROR — this run makes no claim about infringement')
			else:
				print(f'[control] verifying the detector on {host}')
				detector = await check_detector(session, host, SECTIONS)
				for section, stats in detector['sections'].items():
					print(f'  /{section:9} control={stats["control_hits"]:<4} nonsense={stats["nonsense_hits"]:<3} {stats["status"]}')
				print(f'  detector: {detector["detector_status"].upper()} ({len(detector["sections_verified"])}/{len(SECTIONS)} sections)')
				if detector['detector_status'] != 'verified':
					blind = [s for s in SECTIONS if s not in detector['sections_verified']]
					print(f'  WARNING: no observation for {", ".join(blind) or "any section"} — a zero there means nothing')

			if args.self_test and host:
				# The evidence layer needs proving too. A sighting whose fields
				# silently come back empty is the same unverified-zero problem
				# one level down, so self-test exercises it on a control result
				# and reports which fields populated — counts only.
				control = await search(session, host, CONTROL_QUERY, SECTIONS[0])
				if control:
					evidence = await collect_evidence(session, control[0]['url'])
					populated = sorted(k for k, v in evidence.items() if v not in (None, [], '', 0) and k != 'url')
					detector['evidence_fields'] = populated
					detector['evidence_status'] = 'verified' if evidence.get('episode_count') else 'unverified'
					print(f'[control] evidence extraction: {detector["evidence_status"].upper()}')
					print(f'  episodes={evidence.get("episode_count")} range={evidence.get("episode_min")}-{evidence.get("episode_max")} dates={len(evidence.get("upload_dates") or [])}')
					print(f'  populated fields: {", ".join(populated)}')

			targets = works if host else []
			for index, work in enumerate(targets, 1):
				if index > 1 and (index - 1) % PAUSE_EVERY == 0:
					print(f'  ...cooldown {PAUSE_SECONDS:.0f}s after {index - 1} works')
					await asyncio.sleep(PAUSE_SECONDS)
				needle = normalize(work['title'])
				seen_ids: set[str] = set()
				exact: list[dict] = []
				near: list[dict] = []
				scanned = 0
				# Whole names first, then fragments. Fragment hits are never promoted
				# to `exact` on their own — a piece of our title matching some
				# other work is exactly what a crowded fragment query returns.
				probe_plan = [(name, False) for name in work['queries']] + [(piece, True) for piece in (work['fragments'] if use_fragments else [])]
				for query, is_fragment in probe_plan:
					try:
						results = await search_all(session, host, query, SECTIONS)
					except Exception as exc:  # noqa: BLE001
						print(f'  query {query!r} failed: {type(exc).__name__}')
						continue
					scanned += len(results)
					for result in results:
						key = f'{result["section"]}:{result["id"]}'
						if key in seen_ids:
							continue
						seen_ids.add(key)
						# Compare against every alias, keep the strongest verdict.
						best_kind, best_score = 'none', 0.0
						for alias in work['queries']:
							kind, score = classify(normalize(alias), result['title'])
							if kind == 'exact' or score > best_score:
								best_kind, best_score = kind, max(best_score, score)
							if best_kind == 'exact':
								break
						record = {**result, 'score': round(best_score, 3), 'via_query': query, 'via_fragment': is_fragment}
						if best_kind == 'exact' and not is_fragment:
							exact.append({**record, 'score': best_score})
						elif best_kind in ('exact', 'near'):
							near.append(record)
					await asyncio.sleep(2.0)

				for hit in exact + near:
					evidence = await collect_evidence(session, hit['url'])
					entry = update_recurrence(recurrence, work['title'], f'{hit["section"]}:{hit["id"]}', today)
					sightings.append({
						'our_title': work['title'], 'matched_title': hit['title'], 'series_id': hit['id'],
						'section': hit['section'],
						'url': hit['url'], 'match': 'exact' if hit in exact else 'near',
						'score': hit['score'], 'via_query': hit['via_query'],
						'host': host, 'detector_status': detector['detector_status'],
						'episode_count': evidence.get('episode_count'),
						'episode_range': [evidence.get('episode_min'), evidence.get('episode_max')],
						'latest_upload': evidence.get('latest_upload'),
						'upload_dates': evidence.get('upload_dates'),
						'author_line': evidence.get('author_line'), 'status_line': evidence.get('status_line'),
						'cover_url': evidence.get('cover_url'),
						'image_hosts': evidence.get('image_hosts'),
						'image_asset_count': evidence.get('image_asset_count'),
						'first_seen': entry['first_seen'], 'last_seen': entry['last_seen'], 'times_seen': entry['times_seen'],
						'observed_at': now,
					})
					await asyncio.sleep(1.5)

				per_work.append({
					'title': work['title'], 'aliases': work['aliases'],
					'fragments': work['fragments'] if use_fragments else [],
					'results_scanned': scanned, 'exact': len(exact), 'near': len(near),
					'near_via_fragment': sum(1 for h in near if h['via_fragment']),
				})
				sections_hit = sorted({h['section'] for h in exact + near})
				where = f' in {",".join(sections_hit)}' if sections_hit else ''
				print(f'[{index}/{len(works)}] "{work["title"]}" ({len(work["queries"])} queries x {len(SECTIONS)} sections): exact={len(exact)} near={len(near)} scanned={scanned}{where}')
		finally:
			await session.kill()

	if sightings:
		with (OUT_DIR / 'sightings.jsonl').open('a', encoding='utf-8') as handle:
			for record in sightings:
				handle.write(json.dumps(record, ensure_ascii=False) + '\n')
	(OUT_DIR / 'recurrence.json').write_text(json.dumps(recurrence, ensure_ascii=False, indent=2), encoding='utf-8')
	(runs_dir / f'{today}.json').write_text(
		json.dumps({'date': today, 'host': host, **detector, 'works': per_work, 'sightings': len(sightings)}, ensure_ascii=False, indent=2),
		encoding='utf-8',
	)

	exact_total = sum(1 for s in sightings if s['match'] == 'exact')
	print(f'\nDONE -> {OUT_DIR}')
	print(f'  works: {len(per_work)} | exact: {exact_total} | near (needs review): {len(sightings) - exact_total}')
	if host and detector['detector_status'] == 'verified':
		print('  detector verified — a zero above is a real absence of matches')
	else:
		print('  detector UNVERIFIED or no live mirror — this run makes no claim about infringement')
	if sightings:
		print(f'  evidence log: {OUT_DIR / "sightings.jsonl"}')


if __name__ == '__main__':
	asyncio.run(main())
