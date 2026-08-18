"""Mastodon trending tags and links, via the official public API.

One of the 31 catalogue sources whose robots names AI crawlers — but the only
one where that naming does not reach us. mastodon.social blocks GPTBot
specifically; its `User-agent: *` group disallows just /media_proxy/,
/interact/ and one instance endpoint, none of which is touched here. No
Content-Signal is declared. So unlike AniList or UNESCO, which reserve
ai-train=no and name ClaudeBot outright, this is a source we may collect — and
the documented public REST API is the route the instance offers for it.

WHY THIS ONE IS WORTH HAVING
`trends/tags` returns seven daily buckets of uses and accounts per tag. That is
a time series delivered in a single call, so tag velocity is computable
immediately rather than after a week of accumulating our own snapshots — which
is the gap `ranking-collection-plan.md` identifies as the real signal.

WHAT IS DROPPED
`trends/links` carries an `html` oEmbed blob and a publisher-written
`description` that can run to a paragraph of the article. The embed is dropped
outright and the description capped to a short identifying snippet: this indexes
what is circulating, it does not reproduce it. `trends/statuses` is not called
at all, since its payload is post bodies.

PERMISSION IS CHECKED PER INSTANCE
The fediverse has no shared policy, and a survey of ten servers found three
different stances: outright allow, a GPTBot-only block that does not reach a
Claude-operated client, and servers that either reserve ai-train=no via
Content-Signal or name anthropic-ai directly. So each instance's robots is read
before it is collected, and the verdict and reason are written alongside the
data. Adding a server to the list cannot silently add one that refuses us.

Output (MASTODON_OUT, default ~/mastodon_export):
  snapshots/YYYY-MM-DD/<instance>.json
  tag_history.jsonl   - one row per tag per day bucket, appended and deduped
  links.jsonl         - trending link metadata, appended and deduped
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import os
import re
import sys
from pathlib import Path
from urllib.parse import urlsplit

if sys.stdout.encoding and sys.stdout.encoding.lower() != 'utf-8':
	sys.stdout.reconfigure(encoding='utf-8', errors='replace')

import aiohttp

sys.path.insert(0, str(Path(__file__).resolve().parent))
from promo_registry_verify import robots_verdict, scalar_verdict  # noqa: E402

OUT_DIR = Path(os.environ.get('MASTODON_OUT', str(Path.home() / 'mastodon_export')))
INSTANCES = [i for i in os.environ.get('MASTODON_INSTANCES', 'https://mastodon.social').split(',') if i]
# Identifies the project rather than impersonating a browser: this is an
# API client calling a documented public endpoint, and the operator should be
# able to see who is calling.
UA = 'nu-browser-use/1.0 (+https://newunivers.com; nu@newunivers.com)'
HEADERS = {'User-Agent': UA, 'Accept': 'application/json'}
LIMIT = 40
# Agent names that mean us. A GPTBot-only block does not reach a Claude-operated
# client; these do.
CLAUDE_TOKENS = {'anthropic-ai', 'claudebot', 'claude-user', 'claude-searchbot'}
SNIPPET = 200
DELAY = 1.0
TAG_RE = re.compile(r'<[^>]+>')


def snippet(value: object) -> str | None:
	if not isinstance(value, str):
		return None
	cleaned = re.sub(r'\s+', ' ', TAG_RE.sub(' ', value)).strip()
	return cleaned[:SNIPPET] or None


async def permission(session: aiohttp.ClientSession, instance: str) -> tuple[bool, str, str]:
	"""Decide per instance, because the fediverse has no shared policy.

	Measured across ten servers: some allow outright, most name GPTBot alone
	(which does not reach us), and some reserve ai-train=no or name anthropic-ai
	directly. Hand-picking instances would mean re-checking by eye every time
	the list grows, so the collector asks each server itself and records why.
	"""
	try:
		async with session.get(f'{instance}/robots.txt', timeout=aiohttp.ClientTimeout(total=20)) as response:
			body = await response.text(errors='replace') if response.status == 200 else ''
	except Exception as exc:  # noqa: BLE001
		return False, 'unreachable', type(exc).__name__
	if not body:
		# No robots published is no restriction, but say so rather than implying consent.
		return True, 'no_robots', 'no robots.txt published'
	verdict = robots_verdict(body, '/api/v1/trends/tags')
	scalar = scalar_verdict(verdict)
	if verdict['star'] == 'disallow':
		return False, scalar, 'robots disallows * for this path'
	if verdict.get('content_signal', {}).get('ai-train') == 'no':
		return False, scalar, 'Content-Signal reserves ai-train=no'
	named_us = sorted(set(verdict['ai_named']) & CLAUDE_TOKENS)
	if named_us and verdict['ai_named_disallow']:
		return False, scalar, f'names {", ".join(named_us)}'
	return True, scalar, 'permitted'


async def get(session: aiohttp.ClientSession, url: str) -> list | None:
	try:
		async with session.get(url, timeout=aiohttp.ClientTimeout(total=30)) as response:
			if response.status != 200:
				print(f'  {url}: HTTP {response.status}')
				return None
			payload = await response.json(content_type=None)
			return payload if isinstance(payload, list) else None
	except Exception as exc:  # noqa: BLE001
		print(f'  {url}: {type(exc).__name__}')
		return None


def tag_rows(tags: list, instance: str, now: str) -> list[dict]:
	"""One row per tag per day bucket — the API's own history, flattened."""
	rows = []
	for tag in tags:
		if not isinstance(tag, dict) or not tag.get('name'):
			continue
		for bucket in tag.get('history') or []:
			try:
				day = dt.datetime.fromtimestamp(int(bucket['day']), dt.timezone.utc).date().isoformat()
			except (KeyError, ValueError, OSError):
				continue
			rows.append({
				'instance': instance,
				'tag': tag['name'],
				'url': tag.get('url'),
				'day': day,
				'uses': int(bucket.get('uses') or 0),
				'accounts': int(bucket.get('accounts') or 0),
				'observed_at': now,
			})
	return rows


def link_rows(links: list, instance: str, now: str) -> list[dict]:
	"""Link metadata only: the embed blob is dropped, the description capped."""
	rows = []
	for link in links:
		if not isinstance(link, dict) or not link.get('url'):
			continue
		history = link.get('history') or []
		rows.append({
			'instance': instance,
			'url': link['url'],
			'title': snippet(link.get('title')),
			'provider': link.get('provider_name') or None,
			'author': link.get('author_name') or None,
			'language': link.get('language'),
			'type': link.get('type'),
			'snippet': snippet(link.get('description')),
			'recent_uses': sum(int(b.get('uses') or 0) for b in history if isinstance(b, dict)),
			'recent_accounts': sum(int(b.get('accounts') or 0) for b in history if isinstance(b, dict)),
			'observed_at': now,
		})
	return rows


def append_deduped(path: Path, rows: list[dict], key: tuple[str, ...]) -> int:
	seen: set[tuple] = set()
	if path.exists():
		for line in path.open(encoding='utf-8'):
			try:
				existing = json.loads(line)
			except json.JSONDecodeError:
				continue
			seen.add(tuple(str(existing.get(k)) for k in key))
	fresh = []
	for row in rows:
		identity = tuple(str(row.get(k)) for k in key)
		if identity in seen:
			continue
		seen.add(identity)
		fresh.append(row)
	if fresh:
		path.parent.mkdir(parents=True, exist_ok=True)
		with path.open('a', encoding='utf-8') as handle:
			for row in fresh:
				handle.write(json.dumps(row, ensure_ascii=False) + '\n')
	return len(fresh)


async def main() -> None:
	parser = argparse.ArgumentParser()
	parser.add_argument('--instances', nargs='*', default=INSTANCES)
	parser.add_argument('--limit', type=int, default=LIMIT)
	args = parser.parse_args()

	now = dt.datetime.now(dt.timezone.utc).isoformat()
	snap_dir = OUT_DIR / 'snapshots' / dt.date.today().isoformat()
	snap_dir.mkdir(parents=True, exist_ok=True)

	total_tags = total_links = 0
	async with aiohttp.ClientSession(headers=HEADERS) as session:
		gate: list[dict] = []
		for instance in args.instances:
			host = urlsplit(instance).netloc or instance
			allowed, verdict, reason = await permission(session, instance)
			gate.append({'instance': host, 'allowed': allowed, 'verdict': verdict, 'reason': reason})
			if not allowed:
				print(f'{host}: SKIP ({verdict}) — {reason}')
				await asyncio.sleep(DELAY)
				continue
			print(f'{host} [{verdict}]')
			tags = await get(session, f'{instance}/api/v1/trends/tags?limit={args.limit}') or []
			await asyncio.sleep(DELAY)
			links = await get(session, f'{instance}/api/v1/trends/links?limit={args.limit}') or []
			await asyncio.sleep(DELAY)

			tags_flat = tag_rows(tags, host, now)
			links_flat = link_rows(links, host, now)
			(snap_dir / f'{host}.json').write_text(
				json.dumps({'instance': host, 'collected_at': now, 'tags': tags_flat, 'links': links_flat}, ensure_ascii=False, indent=2),
				encoding='utf-8',
			)
			new_tags = append_deduped(OUT_DIR / 'tag_history.jsonl', tags_flat, ('instance', 'tag', 'day'))
			new_links = append_deduped(OUT_DIR / 'links.jsonl', links_flat, ('instance', 'url'))
			total_tags += new_tags
			total_links += new_links
			print(f'  tags: {len(tags)} trending, {len(tags_flat)} day-buckets (+{new_tags} new)')
			print(f'  links: {len(links)} trending (+{new_links} new)')

			movers = sorted(
				({'tag': t['tag'], 'uses': t['uses'], 'day': t['day']} for t in tags_flat),
				key=lambda r: -r['uses'],
			)[:5]
			for row in movers:
				print(f"    {row['tag'][:28]:30} {row['uses']:>6} uses on {row['day']}")

	# The verdict is part of the record: a dataset that cannot say why a server
	# was left out is indistinguishable from one that never asked.
	(snap_dir / 'permission.json').write_text(
		json.dumps({'checked_at': now, 'instances': gate}, ensure_ascii=False, indent=2), encoding='utf-8'
	)
	skipped = [row for row in gate if not row['allowed']]
	print(f'\n{len(gate) - len(skipped)}/{len(gate)} instances permitted, {len(skipped)} skipped')
	for row in skipped:
		print(f'  skipped {row["instance"]}: {row["reason"]}')
	print(f'DONE -> {OUT_DIR} (+{total_tags} tag-days, +{total_links} links)')


if __name__ == '__main__':
	asyncio.run(main())
