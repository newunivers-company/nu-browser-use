"""Probe the Civitai public API: fields for models and images, and pagination cursors."""

from __future__ import annotations

import asyncio
import json

import aiohttp

BASE = 'https://civitai.com/api/v1'
HEADERS = {'User-Agent': 'Mozilla/5.0 reference-collector', 'Accept': 'application/json'}


async def main() -> None:
	async with aiohttp.ClientSession(headers=HEADERS) as s:
		# Models
		async with s.get(f'{BASE}/models', params={'limit': '3', 'nsfw': 'true', 'sort': 'Most Downloaded'}) as r:
			models = await r.json()
		m = models['items'][0]
		print('=== MODEL top-level keys ===')
		print(list(m.keys()))
		print('  name:', m.get('name'), '| type:', m.get('type'), '| nsfw:', m.get('nsfw'))
		print('  stats:', m.get('stats'))
		print('  tags:', m.get('tags'))
		mv = (m.get('modelVersions') or [{}])[0]
		print('  version keys:', list(mv.keys()))
		imgs = mv.get('images') or []
		print('  version image[0] keys:', list(imgs[0].keys()) if imgs else 'none')
		if imgs:
			print('  version image[0] url:', imgs[0].get('url'))
		print('  models metadata:', models['metadata'])

		# Images
		async with s.get(f'{BASE}/images', params={'limit': '3', 'nsfw': 'X', 'sort': 'Most Reactions'}) as r:
			images = await r.json()
		print('\n=== IMAGE top-level keys ===')
		if images.get('items'):
			im = images['items'][0]
			print(list(im.keys()))
			print('  url:', im.get('url'))
			print('  nsfwLevel:', im.get('nsfwLevel'), '| stats:', im.get('stats'))
			meta = im.get('meta') or {}
			print('  meta keys:', list(meta.keys())[:15])
			print('  prompt sample:', str(meta.get('prompt'))[:120])
		print('  images metadata:', images['metadata'])


if __name__ == '__main__':
	asyncio.run(main())
