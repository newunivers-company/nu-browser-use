"""Aggregate the Pinterest export: totals, unique pins across keywords, image count."""

from __future__ import annotations

import json
import os
from pathlib import Path

OUT = Path(os.environ.get('PINTEREST_OUT', r'X:\nu-browser-use\pinterest_export'))
KW = OUT / 'keywords'

kw_dirs = [d for d in KW.iterdir() if d.is_dir()]
total_pins = 0
total_images = 0
unique = set()
empty = []
for d in kw_dirs:
	pj = d / 'pins.json'
	if pj.is_file():
		pins = json.loads(pj.read_text(encoding='utf-8'))
		total_pins += len(pins)
		if not pins:
			empty.append(d.name)
		for p in pins:
			unique.add(p['pinid'])
	img_dir = d / 'images'
	if img_dir.is_dir():
		total_images += sum(1 for _ in img_dir.glob('*.jpg'))

print(f'keyword folders: {len(kw_dirs)}')
print(f'total pins (sum): {total_pins}')
print(f'unique pins (across all keywords): {len(unique)}')
print(f'total image files: {total_images}')
print(f'empty keywords: {len(empty)}')
if empty:
	print('  ', empty[:15])
