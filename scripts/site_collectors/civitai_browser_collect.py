"""CivitAI model inventory from the rendered site, not the API.

CivitAI is the reference catalogue for image and video generation models, and
it was excluded earlier for a good reason and a wrong one. The good reason:
robots disallows /api/*, so the JSON API is off-limits. The wrong one: the
wildcard bug in urllib.robotparser hid which paths that rule actually covers.
With the parser corrected, /models and /images are plainly allowed while
/api/*, /search/* and /questions/* are not — so the web surface is reachable
and the API stays closed.

That makes this a browser job. The listing is client-rendered, so BrowserSession
drives it with allowed_domains pinned to the site, and the API is never called
even though it would be easier.

COUNTERS ARE READ THROUGH AN ANIMATION
Download and like figures render as a rolling-digit widget, so the card's text
contains the whole digit alphabet around the real value ("188.7K 0 1 2 3 4 …").
Single-character digit tokens are therefore dropped and the first two surviving
numbers taken as downloads and likes. It is a heuristic over a presentation
quirk, so `counts_raw` keeps the source text and a layout change shows up as
implausible numbers rather than silently wrong ones.

METADATA ONLY
No images are fetched — on this site the image is the work. No prompt text is
taken either: prompts attached to posted images are their authors' writing, and
what this needs is which models are used and how heavily. Kept per model: id,
slug, name, type (Checkpoint/LoRA/…), base-model tags, author, and the counters.

Output (CIVITAI_BROWSER_OUT, default ~/civitai_browser_export):
  snapshots/YYYY-MM-DD/models.json
  models.jsonl - appended, deduped on model id
"""

from __future__ import annotations

import argparse
import asyncio
import collections
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

sys.path.insert(0, str(Path(__file__).resolve().parent))

import aiohttp
from promo_registry_verify import robots_verdict, scalar_verdict

from browser_use.browser.events import NavigateToUrlEvent
from browser_use.browser.profile import BrowserProfile
from browser_use.browser.session import BrowserSession

BASE = 'https://civitai.com'
OUT_DIR = Path(os.environ.get('CIVITAI_BROWSER_OUT', str(Path.home() / 'civitai_browser_export')))
ALLOWED = ['civitai.com', '*.civitai.com']
UA = 'nu-browser-use/1.0 (+https://newunivers.com; nu@newunivers.com)'
SORTS = ('Most Downloaded', 'Highest Rated', 'Newest')
SETTLE = 9.0
SCROLLS = 4
NAV_TIMEOUT = 60.0
MODEL_RE = re.compile(r'/models/(\d+)/([a-z0-9-]+)')
COUNT_RE = re.compile(r'^([\d.]+)([KM])?$', re.I)
MULTIPLIER = {'K': 1_000, 'M': 1_000_000}
# A `B` suffix on this site is a parameter-count badge (a 4B model), not a
# counter — download figures here do not reach billions. Reading "4B" as four
# billion downloads is exactly the misfire the raw-text field exists to expose,
# and it did on the first run.
SIZE_BADGE_RE = re.compile(r'^[\d.]+B$', re.I)
IMPLAUSIBLE_DOWNLOADS = 50_000_000
# Card chrome that is not a base-model tag.
NOT_TAGS = {'CREATE', 'Early', 'Access', 'Updated', 'New'}

JS_CARDS = r"""
(() => {
	const out = [];
	const seen = new Set();
	document.querySelectorAll('a[href*="/models/"]').forEach(a => {
		const href = a.getAttribute('href') || '';
		if (!/\/models\/\d+/.test(href) || seen.has(href)) return;
		seen.add(href);
		const card = a.closest('div');
		out.push({
			href: href,
			card_text: card ? (card.innerText || '').replace(/\s+/g, ' ').trim().slice(0, 400) : '',
		});
	});
	return JSON.stringify(out);
})()
"""


async def robots_allows(path: str) -> tuple[bool, str]:
	"""Ask before navigating: /models is allowed here, /api/* is not."""
	async with aiohttp.ClientSession(headers={'User-Agent': UA}) as session:
		try:
			async with session.get(f'{BASE}/robots.txt', timeout=aiohttp.ClientTimeout(total=20)) as response:
				body = await response.text(errors='replace') if response.status == 200 else ''
		except Exception as exc:  # noqa: BLE001
			return False, type(exc).__name__
	if not body:
		return True, 'no robots.txt published'
	verdict = robots_verdict(body, path)
	scalar = scalar_verdict(verdict)
	return scalar in ('allow', 'unknown'), scalar


def parse_counts(text: str) -> tuple[list[int], str, str | None]:
	"""Numbers surviving the rolling-digit animation, the raw text, and any size badge."""
	badge = next((t for t in text.split() if SIZE_BADGE_RE.match(t)), None)
	tokens = [t for t in text.split() if COUNT_RE.match(t) and len(t) > 1]
	values: list[int] = []
	for token in tokens:
		match = COUNT_RE.match(token)
		number, suffix = match.groups()
		try:
			value = float(number)
		except ValueError:
			continue
		values.append(int(value * MULTIPLIER[suffix.upper()]) if suffix else int(value))
	values = [v for v in values if v <= IMPLAUSIBLE_DOWNLOADS]
	return values[:2], ' '.join(tokens[:6]), badge


def parse_card(card: dict, sort: str, now: str) -> dict | None:
	match = MODEL_RE.search(card.get('href') or '')
	if not match:
		return None
	model_id, slug = match.group(1), match.group(2)
	text = card.get('card_text') or ''
	words = text.split()
	model_type = words[0] if words and words[0] in ('Checkpoint', 'LoRA', 'LyCORIS', 'Embedding', 'Hypernetwork', 'Controlnet', 'VAE', 'Workflows', 'Wildcards') else None
	# Short uppercase tokens before the author are base-model badges (SD1, XL, IL…)
	tags = [w for w in words[1:8] if w.isupper() and 2 <= len(w) <= 4 and w not in NOT_TAGS]
	counts, raw, size_badge = parse_counts(text)
	return {
		'model_id': model_id,
		'slug': slug,
		'name': slug.replace('-', ' '),
		'type': model_type,
		'base_tags': ' | '.join(tags),
		'downloads': counts[0] if counts else None,
		'likes': counts[1] if len(counts) > 1 else None,
		'counts_raw': raw,
		'size_badge': size_badge,
		'url': f'{BASE}/models/{model_id}/{slug}',
		'via_sort': sort,
		'observed_at': now,
	}


async def collect(session: BrowserSession, sort: str, now: str) -> list[dict]:
	url = f'{BASE}/models?sort={quote(sort)}'
	await asyncio.wait_for(session.event_bus.dispatch(NavigateToUrlEvent(url=url, new_tab=False)), timeout=NAV_TIMEOUT)
	await asyncio.sleep(SETTLE)
	cdp_session = await session.get_or_create_cdp_session()
	for _ in range(SCROLLS):
		await cdp_session.cdp_client.send.Runtime.evaluate(
			params={'expression': 'window.scrollTo(0, document.body.scrollHeight)', 'returnByValue': True},
			session_id=cdp_session.session_id,
		)
		await asyncio.sleep(2.5)
	response = await cdp_session.cdp_client.send.Runtime.evaluate(
		params={'expression': JS_CARDS, 'returnByValue': True}, session_id=cdp_session.session_id
	)
	raw = response.get('result', {}).get('value')
	cards = json.loads(raw) if raw else []
	return [row for row in (parse_card(card, sort, now) for card in cards) if row]


def append_deduped(path: Path, rows: list[dict]) -> int:
	seen: set[str] = set()
	if path.exists():
		for line in path.open(encoding='utf-8'):
			try:
				seen.add(json.loads(line)['model_id'])
			except Exception:  # noqa: BLE001
				continue
	fresh = []
	for row in rows:
		if row['model_id'] in seen:
			continue
		seen.add(row['model_id'])
		fresh.append(row)
	if fresh:
		path.parent.mkdir(parents=True, exist_ok=True)
		with path.open('a', encoding='utf-8') as handle:
			for row in fresh:
				handle.write(json.dumps(row, ensure_ascii=False) + '\n')
	return len(fresh)


async def main() -> None:
	parser = argparse.ArgumentParser()
	parser.add_argument('--sorts', nargs='*', default=list(SORTS))
	parser.add_argument('--headful', action='store_true')
	args = parser.parse_args()

	allowed, verdict = await robots_allows('/models')
	print(f'robots for /models: {verdict}')
	if not allowed:
		print('refusing to navigate — robots does not permit this path')
		return

	now = dt.datetime.now(dt.timezone.utc).isoformat()
	rows: list[dict] = []
	with tempfile.TemporaryDirectory(prefix='civitai_browser_') as profile_dir:
		profile = BrowserProfile(headless=not args.headful, keep_alive=False, user_data_dir=Path(profile_dir), allowed_domains=ALLOWED)
		session = BrowserSession(browser_profile=profile)
		try:
			await session.start()
			for sort in args.sorts:
				try:
					found = await collect(session, sort, now)
				except Exception as exc:  # noqa: BLE001
					print(f'  {sort}: FAILED {type(exc).__name__}')
					continue
				rows.extend(found)
				print(f'  {sort}: {len(found)} model cards')
		finally:
			await session.kill()

	unique = {row['model_id']: row for row in rows}
	snap_dir = OUT_DIR / 'snapshots' / dt.date.today().isoformat()
	snap_dir.mkdir(parents=True, exist_ok=True)
	(snap_dir / 'models.json').write_text(json.dumps({'collected_at': now, 'models': list(unique.values())}, ensure_ascii=False, indent=1), encoding='utf-8')
	new = append_deduped(OUT_DIR / 'models.jsonl', list(unique.values()))

	types = collections.Counter(row['type'] for row in unique.values() if row.get('type'))
	tags = collections.Counter(t for row in unique.values() for t in row['base_tags'].split(' | ') if t)
	print(f'\nunique models {len(unique)} (+{new} new)')
	print('  types:', dict(types.most_common(6)))
	print('  base-model badges:', dict(tags.most_common(8)))
	top = sorted((r for r in unique.values() if r.get('downloads')), key=lambda r: -r['downloads'])[:5]
	for row in top:
		print(f'    {row["name"][:40]:42} {row["downloads"]:>10,} dl  {row["type"]}')
	print(f'DONE -> {OUT_DIR}')


if __name__ == '__main__':
	asyncio.run(main())
