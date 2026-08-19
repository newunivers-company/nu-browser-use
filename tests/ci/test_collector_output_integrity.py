"""Two ways a collector's own output has been silently destroyed, both checked.

The failure has the same shape each time: a second, narrower write lands on top
of a wider one, and nothing downstream can tell the difference between "this
record is gone" and "this record was never collected".

  overwrite   build_watchlist.py exists because a single-locale vigloo run
              overwrote the all-locale programs.json and 14 matched titles could
              no longer be traced to a program. Then on 2026-08-17 a partial
              kuaikan run at 22:24 replaced the scheduled 05:12 snapshot of the
              same date; board_movement read the boards that vanished as 95
              exits and their return the next morning as 37 entries. None of
              those transitions happened.

  malformed   The watchlist is one record per line and nothing enforced it.
              Four vigloo titles carry a newline inside them, so each was
              written across two physical lines: an orphan title fragment with
              no provenance, followed by a line whose title field was the second
              half of someone else's title.

Both are invariants of the write, so both are tested at the write.
"""

import json
from pathlib import Path

from scripts.site_collectors.browser_catalog_collect import snapshot_would_regress
from scripts.site_collectors.build_watchlist import watchlist_line
from scripts.site_collectors.collect_cycle import supersede_existing_record
from scripts.site_collectors.newtoki_watch import parse_watchlist

# --- dated snapshots: a narrower same-day run must not replace a wider one ----


def rows(*boards: str) -> list[dict]:
	return [{'board': b, 'id': f'{b}-1', 'rank': 1} for b in boards]


def write_snapshot(path: Path, *boards: str) -> None:
	path.write_text(json.dumps({'items': rows(*boards)}), encoding='utf-8')


def test_no_prior_snapshot_always_writes(tmp_path: Path):
	assert snapshot_would_regress(tmp_path / 'kuaikan.json', rows('a', 'b')) is False


def test_same_or_wider_run_may_replace(tmp_path: Path):
	path = tmp_path / 'kuaikan.json'
	write_snapshot(path, 'a', 'b')
	assert snapshot_would_regress(path, rows('a', 'b')) is False
	assert snapshot_would_regress(path, rows('a', 'b', 'c')) is False


def test_a_partial_rerun_is_refused(tmp_path: Path):
	"""The 2026-08-17 case: 13 boards on disk, a later run carrying 9."""
	path = tmp_path / 'kuaikan.json'
	write_snapshot(path, *[f'board{n}' for n in range(13)])
	assert snapshot_would_regress(path, rows(*[f'board{n}' for n in range(9)])) is True


def test_a_disjoint_rerun_is_refused(tmp_path: Path):
	"""Equal board count is not enough — these are different boards."""
	path = tmp_path / 'kuaikan.json'
	write_snapshot(path, 'a', 'b')
	assert snapshot_would_regress(path, rows('c', 'd')) is True


def test_unreadable_prior_snapshot_does_not_block_the_write(tmp_path: Path):
	path = tmp_path / 'kuaikan.json'
	path.write_text('{ truncated', encoding='utf-8')
	assert snapshot_would_regress(path, rows('a')) is False


# --- watchlist: provenance survives both line shapes and a title with a pipe --


def watchlist(tmp_path: Path, *lines: str) -> Path:
	path = tmp_path / 'watchlist.txt'
	path.write_text('\n'.join(lines) + '\n', encoding='utf-8')
	return path


def test_provenance_is_read_off_the_end_not_position_one(tmp_path: Path):
	"""A `|` inside the title is why the head is the wrong anchor."""
	works = parse_watchlist(watchlist(tmp_path, 'Law Firm Romance|The Lie|vigloo|15002868|en'))
	assert len(works) == 1
	assert works[0]['title'] == 'Law Firm Romance'
	assert works[0]['aliases'] == ['The Lie']
	assert works[0]['provenance'] == {'platform': 'vigloo', 'program_id': '15002868', 'locale': 'en'}


def test_reelshort_hex_ids_are_provenance_too(tmp_path: Path):
	"""vigloo numbers its programs, reelshort uses 24-char hex."""
	works = parse_watchlist(watchlist(tmp_path, "You've Been Replaced|reelshort|6a469b12d3f5c65f7f095b8a|en"))
	assert works[0]['provenance']['program_id'] == '6a469b12d3f5c65f7f095b8a'


def test_legacy_alias_lines_keep_working(tmp_path: Path):
	"""The old shape is bare titles; nothing there may be mistaken for an id."""
	works = parse_watchlist(watchlist(tmp_path, 'Bare Title', 'Title|Alias One|Alias Two'))
	assert [w['title'] for w in works] == ['Bare Title', 'Title']
	assert all('provenance' not in w for w in works)
	assert works[1]['aliases'] == ['Alias One', 'Alias Two']


def test_a_title_with_a_newline_still_writes_one_line(tmp_path: Path):
	"""The writer's invariant: a reader cannot undo a record split in two."""
	line = watchlist_line('Kind Coffee,\nDelicious Boss', 'vigloo', '15001819', 'hi')
	assert line == 'Kind Coffee, Delicious Boss|vigloo|15001819|hi'

	works = parse_watchlist(watchlist(tmp_path, line))
	assert len(works) == 1
	assert works[0]['provenance']['program_id'] == '15001819'


def test_a_title_that_is_only_whitespace_is_dropped():
	assert watchlist_line(' \n\t ', 'vigloo', '1', 'en') is None
	assert watchlist_line(None, 'vigloo', '1', 'en') is None
	assert watchlist_line('x', 'vigloo', '1', 'en') is None, 'one character is not a title'


# --- cycle run records: a second run the same day must not erase the first ---


def record(path: Path, started_at: str, steps: int = 3) -> None:
	path.write_text(json.dumps({'started_at': started_at, 'steps': [{}] * steps}), encoding='utf-8')


def test_nothing_to_supersede_on_the_first_run(tmp_path: Path):
	assert supersede_existing_record(tmp_path / '2026-08-19-daily.json') is None


def test_the_earlier_record_is_moved_aside_not_erased(tmp_path: Path):
	path = tmp_path / '2026-08-19-daily.json'
	record(path, '2026-08-18T20:00:03.136661+00:00', steps=17)

	moved = supersede_existing_record(path)

	assert moved is not None and moved.exists()
	assert not path.exists(), 'the canonical path is freed for the new run'
	assert json.loads(moved.read_text(encoding='utf-8'))['steps'] == [{}] * 17
	assert ':' not in moved.name, 'Windows rejects a colon in a filename'


def test_two_supersedes_do_not_collide(tmp_path: Path):
	path = tmp_path / '2026-08-19-daily.json'
	record(path, '2026-08-18T20:00:03.136661+00:00')
	first = supersede_existing_record(path)
	record(path, '2026-08-19T05:55:37.587678+00:00')
	second = supersede_existing_record(path)

	assert first is not None and second is not None
	assert first != second
	assert first.exists() and second.exists()


def test_an_unreadable_record_is_still_kept(tmp_path: Path):
	path = tmp_path / '2026-08-19-daily.json'
	path.write_text('{ truncated', encoding='utf-8')

	moved = supersede_existing_record(path)

	assert moved is not None and moved.exists()
	assert moved.read_text(encoding='utf-8') == '{ truncated'
