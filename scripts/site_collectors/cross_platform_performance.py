"""Cross-platform performance for works distributed on 2+ platforms.

title_join already answers "which platforms carry the same work" (multi_platform.csv,
282 works, up to 4 platforms each). What nobody computes yet is what those
co-listings say about platform performance — the original plan's
CROSS_PLATFORM rank_type.

For each multi-platform work with 2+ view counts:
  - per-platform views, and the ratio to the best platform
  - winner / loser per work
Aggregates:
  - platform win shares (where a work performs best)
  - platform pair asymmetry (median ratio A/B and B/A) — distribution muscle:
    a platform winning most head-to-heads for the same title is an audience-
    reach signal, not a content one, because the work is held constant.

Caveats kept visible in the output: view counts are snapshots of unknown
upload dates per platform, so a work that shipped on A months before B is
not a fair pair. Ratios are still the only cross-platform reach measurement
we have; per-work rows keep the raw numbers so any later upload-date signal
can re-interpret them.

Output (JOIN_OUT, default ~/join_export):
  cross_platform_views.csv  - per work x platform views + ratio
  platform_strength.json    - win shares and pair asymmetries
"""

from __future__ import annotations

import csv
import json
import os
from collections import Counter, defaultdict
from pathlib import Path

JOIN_OUT = Path(os.environ.get('JOIN_OUT', str(Path.home() / 'join_export')))
PLATFORMS = ('goodshort', 'mydrama', 'dramaboxdb', 'flextv', 'shortmax', 'netshort', 'reelshort')


def main() -> None:
	multi_path = JOIN_OUT / 'multi_platform.csv'
	rows = list(csv.DictReader(open(multi_path, encoding='utf-8-sig')))

	out_rows: list[dict] = []
	win_shares: Counter = Counter()
	pair_totals: dict[tuple[str, str], list[float]] = defaultdict(list)
	weighted_wins: Counter = Counter()

	for row in rows:
		vals = []
		for p in PLATFORMS:
			raw = (row.get(f'{p}_views') or '').strip()
			if raw:
				try:
					vals.append((p, int(float(raw))))
				except ValueError:
					continue
		if len(vals) < 2:
			continue
		best_platform, best_views = max(vals, key=lambda x: x[1])
		if best_views <= 0:
			continue
		for p, v in vals:
			out_rows.append(
				{
					'title': row['title'],
					'platform': p,
					'views': v,
					'ratio_to_best': round(v / best_views, 4),
					'is_best': p == best_platform,
					'co_platforms': row['platforms'],
				}
			)
		win_shares[best_platform] += 1
		weighted_wins[best_platform] += 1 / len({p for p, _ in vals})
		for i, (pa, va) in enumerate(vals):
			for pb, vb in vals[i + 1 :]:
				if vb > 0 and va > 0:
					pair_totals[(pa, pb)].append(va / vb)
					pair_totals[(pb, pa)].append(vb / va)

	pairs = {}
	for (a, b), ratios in pair_totals.items():
		ratios.sort()
		pairs[f'{a}|{b}'] = {
			'n': len(ratios),
			'median_ratio': round(ratios[len(ratios) // 2], 3),
			'ge_1_share': round(sum(1 for r in ratios if r >= 1) / len(ratios), 3),
		}

	with (JOIN_OUT / 'cross_platform_views.csv').open('w', newline='', encoding='utf-8-sig') as fh:
		writer = csv.DictWriter(fh, fieldnames=['title', 'platform', 'views', 'ratio_to_best', 'is_best', 'co_platforms'])
		writer.writeheader()
		writer.writerows(out_rows)

	summary = {
		'works_compared': len({r['title'] for r in out_rows}),
		'win_shares': dict(win_shares.most_common()),
		'win_shares_fractional': {k: round(v, 2) for k, v in weighted_wins.most_common()},
		'pair_asymmetry': pairs,
		'caveat': 'views are single snapshots; per-platform upload dates are not yet known, so early-platform bias is unquantified',
	}
	(JOIN_OUT / 'platform_strength.json').write_text(json.dumps(summary, ensure_ascii=False, indent=1), encoding='utf-8')

	print(f'works compared: {summary["works_compared"]}')
	print('win shares:', summary['win_shares'])
	for pair, stats in sorted(pairs.items(), key=lambda kv: -kv[1]['n'])[:6]:
		print(f'  {pair}: n={stats["n"]} median={stats["median_ratio"]}')


if __name__ == '__main__':
	main()
