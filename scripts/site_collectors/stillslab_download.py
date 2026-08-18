"""StillsLab frame image downloader — runs ONLY with recorded permission.

Prerequisites (both must exist, or this refuses to start):
  stillslab_export/PERMISSION-RECORD.md   the written-permission record
  stillslab_export/frames.jsonl           the frame inventory (from stillslab_collect)

Downloads the full-frame CDN variants (skipping /thumbnails/ duplicates) into
stillslab_export/images/, politely: 2 concurrent, 0.8s pause between requests,
resumable (existing files are skipped), and every failure is counted and
reported rather than retried in a loop.

Terms honored from the permission record: internal research/reference use,
no redistribution (files stay in this export), attribution retained via the
records file.

Usage:
  python stillslab_download.py            (all frames)
  python stillslab_download.py --limit 50 (smoke test)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path

import aiohttp

OUT_DIR = Path(os.environ.get('STILLSLAB_OUT', str(Path.home() / 'stillslab_export')))
PERMISSION_FILE = OUT_DIR / 'PERMISSION-RECORD.md'
FRAMES_FILE = OUT_DIR / 'frames.jsonl'
IMAGES_DIR = OUT_DIR / 'images'
CDN_BASE = 'https://cdn.stillslab.com/'
UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36'
CONCURRENCY = 2
REQUEST_DELAY = 0.8


def filename_for(path: str) -> str:
	"""Flat filename from the CDN path: movies/<slug>_frames/<f>.webp -> <slug>__<f>."""
	parts = path.strip('/').split('/')
	return '__'.join(parts[1:])


async def main() -> None:
	parser = argparse.ArgumentParser()
	parser.add_argument('--limit', type=int, help='max downloads (smoke tests)')
	parser.add_argument('--yes', action='store_true', help='skip the confirmation prompt')
	args = parser.parse_args()

	if not PERMISSION_FILE.exists():
		raise SystemExit('refusing: no PERMISSION-RECORD.md — written permission is required before any download')
	if not FRAMES_FILE.exists():
		raise SystemExit('refusing: no frames.jsonl — run stillslab_collect.py first')

	targets: list[str] = []
	for line in open(FRAMES_FILE, encoding='utf-8'):
		path = json.loads(line)['path']
		if '/thumbnails/' in path:
			continue
		targets.append(path)
	targets = sorted(set(targets))
	if args.limit:
		targets = targets[: args.limit]

	IMAGES_DIR.mkdir(parents=True, exist_ok=True)
	existing = {f.name for f in IMAGES_DIR.iterdir() if f.is_file()}
	todo = [p for p in targets if filename_for(p) not in existing]
	print(f'targets: {len(targets)} | already on disk: {len(targets) - len(todo)} | to download: {len(todo)}')
	if not todo:
		return

	sem = asyncio.Semaphore(CONCURRENCY)
	done = failed = 0

	async with aiohttp.ClientSession(headers={'User-Agent': UA}) as session:

		async def fetch(path: str) -> None:
			nonlocal done, failed
			async with sem:
				url = CDN_BASE + path
				try:
					async with session.get(url, timeout=aiohttp.ClientTimeout(total=60)) as resp:
						if resp.status == 200 and resp.content_type.startswith('image/'):
							(IMAGES_DIR / filename_for(path)).write_bytes(await resp.read())
							done += 1
						else:
							failed += 1
				except Exception:  # noqa: BLE001 - count and move on
					failed += 1
				await asyncio.sleep(REQUEST_DELAY)

		await asyncio.gather(*(fetch(p) for p in todo))
		print(f'downloaded: {done} | failed: {failed} | total on disk: {len(list(IMAGES_DIR.iterdir()))}')


if __name__ == '__main__':
	asyncio.run(main())
