"""Build a broad Pinterest keyword list: ShotDeck menu vocabulary (turned into visual search
terms) mixed with thematic cinematic expansions. Writes keywords.txt (one per line)."""

from __future__ import annotations

import json
import os
from pathlib import Path

MENU_JSON = Path(os.environ.get('SHOTDECK_OUT', r'X:\nu-browser-use\shotdeck_export')) / 'menu' / 'menu.json'
OUT = Path(os.environ.get('PINTEREST_OUT', r'X:\nu-browser-use\pinterest_export')) / 'keywords.txt'

# Curated thematic seeds (option-3 style broad expansion) for a cinematography reference library.
THEMATIC = [
	'cinematic lighting', 'cinematic photography', 'film still', 'movie still', 'cinematography',
	'moody cinematic', 'cinematic portrait', 'film noir', 'neo noir', 'chiaroscuro lighting',
	'rembrandt lighting', 'low key lighting', 'high key lighting', 'golden hour cinematography',
	'blue hour photography', 'neon lighting night', 'teal and orange grade', 'desaturated film look',
	'monochrome cinematography', 'silhouette photography', 'backlit portrait', 'rim light portrait',
	'practical lights interior', 'volumetric light', 'god rays cinematic', 'foggy atmosphere film',
	'rainy night city', 'cyberpunk aesthetic', 'sci fi cinematography', 'dystopian film',
	'horror film still', 'western film cinematography', 'period drama cinematography', 'war film still',
	'romance film cinematography', 'thriller film still', 'fantasy film cinematography',
	'symmetrical composition film', 'centered composition kubrick', 'negative space cinematography',
	'wide establishing shot', 'extreme close up film', 'over the shoulder shot', 'dutch angle shot',
	'aerial cinematography', 'low angle cinematic', 'overhead shot film', 'anamorphic lens flare',
	'shallow depth of field film', 'bokeh night cinematic', 'street photography cinematic',
	'desert cinematography', 'ocean cinematic', 'forest atmospheric', 'urban night photography',
	'car interior cinematic', 'diner scene film', 'hotel room cinematic', 'neon sign reflection',
	'warm interior lighting', 'cold blue cinematography', 'red lighting scene', 'green lighting film',
	'pastel color film', 'vintage film look', '35mm film photography', 'kodak portra portrait',
	'moody portrait photography', 'dramatic portrait lighting', 'window light portrait',
	'candlelight scene', 'firelight cinematic', 'moonlight scene film',
]

# ShotDeck categories whose human labels make good visual search terms, with a template suffix.
CATEGORY_TEMPLATES = {
	'lighting': '{label} cinematography',
	'lighting_type': '{label} film lighting',
	'shot_type': '{label} shot cinematography',
	'composition': '{label} composition film',
	'frame_size': '{label} shot film',
	'color': '{label} color cinematography',
	'time_of_day': '{label} scene cinematic',
	'time_period': '{label} period film',
	'genre': '{label} film still',
}


def main() -> None:
	keywords: list[str] = []
	seen: set[str] = set()

	def add(kw: str) -> None:
		kw = ' '.join(kw.split()).strip()
		key = kw.lower()
		if kw and key not in seen:
			seen.add(key)
			keywords.append(kw)

	for kw in THEMATIC:
		add(kw)

	if MENU_JSON.is_file():
		menu = json.loads(MENU_JSON.read_text(encoding='utf-8'))
		for cat in menu['categories']:
			template = CATEGORY_TEMPLATES.get(cat['metatype'])
			if not template:
				continue
			for opt in cat['options']:
				label = opt['label'].strip()
				# Skip numeric/format-ish labels that search poorly.
				if not label or any(ch.isdigit() for ch in label) and len(label) <= 6:
					continue
				if cat['metatype'] == 'genre':
					label = label  # e.g. "Drama", "Sci-Fi"
				add(template.format(label=label))

	OUT.parent.mkdir(parents=True, exist_ok=True)
	OUT.write_text('\n'.join(keywords) + '\n', encoding='utf-8')
	print(f'wrote {len(keywords)} keywords -> {OUT}')
	print('sample:', keywords[:8])
	print('...', keywords[-8:])


if __name__ == '__main__':
	main()
