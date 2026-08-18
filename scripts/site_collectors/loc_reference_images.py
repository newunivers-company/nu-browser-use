"""LoC public-domain cinematic reference images — actually downloadable.

The clean-source alternative to StillsLab frames: LoC photography is US
Government work or no-known-restrictions, so unlike film stills these images
can be stored, used in production reference boards, and adapted freely.

Queries mirror the directing-intent vocabulary from nu_directing_taxonomy_v2:
night city, silhouette, backlight, low angle, crowds, interiors, etc. Each
result keeps its LoC record URL and rights statement alongside the file.

Sizes: LoC exposes a thumbnail (`_150px`) and larger derivatives (`_v` and
others) on tile.loc.gov; this fetches the `_v` variant where it exists and
falls back to the thumbnail.

Output (LOC_REF_OUT, default ~/loc_reference_export):
  records.json / .csv - per image: id, title, date, rights, url, file
  images/<id>.jpg    - the actual image files
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import os
from pathlib import Path

import aiohttp

OUT_DIR = Path(os.environ.get('LOC_REF_OUT', str(Path.home() / 'loc_reference_export')))
SEARCH_URL = 'https://www.loc.gov/search/'
UA = 'nu-browser-use collector/1.0 (public-domain reference images; contact: dev)'
PAGE_SIZE = 25
QUERY_DELAY = 2.0
IMG_CONCURRENCY = 3

# Directing-intent queries mirroring the StillsLab combos (taxonomy v2)
QUERIES: dict[str, str] = {
	'night_city': 'night city street',
	'silhouette': 'silhouette photograph',
	'backlight': 'backlight photograph',
	'crowd': 'crowd street photograph',
	'interior_lowkey': 'interior dim light photograph',
	'neon': 'neon sign night',
	'portrait_dramatic': 'portrait dramatic light',
	'desert_wide': 'desert landscape wide',
	'ocean': 'ocean waves photograph',
	'rain': 'rain street photograph',
	'fog': 'fog atmosphere photograph',
	'windows': 'window light interior',
	'urban_night': 'city skyline night',
	'staircase': 'staircase photograph architecture',
	'diner': 'diner interior photograph',
}


async def search_query(session: aiohttp.ClientSession, name: str, query: str) -> list[dict]:
	"""One rights-filtered search page."""
	params = {
		'fo': 'json',
		'c': PAGE_SIZE,
		'q': query,
		'fa': 'rights:no known restrictions|format:photos, prints and drawings',
		'at': 'results',
	}
	try:
		async with session.get(SEARCH_URL, params=params, timeout=aiohttp.ClientTimeout(total=45)) as resp:
			if resp.status != 200:
				return []
			doc = await resp.json()
	except Exception:  # noqa: BLE001
		return []
	rows = []
	for r in doc.get('results') or []:
		urls = r.get('image_url') or []
		if not urls:
			continue
		thumb = urls[0].split('#')[0]
		rows.append(
			{
				'query': name,
				'id': (r.get('id') or '').rstrip('/').rsplit('/', 1)[-1],
				'title': (str(r.get('title')) if r.get('title') else '')[:160],
				'date': r.get('date'),
				'rights': (str(r.get('rights')) if r.get('rights') else 'no known restrictions (facet)')[:200],
				'thumb': thumb,
				'record_url': r.get('id'),
			}
		)
	return rows


def larger_variant(thumb_url: str) -> list[str]:
	"""Candidate larger-image URLs for a thumbnail, best first.

	Two LoC shapes exist: /service/.../<id>_150px.jpg (has _v siblings) and
	IIIF /image-services/iiif/service:... (request a full-size region).
	"""
	if '/iiif/' in thumb_url:
		base = thumb_url.split('/full/')[0] if '/full/' in thumb_url else thumb_url
		return [f'{base}/full/pct:50/0/default.jpg', f'{base}/full/full/0/default.jpg', thumb_url]
	return [thumb_url.replace('_150px', '_v'), thumb_url]


async def main() -> None:
	parser = argparse.ArgumentParser()
	parser.add_argument('--no-images', action='store_true', help='records only')
	args = parser.parse_args()
	img_dir = OUT_DIR / 'images'
	img_dir.mkdir(parents=True, exist_ok=True)

	all_rows: list[dict] = []
	async with aiohttp.ClientSession(headers={'User-Agent': UA}) as session:
		for name, query in QUERIES.items():
			rows = await search_query(session, name, query)
			all_rows.extend(rows)
			print(f'  {name:20} {len(rows)} records')
			await asyncio.sleep(QUERY_DELAY)

		# dedupe by id across queries
		by_id: dict[str, dict] = {}
		for row in all_rows:
			by_id.setdefault(row['id'], row)
		rows = list(by_id.values())
		print(f'  unique: {len(rows)}')

		if not args.no_images:
			sem = asyncio.Semaphore(IMG_CONCURRENCY)

			async def download(row: dict) -> None:
				if not row['id']:
					return
				async with sem:
					for url in larger_variant(row['thumb']):
						try:
							async with session.get(url, timeout=aiohttp.ClientTimeout(total=60)) as resp:
								if resp.status == 200 and resp.content_type.startswith('image/'):
									(img_dir / f"{row['id']}.jpg").write_bytes(await resp.read())
									row['file'] = str(img_dir / f"{row['id']}.jpg")
									return
						except Exception:  # noqa: BLE001
							continue

			await asyncio.gather(*(download(r) for r in rows))

	(OUT_DIR / 'records.json').write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding='utf-8')
	with (OUT_DIR / 'records.csv').open('w', newline='', encoding='utf-8') as fh:
		writer = csv.DictWriter(fh, fieldnames=['query', 'id', 'title', 'date', 'rights', 'record_url', 'file'], extrasaction='ignore')
		writer.writeheader()
		writer.writerows(rows)
	files = sum(1 for r in rows if r.get('file'))
	print(f'done: {len(rows)} records, {files} images -> {OUT_DIR}')


if __name__ == '__main__':
	asyncio.run(main())
