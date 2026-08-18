"""NU Rank — cross-platform short-drama composite score.

ranking-collection-plan.md proposed a composite over several signal axes; this
computes it from whatever observations exist on disk today.

What the data actually supports (2026-08-18, 25k observations):
  PlatformPresence  how many distinct platforms list the work, and with what
                    engagement — the only axis every source feeds
  CrossPlatformRank verticaldrama VDS weekly + platform carousels
  ViewMagnitude     absolute view/read counts (goodshort, flextv, fanqie,
                    duanju007 where titles bridge — mostly per-platform)
  Novelty           duanju007 new-vs-cumulative view ratio (weekly velocity)

PaidUA (SocialPeta) and UserRating (ShortDramaRank) have no collector yet, so
their weights are redistributed rather than assumed zero — a score that
silently ignores missing axes would over-rank works lucky enough to live on
the platforms we do see.

Scores are per-title, normalized 0-100 within this run, and the report states
which axes contributed per title — a rank without its evidence is a number.

Output: nu_rank.json + nu_rank.csv (top 100) under RANKING_OUT.
"""

from __future__ import annotations

import argparse
import collections
import csv
import json
import math
import re
import sys
from pathlib import Path

if sys.stdout.encoding and sys.stdout.encoding.lower() != 'utf-8':
	sys.stdout.reconfigure(encoding='utf-8', errors='replace')

OUT_DIR = Path(r'C:\Users\USER\ranking_export')
SOURCES_GLOB = [
	r'C:\Users\USER\*\observations.jsonl',
	r'C:\Users\USER\reelshort_export\rail_observations.jsonl',
]

WEIGHTS = {
	'platform_presence': 35,
	'cross_platform': 25,
	'view_magnitude': 25,
	'novelty': 15,
}


def load_observations() -> list[dict]:
	records = []
	for pattern in SOURCES_GLOB:
		for path in (
			set(Path(pattern).parent.glob(Path(pattern).name)) if False else [Path(p) for p in __import__('glob').glob(pattern)]
		):
			try:
				records.extend(json.loads(line) for line in path.read_text(encoding='utf-8').splitlines() if line.strip())
			except (OSError, json.JSONDecodeError):
				continue
	return records


def norm_title(title: str) -> str:
	return re.sub(r'[^a-z0-9가-힣 ]', '', (title or '').lower()).strip()


def rank_to_score(rank: int, total: int) -> float:
	"""Ordinal rank -> 0..1 (1st of N beats the tail)."""
	return 1.0 - (rank - 1) / max(total - 1, 1)


def log_norm(value: float) -> float:
	"""Heavy-tailed counters -> 0..1 via log scaling against the run max."""
	if value <= 0:
		return 0.0
	return math.log10(value) / math.log10(MAX_VIEW) if MAX_VIEW > 1 else 0.0


BRIDGE_PATH = Path(r'C:\Users\USER\ranking_export\title_bridge.json')


def load_bridge() -> dict[str, str]:
	"""zh -> en title mapping from shortdramacast bilingual pages."""
	try:
		return {b['zh']: b['en'] for b in json.loads(BRIDGE_PATH.read_text(encoding='utf-8'))}
	except (OSError, json.JSONDecodeError):
		return {}


MAX_VIEW = 1.0  # set during aggregation


def main() -> None:
	parser = argparse.ArgumentParser()
	parser.add_argument('--top', type=int, default=100)
	args = parser.parse_args()

	observations = load_observations()
	bridge = load_bridge()
	print(f'observations loaded: {len(observations)} | bridge pairs: {len(bridge)}')

	# Aggregate per normalized title.
	works: dict[str, dict] = collections.defaultdict(
		lambda: {'platforms': set(), 'views': [], 'ranks': [], 'sources': set(), 'display': '', 'new_ratio': None}
	)
	for o in observations:
		raw_title = (o.get('entity_title') or '').strip()
		if raw_title in bridge:
			raw_title = bridge[raw_title]
		title = norm_title(raw_title)
		if not title:
			continue
		work = works[title]
		work['display'] = o.get('entity_title') or work['display']
		source = o.get('source') or ''
		work['sources'].add(source)
		scope = o.get('scope') or {}
		if scope.get('platform'):
			work['platforms'].add(scope['platform'])
		view = o.get('views') or o.get('raw_score') or o.get('view_count') or o.get('read_count')
		try:
			view = float(view)
			if view > 0:
				work['views'].append(view)
		except (TypeError, ValueError):
			pass
		if o.get('rank') and o.get('rank_type') == 'CROSS_PLATFORM':
			work['ranks'].append((int(o['rank']), 20))  # verticaldrama top size
		# duanju007 new vs cumulative = weekly novelty
		if o.get('rank_type') == 'VIEW_COUNT' and o.get('views') and o.get('raw_score'):
			try:
				ratio = float(o['raw_score']) / float(o['views']) if float(o['views']) else None
				if ratio is not None:
					work['new_ratio'] = max(work['new_ratio'] or 0.0, min(ratio, 1.0))
			except (TypeError, ValueError, ZeroDivisionError):
				pass

	global MAX_VIEW
	all_views = [v for w in works.values() for v in w['views']]
	MAX_VIEW = max(all_views) if all_views else 1.0

	scored = []
	for title, w in works.items():
		presence = min(len(w['platforms']) / 4.0, 1.0)  # 4+ platforms saturates
		cross = max((rank_to_score(r, t) for r, t in w['ranks']), default=0.0)
		magnitude = max((log_norm(v) for v in w['views']), default=0.0)
		novelty = w['new_ratio'] or 0.0
		total = (
			WEIGHTS['platform_presence'] * presence
			+ WEIGHTS['cross_platform'] * cross
			+ WEIGHTS['view_magnitude'] * magnitude
			+ WEIGHTS['novelty'] * novelty
		)
		scored.append(
			{
				'title': w['display'],
				'nu_rank_score': round(total, 1),
				'platforms': sorted(w['platforms']),
				'n_platforms': len(w['platforms']),
				'max_views': max(w['views']) if w['views'] else None,
				'new_view_ratio': round(w['new_ratio'], 3) if w['new_ratio'] is not None else None,
				'sources': sorted(w['sources']),
				'axes': {
					'presence': round(presence, 2),
					'cross': round(cross, 2),
					'magnitude': round(magnitude, 2),
					'novelty': round(novelty, 2),
				},
			}
		)
	scored.sort(key=lambda x: -x['nu_rank_score'])

	OUT_DIR.mkdir(parents=True, exist_ok=True)
	(OUT_DIR / 'nu_rank.json').write_text(
		json.dumps({'generated': '2026-08-18', 'weights': WEIGHTS, 'works': scored[:500]}, ensure_ascii=False, indent=2),
		encoding='utf-8',
	)
	with (OUT_DIR / 'nu_rank.csv').open('w', newline='', encoding='utf-8-sig') as handle:
		writer = csv.writer(handle)
		writer.writerow(['nu_rank', 'title', 'score', 'platforms', 'max_views', 'new_view_ratio', 'sources'])
		for position, s in enumerate(scored[: args.top], 1):
			writer.writerow(
				[
					position,
					s['title'],
					s['nu_rank_score'],
					'|'.join(s['platforms']),
					s['max_views'],
					s['new_view_ratio'],
					'|'.join(s['sources']),
				]
			)

	print(f'works scored: {len(scored)} | multi-platform: {sum(1 for s in scored if s["n_platforms"] >= 2)}')
	print('TOP 10:')
	for position, s in enumerate(scored[:10], 1):
		print(
			f'  {position:2}. {s["title"][:38]:40} {s["nu_rank_score"]:5.1f} | {s["n_platforms"]}플랫폼 | views {s["max_views"]}'
		)
	print(f'-> {OUT_DIR / "nu_rank.csv"}')


if __name__ == '__main__':
	main()
