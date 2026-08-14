"""Analyze collected models to calibrate a reliable SFW filter (nsfw flag is unreliable)."""

from __future__ import annotations

import json
import os
from collections import Counter
from pathlib import Path

OUT = Path(os.environ.get('CIVITAI_OUT', r'X:\nu-browser-use\civitai_export'))

models = [json.loads(l) for l in (OUT / 'models' / 'models.jsonl').read_text(encoding='utf-8').splitlines() if l.strip()]
print(f'models collected: {len(models)}')

flag = Counter(m.get('nsfw') for m in models)
print('nsfw flag distribution:', dict(flag))

levels = Counter(m.get('nsfwLevel') for m in models)
print('nsfwLevel distribution:', dict(sorted(levels.items())))

# Models whose name hints NSFW despite the flag.
hint = [m for m in models if any(w in (m.get('name') or '').lower() for w in ('nsfw', 'lust', 'porn', 'sex', 'hentai', 'nude'))]
print(f'\nname-hinted NSFW models: {len(hint)}')
for m in hint[:12]:
	print(f"  nsfw={m.get('nsfw')} level={m.get('nsfwLevel')} name={m.get('name')[:50]}")

# Bit analysis: Civitai NsfwLevel bits — 1 PG, 2 PG13, 4 R, 8 X, 16 XXX, 32 Blocked
print('\n--- bit breakdown of nsfwLevel values present ---')
for lv in sorted(levels):
	if isinstance(lv, int):
		bits = [name for bit, name in [(1,'PG'),(2,'PG13'),(4,'R'),(8,'X'),(16,'XXX'),(32,'Blocked')] if lv & bit]
		print(f'  {lv}: {"+".join(bits)}  (count {levels[lv]})')

# If we excluded XXX bit (16), how many drop?
xxx = [m for m in models if isinstance(m.get('nsfwLevel'), int) and (m['nsfwLevel'] & 16)]
x = [m for m in models if isinstance(m.get('nsfwLevel'), int) and (m['nsfwLevel'] & 8)]
print(f'\nmodels with XXX bit (16): {len(xxx)} | with X bit (8): {len(x)}')
