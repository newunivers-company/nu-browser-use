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

import re
from pathlib import Path

import pytest

COLLECTORS_DIR = Path(__file__).resolve().parents[2] / 'scripts' / 'site_collectors'
STAGE_SCRIPT = COLLECTORS_DIR / 'stage_to_nas.sh'

# Output dirs deliberately kept off shared storage, with the reason.
EXCLUDED = {
	# Enumeration of a piracy site's listings, produced by a parallel session.
	# It contradicts the "no listing enumeration, no index building" guardrail in
	# newtoki_watch.py; staging would distribute that inventory. Stays local
	# until a human settles the conflict.
	'newtoki_market': 'conflicts with the no-listing-enumeration guardrail',
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


@pytest.mark.parametrize('name', ['catalog_posters', 'source_review', 'trope_rank'])
def test_suffixless_output_dirs_are_covered(name):
	"""Regression: the dirs the *_export rewrite dropped on the floor."""
	assert matches_staging(name, staging_patterns()), f'{name} is not matched by stage_to_nas.sh'
