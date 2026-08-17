"""Ranking movement analysis over the browser-board snapshots.

The boards collectors (kuaikan 人气榜/新作榜/..., goodnovel /rankings + genre
boards) now have multiple dated snapshots. ranking-collection-plan.md asked for
movement from the very start; this computes it.

What is computed per source, per board, between each consecutive day pair:
  - rank delta for works present on both days
  - new entries (absent yesterday, present today)
  - exits (present yesterday, absent today)
  - board hoppers: same work moving between boards (kuaikan charts overlap by
    design — a work on 人气榜 joining 新作榜 is a signal, not noise)
  - top gainers/losers by rank delta

Rank deltas are noisy at the tail (position 95 -> 99 is not a story), so the
gainer/loser lists are cut at boards' top-N only, and raw movements stay in the
JSONL for whatever threshold later analysis wants.

Output (BOARD_MOVEMENT_OUT, default ~/board_movement_export):
  movement_<source>.jsonl - one row per (board, day-pair, work) transition
  summary.json           - per day-pair: entries/exits/movers, top gainers/losers
"""

from __future__ import annotations

import json
import os
from collections import defaultdict
from pathlib import Path

SNAP_ROOT = Path(os.environ.get('BROWSER_CATALOG_OUT', str(Path.home() / 'browser_catalog_export'))) / 'snapshots'
OUT_DIR = Path(os.environ.get('BOARD_MOVEMENT_OUT', str(Path.home() / 'board_movement_export')))
TOP_N_GAINERS = 10


def load_days(source: str) -> dict[str, dict[str, dict]]:
	"""days -> board -> id -> item, from dated snapshots."""
	days: dict[str, dict[str, dict]] = {}
	snap_dir = SNAP_ROOT
	if not snap_dir.exists():
		snap_dir = Path('/mnt/c/Users/USER/browser_catalog_export/snapshots')
	for day_dir in sorted(p for p in snap_dir.iterdir() if p.is_dir()):
		path = day_dir / f'{source}.json'
		if not path.exists():
			continue
		doc = json.load(open(path, encoding='utf-8'))
		by_board: dict[str, dict] = defaultdict(dict)
		for item in doc.get('items', []):
			board = item.get('board') or '?'
			by_board[board][str(item.get('id'))] = item
		days[day_dir.name] = dict(by_board)
	return days


def analyze(source: str) -> tuple[list[dict], dict]:
	"""Transitions across consecutive day pairs, plus a summary."""
	transitions: list[dict] = []
	summary: dict = {'source': source, 'pairs': []}
	days = load_days(source)
	day_names = sorted(days)
	board_names: set[str] = set()

	for prev_day, cur_day in zip(day_names, day_names[1:]):
		prev_boards, cur_boards = days[prev_day], days[cur_day]
		board_names |= set(prev_boards) | set(cur_boards)
		pair: dict = {'from': prev_day, 'to': cur_day, 'boards': []}
		pair_entries = pair_exits = pair_moves = 0

		for board in sorted(set(prev_boards) | set(cur_boards)):
			prev_items = prev_boards.get(board, {})
			cur_items = cur_boards.get(board, {})
			gainers: list[dict] = []
			board_entries = list(set(cur_items) - set(prev_items))
			board_exits = list(set(prev_items) - set(cur_items))

			for work_id in set(prev_items) & set(cur_items):
				# Prefer the live ordering: kuaikan's card numbers freeze for
				# days while the DOM order moves daily, so rank (card) is the
				# weekly ladder and dom_position is the daily one. Analyze the
				# daily one; keep the card number alongside for reference.
				prev_rank = prev_items[work_id].get('dom_position') or prev_items[work_id].get('rank')
				cur_rank = cur_items[work_id].get('dom_position') or cur_items[work_id].get('rank')
				if prev_rank is None or cur_rank is None:
					continue
				delta = prev_rank - cur_rank  # positive = moved up
				transitions.append(
					{
						'source': source,
						'board': board,
						'board_name': cur_items[work_id].get('board_name'),
						'work_id': work_id,
						'title': cur_items[work_id].get('title'),
						'from_day': prev_day,
						'to_day': cur_day,
						'kind': 'move',
						'prev_rank': prev_rank,
						'cur_rank': cur_rank,
						'delta': delta,
					}
				)
				if delta > 0:
					gainers.append({'work_id': work_id, 'title': cur_items[work_id].get('title'), 'delta': delta, 'to_rank': cur_rank})
				pair_moves += 1

			for work_id in board_entries:
				transitions.append(
					{
						'source': source,
						'board': board,
						'board_name': cur_items[work_id].get('board_name'),
						'work_id': work_id,
						'title': cur_items[work_id].get('title'),
						'from_day': prev_day,
						'to_day': cur_day,
						'kind': 'entry',
						'prev_rank': None,
						'cur_rank': cur_items[work_id].get('rank'),
						'delta': None,
					}
				)
			for work_id in board_exits:
				transitions.append(
					{
						'source': source,
						'board': board,
						'board_name': prev_items[work_id].get('board_name'),
						'work_id': work_id,
						'title': prev_items[work_id].get('title'),
						'from_day': prev_day,
						'to_day': cur_day,
						'kind': 'exit',
						'prev_rank': prev_items[work_id].get('rank'),
						'cur_rank': None,
						'delta': None,
					}
				)
			pair_entries += len(board_entries)
			pair_exits += len(board_exits)
			pair['boards'].append(
				{
					'board': board,
					'board_name': next((i.get('board_name') for i in cur_items.values() if i.get('board_name')), board),
					'stable': len(set(prev_items) & set(cur_items)),
					'entries': len(board_entries),
					'exits': len(board_exits),
				}
			)
			gainers.sort(key=lambda g: (-g['delta'], g['to_rank']))
			pair.setdefault('top_gainers', []).extend({'board': board, **g} for g in gainers[:5])

		pair['entries'] = pair_entries
		pair['exits'] = pair_exits
		pair['moves'] = pair_moves
		pair['top_gainers'] = sorted(pair['top_gainers'], key=lambda g: -g['delta'])[:TOP_N_GAINERS]
		summary['pairs'].append(pair)

	# board hoppers: same work on a different board day over day
	by_work: dict[tuple[str, str], set[str]] = defaultdict(set)
	for day in day_names:
		for board, items in days[day].items():
			for work_id in items:
				by_work[(work_id, day)].add(board)
	summary['hoppers'] = []
	for prev_day, cur_day in zip(day_names, day_names[1:]):
		for (work_id, day) in [(k[0], k[1]) for k in by_work if k[1] == cur_day]:
			prev_boards_for_work = by_work.get((work_id, prev_day), set())
			cur_boards_for_work = by_work[(work_id, cur_day)]
			new_boards = cur_boards_for_work - prev_boards_for_work
			if new_boards and prev_boards_for_work:
				title = next(
					(days[cur_day][b][work_id].get('title') for b in cur_boards_for_work if work_id in days[cur_day][b]),
					None,
				)
				summary['hoppers'].append({'work_id': work_id, 'title': title, 'from': prev_day, 'to': cur_day, 'joined': sorted(new_boards)})
	return transitions, summary


def main() -> None:
	OUT_DIR.mkdir(parents=True, exist_ok=True)
	all_summary: dict[str, dict] = {}
	for source in ('kuaikan', 'goodnovel'):
		transitions, summary = analyze(source)
		out_path = OUT_DIR / f'movement_{source}.jsonl'
		with out_path.open('w', encoding='utf-8') as handle:
			for row in transitions:
				handle.write(json.dumps(row, ensure_ascii=False) + '\n')
		all_summary[source] = summary
		for pair in summary['pairs']:
			print(f"{source} {pair['from']} -> {pair['to']}: moves={pair['moves']} entries={pair['entries']} exits={pair['exits']}")
		for hop in summary['hoppers'][:5]:
			print(f"  hop: {hop['title']} joined {hop['joined']} ({hop['from']}->{hop['to']})")
		print(f"  {len(transitions)} transitions -> {out_path.name}")
	(OUT_DIR / 'summary.json').write_text(json.dumps(all_summary, ensure_ascii=False, indent=1), encoding='utf-8')
	print(f'DONE -> {OUT_DIR}')


if __name__ == '__main__':
	main()
