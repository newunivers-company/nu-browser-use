"""Every collector's output directory reaches the NAS, or is excluded on purpose.

stage_to_nas.sh began as a hand-written allowlist and drifted: twelve output
directories had never once been staged by the time anyone checked, because a
directory absent from the list looks exactly like a directory nobody wanted.
Rewriting it to discover `*_export` fixed most of that and immediately created
the next instance of the same bug — `catalog_posters` does not carry the suffix,
so the very next collector added went unstaged again.

So the invariant is checked instead of remembered. Every `Path.home() / 'x'`
default declared by a collector must be matched by the script's find(1)
expression or named in EXCLUDED below with a reason. A new collector whose
output nobody decided about fails here rather than silently never leaving the
workstation.

The parse is deliberately literal: it reads the `-name` tokens out of the shell
script rather than executing it, so the test states what the script says and a
divergence between the two is the failure.
"""

import json
import os
import re
from pathlib import Path

import pytest

from scripts.site_collectors import collect_cycle

COLLECTORS_DIR = Path(__file__).resolve().parents[2] / 'scripts' / 'site_collectors'
STAGE_SCRIPT = COLLECTORS_DIR / 'stage_to_nas.sh'

# The manifest is the record of what the NAS holds and it lives on the NAS, not
# in this repo, so the check below is machine-local by nature and skips where the
# share is absent (CI, any workstation without the mapping). Same candidate order
# as stage_to_nas.sh: the WSL mount and the mapped letter are one share seen from
# different shells.
NAS_CANDIDATES = (
	os.environ.get('NAS_ROOT', ''),
	'/mnt/newunivers-sdb/nu-browser-use',
	'X:/nu-browser-use',
	'/x/nu-browser-use',
	'//192.168.0.136/sdb/nu-browser-use',
)
MANIFEST_NAME = 'COLLECTION-MANIFEST.json'

# Output dirs deliberately kept off the NAS, with the reason. Empty as of
# 2026-08-16: newtoki_market was the only entry and it now stages, because the
# line that mattered turned out to be stage 3, not stage 2.
EXCLUDED: dict[str, str] = {}

# Staged to the NAS but must not continue to Google Drive. The records are
# `id + title` and the site resolves `<host>/<section>/<id>`, so the dataset is
# also a working index of infringing works: holding it on internal storage is
# observation, syncing it to external cloud storage is closer to distribution.
#
# Marked, not enforced. The stage-3 script docs/collection-policy.md names
# (sync_all.ps1) is not in this repo, on the NAS, or in $HOME, so nothing here
# can refuse on its behalf. The marker file is the entire mechanism, which is
# why it gets a test.
#
# instagram_export joins it for a different reason (2026-08-18): those records
# are not derived annotation but the works themselves — twelve third-party reels
# whose licence nobody established. Principle 1 says we do not hold originals;
# the 「지정 URL 참조 영상」 exception permits holding these for internal
# reference and stops the deployment one stage short of external cloud storage.
NO_DRIVE_SYNC = {
	'newtoki_market': '_DO-NOT-SYNC-TO-DRIVE.md',
	'instagram_export': '_DO-NOT-SYNC-TO-DRIVE.md',
}

HOME_DIR_RE = re.compile(r"Path\.home\(\)\s*/\s*'([A-Za-z0-9_]+)'")
NAME_TOKEN_RE = re.compile(r"-name\s+'([^']+)'")


def declared_output_dirs() -> dict[str, set[str]]:
	"""Directory name -> the collector modules that write to it."""
	found: dict[str, set[str]] = {}
	for path in sorted(COLLECTORS_DIR.glob('*.py')):
		for name in HOME_DIR_RE.findall(path.read_text(encoding='utf-8')):
			found.setdefault(name, set()).add(path.name)
	return found


def staging_patterns() -> list[str]:
	body = STAGE_SCRIPT.read_text(encoding='utf-8')
	start = body.index('mapfile -t candidates')
	end = body.index(')', body.index('-printf', start))
	return NAME_TOKEN_RE.findall(body[start:end])


def matches_staging(name: str, patterns: list[str]) -> bool:
	# find -name uses shell globs; the only wildcard in use here is a leading *.
	return any(name.endswith(p[1:]) if p.startswith('*') else name == p for p in patterns)


def test_staging_script_exists_and_declares_patterns():
	assert STAGE_SCRIPT.exists(), f'missing {STAGE_SCRIPT}'
	patterns = staging_patterns()
	assert '*_export' in patterns, f'expected the *_export glob to survive edits, got {patterns}'


def test_every_output_dir_is_staged_or_excluded():
	patterns = staging_patterns()
	declared = declared_output_dirs()
	assert declared, 'found no collector output directories — the parser is broken, not the script'
	unstaged = sorted(name for name in declared if not matches_staging(name, patterns) and name not in EXCLUDED)
	assert unstaged == [], (
		'these collector output dirs would never reach the NAS — add a -name clause '
		'in stage_to_nas.sh or record them in EXCLUDED with a reason: '
		+ ', '.join(f'{n} (from {sorted(declared[n])[0]})' for n in unstaged)
	)


def test_exclusions_name_real_output_dirs():
	"""A stale exclusion silently re-permits whatever it was protecting against."""
	declared = declared_output_dirs()
	orphaned = sorted(name for name in EXCLUDED if name not in declared)
	assert orphaned == [], f'EXCLUDED names output dirs no collector declares: {orphaned}'


def test_excluded_dirs_are_not_also_matched_by_the_script():
	"""An exclusion the glob overrides is worse than no exclusion: it reads as protection."""
	patterns = staging_patterns()
	leaked = sorted(name for name in EXCLUDED if matches_staging(name, patterns))
	assert leaked == [], f'EXCLUDED dirs that stage_to_nas.sh would still upload: {leaked}'


@pytest.mark.parametrize('name', ['catalog_posters', 'source_review', 'trope_rank', 'browser_catalog_export'])
def test_suffixless_output_dirs_are_covered(name):
	"""Regression: the dirs the *_export rewrite dropped on the floor."""
	assert matches_staging(name, staging_patterns()), f'{name} is not matched by stage_to_nas.sh'


# --- which shell stages ------------------------------------------------------
# The cadence spawns a shell to stage. On Windows, `bash` on PATH is the WSL
# launcher: a different $HOME, none of the collector output visible, and no
# environment variables crossing into it. Staging under WSL copied an unrelated
# set of directories and reported success — the cycle went green having deployed
# nothing it collected. These check the guard that now refuses that.


def test_staging_shell_is_never_the_wsl_launcher():
	shell, how = collect_cycle.find_posix_shell()
	assert shell is not None, f'no usable shell for staging: {how}'
	assert 'system32' not in str(shell).lower(), f'staging would run under the WSL launcher: {shell}'


def test_staging_shell_shares_the_collectors_filesystem():
	"""The positive case: the chosen shell's $HOME is where the collectors write."""
	shell, _ = collect_cycle.find_posix_shell()
	if shell is None:
		pytest.skip('no posix shell available on this host')
	ok, detail = collect_cycle.shell_sees_our_home(shell)
	assert ok, f'staging shell cannot see the collector output: {detail}'


@pytest.mark.skipif(not Path(r'C:\Windows\System32\bash.exe').exists(), reason='WSL launcher not installed')
def test_the_guard_actually_rejects_a_foreign_shell():
	"""A check that passes everything is not a check — WSL must fail it."""
	ok, detail = collect_cycle.shell_sees_our_home(Path(r'C:\Windows\System32\bash.exe'))
	assert not ok, f'the WSL launcher passed the home check, so the guard is inert: {detail}'


# --- what the collection loop is allowed to walk -----------------------------
# The "31 AI-crawler-named sources awaiting a ruling" sat on the open-questions
# list for days. There was no open question: docs/collection-policy.md already
# forbids scraping sources that block AI crawlers by name, and the loop already
# refused them. What kept it open was the loop printing "awaiting ruling", and a
# reviewer (me) confusing "robots mentions an AI crawler" with "robots blocks
# us" — news.coupang.com names ClaudeBot precisely to write `Allow: /`.


def test_collection_loop_permits_only_unrestricted_robots_verdicts():
	from scripts.site_collectors import source_collect_loop

	permitted = source_collect_loop.PERMITTED_ROBOTS
	for forbidden in ('disallow', 'named_ai_block', 'ai_train_reserved'):
		assert forbidden not in permitted, (
			f'the collection loop would walk sources whose robots verdict is {forbidden!r}; docs/collection-policy.md forbids it'
		)
	assert permitted == {'allow', 'unknown'}, f'unexpected PERMITTED_ROBOTS: {permitted}'


# --- staged is not the same as recorded --------------------------------------
# stage_to_nas.sh discovers directories, so a new collector reaches the NAS the
# day it first writes — and is invisible in the manifest forever after, because
# nothing ever asked. On 2026-08-18 the manifest described fourteen exports while
# forty-one existed; `promo_export`, written by eight collectors, was among the
# missing. Staging coverage was already a test. Recording coverage was not, which
# is the same bug this file was written to kill, one layer up.


def nas_manifest() -> dict | None:
	for candidate in NAS_CANDIDATES:
		if not candidate:
			continue
		path = Path(candidate) / MANIFEST_NAME
		if path.is_file():
			return json.loads(path.read_text(encoding='utf-8'))
	return None


def test_every_output_dir_is_recorded_in_the_nas_manifest():
	manifest = nas_manifest()
	if manifest is None:
		pytest.skip(f'no NAS manifest reachable (tried: {[c for c in NAS_CANDIDATES if c]})')
	recorded = set(manifest.get('exports', {}))
	declared = declared_output_dirs()
	unrecorded = sorted(name for name in declared if name not in recorded)
	assert unrecorded == [], (
		'these output dirs stage to the NAS but the manifest never mentions them — add an entry: '
		+ ', '.join(f'{n} (from {sorted(declared[n])[0]})' for n in unrecorded)
	)


def test_manifest_deploy_rule_matches_the_drive_decision():
	"""The manifest retired stage 3; docs/collection-policy.md must not still promise it.

	Two records of the same decision drifted apart for four days — the manifest
	said Google Drive sync was retired on 2026-08-14 while principle 4 still read
	as a three-stage deploy ending at Drive.
	"""
	manifest = nas_manifest()
	if manifest is None:
		pytest.skip('no NAS manifest reachable')
	rule = manifest.get('deploy_rule', '')
	assert 'retired' in rule.lower(), f'deploy_rule no longer records the Drive decision: {rule!r}'
	policy = (Path(__file__).resolve().parents[2] / 'docs' / 'collection-policy.md').read_text(encoding='utf-8')
	assert 'G드라이브 동기화는 2026-08-14 은퇴' in policy, (
		'docs/collection-policy.md must state the Drive retirement the manifest records'
	)


# --- NAS yes, Drive no -------------------------------------------------------


def test_no_drive_sync_dirs_are_actually_staged_to_the_nas():
	"""The point is NAS-and-stop, not exclusion; the staging script must include them."""
	patterns = staging_patterns()
	for name in NO_DRIVE_SYNC:
		assert matches_staging(name, patterns), f'{name} is meant to reach the NAS but stage_to_nas.sh skips it'
		assert name not in EXCLUDED, f'{name} cannot be both staged and excluded'


def test_no_drive_sync_dirs_carry_their_marker():
	"""The marker is the only thing standing between this data and Google Drive.

	No script enforces stage 3 — sync_all.ps1 is not present anywhere reachable —
	so if the file goes missing the boundary goes with it silently.
	"""
	for name, marker in NO_DRIVE_SYNC.items():
		directory = Path.home() / name
		if not directory.exists():
			pytest.skip(f'{name} not present on this machine')
		path = directory / marker
		assert path.exists(), f'{name} is staged to the NAS with no {marker} telling stage 3 to skip it'
		text = path.read_text(encoding='utf-8')
		assert len(text) > 200, f'{marker} must explain the decision, not just exist'
