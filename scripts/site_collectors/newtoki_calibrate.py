"""Measure what newtoki_watch can and cannot detect.

The watch reports zeros. Two commits established that a zero is only meaningful
if the detector is proven live, and that the evidence layer needs the same
proof. This closes the remaining question, which is the one that actually
decides the tool's worth:

    if one of our titles WERE uploaded there, would we find it?

Two ceilings sit above that, and they multiply:

  1. SITE RECALL. `?stx=` is a title-only index — `sfl=wr_subject`,
     `sfl=wr_name` and `sfl=wr_content` all return byte-identical result counts
     to no `sfl` at all, so the parameter is ignored and there is no author or
     body search to fall back on. If the site matches titles literally, an
     upload renamed even slightly is unreachable by any query we send, and no
     amount of cleverness in our matcher recovers it.
  2. CLASSIFIER RECALL. Of what search does return, does classify() rank the
     right row as exact or near? NEAR_THRESHOLD was picked by eye at 0.55;
     this measures whether that number is defensible.

Method: sample titles the index is known to carry, perturb each the way a
re-uploader plausibly would (spacing, decorations, truncation, a dropped
character), query the perturbation, and check whether the original series id
comes back. Aggregate recall per perturbation type is the answer.

This reads the public search index only, records ids and rates, and prints no
titles — it measures the instrument, it does not collect a catalog.

Usage:
  python newtoki_calibrate.py --sample 8
Output (NEWTOKI_OUT, default ~/newtoki_watch):
  calibration/YYYY-MM-DD.json - per-perturbation recall + threshold analysis
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import os
import re
import sys
import tempfile
from collections import defaultdict
from pathlib import Path

if sys.stdout.encoding and sys.stdout.encoding.lower() != 'utf-8':
	sys.stdout.reconfigure(encoding='utf-8', errors='replace')

sys.path.insert(0, str(Path(__file__).resolve().parent))

from newtoki_watch import (
	ALLOWED,
	CONTROL_QUERY,
	MIRRORS,
	NEAR_THRESHOLD,
	classify,
	normalize,
	search,
	similarity,
)

from browser_use.browser.profile import BrowserProfile
from browser_use.browser.session import BrowserSession

OUT_DIR = Path(os.environ.get('NEWTOKI_OUT', str(Path.home() / 'newtoki_watch')))
# Decorations a re-uploader appends or strips. Kept generic on purpose.
DECORATION_RE = re.compile(r'\s*(?:\[[^\]]*\]|\([^)]*\)|(?:시즌|season)\s*\d+|\d+부|완결|RAW|raw)\s*')


def perturb(title: str) -> dict[str, str]:
	"""Realistic re-upload renamings of one title.

	Each is a way the same work shows up under a different string; the point is
	to find which of them the site's own search still resolves.
	"""
	stripped = DECORATION_RE.sub(' ', title).strip()
	compact = re.sub(r'\s+', '', title)
	spaced = re.sub(r'(\S)(\S)', r'\1\2', title)  # identity guard for 1-char titles
	variants = {
		'identity': title,
		'no_spaces': compact,
		'decorations_removed': stripped if stripped and stripped != title else '',
		'prefix_60pct': title[: max(2, int(len(title) * 0.6))].strip(),
		'drop_last_char': title[:-1].strip() if len(title) > 2 else '',
		'drop_mid_char': (title[: len(title) // 2] + title[len(title) // 2 + 1 :]).strip() if len(title) > 3 else '',
		'extra_space': ' '.join(spaced) if len(title) <= 12 else '',
	}
	return {name: value for name, value in variants.items() if value and value.strip()}


async def main() -> None:
	parser = argparse.ArgumentParser()
	parser.add_argument('--sample', type=int, default=8, help='how many known-present titles to probe')
	parser.add_argument('--headful', action='store_true')
	args = parser.parse_args()

	today = dt.date.today().isoformat()
	out_dir = OUT_DIR / 'calibration'
	out_dir.mkdir(parents=True, exist_ok=True)

	recall: dict[str, dict[str, int]] = defaultdict(lambda: {'hit': 0, 'miss': 0})
	scores: list[dict] = []
	probes = 0

	with tempfile.TemporaryDirectory(prefix='newtoki_cal_') as profile_dir:
		profile = BrowserProfile(headless=not args.headful, keep_alive=False, user_data_dir=Path(profile_dir), allowed_domains=ALLOWED)
		session = BrowserSession(browser_profile=profile)
		try:
			await session.start()
			host = MIRRORS[0]
			seeds = await search(session, host, CONTROL_QUERY)
			if not seeds:
				print('no seed results — cannot calibrate (detector or mirror is down)')
				return
			seeds = seeds[: args.sample]
			print(f'calibrating on {len(seeds)} titles the index is known to carry (titles not printed)')

			for index, seed in enumerate(seeds, 1):
				variants = perturb(seed['title'])
				found_for_seed = []
				for name, query in variants.items():
					try:
						results = await search(session, host, query)
					except Exception as exc:  # noqa: BLE001
						print(f'  [{index}] {name}: query failed ({type(exc).__name__})')
						continue
					probes += 1
					hit = any(r['id'] == seed['id'] for r in results)
					recall[name]['hit' if hit else 'miss'] += 1
					found_for_seed.append(name if hit else f'-{name}')
					if hit:
						# How does our own classifier score the true row?
						row = next(r for r in results if r['id'] == seed['id'])
						kind, score = classify(normalize(query), row['title'])
						scores.append({'variant': name, 'kind': kind, 'score': round(score, 3), 'true_match': True})
					# Negative side: strongest score among rows that are NOT the seed.
					others = [r for r in results if r['id'] != seed['id']]
					if others:
						best = max(similarity(normalize(query), normalize(r['title'])) for r in others)
						scores.append({'variant': name, 'kind': 'other', 'score': round(best, 3), 'true_match': False})
					await asyncio.sleep(1.5)
				print(f'  [{index}/{len(seeds)}] {len(variants)} variants -> site found: {sum(1 for v in found_for_seed if not v.startswith("-"))}')
		finally:
			await session.kill()

	print(f'\nSITE RECALL by perturbation ({probes} probes)')
	print(f'  {"perturbation":22} {"found":>6} {"missed":>7} {"recall":>8}')
	for name, counts in sorted(recall.items(), key=lambda kv: -(kv[1]['hit'] / max(1, kv[1]['hit'] + kv[1]['miss']))):
		total = counts['hit'] + counts['miss']
		print(f'  {name:22} {counts["hit"]:>6} {counts["miss"]:>7} {counts["hit"] / total:>7.0%}')

	true_scores = [s['score'] for s in scores if s['true_match']]
	false_scores = [s['score'] for s in scores if not s['true_match']]
	analysis: dict = {'threshold_in_use': NEAR_THRESHOLD, 'n_true': len(true_scores), 'n_false': len(false_scores)}
	# Negatives are scarce by construction: a good query returns mostly the one
	# right row, so most probes yield no non-seed rows to score against. Calling
	# a threshold un-separable off a handful of them would dress up a small
	# sample as a finding.
	MIN_NEGATIVES = 20
	if len(false_scores) < MIN_NEGATIVES:
		analysis['verdict'] = 'underpowered'
		print('\nCLASSIFIER SEPARATION')
		print(f'  true matches : n={len(true_scores)}, min={min(true_scores):.3f}' if true_scores else '  no true matches scored')
		print(f'  other rows   : n={len(false_scores)} — under the {MIN_NEGATIVES} needed to say anything')
		print(f'  UNDERPOWERED: threshold {NEAR_THRESHOLD} is neither confirmed nor refuted by this run')
		print('  (raise --sample, or calibrate against queries that return crowded result sets)')
	elif true_scores and false_scores:
		analysis |= {
			'true_match_min': min(true_scores), 'true_match_median': sorted(true_scores)[len(true_scores) // 2],
			'false_match_max': max(false_scores), 'false_match_p95': sorted(false_scores)[int(len(false_scores) * 0.95) - 1],
			'separable': min(true_scores) > max(false_scores),
		}
		print('\nCLASSIFIER SEPARATION')
		print(f'  true matches : min={analysis["true_match_min"]:.3f} median={analysis["true_match_median"]:.3f} (n={len(true_scores)})')
		print(f'  other rows   : max={analysis["false_match_max"]:.3f} p95={analysis["false_match_p95"]:.3f} (n={len(false_scores)})')
		if analysis['separable']:
			print(f'  separable — any threshold in ({analysis["false_match_max"]:.3f}, {analysis["true_match_min"]:.3f}) works; in use: {NEAR_THRESHOLD}')
		else:
			print(f'  NOT separable — true-match floor {analysis["true_match_min"]:.3f} sits below other-row ceiling {analysis["false_match_max"]:.3f};')
			print('  no single threshold separates them, so near-matches need human review by design')

	report = {
		'date': today, 'host': MIRRORS[0], 'probes': probes,
		'site_recall': {name: {**counts, 'recall': counts['hit'] / max(1, counts['hit'] + counts['miss'])} for name, counts in recall.items()},
		'classifier': analysis, 'score_samples': scores[:400],
	}
	(out_dir / f'{today}.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
	print(f'\nDONE -> {out_dir / f"{today}.json"}')


if __name__ == '__main__':
	asyncio.run(main())
