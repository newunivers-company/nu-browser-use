"""Vigloo engagement snapshot collector.

The /<locale>/video/<id>?episode=1 pages SSR a fresher `program` object than
the /content/ pages (verified: view/bookmark counts update there first), so
they are the right surface for point-in-time engagement snapshots.

Each run writes snapshots/YYYY-MM-DD.json with view/like/bookmark counts for
every program, and prints deltas against the most recent previous snapshot —
turning the catalog into a time series for NU Signal trend analysis.

Output (VIGLOO_SNAP_OUT, default <VIGLOO_OUT>/snapshots):
  snapshots/YYYY-MM-DD.json
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import os
import re
from pathlib import Path

import aiohttp

UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36'
DEFAULT_PROGRAMS = Path(os.environ.get('VIGLOO_OUT', str(Path.home() / 'vigloo_export'))) / 'programs.json'
OUT_DIR = Path(os.environ.get('VIGLOO_SNAP_OUT', str(DEFAULT_PROGRAMS.parent / 'snapshots')))
NEXT_DATA_RE = re.compile(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', re.S)
VIDEO_URL = 'https://www.vigloo.com/{locale}/video/{program_id}?episode=1'
CONTENT_URL = 'https://www.vigloo.com/{locale}/content/{program_id}'
HOME_URL = 'https://www.vigloo.com/{locale}'


async def fetch_rank_bundle(session: aiohttp.ClientSession, locale: str) -> list[dict]:
	"""Read the homepage 인기/trending rankBundle (top 10) from __NEXT_DATA__."""
	try:
		async with session.get(HOME_URL.format(locale=locale), timeout=aiohttp.ClientTimeout(total=30)) as response:
			if response.status != 200:
				return []
			html = await response.text()
	except Exception:  # noqa: BLE001
		return []
	match = NEXT_DATA_RE.search(html)
	if not match:
		return []
	try:
		props = json.loads(match.group(1))['props']['pageProps']
	except (json.JSONDecodeError, KeyError):
		return []
	bundle = []
	for position, entry in enumerate(props.get('rankBundle') or [], 1):
		program = entry.get('program') or {}
		if not program.get('id'):
			continue
		bundle.append({
			'rank': position,
			'id': str(program['id']),
			'title': program.get('title') or '',
			'viewCount': int(program.get('viewCount') or 0),
			'bookmarkCount': int(program.get('bookmarkCount') or 0),
		})
	return bundle


async def fetch_program(session: aiohttp.ClientSession, url: str) -> dict | None:
	"""GET a page and return its SSR program object, if present."""
	async with session.get(url, timeout=aiohttp.ClientTimeout(total=30)) as response:
		if response.status != 200:
			return None
		html = await response.text()
	match = NEXT_DATA_RE.search(html)
	if not match:
		return None
	try:
		return json.loads(match.group(1))['props']['pageProps']['program']
	except (json.JSONDecodeError, KeyError, TypeError):
		return None


async def snapshot(session: aiohttp.ClientSession, program_id: str, locale: str, semaphore: asyncio.Semaphore) -> dict | None:
	"""Fetch one program's engagement counts: freshest video page, else content page.

	The newest programs SSR program=None on the web video surface (app-only),
	so the slightly staler /content/ counts stand in for those.
	"""
	async with semaphore:
		try:
			program = await fetch_program(session, VIDEO_URL.format(locale=locale, program_id=program_id))
			if not program:
				program = await fetch_program(session, CONTENT_URL.format(locale=locale, program_id=program_id))
		except Exception:  # noqa: BLE001 - one missed program is not a failed run
			return None
		if not program:
			return None
		return {
			'id': program_id,
			'viewCount': int(program.get('viewCount') or 0),
			'likeCount': int(program.get('likeCount') or 0),
			'bookmarkCount': int(program.get('bookmarkCount') or 0),
		}




def load_previous(snap_dir: Path, today: str) -> dict[str, dict] | None:
	"""Load the most recent snapshot before today, keyed by program id."""
	priors = sorted(p.stem for p in snap_dir.glob('????-??-??.json') if p.stem < today)
	if not priors:
		return None
	data = json.loads((snap_dir / f'{priors[-1]}.json').read_text(encoding='utf-8'))
	return {entry['id']: entry for entry in data.get('programs', [])}


def delta_report(current: dict[str, dict], previous: dict[str, dict] | None, prior_date: str | None) -> list[str]:
	"""Build top-mover lines comparing against the previous snapshot."""
	if not previous:
		return ['  (no prior snapshot - baseline run)']
	deltas = []
	for program_id, entry in current.items():
		prior = previous.get(program_id)
		if not prior:
			continue
		deltas.append((entry['viewCount'] - prior['viewCount'], entry['bookmarkCount'] - prior['bookmarkCount'], entry['viewCount'], program_id))
	deltas.sort(reverse=True)
	lines = [f'  vs {prior_date}: top view gains']
	for gain, bookmark_gain, views, program_id in deltas[:5]:
		lines.append(f'    {program_id}: +{gain:,} views (+{bookmark_gain:,} bookmarks) -> {views:,}')
	return lines


async def main() -> None:
	parser = argparse.ArgumentParser()
	parser.add_argument('--programs', type=Path, default=DEFAULT_PROGRAMS)
	parser.add_argument('--locale', default='en')
	args = parser.parse_args()

	programs = json.loads(args.programs.read_text(encoding='utf-8'))
	ids = [str(p['id']) for p in programs]
	today = dt.date.today().isoformat()
	OUT_DIR.mkdir(parents=True, exist_ok=True)

	print(f'{len(ids)} programs -> snapshot {today} (locale={args.locale})')
	semaphore = asyncio.Semaphore(8)
	async with aiohttp.ClientSession(headers={'User-Agent': UA}) as session:
		entries = await asyncio.gather(*(snapshot(session, pid, args.locale, semaphore) for pid in ids))
		rank_bundle = await fetch_rank_bundle(session, args.locale)
	current = {e['id']: e for e in entries if e}
	missed = len(ids) - len(current)

	previous = load_previous(OUT_DIR, today)
	prior_date = max((p.stem for p in OUT_DIR.glob('????-??-??.json') if p.stem < today), default=None)

	payload = {
		'date': today, 'locale': args.locale, 'count': len(current), 'missed': missed,
		'rank_bundle': rank_bundle,  # homepage 인기 top-10, ordinal = exposure order
		'programs': list(current.values()),
	}
	(OUT_DIR / f'{today}.json').write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')

	print(f'DONE -> {OUT_DIR / f"{today}.json"}  ({len(current)} ok, {missed} missed)')
	if rank_bundle:
		print(f'  rank bundle: {len(rank_bundle)} | #1: {rank_bundle[0]["title"][:40]} ({rank_bundle[0]["viewCount"]:,} views)')
	for line in delta_report(current, previous, prior_date):
		print(line)


if __name__ == '__main__':
	asyncio.run(main())
