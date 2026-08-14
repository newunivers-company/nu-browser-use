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

NAS_CANDIDATES=(
	"${NAS_ROOT:-}"
	/mnt/newunivers-sdb/nu-browser-use
	/x/nu-browser-use
	/mnt/x/nu-browser-use
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

# Every *_export, plus collector output dirs that do not carry the suffix.
mapfile -t candidates < <(
	find "$HOME_ROOT" -maxdepth 1 -mindepth 1 -type d \
		\( -name '*_export' -o -name 'source_loop' -o -name 'source_harvest' -o -name 'newtoki_watch' \) \
		-printf '%f\n' 2>/dev/null | sort
)

if [ ${#candidates[@]} -eq 0 ]; then
	echo "nothing to stage in $HOME_ROOT"
	exit 0
fi

echo "NAS root: $NAS_ROOT"
[ "$DRY_RUN" = "1" ] && echo "(dry run)"

staged=0
skipped=0
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
	if [ "$DRY_RUN" = "1" ]; then
		printf 'WOULD STAGE %-22s %s files -> %s\n' "$name" "$src_files" "$dst"
		staged=$((staged + 1))
		continue
	fi

	mkdir -p "$dst"
	# Incremental, and nothing on the NAS side is ever deleted.
	if command -v rsync >/dev/null 2>&1; then
		rsync -a --times "$src/" "$dst/"
	else
		cp -ru "$src/." "$dst/"
	fi
	dst_files=$(find "$dst" -type f 2>/dev/null | wc -l)
	size=$(du -sh "$dst" 2>/dev/null | cut -f1)
	printf 'STAGED %-22s %s -> %s files, %s\n' "$name" "$src_files" "$dst_files" "$size"
	staged=$((staged + 1))
done

echo "done: $staged staged, $skipped filtered out"
