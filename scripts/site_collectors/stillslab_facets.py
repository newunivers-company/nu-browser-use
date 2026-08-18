"""StillsLab facet-combination frame inventory (metadata-only).

The structured filter endpoints are SSR (verified: Warm vs Cool return
disjoint sets; AND combos narrow). Each query surfaces ~48 frames per page
with no total counter, so this records what the first page exposes — a
curated top slice per combination, not an exhaustive crawl.

Combos are designed from the NU Directing Taxonomy v2 for short-drama
production: dialogue framing, emotional close-ups, reveal silhouettes,
teal-and-orange grading, and so on. Each combo maps to the directing intents
that recur in vertical drama, so the result is a look-up table from
directing intent -> concrete film frames (URLs only, images NOT downloaded
per ToS).

Output (STILLSLAB_OUT, default ~/stillslab_export):
  facet_inventories.json - per combo: query, frame count, frames with gallery attribution
  facet_frames.jsonl     - one row per (combo, frame)
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from pathlib import Path

import aiohttp

OUT_DIR = Path(os.environ.get('STILLSLAB_OUT', str(Path.home() / 'stillslab_export')))
BASE = 'https://stillslab.com'
UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36'
QUERY_DELAY = 2.0

CDN_RE = re.compile(r'cdn\.stillslab\.com/([^\s",\\)]+?\.webp)')

# Directing-intent combos from nu_directing_taxonomy_v2 (see combo design notes)
COMBOS: dict[str, str] = {
	'dialogue_two_person_interior': 'number_of_people=2&interior_exterior=Interior&frame_size=Medium+Close+Up,Close+Up',
	'emotional_closeup_backlit': 'frame_size=Close+Up&lighting=Backlight,Edge+light',
	'reveal_silhouette_night': 'lighting=Silhouette&time_of_day=Night',
	'night_city_exterior': 'time_of_day=Night&interior_exterior=Exterior',
	'romance_warm_sunset': 'color=Warm&time_of_day=Sunset',
	'tension_cool_lowangle': 'color=Cool&shot_type=Low+angle',
	'power_highangle_single': 'shot_type=High+angle&number_of_people=1',
	'unease_dutch_night': 'shot_type=Dutch+angle&time_of_day=Night',
	'loneliness_desat_wide': 'color=Desaturated&number_of_people=1&frame_size=Wide,Extreme+Wide',
	'action_group_exterior': 'number_of_people=3,4,5,6%2B&interior_exterior=Exterior',
	'conspiracy_twoshot_sidelight': 'number_of_people=2&lighting=Side+light',
	'teal_orange_grade': 'color=Warm,Cool&logic=color:and',
	'flashback_bw': 'color=Black+and+White',
	'golden_hour': 'time_of_day=Sunrise,Sunset&color=Warm',
	'insert_shot': 'shot_type=Insert',
}


def frame_source(path: str) -> str:
	"""Gallery attribution from the CDN path: movies/<slug>_frames/... or series/<slug>_frames/..."""
	m = re.match(r'(?:movies|series)/([a-z0-9-]+)_frames', path)
	return m.group(1) if m else '?'


async def main() -> None:
	OUT_DIR.mkdir(parents=True, exist_ok=True)
	inventories: list[dict] = []
	frames_path = OUT_DIR / 'facet_frames.jsonl'

	async with aiohttp.ClientSession(headers={'User-Agent': UA}) as session:
		fh = frames_path.open('w', encoding='utf-8')
		for name, query in COMBOS.items():
			try:
				async with session.get(f'{BASE}/filter?{query}', timeout=aiohttp.ClientTimeout(total=30)) as resp:
					html = await resp.text() if resp.status == 200 else ''
			except Exception:  # noqa: BLE001 - one failed combo is skipped
				html = ''
			frames = sorted({p for p in CDN_RE.findall(html) if '/thumbnails/' not in p})
			from collections import Counter
			sources = Counter(frame_source(p) for p in frames)
			record = {
				'combo': name,
				'query': query,
				'frame_count': len(frames),
				'top_sources': dict(sources.most_common(8)),
			}
			inventories.append(record)
			for path in frames:
				fh.write(json.dumps({'combo': name, 'gallery': frame_source(path), 'path': path}, ensure_ascii=False) + '\n')
			print(f'  {name:32} {len(frames):>3} frames | top: {list(sources)[:4]}')
			await asyncio.sleep(QUERY_DELAY)
		fh.close()

	(OUT_DIR / 'facet_inventories.json').write_text(json.dumps(inventories, ensure_ascii=False, indent=1), encoding='utf-8')
	total = sum(r['frame_count'] for r in inventories)
	print(f'done: {len(inventories)} combos, {total} frame refs -> {OUT_DIR}')


if __name__ == '__main__':
	asyncio.run(main())
