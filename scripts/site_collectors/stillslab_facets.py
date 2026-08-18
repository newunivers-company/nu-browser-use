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
	# --- expansion: the rest of the short-drama directing vocabulary --------
	# 대결/갈등 3인
	'standoff_three': 'number_of_people=3&frame_size=Medium+Close+Up,Medium+Wide',
	# 후회/눈물 클로즈업 (숏드 눈물연출)
	'tears_ecu': 'frame_size=Extreme+Close+Up&number_of_people=1',
	# 재회 2인 야간
	'reunion_night_two': 'number_of_people=2&time_of_day=Night',
	# 비밀 목격: 1인 + 어두운 실내
	'witness_dark_interior': 'number_of_people=1&interior_exterior=Interior&time_of_day=Night',
	# 계급 대비: establishing + exterior
	'wealth_establishing': 'shot_type=Establishing+shot&interior_exterior=Exterior',
	# 폭로/충격: ECU + high contrast
	'shock_ecu_contrast': 'frame_size=Extreme+Close+Up&lighting=High+contrast',
	# 이별: 2인 + desaturated
	'breakup_desat_two': 'number_of_people=2&color=Desaturated',
	# 새벽 재정비: dawn + single
	'dawn_reset_single': 'time_of_day=Dawn&number_of_people=1',
	# 사무실 권력: interior + 2 shot
	'office_power_twoshot': 'shot_type=2+shot&interior_exterior=Interior',
	# 추격 night + low angle
	'chase_night_lowangle': 'shot_type=Low+angle&time_of_day=Night',
	# 우월함: overhead + single
	'domination_overhead': 'shot_type=Overhead&number_of_people=1',
	# 로맨스 대치: 2인 + soft light
	'romance_softlight_two': 'number_of_people=2&lighting=Soft+light',
	# 위장/가면: silhouette + day
	'deception_silhouette_day': 'lighting=Silhouette&time_of_day=Day',
	# 몽환/환상: saturated + sunset
	'dream_saturated_sunset': 'color=Saturated&time_of_day=Sunset,Sunrise',
	# 복수 결의: 1인 + hard light
	'vendetta_hardlight': 'number_of_people=1&lighting=Hard+light',
	# 숨막히는 밀폐: interior + ECU
	'claustrophobic_ecu_interior': 'frame_size=Extreme+Close+Up&interior_exterior=Interior',
	# 군중 속 고립: crowd + wide
	'isolated_in_crowd': 'number_of_people=6%2B&frame_size=Wide,Extreme+Wide',
	# 청량 낮 로맨스: day + warm + exterior
	'daylight_romance': 'time_of_day=Day&color=Warm&interior_exterior=Exterior',
	# 의식/폭력 후: desat + night
	'aftermath_desat_night': 'color=Desaturated&time_of_day=Night',
	# 냉담한 재벌: cool + interior + single
	'cold_wealth_interior': 'color=Cool&interior_exterior=Interior&number_of_people=1',
	# 신비/오컬트: night + backlight + single
	'occult_night_backlit': 'time_of_day=Night&lighting=Backlight&number_of_people=1',
	# 그리움 과거회상: sepia
	'nostalgia_sepia': 'color=Sepia',
	# 승부처: group + high angle
	'staking_group_highangle': 'number_of_people=3,4,5&shot_type=High+angle',
	# 도주: exterior + group + night
	'fleeing_group_night': 'number_of_people=3,4,5,6%2B&time_of_day=Night&interior_exterior=Exterior',
	# 은밀한 거래: 2인 + dusk
	'secret_deal_dusk': 'number_of_people=2&time_of_day=Dusk',
	# 웨딩/결혼: white? — color 없으므로 2인 + soft + day
	'wedding_soft_day': 'number_of_people=2&lighting=Soft+light&time_of_day=Day',
	# 질투 삼각: 3인 + side light
	'jealousy_triangle': 'number_of_people=3&lighting=Side+light',
	# 절망의 바닥: 1인 + low key 유사 (low contrast + night)
	'despair_night_lowcontrast': 'number_of_people=1&time_of_day=Night&lighting=Low+contrast',
	# 위협적 등장: dutch + low angle
	'menace_dutch_lowangle': 'shot_type=Dutch+angle,Low+angle',
	# 아늑한 가족: 3인 + warm + interior
	'family_warm_interior': 'number_of_people=3,4,5&color=Warm&interior_exterior=Interior',
	# 기억상실 혼란: dutch + single
	'confusion_dutch_single': 'shot_type=Dutch+angle&number_of_people=1',
	# 약속/밀회: 2인 + dusk + exterior
	'truyst_dusk_exterior': 'number_of_people=2&time_of_day=Dusk&interior_exterior=Exterior',
	# 시험/승진 압박: 1인 + interior + day + hard
	'pressure_hardlight_day': 'number_of_people=1&lighting=Hard+light&time_of_day=Day',
	# 최후 대결: 2인 + high contrast + night
	'final_showdown': 'number_of_people=2&lighting=High+contrast&time_of_day=Night',
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
