"""Inspect generation meta coverage/structure for SFW images from the Civitai API."""

from __future__ import annotations

import asyncio
import json
from collections import Counter

import aiohttp

BASE = 'https://civitai.com/api/v1'
HEADERS = {'User-Agent': 'Mozilla/5.0 reference-collector', 'Accept': 'application/json'}


async def main() -> None:
	async with aiohttp.ClientSession(headers=HEADERS) as s:
		async with s.get(f'{BASE}/images', params={'limit': '100', 'nsfw': 'None', 'sort': 'Most Reactions'}) as r:
			data = await r.json()
	items = data['items']
	has_meta = sum(1 for im in items if im.get('meta'))
	print(f'images: {len(items)} | with non-empty meta: {has_meta}')

	# Field frequency across metas.
	keys = Counter()
	for im in items:
		meta = im.get('meta') or {}
		for k in meta:
			keys[k] += 1
	print('\ntop meta fields:')
	for k, c in keys.most_common(25):
		print(f'  {k}: {c}')

	# Show one rich example.
	rich = max(items, key=lambda im: len(im.get('meta') or {}))
	meta = rich.get('meta') or {}
	print(f'\n=== richest meta (image {rich["id"]}, {len(meta)} fields) ===')
	for k, v in list(meta.items())[:30]:
		sv = json.dumps(v, ensure_ascii=False)
		print(f'  {k}: {sv[:150]}')


if __name__ == '__main__':
	asyncio.run(main())
