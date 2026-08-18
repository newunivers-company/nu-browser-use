"""Cross-source title join — one work identity across seven platform catalogs.

Seven collectors produce ~55,200 title records, each keyed by its own platform
id (GoodShort bookId, My Drama UUID, DramaBoxDB bookId, netshort dramaId,
ReelShort book_id, FlexTV and ShortMax slugs). Nothing joins them, so questions
that need more than one source — is a title carried by several platforms, do the
platforms agree on how it performs, does a trope travel — cannot be asked at all.

netshort and ReelShort were missing from this list until 2026-08-15, and netshort
alone is 51,834 titles. Adding them took the joined set from 2,545 works to
53,074 and cross-platform works from 39 to 253. The *rate* fell, 1.5% to 0.48%,
because netshort is large and largely disjoint — which is why the rate on its own
is the wrong number to quote. The absolute count is the evidence; the rate is a
statement about catalogue sizes.

HOW CONFIDENT A MATCH IS, AND WHY THAT MATTERS HERE
Short-drama titles are formulaic by design: dozens begin "The Billionaire's",
"My CEO", "Reborn". A fuzzy matcher tuned loosely will happily merge two
different works with similar names, and a merged work is worse than no join —
it silently averages two catalogs' numbers into a figure describing neither.

So merging is exact-only, on the normalized title with season and dub
decorations stripped. Near matches are computed and written out for review, but
never merged. The reviewed file is the deliverable for anyone who wants to
raise coverage; the merged set stays defensible without them.

Genres stay per-platform. GoodShort, My Drama and DramaBoxDB each publish their
own vocabulary (Counterattack / Enemies To Lovers / Revenge), and mapping them
onto one taxonomy would be an editorial act invented here rather than measured.
What this does instead is report, for works present on two platforms, which
labels co-occur — evidence for building that mapping later, from data.

COVERAGE CAVEAT
Overlap is measured over what has been collected, not over the platforms' full
catalogs: DramaBoxDB came from a recommendation walk, FlexTV from home and genre
rails, GoodShort from home plus tag pages. Every one of those is partial, so the
observed overlap is a LOWER BOUND and a low number here is not evidence that the
catalogs are disjoint.

Metrics are NOT combined. DramaBoxDB reports view counts two to three orders of
magnitude above My Drama's, almost certainly counting per-episode plays against
per-series views; the collectors already tag that basis as uncalibrated. Each
platform's numbers are carried side by side, never summed or averaged.

Output (JOIN_OUT, default ~/join_export):
  works.json          - unified works with per-platform records
  works.csv           - flat view, one row per work
  multi_platform.csv  - only works seen on 2+ platforms
  near_matches.csv    - candidate merges for human review, NOT applied
  genre_cooccurrence.csv - platform label pairs on the same work
  cross_platform_ratios.csv - counter spread per platform pair
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import os
import statistics
import sys
from collections import Counter, defaultdict
from pathlib import Path

if sys.stdout.encoding and sys.stdout.encoding.lower() != 'utf-8':
	sys.stdout.reconfigure(encoding='utf-8', errors='replace')

sys.path.insert(0, str(Path(__file__).resolve().parent))
from textmatch import normalize, similarity, strip_decorations  # noqa: E402

OUT_DIR = Path(os.environ.get('JOIN_OUT', str(Path.home() / 'join_export')))
# Set from the measured score distribution rather than by eye. Across 60,310
# blocked cross-platform pairs the tail is genuinely sparse: 0 candidates at
# 0.75, 2 at 0.65, 10 at 0.55. 0.70 surfaces the tail without dragging in the
# formulaic-title noise that starts below it.
NEAR_THRESHOLD = 0.70

# Where each collector writes, and how to read one of its records. `views` is
# kept under the platform's own name so nothing implies the units agree.
SOURCES: dict[str, dict] = {
	'goodshort': {
		'env': 'GOODSHORT_OUT',
		'default': 'goodshort_export',
		'file': 'books.json',
		'id': 'bookId',
		'title': 'bookName',
		'views': 'viewCount',
		'episodes': 'chapterCount',
		'genres': ('genres', 'tropes'),
		'url': 'url',
	},
	'mydrama': {
		'env': 'MYDRAMA_OUT',
		'default': 'mydrama_export',
		'file': 'series.json',
		'id': 'series_id',
		'title': 'title',
		'views': 'watch_count',
		'episodes': 'episodes',
		'genres': ('genres',),
		'url': 'url',
	},
	'dramaboxdb': {
		'env': 'DRAMABOXDB_OUT',
		'default': 'dramaboxdb_export',
		'file': 'books.json',
		'id': 'book_id',
		'title': 'title',
		'views': 'view_count',
		'episodes': 'chapter_count',
		'genres': ('genre',),
		'url': 'url',
	},
	'flextv': {
		'env': 'FLEXTV_OUT',
		'default': 'flextv_export',
		'file': 'dramas.json',
		'id': 'drama_id',
		'title': 'title',
		'views': 'views',
		'episodes': None,
		'genres': (),
		'url': 'url',
	},
	'shortmax': {
		'env': 'SHORTMAX_OUT',
		'default': 'shortmax_export',
		'file': 'dramas.json',
		'id': 'drama_id',
		'title': 'title',
		'views': 'plays',
		'episodes': 'episodes',
		'genres': ('category',),
		'url': 'url',
	},
	# netshort and reelshort were both absent, which is why the join reported
	# only 1.5% of works on more than one platform. netshort alone carries 51,834
	# titles — twenty times the entire joined set — so their absence was not a
	# rounding error in the cross-platform figure, it was the figure.
	# netshort publishes no view or genre field; recording that as None is
	# honest, and the title is what the join needs.
	'netshort': {
		'env': 'NETSHORT_OUT',
		'default': 'netshort_export',
		'file': 'dramas.json',
		'id': 'dramaId',
		'title': 'title',
		'views': None,
		'episodes': 'episodeCount',
		'genres': (),
		'url': 'url',
	},
	'reelshort': {
		'env': 'REELSHORT_OUT',
		'default': 'reelshort_export',
		'file': 'books.json',
		'id': 'book_id',
		'title': 'book_title',
		'views': 'read_count',
		'episodes': 'chapter_count',
		'genres': ('theme', 'theme_list'),
		'url': None,
	},
}


CATALOG_STATE = Path(os.environ.get('CATALOG_STATE_OUT', str(Path.home() / 'catalog_state_export')))


def source_path(name: str, spec: dict, root: Path | None) -> Path:
	"""Prefer the cumulative catalogue over the newest run's view.

	A collector's books.json is what one walk reached, not what the platform
	carries: across three days GoodShort's file held 1,383 then 1,434 then 1,284
	titles, and 74 works were provably missed rather than delisted. Joining
	against the newest file therefore dropped a different few hundred works every
	day. catalog_state.py writes the union in the same record shape, so this is a
	swap and not a rewrite — and it falls back when that file does not exist yet.
	"""
	cumulative = CATALOG_STATE / f'{name}_catalog.json'
	if root is None and cumulative.exists():
		return cumulative
	if root is not None:
		return root / spec['default'] / spec['file']
	return Path(os.environ.get(spec['env'], str(Path.home() / spec['default']))) / spec['file']


def genre_values(record: dict, fields: tuple) -> list[str]:
	out: list[str] = []
	for field in fields:
		value = record.get(field)
		if isinstance(value, str):
			out.extend(part.strip() for part in value.split('|') if part.strip())
		elif isinstance(value, list):
			out.extend(str(v).strip() for v in value if v)
	return sorted(dict.fromkeys(out))


def load(name: str, spec: dict, root: Path | None) -> list[dict]:
	path = source_path(name, spec, root)
	if not path.exists():
		print(f'  {name}: MISSING ({path})')
		return []
	records = json.loads(path.read_text(encoding='utf-8'))
	rows = []
	for record in records:
		title = record.get(spec['title'])
		if not title:
			continue
		rows.append(
			{
				'platform': name,
				'platform_id': str(record.get(spec['id'])),
				'title': title,
				'key': normalize(strip_decorations(title)),
				# Some catalogues publish no views and some no canonical URL; a spec
				# may therefore name None rather than a field, which is different
				# from naming a field that happens to be empty.
				'views': record.get(spec['views']) if spec['views'] else None,
				'episodes': record.get(spec['episodes']) if spec['episodes'] else None,
				'genres': genre_values(record, spec['genres']),
				'url': record.get(spec['url']) if spec['url'] else None,
			}
		)
	print(f'  {name}: {len(rows)} titles')
	return rows


def build_works(rows: list[dict]) -> list[dict]:
	"""Group by exact normalized key. Merging is never fuzzy — see the docstring."""
	grouped: dict[str, list[dict]] = defaultdict(list)
	for row in rows:
		if row['key']:
			grouped[row['key']].append(row)
	works = []
	for key, members in grouped.items():
		platforms = sorted({m['platform'] for m in members})
		works.append(
			{
				'key': key,
				'title': sorted(members, key=lambda m: len(m['title']))[0]['title'],
				'platforms': platforms,
				'platform_count': len(platforms),
				'records': {
					m['platform']: {
						'id': m['platform_id'],
						'title': m['title'],
						'views': m['views'],
						'episodes': m['episodes'],
						'genres': m['genres'],
						'url': m['url'],
					}
					for m in members
				},
			}
		)
	return sorted(works, key=lambda w: (-w['platform_count'], w['title']))


def near_candidates(works: list[dict], limit: int) -> list[dict]:
	"""Unmerged look-alikes, blocked on a shared leading bigram to stay tractable."""
	singles = [w for w in works if w['platform_count'] == 1]
	buckets: dict[str, list[dict]] = defaultdict(list)
	for work in singles:
		buckets[work['key'][:2]].append(work)
	out: list[dict] = []
	for bucket in buckets.values():
		for i, left in enumerate(bucket):
			for right in bucket[i + 1 :]:
				left_platform = left['platforms'][0]
				right_platform = right['platforms'][0]
				if left_platform == right_platform:
					continue
				score = similarity(left['key'], right['key'])
				if score >= NEAR_THRESHOLD:
					out.append(
						{
							'score': round(score, 3),
							'left_platform': left_platform,
							'left_title': left['title'],
							'right_platform': right_platform,
							'right_title': right['title'],
						}
					)
			if len(out) >= limit:
				return sorted(out, key=lambda r: -r['score'])[:limit]
	return sorted(out, key=lambda r: -r['score'])[:limit]


def ratio_report(works: list[dict]) -> list[dict]:
	"""Per platform pair, how far apart the two counters sit on the same work.

	This is the evidence for refusing to combine metrics. If two platforms
	counted the same thing, their ratio on shared works would cluster; a wide
	spread means no conversion factor exists and any normalization would be
	invented.
	"""
	ratios: dict[tuple[str, str], list[float]] = defaultdict(list)
	for work in works:
		if work['platform_count'] < 2:
			continue
		platforms = work['platforms']
		for i, left in enumerate(platforms):
			for right in platforms[i + 1 :]:
				left_views = work['records'][left]['views']
				right_views = work['records'][right]['views']
				if isinstance(left_views, int) and isinstance(right_views, int) and left_views and right_views:
					ratios[(left, right)].append(max(left_views, right_views) / min(left_views, right_views))
	rows = []
	for (left, right), values in sorted(ratios.items(), key=lambda kv: -len(kv[1])):
		values.sort()
		rows.append(
			{
				'platform_a': left,
				'platform_b': right,
				'shared_works': len(values),
				'min_ratio': round(values[0], 2),
				'median_ratio': round(statistics.median(values), 2),
				'max_ratio': round(values[-1], 2),
				'spread': round(values[-1] / values[0], 1) if values[0] else None,
			}
		)
	return rows


def write_outputs(works: list[dict], near: list[dict], now: str) -> None:
	OUT_DIR.mkdir(parents=True, exist_ok=True)
	(OUT_DIR / 'works.json').write_text(
		json.dumps({'built_at': now, 'works': works}, ensure_ascii=False, indent=2), encoding='utf-8'
	)

	platforms = list(SOURCES)
	with (OUT_DIR / 'works.csv').open('w', newline='', encoding='utf-8-sig') as handle:
		writer = csv.writer(handle)
		writer.writerow(
			['title', 'platform_count', 'platforms'] + [f'{p}_views' for p in platforms] + [f'{p}_episodes' for p in platforms]
		)
		for work in works:
			records = work['records']
			writer.writerow(
				[work['title'], work['platform_count'], ' | '.join(work['platforms'])]
				+ [records.get(p, {}).get('views') for p in platforms]
				+ [records.get(p, {}).get('episodes') for p in platforms]
			)

	multi = [w for w in works if w['platform_count'] > 1]
	with (OUT_DIR / 'multi_platform.csv').open('w', newline='', encoding='utf-8-sig') as handle:
		writer = csv.writer(handle)
		writer.writerow(['title', 'platforms'] + [f'{p}_views' for p in platforms] + [f'{p}_genres' for p in platforms])
		for work in multi:
			records = work['records']
			writer.writerow(
				[work['title'], ' | '.join(work['platforms'])]
				+ [records.get(p, {}).get('views') for p in platforms]
				+ [' | '.join(records.get(p, {}).get('genres') or []) for p in platforms]
			)

	ratios = ratio_report(works)
	with (OUT_DIR / 'cross_platform_ratios.csv').open('w', newline='', encoding='utf-8-sig') as handle:
		writer = csv.DictWriter(
			handle, fieldnames=['platform_a', 'platform_b', 'shared_works', 'min_ratio', 'median_ratio', 'max_ratio', 'spread']
		)
		writer.writeheader()
		writer.writerows(ratios)

	with (OUT_DIR / 'near_matches.csv').open('w', newline='', encoding='utf-8-sig') as handle:
		writer = csv.DictWriter(handle, fieldnames=['score', 'left_platform', 'left_title', 'right_platform', 'right_title'])
		writer.writeheader()
		writer.writerows(near)

	# Which platform labels land on the same work — the raw material for a
	# cross-platform taxonomy, rather than one invented here.
	pairs: Counter[tuple[str, str]] = Counter()
	for work in multi:
		labelled = [(p, g) for p, rec in work['records'].items() for g in (rec.get('genres') or [])]
		for i, (left_platform, left_genre) in enumerate(labelled):
			for right_platform, right_genre in labelled[i + 1 :]:
				if left_platform != right_platform:
					pairs[tuple(sorted([f'{left_platform}:{left_genre}', f'{right_platform}:{right_genre}']))] += 1
	with (OUT_DIR / 'genre_cooccurrence.csv').open('w', newline='', encoding='utf-8-sig') as handle:
		writer = csv.writer(handle)
		writer.writerow(['label_a', 'label_b', 'works'])
		for (left, right), count in pairs.most_common(400):
			writer.writerow([left, right, count])


def main() -> None:
	parser = argparse.ArgumentParser()
	parser.add_argument('--root', type=Path, help='directory holding all *_export dirs (defaults to per-source env vars)')
	parser.add_argument('--near-limit', type=int, default=300)
	args = parser.parse_args()

	now = dt.datetime.now(dt.timezone.utc).isoformat()
	print('[1/3] loading source catalogs')
	rows: list[dict] = []
	for name, spec in SOURCES.items():
		rows.extend(load(name, spec, args.root))
	if not rows:
		print('no source catalogs found — nothing to join')
		return

	print('[2/3] grouping on exact normalized title')
	works = build_works(rows)
	print('[3/3] scoring unmerged look-alikes for review')
	near = near_candidates(works, args.near_limit)
	write_outputs(works, near, now)

	multi = [w for w in works if w['platform_count'] > 1]
	spread = Counter(w['platform_count'] for w in works)
	print(f'\n{len(rows)} records -> {len(works)} distinct works')
	print(f'  on 1 platform: {spread[1]} | 2: {spread[2]} | 3+: {sum(v for k, v in spread.items() if k >= 3)}')
	print(f'  near-match candidates for review (NOT merged): {len(near)}')
	if multi:
		print('\nworks carried by more than one platform:')
		for work in multi[:15]:
			views = ', '.join(
				f'{p}={work["records"][p]["views"]:,}' if isinstance(work['records'][p]['views'], int) else f'{p}=?'
				for p in work['platforms']
			)
			print(f'  {work["title"][:46]:48} {views}')

	ratios = [r for r in ratio_report(works) if r['shared_works'] >= 3]
	if ratios:
		print('\nview-count ratio on shared works (why metrics are never combined):')
		for row in ratios:
			print(
				f'  {row["platform_a"]}/{row["platform_b"]}  n={row["shared_works"]:<3} median={row["median_ratio"]}x  range {row["min_ratio"]}x-{row["max_ratio"]}x  spread {row["spread"]}x'
			)
		if any(r['spread'] and r['spread'] > 10 for r in ratios):
			print('  no stable conversion factor — these counters measure different things')

	print(f'\nDONE -> {OUT_DIR}')


if __name__ == '__main__':
	main()
