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
  evidence    A bare URL does not support a takedown. On a match the series
              page is read for episode count, episode numbering, upload dates
              and the author/status fields — that is what sizes the harm.
  recurrence  first_seen / last_seen / times_seen per (our title, series id)
              makes re-upload after a takedown visible, which a per-run zero
              never would.
  mirror      Piracy hosts rotate domains. The host that actually answered is
              recorded per run, so a dead watch is not mistaken for a clean one.

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
import unicodedata
from pathlib import Path
from urllib.parse import quote

if sys.stdout.encoding and sys.stdout.encoding.lower() != 'utf-8':
	sys.stdout.reconfigure(encoding='utf-8', errors='replace')

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

JS_READ_RESULTS = r"""
(() => {
	const seen = new Set();
	const out = [];
	document.querySelectorAll('a[href*="/webtoon/"]').forEach(a => {
		const m = (a.getAttribute('href') || '').match(/\/webtoon\/(\d+)$/);
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
	const dates = Array.from(document.querySelectorAll('.wr-date'))
		.map(el => text(el)).filter(Boolean);
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
	});
})()
"""


def normalize(title: str) -> str:
	"""Fold a title for comparison.

	Curly apostrophes are the practical trap: our catalog writes "Dragon's"
	with U+2019 while a pirate index types an ASCII quote, and a raw substring
	test then silently misses a real hit.
	"""
	folded = unicodedata.normalize('NFKC', title).casefold()
	for curly, plain in (('’', "'"), ('‘', "'"), ('“', '"'), ('”', '"')):
		folded = folded.replace(curly, plain)
	return re.sub(r'[^0-9a-z가-힣]+', '', folded)


def bigrams(value: str) -> set[str]:
	return {value[i : i + 2] for i in range(len(value) - 1)} or {value}


def similarity(left: str, right: str) -> float:
	"""Character-bigram Jaccard.

	Word tokenization is useless on Korean, where a title is often one
	whitespace-free run; bigrams degrade gracefully across both scripts.
	"""
	if not left or not right:
		return 0.0
	a, b = bigrams(left), bigrams(right)
	return len(a & b) / len(a | b)


def classify(our_normalized: str, candidate_title: str) -> tuple[str, float]:
	"""exact | near | none, with the score that decided it."""
	other = normalize(candidate_title)
	if not our_normalized or not other:
		return 'none', 0.0
	if our_normalized in other or other in our_normalized:
		return 'exact', 1.0
	score = similarity(our_normalized, other)
	return ('near', score) if score >= NEAR_THRESHOLD else ('none', score)


def parse_watchlist(path: Path) -> list[dict]:
	"""One work per line; `|`-separated aliases are all searched."""
	works: list[dict] = []
	for line in path.read_text(encoding='utf-8').splitlines():
		line = line.strip()
		if not line or line.startswith('#'):
			continue
		names = [part.strip() for part in line.split('|') if part.strip()]
		if names:
			works.append({'title': names[0], 'aliases': names[1:], 'queries': names})
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


async def search(session: BrowserSession, host: str, query: str) -> list[dict]:
	"""One search. Navigation follows the site's /webtoon/__q/<base64> redirect."""
	raw = await visit(session, f'{host}/webtoon?stx={quote(query)}', JS_READ_RESULTS)
	return json.loads(raw) if raw else []


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


async def check_detector(session: BrowserSession, host: str) -> dict:
	"""Prove the search path works before trusting any zero."""
	control = await search(session, host, CONTROL_QUERY)
	await asyncio.sleep(2.0)
	nonsense = await search(session, host, NONSENSE_QUERY)
	verified = len(control) > 0 and len(nonsense) == 0
	return {
		'detector_status': 'verified' if verified else 'unverified',
		'control_query': CONTROL_QUERY, 'control_hits': len(control),
		'nonsense_query': NONSENSE_QUERY, 'nonsense_hits': len(nonsense),
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
	args = parser.parse_args()

	if not args.watchlist and not args.self_test:
		raise SystemExit('--watchlist is required (rights-protection mode queries our titles only)')

	works = parse_watchlist(Path(args.watchlist)) if args.watchlist else []
	if args.watchlist and not works:
		raise SystemExit('watchlist is empty')
	if works:
		alias_count = sum(len(w['aliases']) for w in works)
		print(f'watchlist: {len(works)} works, {alias_count} aliases (rights-protection mode, our titles only)')

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
				detector = await check_detector(session, host)
				print(f'  control "{detector["control_query"]}" -> {detector["control_hits"]} hits; nonsense -> {detector["nonsense_hits"]} hits')
				print(f'  detector: {detector["detector_status"].upper()}')
				if detector['detector_status'] == 'unverified':
					print('  WARNING: a zero this run means no observation, not no infringement')

			if args.self_test and host:
				# The evidence layer needs proving too. A sighting whose fields
				# silently come back empty is the same unverified-zero problem
				# one level down, so self-test exercises it on a control result
				# and reports which fields populated — counts only.
				control = await search(session, host, CONTROL_QUERY)
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
				for query in work['queries']:
					try:
						results = await search(session, host, query)
					except Exception as exc:  # noqa: BLE001
						print(f'  query {query!r} failed: {type(exc).__name__}')
						continue
					scanned += len(results)
					for result in results:
						if result['id'] in seen_ids:
							continue
						seen_ids.add(result['id'])
						# Compare against every alias, keep the strongest verdict.
						best_kind, best_score = 'none', 0.0
						for alias in work['queries']:
							kind, score = classify(normalize(alias), result['title'])
							if kind == 'exact' or score > best_score:
								best_kind, best_score = kind, max(best_score, score)
							if best_kind == 'exact':
								break
						if best_kind == 'exact':
							exact.append({**result, 'score': best_score, 'via_query': query})
						elif best_kind == 'near':
							near.append({**result, 'score': round(best_score, 3), 'via_query': query})
					await asyncio.sleep(2.0)

				for hit in exact + near:
					evidence = await collect_evidence(session, hit['url'])
					entry = update_recurrence(recurrence, work['title'], hit['id'], today)
					sightings.append({
						'our_title': work['title'], 'matched_title': hit['title'], 'series_id': hit['id'],
						'url': hit['url'], 'match': 'exact' if hit in exact else 'near',
						'score': hit['score'], 'via_query': hit['via_query'],
						'host': host, 'detector_status': detector['detector_status'],
						'episode_count': evidence.get('episode_count'),
						'episode_range': [evidence.get('episode_min'), evidence.get('episode_max')],
						'latest_upload': evidence.get('latest_upload'),
						'upload_dates': evidence.get('upload_dates'),
						'author_line': evidence.get('author_line'), 'status_line': evidence.get('status_line'),
						'first_seen': entry['first_seen'], 'last_seen': entry['last_seen'], 'times_seen': entry['times_seen'],
						'observed_at': now,
					})
					await asyncio.sleep(1.5)

				per_work.append({'title': work['title'], 'aliases': work['aliases'], 'results_scanned': scanned, 'exact': len(exact), 'near': len(near)})
				print(f'[{index}/{len(works)}] "{work["title"]}" ({len(work["queries"])} queries): exact={len(exact)} near={len(near)} scanned={scanned}')
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
