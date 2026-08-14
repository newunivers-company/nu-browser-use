"""Pinterest keyword builder v2 — data-driven instead of template-sprayed.

v1 problem: mechanical "{label} cinematography" templates produced 31 keywords
yielding <10 pins each (DP vocabulary != Pinterest search language).

v2 changes, driven by the actual ShotDeck export + prior collection stats:
  1. PRIOR_YIELD: keywords that already worked (>=15 pins) stay; proven
     low-yield patterns are dropped or rewritten in Pinterest-native phrasing.
  2. COMBINATIONS: the highest-value ShotDeck signal is axis crosses
     (lighting x time-of-day, frame x shot-type); v1 never crossed axes.
  3. Pinterest-native phrasing: "lighting type" values map to how creators
     actually search ("golden hour" not "Daylight scene cinematic").
  4. Ranked output: high-yield proven seeds first, combos next, long tail last.

Reads:  SHOTDECK_OUT/shots.json + menu/menu.json, PINTEREST_OUT/_summary.json
Writes: PINTEREST_OUT/keywords_v2.txt (one per line, deduped, ranked)
"""

from __future__ import annotations

import json
import os
from collections import Counter
from pathlib import Path

SHOTDECK = Path(os.environ.get('SHOTDECK_OUT', r'X:\nu-browser-use\shotdeck_export'))
PINTEREST = Path(os.environ.get('PINTEREST_OUT', r'X:\nu-browser-use\pinterest_export'))
OUT = PINTEREST / 'keywords_v2.txt'

# Rewrites for values whose DP term searches poorly on Pinterest.
PHRASE_MAP = {
	'Daylight': 'natural light photography', 'Sunny': 'hard sunlight photography',
	'Practical light': 'practical lighting film', 'Moonlight': 'moonlight photography',
	'Overcast': 'overcast cinematic', 'Fluorescent': 'fluorescent lighting film',
	'Mixed light': 'mixed lighting photography',
	'Extreme Close Up': 'extreme close up', 'Medium Close Up': 'medium close up portrait',
	'Medium Wide': 'medium wide shot', 'Extreme Wide': 'extreme wide shot',
	'Clean single': 'solo shot cinematic', 'Low angle': 'low angle shot',
	'High angle': 'high angle shot', 'Establishing shot': 'establishing shot',
	'Over the shoulder': 'over the shoulder shot', '2 shot': 'two shot film',
	'Ultra Wide / Fisheye': 'fisheye lens cinematic', 'Long Lens': 'telephoto cinematic',
}

# Axis-cross combos: the ShotDeck insight v1 missed. Each pair is a keyword.
COMBO_AXES = {
	'lighting_type': ['Neon', 'Practical light', 'Moonlight', 'Firelight', 'Candlelight', 'Sodium vapor'],
	'time_of_day': ['Golden hour', 'Blue hour', 'Night', 'Sunset'],
	'subject/context': ['portrait', 'city street', 'interior room', 'car interior', 'forest', 'rooftop'],
}

# Menu-derived seeds: ShotDeck axes whose FULL option lists are strong Pinterest
# queries but that v2 rounds 1-3 barely touched. Phrased per-axis in creator
# language; derived from menu.json option labels (counts stripped).
MENU_AXIS_SEEDS = {
	'lighting': '{label} photography',          # Soft light, Hard light, Silhouette, Backlight, Edge light...
	'shot_type': '{label} shot',                # Aerial, Overhead, Dutch angle, Insert...
	'color': '{label} color palette film',      # Teal/Cyan/Purple/Magenta/Sepia grade-able hues
	'optical_format': '{label} look',           # Anamorphic, Super 35, Open Gate...
	'format': 'shot on {label}',                # shot on 16mm / Super 8 / IMAX / Tape...
	'time_period': '{label} aesthetic',         # 1980s aesthetic, Renaissance aesthetic...
	'subject_age': '{label} portrait film',     # Teenager/Senior portrait character studies
}
# Menu options that read poorly as search terms even rephrased.
MENU_SKIP = {'clear', 'tape', 'digital', 'animation', 'none', 'trans', '3 perf', '2 perf', '3d', 'mixed', 'white'}
# Range-style period labels ("Renaissance: 1400-1700") search poorly; keep the bare era word.
MENU_LABEL_CLEAN = {
	'Renaissance: 1400-1700': 'Renaissance',
	'Medieval: 500-1400': 'Medieval',
	'Ancient: 2000BC-500AD': 'Ancient world',
	'Stone Age: pre-2000BC': 'Prehistoric',
}


def load_yield() -> dict[str, int]:
	"""Keyword -> pins from the prior run's summary, so proven seeds survive."""
	summary = PINTEREST / '_summary.json'
	if not summary.is_file():
		return {}
	try:
		return {row['keyword']: row.get('pins', 0) for row in json.loads(summary.read_text(encoding='utf-8'))}
	except (json.JSONDecodeError, KeyError):
		return {}


def main() -> None:
	shots = json.loads((SHOTDECK / 'shots.json').read_text(encoding='utf-8'))
	menu = json.loads((SHOTDECK / 'menu' / 'menu.json').read_text(encoding='utf-8'))
	prior = load_yield()

	keywords: list[str] = []
	seen: set[str] = set()

	def add(kw: str, bucket: list[str]) -> None:
		kw = ' '.join(kw.split()).strip().lower()
		if len(kw) > 2 and kw not in seen:
			seen.add(kw)
			bucket.append(kw)

	# 1. Proven seeds from v1 (>=15 pins observed) — highest precision tier.
	proven: list[str] = []
	for kw, pins in sorted(prior.items(), key=lambda kv: -kv[1]):
		if pins >= 15:
			add(kw, proven)

	# 2. Axis crosses — the ShotDeck-derived combos v1 never generated.
	combos: list[str] = []
	lighting = COMBO_AXES['lighting_type']
	times = COMBO_AXES['time_of_day']
	contexts = COMBO_AXES['subject/context']
	for light in lighting:
		for time in times:
			add(f'{light} {time.lower()}', combos)
	for light in lighting[:4]:
		for context in contexts:
			add(f'{light} {context}', combos)

	# 2.5. Menu-axis seeds: full option lists of axes the earlier tiers under-used.
	menu_seeds: list[str] = []
	for cat in menu['categories']:
		template = MENU_AXIS_SEEDS.get(cat['metatype'])
		if not template:
			continue
		for opt in cat['options']:
			label = MENU_LABEL_CLEAN.get(opt['label'].strip(), opt['label'].strip())
			key = label.lower()
			if not label or key in MENU_SKIP or any(ch.isdigit() and len(label) <= 7 for ch in label):
				continue
			add(template.format(label=label), menu_seeds)

	# 3. Observed-distribution singles: only the DP values that actually
	#    dominate the ShotDeck sample (top of each axis), Pinterest-phrased.
	tail: list[str] = []
	counts: dict[str, Counter] = {}
	for shot in shots:
		meta = shot.get('metadata') or {}
		for field in ('Shot Type', 'Lighting Type', 'Frame Size', 'Composition', 'Time of Day', 'Lens Size'):
			value = meta.get(field)
			values = value if isinstance(value, list) else [value]
			for v in values:
				if v:
					counts.setdefault(field, Counter())[v] += 1
	for field, counter in counts.items():
		for value, _n in counter.most_common(6):
			phrase = PHRASE_MAP.get(value, value.lower())
			add(f'{phrase} cinematic', tail)

	# 4. v1 thematic seeds that were neither proven nor failed — keep as filler.
	filler: list[str] = []
	v1_file = PINTEREST / 'keywords.txt'
	if v1_file.is_file():
		for line in v1_file.read_text(encoding='utf-8').splitlines():
			pins = prior.get(line.strip(), -1)
			if pins == -1 or pins >= 10:
				add(line, filler)

	keywords = proven + combos + menu_seeds + tail + filler
	OUT.parent.mkdir(parents=True, exist_ok=True)
	OUT.write_text('\n'.join(keywords) + '\n', encoding='utf-8')
	print(f'wrote {len(keywords)} keywords -> {OUT}')
	print(f'  proven: {len(proven)} | combos: {len(combos)} | menu-axis seeds: {len(menu_seeds)} | distribution tail: {len(tail)} | filler: {len(filler)}')
	print('  top proven:', proven[:6])
	print('  menu-axis samples:', menu_seeds[:8])


if __name__ == '__main__':
	main()
