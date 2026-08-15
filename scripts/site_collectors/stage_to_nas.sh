#!/usr/bin/env bash
# Stage collector outputs to the NAS deploy root (stage 2 of the 3-stage deploy
# in docs/collection-policy.md):
#   local ~/<name>_export  ->  <NAS>/<name>_export
# Stage 3 (NAS -> Google Drive) is done from Windows via sync_all.ps1, which
# stages through X:\nu-browser-use (the same NAS share mapped as a drive letter).
#
# DISCOVERY, NOT AN ALLOWLIST
# This used to stage a hardcoded list of export directories. Every collector
# added since was therefore skipped in silence — which is precisely the
# "누락 금지" the policy exists to prevent, and it had already swallowed nine
# exports. Directories are now discovered from $HOME, so a new collector is
# staged the day it first writes.
#
# The NAS is reached at whichever of the known roots exists, because the WSL
# mount and the mapped drive letter are the same share seen from different
# shells; hardcoding the WSL path made this unusable from Git Bash.
#
# Usage: stage_to_nas.sh              (stage every *_export plus known extras)
#        stage_to_nas.sh fal comfy    (stage only matching names)
#        DRY_RUN=1 stage_to_nas.sh    (report what would be staged)

set -u

# X: is a mapped drive (\\192.168.0.136\sdb), and a drive mapping belongs to a
# logon session — a scheduled task that runs without one sees no X: at all. The
# UNC form of the same share needs no mapping, so it goes last as the fallback
# that keeps staging working when the letter is absent. Order is deliberate:
# where the mapping exists, nothing changes.
NAS_CANDIDATES=(
	"${NAS_ROOT:-}"
	/mnt/newunivers-sdb/nu-browser-use
	/x/nu-browser-use
	/mnt/x/nu-browser-use
	//192.168.0.136/sdb/nu-browser-use
)
NAS_ROOT=""
for candidate in "${NAS_CANDIDATES[@]}"; do
	[ -n "$candidate" ] || continue
	parent=$(dirname "$candidate")
	if [ -d "$candidate" ] || [ -d "$parent" ]; then
		NAS_ROOT="$candidate"
		break
	fi
done
if [ -z "$NAS_ROOT" ]; then
	echo "ERROR: no NAS root reachable (tried: ${NAS_CANDIDATES[*]})" >&2
	exit 1
fi

HOME_ROOT="$HOME"
DRY_RUN="${DRY_RUN:-0}"
only=("$@")

# Every *_export, plus the collector output dirs that do not carry the suffix.
# tests/ci/test_staging_coverage.py keeps this in step with what the collectors
# actually declare — the previous hand-maintained version silently dropped
# twelve directories before anyone noticed.
#
# newtoki_market is deliberately absent. It holds a parallel session's
# enumeration of a piracy site's listings, which contradicts the "no listing
# enumeration, no index building" guardrail in newtoki_watch.py. Staging it
# would push that inventory onto shared storage, so it stays local until a
# human settles the conflict.
mapfile -t candidates < <(
	find "$HOME_ROOT" -maxdepth 1 -mindepth 1 -type d \
		\( -name '*_export' \
		   -o -name 'source_loop' -o -name 'source_harvest' -o -name 'source_review' \
		   -o -name 'newtoki_watch' -o -name 'catalog_posters' \
		   -o -name 'trope_rank' -o -name 'collect_cycle' \) \
		-printf '%f\n' 2>/dev/null | sort
)

if [ ${#candidates[@]} -eq 0 ]; then
	echo "nothing to stage in $HOME_ROOT"
	exit 0
fi

echo "NAS root: $NAS_ROOT"
[ "$DRY_RUN" = "1" ] && echo "(dry run)"

# Per-directory stamps live locally: deciding whether to write to the NAS must
# never itself require reading the NAS.
STATE_DIR="${STAGE_STATE_DIR:-$HOME_ROOT/.stage_state}"
FORCE="${FORCE:-0}"
mkdir -p "$STATE_DIR"

staged=0
skipped=0
unchanged=0
failed=0
for name in "${candidates[@]}"; do
	if [ ${#only[@]} -gt 0 ]; then
		match=0
		for pattern in "${only[@]}"; do
			[[ "$name" == *"$pattern"* ]] && match=1
		done
		if [ $match -eq 0 ]; then
			skipped=$((skipped + 1))
			continue
		fi
	fi

	src="$HOME_ROOT/$name"
	dst="$NAS_ROOT/$name"
	src_files=$(find "$src" -type f 2>/dev/null | wc -l)
	stamp="$STATE_DIR/$name.stamp"

	# Skip a directory nothing has written to since it was last staged. The test
	# is local-only, so an unchanged tree costs no network I/O whatsoever — the
	# difference between touching ~270k files over SMB nightly and touching the
	# handful the cadence actually wrote.
	if [ "$FORCE" != "1" ] && [ -f "$stamp" ] && [ -z "$(find "$src" -newer "$stamp" -print -quit 2>/dev/null)" ]; then
		printf 'UNCHANGED %-22s %s files\n' "$name" "$src_files"
		unchanged=$((unchanged + 1))
		continue
	fi

	if [ "$DRY_RUN" = "1" ]; then
		printf 'WOULD STAGE %-22s %s files -> %s\n' "$name" "$src_files" "$dst"
		staged=$((staged + 1))
		continue
	fi

	mkdir -p "$dst"
	# Incremental, and nothing on the NAS side is ever deleted.
	#
	# The destination is deliberately no longer counted or sized. `find "$dst" |
	# wc -l` plus `du -sh "$dst"` walked the entire remote tree once per
	# directory — 208k files for shotdeck alone — purely to print a number. On
	# 2026-08-15 a staging run under Task Scheduler died with 0x8007006B
	# (ERROR_SEM_TIMEOUT) while another session wrote to the same share, and
	# three background runs died the same way. rsync already reports what it
	# moved, and that number costs nothing.
	# rsync is not installed in this Git Bash, so `cp` is the path that actually
	# runs — worth stating, because the rsync branch reads like the default and
	# has never once executed here. `-p` preserves timestamps: without it every
	# copy stamped the destination with the copy time, which is why NAS files
	# kept looking newer than the local originals they came from.
	moved='n/a (cp)'
	rc=1
	for attempt in 1 2; do
		if command -v rsync >/dev/null 2>&1; then
			moved=$(rsync -a --times --out-format='%n' "$src/" "$dst/" 2>/dev/null | wc -l)
			rc=${PIPESTATUS[0]}
		else
			cp -rup "$src/." "$dst/"
			rc=$?
		fi
		[ "$rc" -eq 0 ] && break
		printf 'RETRY %-22s attempt %s failed (rc=%s)\n' "$name" "$attempt" "$rc"
		sleep 5
	done

	if [ "$rc" -ne 0 ]; then
		# A flaky share should cost one directory, not the remainder of the run.
		printf 'FAILED %-22s rc=%s\n' "$name" "$rc"
		failed=$((failed + 1))
		continue
	fi
	touch "$stamp"
	printf 'STAGED %-22s %s local files, %s transferred\n' "$name" "$src_files" "$moved"
	staged=$((staged + 1))
done

echo "done: $staged staged, $unchanged unchanged, $failed failed, $skipped filtered out"
[ "$failed" -gt 0 ] && exit 1
exit 0
