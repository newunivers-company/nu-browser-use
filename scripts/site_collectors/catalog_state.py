"""Cumulative catalogue: what is known, versus what one run happened to see.

The daily snapshots were being read as the catalogue. They are not. Across three
days GoodShort reported 1,383 / 1,434 / 1,284 titles, DramaBoxDB 1,009 / 994 /
965, and between the last two days GoodShort "lost" 349 titles and "gained" 199.
A catalogue does not do that overnight. These collectors walk listing pages under
a page budget, so each run reaches a different subset and the per-run count
measures the walk, not the platform.

Left uncorrected this poisons everything downstream: title_join treats the newest
file as the catalogue, churn metrics read walk noise as market movement, and a
title's disappearance looks like a delisting.

So the union is maintained here, with first_seen / last_seen / times_seen per
work, and each run recorded as an observation of a subset. `books.json` stays
what it is — the latest run's view — and this is what should be joined against.

FLAPPING IS THE PROOF, NOT THE ASSUMPTION
Claiming "absent means unvisited" would just be a second unverified story. A
title absent on one day and present both before and after cannot have been
delisted and relisted; it was missed. That count is reported per source as
`flapped`, and it is the evidence that the shortfall is sampling.

The converse is decided by where the gap sits, not by how long the history is. A
work absent only from the newest run has no sighting after the gap, so a miss and
a departure look identical no matter how many runs precede it — that set is
`absent_one_run_unclassified` and it stays unclassified permanently, not until
some run count is reached. Absence across two or more consecutive runs is
`candidate_departures`: weak evidence, since these walks demonstrably miss the
same work twice, but evidence.

Keying this off history length instead was wrong and briefly declared 268
GoodShort works interpretable that had simply been missed by the latest walk.

A DATE HERE IS THE SNAPSHOT DIRECTORY'S DATE
Collectors name snapshot directories with the local date and stamp observations
in UTC, so a 05:00 KST run writes 2026-08-16/ containing 2026-08-15T20:00Z rows.
This tool keys on the directory name — the local day the run belongs to — and
never mixes the two.

Output (CATALOG_STATE_OUT, default ~/catalog_state_export):
  <source>.jsonl  - one row per known work: first/last seen, times seen, latest fields
  <source>_catalog.json - the union in the collector's own record shape, so
                    anything reading books.json can read this instead
  summary.json    - per source: union, per-day coverage, flapping, unclassified
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
from pathlib import Path

if sys.stdout.encoding and sys.stdout.encoding.lower() != 'utf-8':
	sys.stdout.reconfigure(encoding='utf-8', errors='replace')

OUT_DIR = Path(os.environ.get('CATALOG_STATE_OUT', str(Path.home() / 'catalog_state_export')))
ROOT = Path(os.environ.get('EXPORT_ROOT', str(Path.home())))

# source -> (export dir, snapshot filename, id field, title field, metric field)
SOURCES: dict[str, tuple[str, str, str, str, str | None]] = {
	'goodshort': ('goodshort_export', 'books.json', 'bookId', 'bookName', 'viewCount'),
	'dramaboxdb': ('dramaboxdb_export', 'books.json', 'book_id', 'title', 'view_count'),
	'mydrama': ('mydrama_export', 'series.json', 'series_id', 'title', 'rating_count'),
	'shortmax': ('shortmax_export', 'dramas.json', 'drama_id', 'title', 'plays'),
	'flextv': ('flextv_export', 'dramas.json', 'drama_id', 'title', 'views'),
}


def records(payload: object) -> list[dict]:
	"""Snapshot files are sometimes a bare list, sometimes a wrapper object."""
	if isinstance(payload, list):
		return [r for r in payload if isinstance(r, dict)]
	if isinstance(payload, dict):
		for value in payload.values():
			if isinstance(value, list) and value and isinstance(value[0], dict):
				return value
	return []


def load_days(export_dir: Path, filename: str) -> dict[str, list[dict]]:
	snap = export_dir / 'snapshots'
	if not snap.exists():
		return {}
	days: dict[str, list[dict]] = {}
	for day_dir in sorted(p for p in snap.iterdir() if p.is_dir()):
		path = day_dir / filename
		if not path.exists():
			continue
		try:
			days[day_dir.name] = records(json.loads(path.read_text(encoding='utf-8')))
		except json.JSONDecodeError:
			print(f'  {export_dir.name}/{day_dir.name}: unreadable snapshot, skipped')
	return days


def build(source: str, spec: tuple, root: Path) -> tuple[list[dict], dict]:
	export_dir_name, filename, id_field, title_field, metric_field = spec
	export_dir = root / export_dir_name
	days = load_days(export_dir, filename)
	if not days:
		return [], {'source': source, 'error': 'no dated snapshots'}, []

	dates = sorted(days)
	seen_by_id: dict[str, list[str]] = {}
	latest: dict[str, dict] = {}
	for date in dates:
		for record in days[date]:
			identifier = record.get(id_field)
			if identifier in (None, ''):
				continue
			key = str(identifier)
			seen_by_id.setdefault(key, []).append(date)
			latest[key] = record  # dates walked in order, so this ends on the newest

	# Every known work carried in the source's own record shape, so anything that
	# reads books.json can read this instead without a new parser — the union
	# rather than whatever the newest walk happened to reach.
	union_records = []
	for key, seen in seen_by_id.items():
		union_records.append(
			{
				**latest[key],
				'_first_seen': seen[0],
				'_last_seen': seen[-1],
				'_times_seen': len(seen),
				'_in_latest_run': seen[-1] == dates[-1],
			}
		)

	rows = []
	for key, seen in seen_by_id.items():
		record = latest[key]
		rows.append(
			{
				'source': source,
				'entity_id': key,
				'title': record.get(title_field),
				'first_seen': seen[0],
				'last_seen': seen[-1],
				'times_seen': len(seen),
				'runs_available': len(dates),
				'seen_on': seen,
				'latest_metric_name': metric_field,
				'latest_metric': record.get(metric_field) if metric_field else None,
				'url': record.get('url'),
			}
		)

	# Flapping: absent on a day that has a sighting on both sides. Cannot be a
	# delisting followed by a relisting; it is a run that did not reach the title.
	index = {date: i for i, date in enumerate(dates)}
	flapped = 0
	for row in rows:
		positions = sorted(index[d] for d in row['seen_on'])
		if positions and (positions[-1] - positions[0] + 1) > len(positions):
			flapped += 1
			row['flapped'] = True
	last_date = dates[-1]
	gone = [r for r in rows if r['last_seen'] != last_date]

	# How many runs in a row, counting back from the newest, has this work been
	# absent from? That is the axis that decides interpretability, not the length
	# of the history: a work missed only by the newest run has no sighting after
	# the gap and cannot be told from a departure however many runs precede it.
	# Two consecutive misses is weak evidence of a departure; one is none at all.
	for row in rows:
		last_index = index[row['last_seen']]
		row['tail_absent'] = len(dates) - 1 - last_index
	unclassified = [r for r in gone if r['tail_absent'] == 1]
	candidate_departures = [r for r in gone if r['tail_absent'] >= 2]

	# Earlier this keyed off the length of the history, which was the wrong axis:
	# at four runs it declared 268 GoodShort works interpretable when most of them
	# had simply been missed by the newest walk. The gap's position is what
	# matters, not how much history sits behind it.
	summary = {
		'source': source,
		'dates': dates,
		'union': len(rows),
		'per_day': {d: len(days[d]) for d in dates},
		'seen_every_day': sum(1 for r in rows if r['times_seen'] == len(dates)),
		'flapped': flapped,
		'missing_from_latest': len(gone),
		'absent_one_run_unclassified': len(unclassified),
		'candidate_departures': len(candidate_departures),
		'latest_coverage': round(len(days[last_date]) / len(rows), 3) if rows else None,
	}
	return rows, summary, union_records


def main() -> int:
	parser = argparse.ArgumentParser()
	parser.add_argument('--sources', nargs='*', default=list(SOURCES), choices=list(SOURCES))
	parser.add_argument('--root', type=Path, default=ROOT)
	args = parser.parse_args()

	OUT_DIR.mkdir(parents=True, exist_ok=True)
	summaries = []
	for source in args.sources:
		rows, summary, union_records = build(source, SOURCES[source], args.root)
		summaries.append(summary)
		if not rows:
			print(f'{source:12} {summary.get("error")}')
			continue
		with (OUT_DIR / f'{source}.jsonl').open('w', encoding='utf-8') as handle:
			for row in rows:
				handle.write(json.dumps(row, ensure_ascii=False) + '\n')
		# Drop-in for the collector's own books.json / dramas.json: identical record
		# shape, but every known work instead of the newest walk's subset.
		(OUT_DIR / f'{source}_catalog.json').write_text(json.dumps(union_records, ensure_ascii=False, indent=1), encoding='utf-8')
		latest = summary['dates'][-1]
		print(
			f'{source:12} union={summary["union"]:>6,}  latest run saw {summary["per_day"][latest]:>6,} '
			f'({summary["latest_coverage"]:.0%})  every-day={summary["seen_every_day"]:>6,}  '
			f'flapped={summary["flapped"]:>4}  '
			f'missed-once={summary["absent_one_run_unclassified"]:>4}  '
			f'gone-2+runs={summary["candidate_departures"]:>4}'
		)

	(OUT_DIR / 'summary.json').write_text(
		json.dumps(
			{'built_at': dt.datetime.now(dt.timezone.utc).isoformat(), 'sources': summaries}, ensure_ascii=False, indent=1
		),
		encoding='utf-8',
	)
	usable = [s for s in summaries if not s.get('error')]
	if usable:
		total_flap = sum(s['flapped'] for s in usable)
		print(
			f'\n{total_flap:,} works flapped — absent from a run that has sightings on both sides. '
			'That is proof the shortfall is sampling, not delisting.'
		)
		missed_once = sum(s['absent_one_run_unclassified'] for s in usable)
		departures = sum(s['candidate_departures'] for s in usable)
		print(
			f'  missed-once={missed_once:,} is unclassifiable by construction — absent from the newest run only, '
			'so there is no sighting after the gap to tell a miss from a departure.'
		)
		print(
			f'  gone-2+runs={departures:,} have been absent from two or more consecutive runs. That is the '
			'candidate-departure set, and it is weak evidence rather than a delisting log: these walks miss '
			'the same work twice often enough that a third absence is worth waiting for.'
		)
	print(f'DONE -> {OUT_DIR}')
	return 0


if __name__ == '__main__':
	raise SystemExit(main())
