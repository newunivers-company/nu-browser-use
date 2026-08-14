"""Trope Rank — which subjects get made, and which get watched.

`ranking-collection-plan.md` asked for a trope ranking. Three platforms publish
their own label vocabulary alongside a per-title view metric, which makes the
useful question answerable: not "which trope appears most" — that only measures
what studios commission — but "which trope earns its slot".

Two rankings per platform, and the gap between them is the finding:

  supply    share of titles carrying a label
  demand    share of that platform's views landing on those titles
  lift      demand / supply

Lift above 1 means a label pulls more attention than its share of the catalogue;
below 1 means the catalogue is over-supplied relative to interest. Median views
per title is reported next to it, because a single breakout can carry a label's
total while its typical title does nothing.

WHY NOTHING IS COMBINED ACROSS PLATFORMS
title_join measured the same works on two platforms and found their view
counters unconvertible — a median ratio of 54x with a spread from 1.5x to 240x
across 32 shared titles. There is no factor that turns one into the other, so
shares are computed within a platform and the platforms are printed side by
side, never pooled. Labels are likewise kept in each platform's own words:
mapping "Counterattack" onto "Revenge" would be an editorial act invented here,
and the join already emits the co-occurrence evidence needed to do it from data.

Output (TROPE_OUT, default ~/trope_rank):
  trope_rank_<platform>.csv  - per-label supply, demand, lift, medians
  trope_rank.json            - the same, machine-readable
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import os
import statistics
import sys
from collections import defaultdict
from pathlib import Path

if sys.stdout.encoding and sys.stdout.encoding.lower() != 'utf-8':
	sys.stdout.reconfigure(encoding='utf-8', errors='replace')

OUT_DIR = Path(os.environ.get('TROPE_OUT', str(Path.home() / 'trope_rank')))
MIN_TITLES = 5  # a label on fewer titles than this is anecdote, not a ranking row

# platform -> (export dir env, default dir, file, title key, views key, label fields)
SOURCES: dict[str, dict] = {
	'goodshort': {
		'env': 'GOODSHORT_OUT', 'default': 'goodshort_export', 'file': 'books.json',
		'title': 'bookName', 'views': 'viewCount', 'labels': ('tropes', 'genres'),
	},
	'mydrama': {
		'env': 'MYDRAMA_OUT', 'default': 'mydrama_export', 'file': 'series.json',
		'title': 'title', 'views': 'watch_count', 'labels': ('genres',),
	},
	'dramaboxdb': {
		'env': 'DRAMABOXDB_OUT', 'default': 'dramaboxdb_export', 'file': 'books.json',
		'title': 'title', 'views': 'view_count', 'labels': ('genre',),
	},
}


def labels_of(record: dict, fields: tuple) -> list[str]:
	out: list[str] = []
	for field in fields:
		value = record.get(field)
		if isinstance(value, str):
			out.extend(part.strip() for part in value.split('|') if part.strip())
		elif isinstance(value, list):
			out.extend(str(item).strip() for item in value if item)
	return sorted(dict.fromkeys(out))


def load(platform: str, spec: dict, root: Path | None) -> list[dict]:
	base = root / spec['default'] if root else Path(os.environ.get(spec['env'], str(Path.home() / spec['default'])))
	path = base / spec['file']
	if not path.exists():
		print(f'  {platform}: MISSING ({path})')
		return []
	rows = []
	for record in json.loads(path.read_text(encoding='utf-8')):
		views = record.get(spec['views'])
		if not isinstance(views, (int, float)) or views <= 0:
			continue
		labels = labels_of(record, spec['labels'])
		if labels:
			rows.append({'title': record.get(spec['title']), 'views': float(views), 'labels': labels})
	print(f'  {platform}: {len(rows)} titles with views and labels')
	return rows


def rank(rows: list[dict]) -> list[dict]:
	"""Supply, demand and lift per label, within one platform."""
	total_titles = len(rows)
	total_views = sum(r['views'] for r in rows)
	if not total_titles or not total_views:
		return []
	per_label: dict[str, list[float]] = defaultdict(list)
	for row in rows:
		for label in row['labels']:
			per_label[label].append(row['views'])

	ranked = []
	for label, views in per_label.items():
		if len(views) < MIN_TITLES:
			continue
		supply = len(views) / total_titles
		demand = sum(views) / total_views
		ranked.append({
			'label': label,
			'titles': len(views),
			'supply_share': round(supply, 5),
			'demand_share': round(demand, 5),
			'lift': round(demand / supply, 3) if supply else None,
			'median_views': int(statistics.median(views)),
			'mean_views': int(sum(views) / len(views)),
			'max_views': int(max(views)),
		})
	return sorted(ranked, key=lambda r: -(r['lift'] or 0))


def main() -> None:
	parser = argparse.ArgumentParser()
	parser.add_argument('--root', type=Path, help='directory holding the *_export dirs')
	parser.add_argument('--top', type=int, default=10)
	args = parser.parse_args()

	print('loading catalogues')
	report: dict[str, list[dict]] = {}
	for platform, spec in SOURCES.items():
		rows = load(platform, spec, args.root)
		if rows:
			report[platform] = rank(rows)

	if not report:
		print('no catalogues with both labels and view counts — nothing to rank')
		return

	OUT_DIR.mkdir(parents=True, exist_ok=True)
	(OUT_DIR / 'trope_rank.json').write_text(
		json.dumps({'built_at': dt.datetime.now(dt.timezone.utc).isoformat(), 'min_titles': MIN_TITLES, 'platforms': report}, ensure_ascii=False, indent=2),
		encoding='utf-8',
	)
	columns = ['label', 'titles', 'supply_share', 'demand_share', 'lift', 'median_views', 'mean_views', 'max_views']
	for platform, ranked in report.items():
		with (OUT_DIR / f'trope_rank_{platform}.csv').open('w', newline='', encoding='utf-8-sig') as handle:
			writer = csv.DictWriter(handle, fieldnames=columns)
			writer.writeheader()
			writer.writerows(ranked)

	for platform, ranked in report.items():
		if not ranked:
			continue
		print(f'\n=== {platform} — {len(ranked)} labels on >= {MIN_TITLES} titles ===')
		print(f'  {"label":26} {"titles":>6} {"supply":>7} {"demand":>7} {"lift":>6} {"median":>10}')
		print('  over-performing:')
		for row in ranked[: args.top]:
			print(f'  {row["label"][:25]:26} {row["titles"]:>6} {row["supply_share"]:>7.1%} {row["demand_share"]:>7.1%} {row["lift"]:>6.2f} {row["median_views"]:>10,}')
		print('  under-performing:')
		for row in ranked[-3:]:
			print(f'  {row["label"][:25]:26} {row["titles"]:>6} {row["supply_share"]:>7.1%} {row["demand_share"]:>7.1%} {row["lift"]:>6.2f} {row["median_views"]:>10,}')

	print(f'\nplatforms are never pooled — see the module docstring on unconvertible counters\nDONE -> {OUT_DIR}')


if __name__ == '__main__':
	main()
