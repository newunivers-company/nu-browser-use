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
import shutil
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
	('goodshort', [str(HERE / 'goodshort_collect.py'), '--pages', '220', '--seed-from-union'], 45),
	('dramaboxdb', [str(HERE / 'dramaboxdb_collect.py'), '--expand', '3', '--max-titles', '2500', '--seed-from-union'], 45),
	('mydrama', [str(HERE / 'mydrama_collect.py')], 20),
	('reelshort', [str(HERE / 'reelshort_collect.py'), '--no-posters'], 30),
	('netshort', [str(HERE / 'netshort_collect.py')], 20),
	# Munpia rails are a daily ranking surface with per-novel view/preference/
	# like counts — the Korean web-novel counterpart to the goodnovel boards.
	# Keyless JSON API, no browser, ~2 minutes for 12 genre rails + details.
	('munpia', [str(HERE / 'munpia_collect.py')], 15),
	# Daily despite costing two Chromium launches, unlike the other browser steps
	# which sit in the weekly cadence. These are ordered ranking boards and the
	# cards state their own movement ("上升6名" — up six places), so the thing
	# being measured demonstrably turns over daily. Sampling weekly would record
	# the ordinal and lose the velocity, which is the only reason to collect a
	# rank at all. Contrast dramabox_rails, moved the other way for the opposite
	# reason: its numbers are fixed at build time.
	('browser_boards', [str(HERE / 'browser_catalog_collect.py')], 35),
	# Reads the two board snapshots the step above just wrote; local-only and
	# seconds long. Day-over-day movement is the reason browser_boards is daily.
	('board_movement', [str(HERE / 'board_movement.py')], 10),
	# The other half of the movement story: absolute view deltas rather than
	# ordinal ones. Reads the goodshort daily snapshots the step above wrote, so
	# it can only be daily — a weekly run would compare two days a week apart and
	# call the gap velocity. Local-only, seconds long.
	('velocity', [str(HERE / 'velocity_report.py')], 10),
	# Local-only and seconds long: folds today's snapshot into the cumulative
	# catalogue so "known" and "seen by the latest walk" stay distinguishable.
	# Runs last in the daily cadence because it reads what the collectors above
	# just wrote.
	('catalog_state', [str(HERE / 'catalog_state.py')], 10),
]

WEEKLY: list[tuple[str, list[str], int]] = [
	# Rail ordinals, not the catalogue: DramaBox's homepage ordering is a
	# PLATFORM_INTERNAL ranking. It was daily on the assumption the ordering
	# turns over daily; it does not. The page is statically generated, so the
	# rails and their viewCounts are fixed at build time — two fresh fetches 20
	# hours apart returned byte-identical figures under a buildId six weeks old.
	# Daily would bank the same 42 observations every day until DramaBox next
	# deploys, so this now samples at a rate a rebuild can actually beat.
	('dramabox_rails', [str(HERE / 'dramabox_ranking.py')], 15),
	('registry_verify', [str(HERE / 'promo_registry_verify.py')], 15),
	('corpsites', [str(HERE / 'corpsite_watch.py')], 15),
	('verticaldrama', [str(HERE / 'verticaldrama_collect.py')], 15),
	('duanju007', [str(HERE / 'duanju007_collect.py')], 15),
	# Home rails and rank positions turn over slowly, and the reads/writes are
	# absolute counters on detail pages — weekly deltas are the usable signal,
	# and the same rail repeated daily would add rows without information.
	('fanqie', [str(HERE / 'fanqie_collect.py')], 20),
	('vigloo_snapshot', [str(HERE / 'vigloo_snapshot.py')], 30),
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
	# Analysis runs after the catalogues it reads, never before — and title_join
	# now reads the cumulative catalogue, so this has to refresh it first.
	('catalog_state', [str(HERE / 'catalog_state.py')], 10),
	# Before title_join, because it raises that join's ceiling: the Chinese
	# ranking source speaks zh and everything else speaks en, so the 84
	# multi-source titles were a language limit, not a data limit. Expensive —
	# 3,339 slugs x 2 language pages, serial with a 1.2s gap and a 20s pause
	# every 60, so ~2.5h by construction rather than by slowness. The obvious
	# optimisation is skipping slugs already in title_bridge.json; until that
	# exists this gets a source_loop-sized budget.
	('shortdramacast', [str(HERE / 'shortdramacast_map.py')], 180),
	('title_join', [str(HERE / 'title_join.py')], 10),
	('trope_rank', [str(HERE / 'trope_rank.py')], 10),
	# Read whatever the join produced; local-only and seconds long. Order is a
	# dependency chain, not a preference — cross_platform reads multi_platform.csv
	# and nu_rank reads both the observations and the bridge above.
	('cross_platform', [str(HERE / 'cross_platform_performance.py')], 10),
	('nu_rank', [str(HERE / 'nu_rank.py')], 10),
	# Pure local analysis over the five China exports another operation writes to
	# the same NAS share. Weekly because we do not control their cadence and a
	# daily re-render of unchanged inputs is only noise; it now resolves the NAS
	# root instead of assuming the WSL mount, so an unreachable share is loud.
	('china_dashboard', [str(HERE / 'china_dashboard.py'), '--write'], 10),
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
#   blocked by the source   gdelt_collect — has never returned a row. Every
#                           snapshot on disk is an empty array, articles.csv was
#                           never created, and a single isolated request answers
#                           429 telling high-traffic users to switch to the
#                           ngrams dataset. It reported `ok` regardless until it
#                           was made to exit non-zero. Re-schedule when someone
#                           has taken one of the paths GDELT names.
#   recon, not collection   promo_recon, newtoki_calibrate, login_source_probe,
#                           promo_browser_collect, browser_upgrade_review,
#                           browser_render_probe — run by hand when a source's
#                           shape, or what to build next, is in question. The
#                           render probe in particular launches one Chromium per
#                           source and answers a question that only changes when
#                           a site is rebuilt.
#   one-shot asset pulls    vigloo_assets, vigloo_episode_thumbs — posters and
#                           episode stills are fetched once and cached; re-running
#                           re-downloads images for no new signal
#   operator-supplied list  instagram_post_collect — reads only the post URLs it
#                           is handed, so there is no standing target set to run
#                           against. Scheduling it would mean either an empty run
#                           or a stored list that grows, and a list that grows is
#                           the profile walk docs/collection-policy.md's
#                           「지정 URL 참조 영상」 section rules out.
#   rights-protection prep  build_watchlist — produces the watchlist file
#                           newtoki_watch consumes. Scheduling the producer while
#                           the consumer stays unscheduled buys nothing; the two
#                           move together the day the watchlist lands.
#   on-demand rights work   newtoki_market_intel, newtoki_work_meta — piracy
#                           supply observation, now covered by the
#                           권리보호·침해 유통 관측 section of
#                           docs/collection-policy.md. Unscheduled because the
#                           question they answer ("what circulates") is asked
#                           occasionally, not daily, and each run drives a real
#                           browser against a host that rotates domains.
UNSCHEDULED_BY_DESIGN = {
	'gutenberg_catalog_collect.py',
	'loc_collect.py',
	'wikidata_graph_collect.py',
	'videofeedback_collect.py',
	'pin_collect.py',
	'shotdeck_collect.py',
	'shotdeck_render_collect.py',
	'dramabox_collect.py',
	'vigloo_collect.py',
	'newtoki_watch.py',
	'promo_recon.py',
	'newtoki_calibrate.py',
	'login_source_probe.py',
	'promo_browser_collect.py',
	'browser_upgrade_review.py',
	'browser_render_probe.py',
	'gdelt_collect.py',
	'comfy_workflow_collect.py',
	'fal_collect.py',
	'vigloo_assets.py',
	'vigloo_episode_thumbs.py',
	'newtoki_market_intel.py',
	'newtoki_work_meta.py',
	'instagram_post_collect.py',
	'build_watchlist.py',
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
			[PYTHON, *argv],
			cwd=str(REPO_ROOT),
			env=env_for_step(),
			capture_output=True,
			text=True,
			encoding='utf-8',
			errors='replace',
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


def find_posix_shell() -> tuple[Path | None, str]:
	"""The shell that shares a filesystem view with the collectors.

	On Windows, `bash` on PATH resolves to System32\\bash.exe — the WSL launcher.
	WSL is a different machine for our purposes: $HOME is /home/<user>, none of
	the Windows-side collector output is visible there, and environment variables
	do not cross without WSLENV. Staging under it therefore discovers a
	*different* set of directories, copies those, and reports success — a green
	cycle that never deployed anything it collected. Git Bash shares the Windows
	filesystem and is the only correct choice here.
	"""
	if os.name != 'nt':
		found = shutil.which('bash')
		return (Path(found) if found else None), 'posix host'
	# Known locations FIRST, PATH only as a fallback. Under Task Scheduler the
	# working directory is C:\Windows\System32, and shutil.which() searches the
	# current directory before PATH on Windows — so it returns the bare string
	# 'bash.EXE', which is the WSL launcher sitting in that very directory. A
	# substring test for 'system32' cannot see it, because the path has no
	# directory part at all. Resolving first is what makes the check meaningful.
	candidates = [
		Path(r'C:\Program Files\Git\bin\bash.exe'),
		Path(r'C:\Program Files\Git\usr\bin\bash.exe'),
		Path(r'C:\Program Files (x86)\Git\bin\bash.exe'),
	]
	on_path = shutil.which('bash')
	if on_path:
		resolved = Path(on_path).resolve()
		if 'system32' not in str(resolved).lower():
			candidates.append(resolved)
	for candidate in candidates:
		if candidate.exists():
			return candidate, str(candidate)
	return None, 'no Git Bash found'


def expected_shell_home(home: str) -> str:
	"""How the staging shell should spell the home directory the collectors used.

	Git Bash writes `C:\\Users\\USER` as `/c/Users/USER`, so the comparison has to
	be made on that footing — but only where there is a drive letter to move.
	Applying the rewrite unconditionally turned a POSIX `/home/runner` into
	`//home/runner`, because `partition(':')` puts the whole path in `drive` when
	there is no colon and the leading `/` then gets prepended to a string that
	already had one. Windows hosts never saw it; the first CI run on Linux did.
	"""
	drive, colon, rest = home.partition(':')
	if not colon:
		return home
	return f'/{drive.lower()}{rest}'.replace('\\', '/')


def shell_sees_our_home(shell: Path) -> tuple[bool, str]:
	"""Refuse to stage from a shell whose $HOME is not the one the collectors wrote to."""
	try:
		proc = subprocess.run(
			[str(shell), '-c', 'echo "$HOME"'],
			capture_output=True,
			text=True,
			encoding='utf-8',
			errors='replace',
			timeout=60,
		)
	except (subprocess.TimeoutExpired, OSError) as exc:
		return False, type(exc).__name__
	seen = (proc.stdout or '').strip()
	if not seen:
		return False, 'shell reported no $HOME'
	expected = expected_shell_home(str(Path.home()))
	return seen.lower() == expected.lower(), f'{seen} (expected {expected})'


def stage(dry_run: bool) -> dict:
	"""Stage 2 of the deploy. Runs even when steps failed — partial data still ships."""
	script = HERE / 'stage_to_nas.sh'
	if dry_run:
		print('  would stage to NAS')
		return {'step': 'stage_to_nas', 'status': 'dry_run', 'seconds': 0}
	started = dt.datetime.now(dt.timezone.utc)
	print('  stage_to_nas ...', end='', flush=True)
	shell, how = find_posix_shell()
	if shell is None:
		print(f' failed (0s) — {how}')
		return {'step': 'stage_to_nas', 'status': 'failed', 'seconds': 0.0, 'tail': [f'no usable shell: {how}']}
	ok_home, detail = shell_sees_our_home(shell)
	if not ok_home:
		# Louder than a silent mis-stage: this is the failure that looks like success.
		print(' failed (0s) — wrong filesystem view')
		return {
			'step': 'stage_to_nas',
			'status': 'failed',
			'seconds': 0.0,
			'tail': [f'{shell} has $HOME={detail}; it cannot see the collector output, refusing to stage'],
		}
	try:
		# Relative to REPO_ROOT: MSYS bash rejects a `C:\...` or `C:/...` argument.
		rel = script.relative_to(REPO_ROOT).as_posix()
		proc = subprocess.run(
			[str(shell), rel],
			cwd=str(REPO_ROOT),
			env=env_for_step(),
			capture_output=True,
			text=True,
			encoding='utf-8',
			errors='replace',
			timeout=60 * 60,
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
	report_path = OUT_DIR / 'runs' / f'{stamp}-{cadence}.json'
	report = {
		'cadence': cadence,
		'started_at': started.isoformat(),
		'seconds': round((dt.datetime.now(dt.timezone.utc) - started).total_seconds(), 1),
		'steps': results,
	}
	# A dry run must never stand in for a real one. Writing unconditionally cost
	# us today's real daily record — twelve steps with their output tails,
	# replaced by a listing of what would have run, and only recoverable because
	# staging had already copied it to the NAS.
	if args.dry_run and report_path.exists():
		print(f'\n(dry run) leaving the existing record at {report_path} untouched')
	else:
		report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')

	failed = [r for r in results if r['status'] in ('failed', 'timeout')]
	print(f'\n{len(results) - len(failed)}/{len(results)} ok, {len(failed)} failed in {report["seconds"] / 60:.1f}m')
	for row in failed:
		print(f'  FAILED {row["step"]}: {"; ".join(row.get("tail") or [])[:120]}')
	print(f'-> {report_path}')
	# Non-zero only if everything failed; a partial cycle is still a useful cycle.
	return 1 if failed and len(failed) == len(results) else 0


if __name__ == '__main__':
	raise SystemExit(main())
