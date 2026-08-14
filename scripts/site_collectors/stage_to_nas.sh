#!/usr/bin/env bash
# Stage collector outputs from WSL home to the NAS deploy root (stage 2 of the
# 3-stage deploy in docs_collection-policy.md):
#   local ~/<name>_export  ->  /mnt/newunivers-sdb/nu-browser-use/<name>_export
# Stage 3 (NAS -> Google Drive) is done from Windows via sync_all.ps1, which
# stages through X:\nu-browser-use (the same NAS share mapped as a drive letter).
#
# Usage: stage_to_nas.sh            (stage everything present)
#        stage_to_nas.sh fal comfy  (stage only named exports)

set -u
NAS_ROOT="/mnt/newunivers-sdb/nu-browser-use"
HOME_ROOT="$HOME"

# local dir name -> NAS dir name (identical unless listed here)
declare -A TARGETS=(
	[fal_export]=fal_export
	[comfy_export]=comfy_export
	[social_trend_export]=social_trend_export
	[loc_export]=loc_export
	[gutenberg_export]=gutenberg_export
	[wikidata_export]=wikidata_export
	[gdelt_export]=gdelt_export
	[vigloo_export]=vigloo_export
	[reelshort_export]=reelshort_export
	[pinterest_export]=pinterest_export
	[shotdeck_export]=shotdeck_export
	[civitai_export]=civitai_export
	[munpia_export]=munpia_export
)

only=("$@")
for local_name in "${!TARGETS[@]}"; do
	if [ ${#only[@]} -gt 0 ]; then
		match=0
		for o in "${only[@]}"; do
			[[ "$local_name" == *"$o"* ]] && match=1
		done
		[ $match -eq 0 ] && continue
	fi
	src="$HOME_ROOT/$local_name"
	dst="$NAS_ROOT/${TARGETS[$local_name]}"
	if [ ! -d "$src" ]; then
		echo "SKIP $local_name (not in $HOME_ROOT)"
		continue
	fi
	mkdir -p "$dst"
	# rsync: incremental, preserve times, delete nothing on the NAS side
	if command -v rsync >/dev/null 2>&1; then
		rsync -a --times "$src/" "$dst/"
	else
		cp -au "$src/." "$dst/"
	fi
	count=$(find "$dst" -type f | wc -l)
	size=$(du -sh "$dst" | cut -f1)
	echo "STAGED $local_name -> $dst ($count files, $size)"
done
