"""Bluesky + Mastodon keyless trend snapshot collector.

Both endpoints are public, unauthenticated, and TODAY-ONLY (no history API) —
so the accumulated snapshots themselves are the asset. Run on a schedule
(cron) and never overwrite: each run appends a dated snapshot file.

  Bluesky:  GET https://public.api.bsky.app/xrpc/app.bsky.unspecced.getTrendingTopics
            (unspecced = unstable/removable; best-effort by design)
  Mastodon: GET https://mastodon.social/api/v1/trends/tags|links|statuses
            (per-instance ranking; add more instances via MASTODON_INSTANCES)

Output layout (SOCIAL_TREND_OUT, default ~/social_trend_export):
  snapshots/YYYY-MM-DDTHHMMSS.json  - raw response per source
  trends.csv                        - appended rows: timestamp, source, rank, topic, meta
"""

from __future__ import annotations

import asyncio
import csv
import json
import os
from datetime import datetime, timezone
from pathlib import Path

import aiohttp

OUT_DIR = Path(os.environ.get('SOCIAL_TREND_OUT', str(Path.home() / 'social_trend_export')))
BSKY_TRENDING_URL = 'https://public.api.bsky.app/xrpc/app.bsky.unspecced.getTrendingTopics'
MASTODON_INSTANCES = ('https://mastodon.social',)
MASTODON_TREND_KINDS = ('tags', 'links')
BSKY_LIMIT = 25
MASTO_LIMIT = 20
UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36'


async def fetch_json(session: aiohttp.ClientSession, url: str, params: dict | None = None) -> dict | list | None:
	"""GET one JSON endpoint; a failure returns None and is recorded, not fatal."""
	try:
		async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=20)) as response:
			if response.status != 200:
				return None
			return await response.json()
	except Exception:  # noqa: BLE001
		return None


def bsky_rows(doc: dict) -> list[dict]:
	"""Flatten one Bluesky trending response into trend rows."""
	rows = []
	for rank, topic in enumerate(doc.get('topics', []), 1):
		rows.append(
			{
				'source': 'bluesky',
				'rank': rank,
				'topic': topic.get('topic') or topic.get('displayName') or '',
				'description': (topic.get('description') or '')[:300],
				'link': topic.get('link') or '',
			}
		)
	return rows


def mastodon_rows(kind: str, doc: list) -> list[dict]:
	"""Flatten one Mastodon trends response into trend rows."""
	rows = []
	for rank, item in enumerate(doc, 1):
		if kind == 'tags':
			history = item.get('history') or []
			today = history[0] if history else {}
			rows.append(
				{
					'source': 'mastodon:tags',
					'rank': rank,
					'topic': item.get('name') or '',
					'description': f"accounts={today.get('accounts', '')} uses={today.get('uses', '')}",
					'link': item.get('url') or '',
				}
			)
		else:  # links
			rows.append(
				{
					'source': 'mastodon:links',
					'rank': rank,
					'topic': item.get('title') or item.get('url') or '',
					'description': (item.get('description') or '')[:300],
					'link': item.get('url') or '',
				}
			)
	return rows


async def main() -> None:
	snap_dir = OUT_DIR / 'snapshots'
	snap_dir.mkdir(parents=True, exist_ok=True)
	stamp = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H%M%SZ')

	async with aiohttp.ClientSession(headers={'User-Agent': UA}) as session:
		snapshots: dict[str, object] = {}
		rows: list[dict] = []

		bsky = await fetch_json(session, BSKY_TRENDING_URL, {'limit': BSKY_LIMIT})
		if isinstance(bsky, dict):
			snapshots['bluesky_trending'] = bsky
			rows.extend(bsky_rows(bsky))
		else:
			snapshots['bluesky_trending'] = {'error': 'fetch_failed'}

		for instance in MASTODON_INSTANCES:
			host = instance.rstrip('/').split('//')[1]
			for kind in MASTODON_TREND_KINDS:
				doc = await fetch_json(session, f'{instance}/api/v1/trends/{kind}', {'limit': MASTO_LIMIT})
				if isinstance(doc, list):
					key = f'mastodon_{host}_{kind}'
					snapshots[key] = doc
					rows.extend(mastodon_rows(kind, doc))
				await asyncio.sleep(0.5)

	snapshots['collected_at'] = stamp
	(snap_dir / f'{stamp}.json').write_text(json.dumps(snapshots, ensure_ascii=False, indent=1), encoding='utf-8')

	new_file = not (OUT_DIR / 'trends.csv').exists()
	with (OUT_DIR / 'trends.csv').open('a', newline='', encoding='utf-8') as fh:
		writer = csv.DictWriter(fh, fieldnames=['collected_at', 'source', 'rank', 'topic', 'description', 'link'])
		if new_file:
			writer.writeheader()
		for row in rows:
			row['collected_at'] = stamp
			writer.writerow(row)

	print(f'{stamp}: {len(rows)} trend rows ({sum(1 for r in rows if r["source"] == "bluesky")} bluesky, '
		  f'{sum(1 for r in rows if r["source"].startswith("mastodon"))} mastodon) -> {OUT_DIR}')


if __name__ == '__main__':
	asyncio.run(main())
