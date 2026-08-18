"""Cross-layer edges: story genre -> directing intent -> visual reference.

Three layers already collected independently:
  munpia_export/rankings.csv    Korean web-novel genres (story layer)
  stillslab facet combos        directing-intent frame inventories (cinematic layer)
  pinterest keywords            visual-reference keyword collections (reference layer)

Nothing connects them. This builds the mapping tables so a story genre can be
looked down into directing intents, and a directing intent into reference
keyword sets — the first concrete instance of the Social→Story→Directing→
Reference chain the original plan drew.

Edges are curated (not learned): genre->intent pairs reflect how each genre is
actually shot in vertical drama, intent->keywords reuse the taxonomy vocabulary
so any future expansion of the facet combos inherits the links automatically.

Output (STILLSLAB_OUT/derived, default ~/stillslab_export/derived):
  cross_layer_links.json - genre -> {intents[]} -> {pinterest_keywords[]}
"""

from __future__ import annotations

import csv
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from stillslab_facets import COMBOS  # noqa: E402 - combo names are the intent vocabulary

MUNPIA_RANKINGS = Path(os.environ.get('MUNPIA_OUT', str(Path.home() / 'munpia_export'))) / 'rankings.csv'
PINTEREST_KEYWORDS = Path('/mnt/newunivers-sdb/nu-browser-use/pinterest_export/keywords')
OUT = Path(os.environ.get('STILLSLAB_OUT', str(Path.home() / 'stillslab_export'))) / 'derived' / 'cross_layer_links.json'

# genre -> directing intents (from COMBOS vocabulary), reflecting how the genre
# is shot in vertical drama
GENRE_TO_INTENTS: dict[str, list[str]] = {
	'로맨스': ['romance_warm_sunset', 'romance_softlight_two', 'reunion_night_two', 'daylight_romance', 'wedding_soft_day', 'truyst_dusk_exterior', 'emotional_closeup_backlit'],
	'현대판타지': ['power_highangle_single', 'wealth_establishing', 'cold_wealth_interior', 'office_power_twoshot', 'menace_dutch_lowangle'],
	'판타지': ['occult_night_backlit', 'dream_saturated_sunset', 'menace_dutch_lowangle', 'domination_overhead'],
	'무협': ['chase_night_lowangle', 'final_showdown', 'action_group_exterior', 'fleeing_group_night', 'standoff_three'],
	'드라마': ['breakup_desat_two', 'despair_night_lowcontrast', 'family_warm_interior', 'witness_dark_interior', 'aftermath_desat_night'],
	'추리': ['witness_dark_interior', 'secret_deal_dusk', 'claustrophobic_ecu_interior', 'shock_ecu_contrast'],
	'공포·미스테리': ['occult_night_backlit', 'deception_silhouette_day', 'reveal_silhouette_night', 'unease_dutch_night', 'claustrophobic_ecu_interior'],
	'전쟁·밀리터리': ['action_group_exterior', 'aftermath_desat_night', 'fleeing_group_night', 'tension_cool_lowangle'],
	'SF': ['night_city_exterior', 'dream_saturated_sunset', 'isolated_in_crowd', 'occult_night_backlit'],
	'스포츠': ['pressure_hardlight_day', 'staking_group_highangle', 'chase_night_lowangle'],
	'대체역사': ['wealth_establishing', 'nostalgia_sepia', 'flashback_bw', 'family_warm_interior'],
	'게임': ['confusion_dutch_single', 'isolated_in_crowd', 'dream_saturated_sunset'],
	'퓨전': ['menace_dutch_lowangle', 'power_highangle_single', 'final_showdown'],
	'일반소설': ['loneliness_desat_wide', 'nostalgia_sepia', 'insert_shot'],
}

# intent -> pinterest keyword slugs (the reference layer collects keyword dirs)
INTENT_TO_KEYWORDS: dict[str, list[str]] = {
	'emotional_closeup_backlit': ['rembrandt_lighting', 'rim_light_portrait', 'backlit_portrait'],
	'reveal_silhouette_night': ['silhouette_photography', 'low_key_lighting', 'moody_cinematic'],
	'night_city_exterior': ['neon_sign_reflection', 'rainy_night_city', 'cyberpunk_aesthetic'],
	'romance_warm_sunset': ['golden_hour_cinematography', 'blue_hour_photography', 'warm_interior_lighting'],
	'tension_cool_lowangle': ['cold_blue_cinematography', 'low_angle_cinematic'],
	'power_highangle_single': ['high_angle_shot', 'moody_portrait_photography'],
	'loneliness_desat_wide': ['desaturated_film_look', 'wide_establishing_shot', 'negative_space_cinematography'],
	'golden_hour': ['golden_hour_cinematography', 'warm_interior_lighting'],
	'flashback_bw': ['monochrome_cinematography', 'vintage_film_look'],
	'insert_shot': ['extreme_close_up_film', 'shallow_depth_of_field_film'],
	'dawn_reset_single': ['dawn_scene_cinematic', 'moody_portrait_photography'],
	'tears_ecu': ['extreme_close_up_film', 'dramatic_portrait_lighting'],
	'occult_night_backlit': ['low_key_lighting', 'volumetric_light', 'horror_film_still'],
	'nostalgia_sepia': ['vintage_film_look', 'candlelight_scene'],
	'vendetta_hardlight': ['high_contrast_cinematography', 'hard_film_noir', 'dramatic_portrait_lighting'],
	'daylight_romance': ['golden_hour_cinematography', 'pastel_color_film'],
	'cold_wealth_interior': ['cold_blue_cinematography', 'practical_lights_interior'],
	'dream_saturated_sunset': ['pastel_color_film', 'teal_and_orange_grade'],
	'teal_orange_grade': ['teal_and_orange_grade'],
	'despair_night_lowcontrast': ['moody_cinematic', 'low_key_lighting'],
	'shock_ecu_contrast': ['high_contrast_cinematography', 'extreme_close_up_film'],
	'deception_silhouette_day': ['silhouette_photography'],
	'chase_night_lowangle': ['low_angle_cinematic', 'neon_sign_reflection'],
	'domination_overhead': ['overhead_shot_film', 'centered_composition_kubrick'],
	'family_warm_interior': ['warm_interior_lighting', 'practical_lights_interior'],
	'isolated_in_crowd': ['negative_space_cinematography', 'street_photography_cinematic'],
	'office_power_twoshot': ['over_the_shoulder_shot', 'practical_lights_interior'],
	'reunion_night_two': ['neon_lighting_night', 'moody_cinematic'],
	'witness_dark_interior': ['low_key_lighting', 'film_noir', 'neo_noir'],
	'wealth_establishing': ['wide_establishing_shot', 'luxury_penthouse'],
	'breakup_desat_two': ['desaturated_film_look'],
	'aftermath_desat_night': ['desaturated_film_look', 'moody_cinematic'],
	'romance_softlight_two': ['window_light_portrait', 'soft_cinematic'],
	'wedding_soft_day': ['window_light_portrait'],
	'jealousy_triangle': ['side_light_film', 'dramatic_portrait_lighting'],
	'staking_group_highangle': ['high_angle_shot'],
	'fleeing_group_night': ['rainy_night_city', 'neon_lighting_night'],
	'secret_deal_dusk': ['blue_hour_photography'],
	'truyst_dusk_exterior': ['blue_hour_photography', 'golden_hour_cinematography'],
	'final_showdown': ['high_contrast_cinematography', 'hard_film_noir'],
	'confusion_dutch_single': ['dutch_angle_shot', 'moody_portrait_photography'],
	'menace_dutch_lowangle': ['dutch_angle_shot', 'low_angle_cinematic'],
	'claustrophobic_ecu_interior': ['extreme_close_up_film', 'practical_lights_interior'],
	'desperation_ecu': ['extreme_close_up_film'],
	'standoff_three': ['over_the_shoulder_shot'],
	'pressure_hardlight_day': ['high_contrast_cinematography'],
	'conspiracy_twoshot_sidelight': ['side_light_film', 'film_noir'],
	'wealth_cold_penthouse': ['luxury_penthouse', 'cold_blue_cinematography'],
	'desperate_flee_night': ['rainy_night_city'],
	'wealth_arrival': ['luxury_penthouse'],
	'vendetta_femme': ['dramatic_portrait_lighting'],
	'confrontation_2shot': ['over_the_shoulder_shot'],
	'secret_lover_insert': ['extreme_close_up_film'],
	'inheritance_reveal': ['centered_composition_kubrick'],
	'loneliness_desat_wide_2': ['negative_space_cinematography'],
}


def main() -> None:
	# which pinterest keyword dirs actually exist (only link what we have)
	existing_keywords = {p.name for p in PINTEREST_KEYWORDS.iterdir()} if PINTEREST_KEYWORDS.exists() else set()

	# munpia genres present in current rankings
	genres_present = set()
	if MUNPIA_RANKINGS.exists():
		for row in csv.DictReader(open(MUNPIA_RANKINGS, encoding='utf-8')):
			for g in (row.get('genres') or '').split('|'):
				if g.strip():
					genres_present.add(g.strip())

	links = {'created': '2026-08-18', 'genre_to_intents': {}, 'intent_to_keywords': {}}
	unmatched_intents = []
	for genre, intents in GENRE_TO_INTENTS.items():
		valid = [i for i in intents if i in COMBOS]
		dropped = [i for i in intents if i not in COMBOS]
		if dropped:
			unmatched_intents.extend(dropped)
		if valid:
			links['genre_to_intents'][genre] = {'intents': valid, 'in_current_munpia': genre in genres_present}

	for intent in COMBOS:
		kws = INTENT_TO_KEYWORDS.get(intent, [])
		have = [k for k in kws if k in existing_keywords]
		links['intent_to_keywords'][intent] = {'keywords': have, 'keywords_defined_but_absent': len(kws) - len(have)}

	OUT.parent.mkdir(parents=True, exist_ok=True)
	OUT.write_text(json.dumps(links, ensure_ascii=False, indent=1), encoding='utf-8')

	covered_genres = sum(1 for v in links['genre_to_intents'].values() if v['in_current_munpia'])
	covered_intents = sum(1 for i, v in links['intent_to_keywords'].items() if v['keywords'])
	print(f'genres linked: {len(links["genre_to_intents"])} ({covered_genres} in current munpia data)')
	print(f'intents with pinterest refs: {covered_intents}/{len(COMBOS)}')
	print(f'keyword slugs on disk matched: {sum(len(v["keywords"]) for v in links["intent_to_keywords"].values())} refs')
	if unmatched_intents:
		print('intent names defined but not in COMBOS (cleaned):', sorted(set(unmatched_intents))[:6])
	print(f'-> {OUT}')


if __name__ == '__main__':
	main()
