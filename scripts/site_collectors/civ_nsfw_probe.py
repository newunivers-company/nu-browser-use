"""Inspect the exact nsfw / nsfwLevel field types for models and images."""

from __future__ import annotations

import asyncio

import aiohttp

BASE = 'https://civitai.com/api/v1'
HEADERS = {'User-Agent': 'Mozilla/5.0 reference-collector', 'Accept': 'application/json'}


async def main() -> None:
	async with aiohttp.ClientSession(headers=HEADERS) as s:
		async with s.get(f'{BASE}/models', params={'limit': '6', 'nsfw': 'false', 'sort': 'Most Downloaded'}) as r:
			models = await r.json()
		print('=== models (nsfw=false) ===')
		for m in models['items']:
			print(f"  {m['name'][:30]:30} nsfw={m.get('nsfw')!r} nsfwLevel={m.get('nsfwLevel')!r} ({type(m.get('nsfwLevel')).__name__}) sfwOnly={m.get('sfwOnly')!r}")

		async with s.get(f'{BASE}/images', params={'limit': '6', 'nsfw': 'None', 'sort': 'Most Reactions'}) as r:
			images = await r.json()
		print('\n=== images (nsfw=None) ===')
		for im in images['items']:
			print(f"  id={im['id']} nsfw={im.get('nsfw')!r} nsfwLevel={im.get('nsfwLevel')!r} ({type(im.get('nsfwLevel')).__name__}) browsingLevel={im.get('browsingLevel')!r}")


if __name__ == '__main__':
	asyncio.run(main())
