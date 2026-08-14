"""Telegram public channel-card collector — subscriber counts only.

NetShort is the one short-drama platform running Telegram as a primary
promotion channel (423,587 subscribers on 2026-08-14). The research doc
proposed collecting its posts, but the `/s/` web preview is **disabled**:
t.me/s/netshort_official returns zero message blocks against 206 for the
t.me/s/durov control. Reading posts would require joining the channel, which
is authentication, and out of policy.

What remains is the landing card, which Telegram serves to anyone: channel
title, description, and subscriber count. That is worth collecting on its own —
the weekly delta on a 400k-subscriber channel is a clean proxy for how hard a
platform is pushing, and it is one of the few audience-size numbers left after
the social tiers were ruled out.

Channels come from the registry (`channel_type: telegram`, `collect: true`), so
adding one is a YAML edit rather than a code change.

Output (PROMO_OUT, default ~/promo_export):
  snapshots/YYYY-MM-DD/telegram.json
  channel_size_observations.jsonl   - appended ChannelSizeObservation rows
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import html
import json
import os
import re
import sys
from pathlib import Path

if sys.stdout.encoding and sys.stdout.encoding.lower() != 'utf-8':
	sys.stdout.reconfigure(encoding='utf-8', errors='replace')

import aiohttp

sys.path.insert(0, str(Path(__file__).resolve().parent))
from registry.models import ChannelType, load_registry  # noqa: E402

OUT_DIR = Path(os.environ.get('PROMO_OUT', str(Path.home() / 'promo_export')))
UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36'
HEADERS = {'User-Agent': UA, 'Accept': 'text/html,application/xhtml+xml,*/*;q=0.8', 'Accept-Language': 'en-US,en;q=0.9'}
TITLE_RE = re.compile(r'tgme_page_title[^>]*>\s*(?:<[^>]+>)*([^<]+)')
EXTRA_RE = re.compile(r'tgme_page_extra[^>]*>([^<]+)')
DESC_RE = re.compile(r'tgme_page_description[^>]*>(.*?)</div>', re.S)
# "423 587 subscribers" — Telegram groups digits with a non-breaking space.
COUNT_RE = re.compile(r'([\d\s  ,\.]+?)\s*(subscriber|member)', re.I)


def strip_tags(value: str) -> str:
	return html.unescape(re.sub(r'<[^>]+>', ' ', value)).strip()


def parse_card(page: str) -> dict:
	title = TITLE_RE.search(page)
	extra = EXTRA_RE.search(page)
	description = DESC_RE.search(page)
	subscribers = None
	if extra:
		match = COUNT_RE.search(extra.group(1))
		if match:
			digits = re.sub(r'[^\d]', '', match.group(1))
			subscribers = int(digits) if digits else None
	return {
		'title': strip_tags(title.group(1)) if title else None,
		'subscribers': subscribers,
		'extra_raw': strip_tags(extra.group(1)) if extra else None,
		'description': strip_tags(description.group(1))[:600] if description else None,
	}


async def fetch_card(session: aiohttp.ClientSession, url: str) -> dict:
	async with session.get(url, timeout=aiohttp.ClientTimeout(total=25)) as response:
		if response.status != 200:
			return {'error': f'HTTP {response.status}'}
		return parse_card(await response.text(errors='replace'))


def previous_counts(today: str) -> dict[str, int]:
	"""Subscriber counts from the most recent earlier snapshot, for deltas."""
	snap_root = OUT_DIR / 'snapshots'
	if not snap_root.exists():
		return {}
	earlier = sorted(p for p in snap_root.iterdir() if p.is_dir() and p.name < today and (p / 'telegram.json').exists())
	if not earlier:
		return {}
	rows = json.loads((earlier[-1] / 'telegram.json').read_text(encoding='utf-8'))['channels']
	return {row['url']: row['subscribers'] for row in rows if row.get('subscribers')}


async def main() -> None:
	parser = argparse.ArgumentParser()
	parser.add_argument('--only', nargs='*', help='restrict to these brand/company ids')
	args = parser.parse_args()

	registry = load_registry()
	channels = [c for c in registry.collectible() if c.channel_type is ChannelType.TELEGRAM]
	if args.only:
		wanted = set(args.only)
		channels = [c for c in channels if c.brand in wanted or c.company in wanted]
	if not channels:
		print('no collectible telegram channels in the registry')
		return

	today = dt.date.today().isoformat()
	now = dt.datetime.now(dt.timezone.utc).isoformat()
	snap_dir = OUT_DIR / 'snapshots' / today
	snap_dir.mkdir(parents=True, exist_ok=True)
	previous = previous_counts(today)

	rows: list[dict] = []
	async with aiohttp.ClientSession(headers=HEADERS) as session:
		for channel in channels:
			url = str(channel.url)
			try:
				card = await fetch_card(session, url)
			except Exception as exc:  # noqa: BLE001
				card = {'error': type(exc).__name__}
			before = previous.get(url)
			row = {
				'url': url, 'brand': channel.brand, 'company': channel.company,
				**card,
				'previous_subscribers': before,
				'delta': (card.get('subscribers') - before) if (card.get('subscribers') and before) else None,
				'observed_at': now,
			}
			rows.append(row)
			delta = f" ({row['delta']:+,} since last snapshot)" if row['delta'] else ''
			print(f"  {channel.brand or channel.company}: {row.get('subscribers'):,} subscribers{delta}" if row.get('subscribers') else f"  {channel.brand}: {row.get('error', 'no count')}")
			await asyncio.sleep(0.5)

	(snap_dir / 'telegram.json').write_text(json.dumps({'collected_at': now, 'channels': rows}, ensure_ascii=False, indent=2), encoding='utf-8')
	with (OUT_DIR / 'channel_size_observations.jsonl').open('a', encoding='utf-8') as handle:
		for row in rows:
			if row.get('subscribers') is None:
				continue
			handle.write(
				json.dumps(
					{
						'source': 't.me', 'observation_type': 'ChannelSizeObservation',
						'channel_url': row['url'], 'channel_type': 'telegram',
						'brand': row['brand'], 'company': row['company'],
						'metric': 'subscribers', 'value': row['subscribers'], 'delta': row['delta'],
						'observed_at': now,
					},
					ensure_ascii=False,
				)
				+ '\n'
			)
	print(f'DONE -> {snap_dir / "telegram.json"} ({len(rows)} channels)')


if __name__ == '__main__':
	asyncio.run(main())
