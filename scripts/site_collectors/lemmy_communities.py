"""Lemmy community activity, via the keyless public API, permission-gated.

Reddit is the obvious source for community signal and it is closed: its
robots.txt is `User-agent: * / Disallow: /` with a policy note attached, the
keyless .json endpoint answers 403, and oauth.reddit.com refuses without a
token. There is no keyless route, so Reddit is not collected.

Lemmy is the same shape — communities are subreddits, posts carry scores and
comment counts — and it is open: four instances checked all publish robots that
disallow only /login, and /api/v3 answers without any credential.

Collected per post: title, link, community, author handle, publish time, and
the vote and comment counters. The post `body` is never stored — that is the
author's writing, and a community-activity signal needs what was posted where
and how it was received, not the text itself.

Instance permission is checked the same way the Mastodon collector checks it,
because the fediverse has no shared policy: robots is read per instance before
anything is fetched, a Content-Signal reserving ai-train=no is honoured, and a
server naming Claude crawlers is skipped with the reason recorded.

Output (LEMMY_OUT, default ~/lemmy_export):
  snapshots/YYYY-MM-DD/<instance>.json
  posts.jsonl        - post metadata, appended and deduped on ap_id
  communities.jsonl  - per-community activity rollup
  permission.json    - per-instance verdict and reason
"""

from __future__ import annotations

import argparse
import asyncio
import collections
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

OUT_DIR = Path(os.environ.get('LEMMY_OUT', str(Path.home() / 'lemmy_export')))
INSTANCES = [i for i in os.environ.get('LEMMY_INSTANCES', 'https://lemmy.world,https://lemmy.ml,https://sh.itjust.works,https://programming.dev').split(',') if i]
UA = 'nu-browser-use/1.0 (+https://newunivers.com; nu@newunivers.com)'
HEADERS = {'User-Agent': UA, 'Accept': 'application/json'}
CLAUDE_TOKENS = {'anthropic-ai', 'claudebot', 'claude-user', 'claude-searchbot'}
SORTS = ('TopDay', 'TopWeek', 'Hot')
PAGE_LIMIT = 50
PAGES = 2
DELAY = 1.2


async def permission(session: aiohttp.ClientSession, instance: str) -> tuple[bool, str, str]:
	try:
		async with session.get(f'{instance}/robots.txt', timeout=aiohttp.ClientTimeout(total=20)) as response:
			body = await response.text(errors='replace') if response.status == 200 else ''
	except Exception as exc:  # noqa: BLE001
		return False, 'unreachable', type(exc).__name__
	if not body:
		return True, 'no_robots', 'no robots.txt published'
	verdict = robots_verdict(body, '/api/v3/post/list')
	scalar = scalar_verdict(verdict)
	if verdict['star'] == 'disallow':
		return False, scalar, 'robots disallows * for this path'
	if verdict.get('content_signal', {}).get('ai-train') == 'no':
		return False, scalar, 'Content-Signal reserves ai-train=no'
	named_us = sorted(set(verdict['ai_named']) & CLAUDE_TOKENS)
	if named_us and verdict['ai_named_disallow']:
		return False, scalar, f'names {", ".join(named_us)}'
	return True, scalar, 'permitted'


async def fetch_posts(session: aiohttp.ClientSession, instance: str, sort: str, page: int) -> list[dict]:
	url = f'{instance}/api/v3/post/list?type_=All&sort={sort}&limit={PAGE_LIMIT}&page={page}'
	try:
		async with session.get(url, timeout=aiohttp.ClientTimeout(total=30)) as response:
			if response.status != 200:
				return []
			payload = await response.json(content_type=None)
	except Exception:  # noqa: BLE001
		return []
	return payload.get('posts') or []


def flatten(view: dict, instance: str, sort: str, now: str) -> dict | None:
	"""Metadata only — `body` is deliberately absent."""
	post = view.get('post') or {}
	counts = view.get('counts') or {}
	community = view.get('community') or {}
	creator = view.get('creator') or {}
	if not post.get('ap_id'):
		return None
	return {
		'instance': instance,
		'ap_id': post['ap_id'],
		'title': (post.get('name') or '')[:300],
		'link': post.get('url'),
		'community': community.get('name'),
		'community_title': (community.get('title') or '')[:120],
		'author': creator.get('name'),
		'published': post.get('published'),
		'nsfw': bool(post.get('nsfw')),
		'score': counts.get('score'),
		'upvotes': counts.get('upvotes'),
		'downvotes': counts.get('downvotes'),
		'comments': counts.get('comments'),
		'via_sort': sort,
		'observed_at': now,
	}


def append_deduped(path: Path, rows: list[dict], key: str) -> int:
	seen: set[str] = set()
	if path.exists():
		for line in path.open(encoding='utf-8'):
			try:
				seen.add(json.loads(line)[key])
			except Exception:  # noqa: BLE001
				continue
	fresh = [row for row in rows if row.get(key) and row[key] not in seen and not seen.add(row[key])]
	if fresh:
		path.parent.mkdir(parents=True, exist_ok=True)
		with path.open('a', encoding='utf-8') as handle:
			for row in fresh:
				handle.write(json.dumps(row, ensure_ascii=False) + '\n')
	return len(fresh)


async def main() -> None:
	parser = argparse.ArgumentParser()
	parser.add_argument('--instances', nargs='*', default=INSTANCES)
	parser.add_argument('--pages', type=int, default=PAGES)
	args = parser.parse_args()

	now = dt.datetime.now(dt.timezone.utc).isoformat()
	snap_dir = OUT_DIR / 'snapshots' / dt.date.today().isoformat()
	snap_dir.mkdir(parents=True, exist_ok=True)
	gate: list[dict] = []
	all_rows: list[dict] = []

	async with aiohttp.ClientSession(headers=HEADERS) as session:
		for instance in args.instances:
			host = urlsplit(instance).netloc or instance
			allowed, verdict, reason = await permission(session, instance)
			gate.append({'instance': host, 'allowed': allowed, 'verdict': verdict, 'reason': reason})
			if not allowed:
				print(f'{host}: SKIP ({verdict}) — {reason}')
				continue

			rows: list[dict] = []
			for sort in SORTS:
				for page in range(1, args.pages + 1):
					views = await fetch_posts(session, instance, sort, page)
					rows.extend(r for r in (flatten(v, host, sort, now) for v in views) if r)
					await asyncio.sleep(DELAY)
			unique = {row['ap_id']: row for row in rows}
			all_rows.extend(unique.values())
			(snap_dir / f'{host}.json').write_text(json.dumps({'instance': host, 'collected_at': now, 'posts': list(unique.values())}, ensure_ascii=False, indent=1), encoding='utf-8')
			communities = collections.Counter(row['community'] for row in unique.values() if row.get('community'))
			print(f'{host} [{verdict}]: {len(unique)} posts across {len(communities)} communities')
			for name, count in communities.most_common(4):
				print(f'    c/{name[:26]:28} {count} posts')

	new_posts = append_deduped(OUT_DIR / 'posts.jsonl', all_rows, 'ap_id')

	# Per-community rollup: where the activity actually is.
	rollup: dict[tuple, dict] = {}
	for row in all_rows:
		key = (row['instance'], row.get('community'))
		entry = rollup.setdefault(key, {'instance': row['instance'], 'community': row.get('community'), 'posts': 0, 'score': 0, 'comments': 0, 'observed_at': now})
		entry['posts'] += 1
		entry['score'] += row.get('score') or 0
		entry['comments'] += row.get('comments') or 0
	with (OUT_DIR / 'communities.jsonl').open('a', encoding='utf-8') as handle:
		for entry in sorted(rollup.values(), key=lambda e: -e['score']):
			handle.write(json.dumps(entry, ensure_ascii=False) + '\n')

	(snap_dir / 'permission.json').write_text(json.dumps({'checked_at': now, 'instances': gate}, ensure_ascii=False, indent=2), encoding='utf-8')
	skipped = [row for row in gate if not row['allowed']]
	print(f'\n{len(gate) - len(skipped)}/{len(gate)} instances permitted, {len(skipped)} skipped')
	for row in skipped:
		print(f'  skipped {row["instance"]}: {row["reason"]}')
	print(f'posts +{new_posts} new, {len(rollup)} community rows')
	print(f'DONE -> {OUT_DIR}')


if __name__ == '__main__':
	asyncio.run(main())
