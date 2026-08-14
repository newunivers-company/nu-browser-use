"""Every collector is scheduled or explicitly excluded — never merely forgotten.

The cadence went stale once already: collectors kept being added while
collect_cycle.py was not updated, so most of them ran exactly once and the
snapshot series they were built to produce never accumulated. Nothing caught it
because a missing entry looks identical to a deliberate omission.

This makes the difference checkable. A collector must appear in DAILY, in
WEEKLY, or in UNSCHEDULED_BY_DESIGN, and the last one carries a written reason
in the comment above it. Adding a collector without deciding its cadence fails
here rather than quietly producing one row forever.
"""

from pathlib import Path

import pytest

from scripts.site_collectors import collect_cycle

COLLECTORS_DIR = Path(collect_cycle.__file__).resolve().parent

# Modules that are libraries or entry points rather than collectors.
NOT_COLLECTORS = {
	'__init__.py',
	'collect_cycle.py',
	'textmatch.py',
	'stage_to_nas.sh',
}
# Exploratory scripts from earlier sessions: one-off probes kept for reference,
# named for what they poked at rather than what they collect.
PROBE_PREFIXES = ('probe_', 'pin_', 'debug_', 'check_', 'diagnose_', 'find_', 'clone_', 'reload_', 'test_', 'drama_recon', 'dramabox_page', 'dramabox_browse', 'dramabox_recon', 'netshort_recon', 'shotdeck_recon', 'shotdeck_totals', 'shotdeck_menu', 'build_keywords')


def scheduled_scripts() -> set[str]:
	names = set()
	for _, argv, _ in list(collect_cycle.DAILY) + list(collect_cycle.WEEKLY):
		for arg in argv:
			if arg.endswith('.py'):
				names.add(Path(arg).name)
	return names


def collector_modules() -> set[str]:
	found = set()
	for path in COLLECTORS_DIR.glob('*.py'):
		name = path.name
		if name in NOT_COLLECTORS or name.startswith(PROBE_PREFIXES):
			continue
		found.add(name)
	return found


def test_every_collector_has_a_cadence_decision():
	undecided = sorted(collector_modules() - scheduled_scripts() - collect_cycle.UNSCHEDULED_BY_DESIGN)
	assert undecided == [], (
		'these collectors are in neither cadence nor the exclusion list — '
		f'decide and record it in collect_cycle.py: {undecided}'
	)


def test_exclusions_name_real_modules():
	"""A stale exclusion hides the fact that a collector was renamed or removed."""
	missing = sorted(name for name in collect_cycle.UNSCHEDULED_BY_DESIGN if not (COLLECTORS_DIR / name).exists())
	assert missing == [], f'UNSCHEDULED_BY_DESIGN names modules that no longer exist: {missing}'


def test_scheduled_scripts_exist():
	missing = sorted(name for name in scheduled_scripts() if not (COLLECTORS_DIR / name).exists())
	assert missing == [], f'cadence schedules modules that do not exist: {missing}'


def test_no_step_is_scheduled_twice_in_one_cadence():
	for cadence_name, cadence in (('DAILY', collect_cycle.DAILY), ('WEEKLY', collect_cycle.WEEKLY)):
		names = [step for step, _, _ in cadence]
		assert len(names) == len(set(names)), f'{cadence_name} has duplicate step names: {names}'


def test_analysis_runs_after_the_collectors_it_reads():
	"""title_join and trope_rank consume catalogue output, so they must come last."""
	order = [step for step, _, _ in collect_cycle.WEEKLY]
	for analysis in ('title_join', 'trope_rank'):
		assert analysis in order, f'{analysis} should be in the weekly cadence'
	producers = [order.index(name) for name in ('shortmax', 'flextv') if name in order]
	assert producers, 'expected catalogue collectors in the weekly cadence'
	assert order.index('title_join') > max(producers)
	assert order.index('trope_rank') > order.index('title_join')


@pytest.mark.parametrize('cadence', [collect_cycle.DAILY, collect_cycle.WEEKLY])
def test_every_step_has_a_sane_timeout(cadence):
	for step, _, minutes in cadence:
		assert 1 <= minutes <= 240, f'{step} has an implausible timeout: {minutes}m'
