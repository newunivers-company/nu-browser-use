"""Velocity report — first pass over the accumulated daily snapshots.

The whole point of the snapshot cadence (ranking-collection-plan.md) is
movement, not position: rank velocity, view deltas, new entries. With 4-5
days on disk the first honest time series is now computable.

This reads the goodshort daily snapshots (the richest absolute-view series:
1.3k-1.7k works/day, viewCount on every record) and reports, between the two
most recent days:

  view_delta     viewCount(today) - viewCount(yesterday), per matched work
  view_velocity  delta normalized per day, for works present both days
  new_entries    in today's snapshot but not yesterday's — discovery signal
  dropped        gone from today's snapshot — shelf churn

Works are keyed by bookId (stable), so no title normalization is involved.

Output (RANKING_OUT):
  velocity_latest.json   - top movers + entrants + churn summary
  velocity_series.jsonl  - append-only per-day aggregate (for later charting)
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding='utf-8', errors='replace')

SNAP_ROOT = Path(r'C:\Users\USER\goodshort_export\snapshots')
OUT_DIR = Path(r'C:\Users\USER\ranking_export')


def load_day(day: str) -> dict[str, dict]:
	path = SNAP_ROOT / day / 'books.json'
	try:
		books = json.loads(path.read_text(encoding='utf-8'))
	except (OSError, json.JSONDecodeError):
		return {}
	return {str(b.get('bookId')): b for b in books if b.get('bookId')}


def main() -> None:
	parser = argparse.ArgumentParser()
	parser.add_argument('--days', type=int, default=2, help='compare the latest N days pairwise')
	args = parser.parse_args()

	days = sorted(p.name for p in SNAP_ROOT.glob('????-??-??') if (p / 'books.json').is_file())
	if len(days) < 2:
		raise SystemExit('need at least 2 daily snapshots')
	days = days[-args.days :]
	print(f'snapshots: {days}')

	series_rows = []
	for previous, current in zip(days, days[1:]):
		prev_books = load_day(previous)
		curr_books = load_day(current)

		shared = set(prev_books) & set(curr_books)
		new_entries = set(curr_books) - set(prev_books)
		dropped = set(prev_books) - set(curr_books)

		deltas = []
		for bid in shared:
			try:
				pv = int(prev_books[bid].get('viewCount') or 0)
				cv = int(curr_books[bid].get('viewCount') or 0)
			except (TypeError, ValueError):
				continue
			if cv >= pv:  # ignore counter resets
				title = curr_books[bid].get('title') or curr_books[bid].get('bookName') or bid
				deltas.append({'bookId': bid, 'title': title, 'prev': pv, 'curr': cv, 'delta': cv - pv})
		deltas.sort(key=lambda d: -d['delta'])

		report = {
			'compared': [previous, current],
			'works_prev': len(prev_books),
			'works_curr': len(curr_books),
			'shared': len(shared),
			'new_entries': len(new_entries),
			'dropped': len(dropped),
			'top_view_gains': [{'title': d['title'][:40], 'delta': d['delta'], 'views': d['curr']} for d in deltas[:20]],
			'new_entry_sample': [
				(curr_books[bid].get('title') or curr_books[bid].get('bookName') or bid)[:40] for bid in sorted(new_entries)[:20]
			],
		}
		series_rows.append(report)

		print(f'\n=== {previous} -> {current} ===')
		print(f'  shared {len(shared)} | new {len(new_entries)} | dropped {len(dropped)}')
		print('  top view gains:')
		for d in deltas[:5]:
			print(f'    +{d["delta"]:>9,}  {d["title"][:38]} ({d["curr"]:,})')

	OUT_DIR.mkdir(parents=True, exist_ok=True)
	(OUT_DIR / 'velocity_latest.json').write_text(json.dumps(series_rows[-1], ensure_ascii=False, indent=2), encoding='utf-8')
	with (OUT_DIR / 'velocity_series.jsonl').open('a', encoding='utf-8') as handle:
		for row in series_rows:
			handle.write(json.dumps(row, ensure_ascii=False) + '\n')
	print(f'\n-> {OUT_DIR / "velocity_latest.json"}')


if __name__ == '__main__':
	main()
