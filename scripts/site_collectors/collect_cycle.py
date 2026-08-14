"""Run the collection cadence, then stage the results.

Everything collected so far is a single photograph. `ranking-collection-plan.md`
argues the signal is not the rank but the movement — rank velocity, new entries,
days in top ten — and none of that can be computed from one snapshot. The
collectors already write dated snapshots and dedupe on re-runs; what was missing
was anything that runs them again tomorrow.

Two cadences, chosen by how fast the underlying thing changes:

  daily   things that move day to day and whose delta is the point — catalogue
          view counts, app-store ratings and release cadence, channel size, and
          social trend surfaces (Mastodon, Lemmy, Bluesky)
  weekly  structure, inventory and ranking tables published weekly, where daily
          churn would be noise — registry liveness, corporate-site diffs, the
          weekly ranking sources, the catalog-wide source loop, and the join and
          trope analysis that read whatever the collectors produced

Anything not in either list is excluded on purpose; see UNSCHEDULED_BY_DESIGN
for which and why. A collector missing from a cadence should be a decision, not
something nobody noticed.

Each step is a separate process, so one failure cannot take the cycle down; the
exit code, duration and output tail are recorded per step. Staging to the NAS
runs last and always, because a cycle whose results never leave the machine is
the failure mode `docs/collection-policy.md` calls out by name.

Usage:
  python collect_cycle.py --daily
  python collect_cycle.py --weekly
  python collect_cycle.py --daily --dry-run
Output (CYCLE_OUT, default ~/collect_cycle):
  runs/YYYY-MM-DD-<cadence>.json   - per-step status, duration, tail
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import subprocess
import sys
from pathlib import Path

if sys.stdout.encoding and sys.stdout.encoding.lower() != 'utf-8':
	sys.stdout.reconfigure(encoding='utf-8', errors='replace')

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[1]
OUT_DIR = Path(os.environ.get('CYCLE_OUT', str(Path.home() / 'collect_cycle')))
PYTHON = sys.executable

# (step name, argv after the interpreter, minutes allowed)
DAILY: list[tuple[str, list[str], int]] = [
	('appstore', [str(HERE / 'appstore_watch.py')], 10),
	('telegram', [str(HERE / 'telegram_card.py')], 5),
	('mastodon', [str(HERE / 'mastodon_trends.py')], 15),
	('lemmy', [str(HERE / 'lemmy_communities.py')], 20),
	('bluesky', [str(HERE / 'bsky_trend_collect.py')], 15),
	# Downloads are a monotonic counter, so the daily delta is the adoption
	# signal — the same shape as app-store rating counts.
	('huggingface', [str(HERE / 'huggingface_hub_collect.py')], 25),
	('goodshort', [str(HERE / 'goodshort_collect.py'), '--pages', '220'], 45),
	('dramaboxdb', [str(HERE / 'dramaboxdb_collect.py'), '--expand', '3', '--max-titles', '2500'], 45),
	('mydrama', [str(HERE / 'mydrama_collect.py')], 20),
	('reelshort', [str(HERE / 'reelshort_collect.py'), '--no-posters'], 30),
	('netshort', [str(HERE / 'netshort_collect.py')], 20),
	# Rail ordinals, not the catalogue: DramaBox's homepage ordering is itself a
	# PLATFORM_INTERNAL ranking and changes daily.
	('dramabox_rails', [str(HERE / 'dramabox_ranking.py')], 15),
]

WEEKLY: list[tuple[str, list[str], int]] = [
	('registry_verify', [str(HERE / 'promo_registry_verify.py')], 15),
	('corpsites', [str(HERE / 'corpsite_watch.py')], 15),
	('verticaldrama', [str(HERE / 'verticaldrama_collect.py')], 15),
	('duanju007', [str(HERE / 'duanju007_collect.py')], 15),
	('vigloo_snapshot', [str(HERE / 'vigloo_snapshot.py')], 30),
	('gdelt', [str(HERE / 'gdelt_collect.py')], 20),
	('prompt_repos', [str(HERE / 'prompt_repo_collect.py')], 20),
	# Browser-rendered listing, so weekly rather than daily: the cost is a real
	# Chromium launch and the leaderboard does not turn over in a day.
	('civitai_models', [str(HERE / 'civitai_browser_collect.py')], 30),
	('source_harvest', [str(HERE / 'source_harvest.py')], 40),
	('source_loop', [str(HERE / 'source_collect_loop.py')], 180),
	# Browser-driven, so slowest and last: a hung page must not delay the rest.
	('shortmax', [str(HERE / 'shortmax_collect.py')], 60),
	('flextv', [str(HERE / 'flextv_collect.py'), '--genres', '6'], 60),
	# Cover art for whatever the daily catalogue runs added this week. Weekly
	# rather than daily because a download pass over unchanged titles is pure
	# cost — the collector skips what is already on disk.
	('catalog_posters', [str(HERE / 'catalog_posters.py')], 45),
	# Analysis runs after the catalogues it reads, never before.
	('title_join', [str(HERE / 'title_join.py')], 10),
	('trope_rank', [str(HERE / 'trope_rank.py')], 10),
]

# Deliberately NOT scheduled, so an omission reads as a decision rather than an
# oversight:
#   one-shot corpora        gutenberg_catalog, loc, wikidata_graph, videofeedback
#                           — static datasets; re-running them yields the same rows
#   operator-session bound  pin_collect, shotdeck_* — need a signed-in browser
#                           and should not run unattended
#   superseded              dramabox_collect (dramaboxdb_collect covers DramaBox
#                           with view counts), vigloo_collect (vigloo_snapshot is
#                           the recurring half)
#   blocked on input        newtoki_watch — needs the rights-holder watchlist;
#                           add it here the day that lands
#   recon, not collection   promo_recon, newtoki_calibrate, login_source_probe,
#                           promo_browser_collect, browser_upgrade_review — run
#                           by hand when a source's shape, or what to build
#                           next, is in question
#   one-shot asset pulls    vigloo_assets, vigloo_episode_thumbs — posters and
#                           episode stills are fetched once and cached; re-running
#                           re-downloads images for no new signal
#   unresolved policy       newtoki_market_intel, newtoki_work_meta — written by
#                           a parallel session and untracked. They enumerate a
#                           piracy site's listings, which contradicts the
#                           "no listing enumeration" guardrail in
#                           newtoki_watch.py. Not scheduled until that conflict
#                           is settled by a human.
UNSCHEDULED_BY_DESIGN = {
	'gutenberg_catalog_collect.py', 'loc_collect.py', 'wikidata_graph_collect.py',
	'videofeedback_collect.py', 'pin_collect.py', 'shotdeck_collect.py',
	'shotdeck_render_collect.py', 'dramabox_collect.py', 'vigloo_collect.py',
	'newtoki_watch.py', 'promo_recon.py', 'newtoki_calibrate.py',
	'login_source_probe.py', 'promo_browser_collect.py', 'munpia_collect.py',
	'browser_upgrade_review.py',
	'comfy_workflow_collect.py', 'fal_collect.py',
	'vigloo_assets.py', 'vigloo_episode_thumbs.py',
	'newtoki_market_intel.py', 'newtoki_work_meta.py',
}

def env_for_step() -> dict[str, str]:
	"""Collector output roots default to $HOME, which is what staging reads."""
	env = dict(os.environ)
	env.setdefault('PYTHONIOENCODING', 'utf-8')
	env.setdefault('PYTHONUNBUFFERED', '1')
	return env


def run_step(name: str, argv: list[str], minutes: int, dry_run: bool) -> dict:
	started = dt.datetime.now(dt.timezone.utc)
	if dry_run:
		print(f'  would run {name}: {" ".join(Path(a).name if a.endswith(".py") else a for a in argv)}')
		return {'step': name, 'status': 'dry_run', 'seconds': 0}
	print(f'  {name} ...', end='', flush=True)
	try:
		proc = subprocess.run(
			[PYTHON, *argv], cwd=str(REPO_ROOT), env=env_for_step(),
			capture_output=True, text=True, encoding='utf-8', errors='replace',
			timeout=minutes * 60,
		)
		status = 'ok' if proc.returncode == 0 else 'failed'
		tail = (proc.stdout or proc.stderr or '').strip().splitlines()[-6:]
	except subprocess.TimeoutExpired:
		status, tail, proc = 'timeout', [f'exceeded {minutes}m'], None
	seconds = (dt.datetime.now(dt.timezone.utc) - started).total_seconds()
	print(f' {status} ({seconds:.0f}s)')
	return {
		'step': name,
		'status': status,
		'returncode': proc.returncode if proc else None,
		'seconds': round(seconds, 1),
		'tail': tail,
	}


def stage(dry_run: bool) -> dict:
	"""Stage 2 of the deploy. Runs even when steps failed — partial data still ships."""
	script = HERE / 'stage_to_nas.sh'
	if dry_run:
		print('  would stage to NAS')
		return {'step': 'stage_to_nas', 'status': 'dry_run', 'seconds': 0}
	started = dt.datetime.now(dt.timezone.utc)
	print('  stage_to_nas ...', end='', flush=True)
	try:
		proc = subprocess.run(
			['bash', str(script)], cwd=str(REPO_ROOT), env=env_for_step(),
			capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=60 * 60,
		)
		status = 'ok' if proc.returncode == 0 else 'failed'
		tail = (proc.stdout or proc.stderr or '').strip().splitlines()[-8:]
	except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
		status, tail = 'failed', [type(exc).__name__]
	seconds = (dt.datetime.now(dt.timezone.utc) - started).total_seconds()
	print(f' {status} ({seconds:.0f}s)')
	return {'step': 'stage_to_nas', 'status': status, 'seconds': round(seconds, 1), 'tail': tail}


def main() -> int:
	parser = argparse.ArgumentParser()
	parser.add_argument('--daily', action='store_true')
	parser.add_argument('--weekly', action='store_true')
	parser.add_argument('--only', nargs='*', help='run only these step names')
	parser.add_argument('--dry-run', action='store_true')
	parser.add_argument('--no-stage', action='store_true')
	args = parser.parse_args()

	if not (args.daily or args.weekly):
		parser.error('pick --daily or --weekly')

	steps = (DAILY if args.daily else []) + (WEEKLY if args.weekly else [])
	if args.only:
		wanted = set(args.only)
		steps = [s for s in steps if s[0] in wanted]
	cadence = 'daily' if args.daily and not args.weekly else 'weekly' if args.weekly and not args.daily else 'both'

	started = dt.datetime.now(dt.timezone.utc)
	print(f'{cadence} cycle: {len(steps)} steps')
	results = [run_step(name, argv, minutes, args.dry_run) for name, argv, minutes in steps]
	if not args.no_stage:
		results.append(stage(args.dry_run))

	OUT_DIR.mkdir(parents=True, exist_ok=True)
	(OUT_DIR / 'runs').mkdir(exist_ok=True)
	stamp = dt.date.today().isoformat()
	report = {
		'cadence': cadence,
		'started_at': started.isoformat(),
		'seconds': round((dt.datetime.now(dt.timezone.utc) - started).total_seconds(), 1),
		'steps': results,
	}
	(OUT_DIR / 'runs' / f'{stamp}-{cadence}.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')

	failed = [r for r in results if r['status'] in ('failed', 'timeout')]
	print(f'\n{len(results) - len(failed)}/{len(results)} ok, {len(failed)} failed in {report["seconds"] / 60:.1f}m')
	for row in failed:
		print(f'  FAILED {row["step"]}: {"; ".join(row.get("tail") or [])[:120]}')
	print(f'-> {OUT_DIR / "runs" / f"{stamp}-{cadence}.json"}')
	# Non-zero only if everything failed; a partial cycle is still a useful cycle.
	return 1 if failed and len(failed) == len(results) else 0


if __name__ == '__main__':
	raise SystemExit(main())
