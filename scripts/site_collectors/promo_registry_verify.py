"""Promotion-channel registry verifier.

Reads registry/promotion_channels.yaml and checks every channel the registry
says we may collect. Competitor rebrands, domain moves and dead sites are
themselves signals, so this is not housekeeping — the first run of this logic
caught three stale URLs in the source research doc (shortmax.app ->
shorttv.live, gammatime.ai -> gammatime.live, megamatrix.io/home -> 404).

Policy enforcement is mechanical, not documentary: rows with `collect: false`
(every T2 authwall / robots-prohibited channel) are never requested. They are
reported as `skipped_by_policy`. `--probe-blocked` exists only for a
deliberate one-off audit and is not used by the cron.

robots.txt is fetched once per host and cached in the snapshot, so a verdict
change (a host adding a Disallow, or naming an AI crawler) shows up as a diff
rather than needing a manual re-read.

Output (PROMO_OUT, default ~/promo_export):
  snapshots/YYYY-MM-DD/registry_verify.json
  registry_changes.jsonl   - appended diffs vs the previous snapshot
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import hashlib
import json
import os
import re
import sys
from pathlib import Path
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

if sys.stdout.encoding and sys.stdout.encoding.lower() != 'utf-8':
	sys.stdout.reconfigure(encoding='utf-8', errors='replace')

import aiohttp

sys.path.insert(0, str(Path(__file__).resolve().parent))
from registry.models import AccessTier, Channel, load_registry  # noqa: E402

OUT_DIR = Path(os.environ.get('PROMO_OUT', str(Path.home() / 'promo_export')))
UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36'
# Sites reject bare aiohttp requests (col.com 405, vigloo-blog 403) purely for
# missing Accept headers. Sending what a browser sends is honest client
# behaviour, not evasion — the User-Agent still says who we are.
HEADERS = {
	'User-Agent': UA,
	'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8',
	'Accept-Language': 'en-US,en;q=0.9,ko;q=0.8',
	'Upgrade-Insecure-Requests': '1',
}
CONCURRENCY = 6
# Markers that mean "200 OK but you are looking at a wall, not content".
WALL_MARKERS = (('authwall', 'authwall'), ('loginPage', 'login_shell'), ('Join now', 'linkedin_join'))
# UA tokens that identify an AI-crawler group in robots.txt.
# Content-Signal (robots extension, cited by publishers as an Article 4 DSM
# reservation) states permitted USES rather than permitted agents:
#   search=yes, ai-train=no, use=reference
# It is how sites now express "index me, do not train on me", and it appears in
# the `User-agent: *` group, so it binds a generic client that no named-agent
# rule touches. Seen on linktr.ee, anilist.co and whc.unesco.org.
CONTENT_SIGNAL_RE = re.compile(r'(?im)^\s*content-signal:\s*(.+)$')
AI_UA_TOKENS = ('anthropic-ai', 'claudebot', 'claude-user', 'claude-searchbot', 'gptbot', 'oai-searchbot', 'chatgpt-user', 'perplexitybot', 'google-extended', 'ccbot', 'bytespider', 'meta-externalagent', 'applebot-extended')


def _parse_groups(text: str) -> list[tuple[set[str], list[tuple[bool, str]]]]:
	"""robots.txt -> [(agents, [(allow?, path_pattern), ...]), ...].

	Consecutive User-agent lines share one rule block (RFC 9309 §2.2.1) —
	TikTok and Medium both stack a dozen AI crawlers onto a single group, so
	getting this wrong silently mislabels every host.
	"""
	groups: list[tuple[set[str], list[tuple[bool, str]]]] = []
	agents: set[str] = set()
	rules: list[tuple[bool, str]] = []
	for raw in text.splitlines():
		line = raw.split('#', 1)[0].strip()
		if not line or ':' not in line:
			continue
		field, value = (part.strip() for part in line.split(':', 1))
		field = field.lower()
		if field == 'user-agent':
			if rules:  # a new agent list after rules starts a new group
				groups.append((agents, rules))
				agents, rules = set(), []
			agents.add(value.lower())
		elif field in ('allow', 'disallow'):
			rules.append((field == 'allow', value))
	if agents:
		groups.append((agents, rules))
	return groups


def _rule_matches(pattern: str, path: str) -> int:
	"""Return match length if `pattern` applies to `path`, else -1. Supports * and $."""
	if pattern == '':
		return -1
	regex = ''.join('.*' if ch == '*' else ('$' if ch == '$' else re.escape(ch)) for ch in pattern)
	return len(pattern) if re.match(regex, path) else -1


def _group_allows(rules: list[tuple[bool, str]], path: str) -> bool:
	"""Longest-match-wins; ties go to Allow (RFC 9309 §2.2.2)."""
	best_len, best_allow = -1, True
	for allow, pattern in rules:
		length = _rule_matches(pattern, path)
		if length > best_len or (length == best_len and allow):
			best_len, best_allow = length, allow
	return best_allow


def content_signals(text: str) -> dict[str, str]:
	"""Parse Content-Signal directives into {use: yes|no}.

	Only the first declaration is taken: sites publish one per agent group and
	the `*` group leads, which is the group that binds us.
	"""
	match = CONTENT_SIGNAL_RE.search(text)
	if not match:
		return {}
	signals: dict[str, str] = {}
	for part in match.group(1).split(','):
		if '=' in part:
			key, _, value = part.partition('=')
			signals[key.strip().lower()] = value.strip().lower()
	return signals


def robots_verdict(text: str, path: str) -> dict:
	"""Evaluate robots.txt for this path, separating two distinct questions.

	`star` is what a generic unnamed crawler may do — that is the rule that
	binds us, and stdlib's RobotFileParser decides it (checked against the
	hand-rolled group walker below on tiktok / medium / linkedin / linktr.ee /
	dramashorts / reelshort: all six agreed, so the well-tested implementation
	wins for the verdict that carries the policy weight).

	`ai_named_disallow` is whether the host singled out AI crawlers with a
	blanket block. RobotFileParser cannot answer that — it evaluates one agent
	at a time and will not tell you *which* agents a group names — so the group
	walker stays for this half. The two questions diverge constantly: TikTok's
	`*` group actually allows /@user paths and only the AI group is blocked,
	and Medium's feed is likewise open to `*` but shut to ClaudeBot. Collapsing
	them (the first version of this function did) both overstates the technical
	barrier and hides the policy question.
	"""
	if '<html' in text[:200].lower() or '<!doctype' in text[:200].lower():
		return {'star': 'unknown', 'ai_named': [], 'ai_named_disallow': False, 'content_signal': {}}
	parser = RobotFileParser()
	parser.parse(text.splitlines())
	star = 'allow' if parser.can_fetch('*', path) else 'disallow'
	ai_named: list[str] = []
	ai_disallow = False
	for agents, rules in _parse_groups(text):
		named = sorted(agents & set(AI_UA_TOKENS))
		if named:
			ai_named.extend(named)
			if not _group_allows(rules, path):
				ai_disallow = True
	return {
		'star': star,
		'ai_named': sorted(set(ai_named)),
		'ai_named_disallow': ai_disallow,
		'content_signal': content_signals(text),
	}


def scalar_verdict(robots: dict) -> str:
	"""One diffable string, most restrictive first."""
	if robots['star'] == 'unknown':
		return 'unknown'
	if robots['star'] == 'disallow':
		return 'disallow'
	# A stated reservation binds us even when no agent name matches ours.
	if robots.get('content_signal', {}).get('ai-train') == 'no':
		return 'ai_train_reserved'
	if robots['ai_named_disallow']:
		return 'named_ai_block'
	return 'allow'


async def fetch_robots(session: aiohttp.ClientSession, host: str, path: str, cache: dict[str, dict]) -> dict:
	key = f'{host}{path}'
	if key in cache:
		return cache[key]
	robots = {'star': 'unknown', 'ai_named': [], 'ai_named_disallow': False}
	try:
		async with session.get(f'https://{host}/robots.txt', timeout=aiohttp.ClientTimeout(total=15)) as response:
			if response.status == 200:
				robots = robots_verdict(await response.text(errors='replace'), path)
	except Exception:  # noqa: BLE001 - unreachable robots is itself 'unknown'
		pass
	cache[key] = robots
	return robots


async def check(session: aiohttp.ClientSession, channel: Channel, robots_cache: dict[str, dict], semaphore: asyncio.Semaphore, probe_blocked: bool = False) -> dict:
	"""Resolve one channel: final URL, status, body fingerprint, robots verdict."""
	url = str(channel.url)
	row = {
		'url': url,
		'brand': channel.brand,
		'company': channel.company,
		'channel_type': channel.channel_type.value,
		'declared_tier': channel.access_tier.value,
		'declared_robots': channel.robots_verdict.value,
		'official_status': channel.official_status.value,
	}
	if not (channel.collect or probe_blocked):
		row |= {'result': 'skipped_by_policy', 'status': None, 'final_url': None}
		return row

	parts = urlsplit(url)
	async with semaphore:
		robots = await fetch_robots(session, parts.netloc, parts.path or '/', robots_cache)
		row['robots_observed'] = scalar_verdict(robots)
		row['robots_detail'] = robots
		try:
			async with session.get(url, timeout=aiohttp.ClientTimeout(total=30), allow_redirects=True) as response:
				body = await response.text(errors='replace')
				row |= {
					'result': 'ok' if response.status == 200 else 'http_error',
					'status': response.status,
					'final_url': str(response.url),
					'bytes': len(body),
					'title': (re.search(r'<title[^>]*>(.*?)</title>', body, re.S | re.I) or [None, ''])[1].strip()[:120],
					'body_sha256': hashlib.sha256(body.encode('utf-8', 'replace')).hexdigest(),
					'walls': sorted({label for marker, label in WALL_MARKERS if label and marker in body}),
				}
		except Exception as exc:  # noqa: BLE001
			row |= {'result': 'unreachable', 'status': None, 'final_url': None, 'error': type(exc).__name__}
	return row


def previous_snapshot(today: str) -> dict[str, dict]:
	"""Most recent snapshot before today, keyed by url — for diffing."""
	snap_root = OUT_DIR / 'snapshots'
	if not snap_root.exists():
		return {}
	earlier = sorted(p for p in snap_root.iterdir() if p.is_dir() and p.name < today and (p / 'registry_verify.json').exists())
	if not earlier:
		return {}
	rows = json.loads((earlier[-1] / 'registry_verify.json').read_text(encoding='utf-8'))['channels']
	return {row['url']: row for row in rows}


def diff(previous: dict[str, dict], rows: list[dict], now: str) -> list[dict]:
	"""Report only changes that mean something: liveness, redirect target, robots, content."""
	changes = []
	for row in rows:
		before = previous.get(row['url'])
		if before is None:
			changes.append({'url': row['url'], 'change': 'new_channel', 'observed_at': now})
			continue
		for field, label in (('status', 'status_changed'), ('final_url', 'redirect_target_changed'), ('robots_observed', 'robots_changed'), ('body_sha256', 'content_changed')):
			if field in row and before.get(field) != row.get(field):
				changes.append({'url': row['url'], 'change': label, 'from': before.get(field), 'to': row.get(field), 'observed_at': now})
	return changes


async def main() -> None:
	parser = argparse.ArgumentParser()
	parser.add_argument('--probe-blocked', action='store_true', help='one-off audit only: also request collect:false channels')
	parser.add_argument('--type', nargs='*', help='restrict to these channel_type values')
	parser.add_argument('--tier', nargs='*', choices=[tier.value for tier in AccessTier], help='restrict to these access tiers')
	args = parser.parse_args()

	registry = load_registry()
	channels = registry.channels
	if args.type:
		channels = [c for c in channels if c.channel_type.value in args.type]
	if args.tier:
		channels = [c for c in channels if c.access_tier.value in args.tier]
	if args.probe_blocked:
		print('WARNING: --probe-blocked requests authwall / robots-prohibited channels. Audit use only.')

	today = dt.date.today().isoformat()
	now = dt.datetime.now(dt.timezone.utc).isoformat()
	snap_dir = OUT_DIR / 'snapshots' / today
	snap_dir.mkdir(parents=True, exist_ok=True)

	robots_cache: dict[str, dict] = {}
	semaphore = asyncio.Semaphore(CONCURRENCY)
	async with aiohttp.ClientSession(headers=HEADERS) as session:
		rows = await asyncio.gather(*(check(session, c, robots_cache, semaphore, args.probe_blocked) for c in channels))
	rows = list(rows)

	changes = diff(previous_snapshot(today), rows, now)
	(snap_dir / 'registry_verify.json').write_text(
		json.dumps({'verified_at': now, 'robots_by_target': robots_cache, 'channels': rows}, ensure_ascii=False, indent=2),
		encoding='utf-8',
	)
	if changes:
		with (OUT_DIR / 'registry_changes.jsonl').open('a', encoding='utf-8') as handle:
			for change in changes:
				handle.write(json.dumps(change, ensure_ascii=False) + '\n')

	tally: dict[str, int] = {}
	for row in rows:
		tally[row['result']] = tally.get(row['result'], 0) + 1
	print(f'checked {len(rows)} channels: ' + ', '.join(f'{k}={v}' for k, v in sorted(tally.items())))

	# A declared verdict that no longer matches reality is the whole point of this run.
	for row in rows:
		observed = row.get('robots_observed')
		if observed == 'disallow':
			print(f"  POLICY VIOLATION {row['url']}: robots disallows * but registry says collect:true — flip to T2")
		elif observed == 'named_ai_block':
			named = ', '.join(row['robots_detail']['ai_named'])
			print(f"  AI-NAMED {row['url']}: * is allowed, but the host blocks [{named}] — human call required")
		if observed and row['declared_robots'] not in (None, 'unknown') and observed != row['declared_robots']:
			print(f"  ROBOTS DRIFT {row['url']}: declared={row['declared_robots']} observed={observed}")
		if row.get('walls'):
			print(f"  WALL {row['url']}: {row['walls']} (tier {row['declared_tier']} may be wrong)")
		if row['result'] in ('http_error', 'unreachable'):
			print(f"  DOWN {row['url']}: {row['result']} {row.get('status') or row.get('error')}")
	for change in changes:
		if change['change'] in ('redirect_target_changed', 'status_changed', 'robots_changed'):
			print(f"  CHANGE {change['url']}: {change['change']} {change.get('from')} -> {change.get('to')}")

	print(f'DONE -> {snap_dir} ({len(changes)} changes)')


if __name__ == '__main__':
	asyncio.run(main())
