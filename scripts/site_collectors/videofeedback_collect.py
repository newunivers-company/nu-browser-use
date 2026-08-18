"""VideoFeedback corpus collector (Apache-2.0).

Downloads the TIGER-Lab/VideoFeedback corpus — 37.6k AI-generated mp4 clips
with 5-dimension human quality annotations (video-text correspondence,
fidelity, aesthetic, motion smoothness, dynamics) — from HuggingFace.

Two repos:
  TIGER-Lab/VideoFeedback          - annotations (parquet, ~49MB)
  hexuan21/VideoFeedback-videos-mp4 - the mp4 clips (8.81GB, 37,664 files)

Both are ungated and anonymously downloadable (verified 2026-08-14).

Output (VIDEOFEEDBACK_OUT):
  annotations/   - annotated + real parquet subsets
  videos/        - mp4 tree (<batch>/<id>.mp4)
  manifest.json  - per-file status map (rerun-safe)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path

import aiohttp

OUT_DIR = Path(os.environ.get('VIDEOFEEDBACK_OUT', str(Path.home() / 'videofeedback_export')))
HF = 'https://huggingface.co'
ANNOTATION_REPO = 'datasets/TIGER-Lab/VideoFeedback'
VIDEO_REPO = 'datasets/hexuan21/VideoFeedback-videos-mp4'
ANNOTATION_FILES = [
	'annotated/train-00000-of-00001.parquet',
	'annotated/test-00000-of-00001.parquet',
	'real/train-00000-of-00001.parquet',
]
CONCURRENCY = 12


async def list_repo_files(session: aiohttp.ClientSession, repo: str) -> list[str]:
	"""List every file in an HF dataset repo via the public API."""
	url = f'{HF}/api/{repo}'
	async with session.get(url, timeout=aiohttp.ClientTimeout(total=60)) as response:
		data = await response.json()
	return [entry['rfilename'] for entry in data.get('siblings', [])]


async def download(session: aiohttp.ClientSession, repo: str, remote: str, dest: Path, semaphore: asyncio.Semaphore) -> str:
	"""Fetch one file from HF resolve -> CDN, skipping files already on disk."""
	if dest.exists() and dest.stat().st_size > 0:
		return 'cached'
	async with semaphore:
		url = f'{HF}/{repo}/resolve/main/{remote}'
		try:
			async with session.get(url, timeout=aiohttp.ClientTimeout(total=600)) as response:
				if response.status != 200:
					return f'http {response.status}'
				dest.parent.mkdir(parents=True, exist_ok=True)
				tmp = dest.with_suffix(dest.suffix + '.part')
				with tmp.open('wb') as handle:
					async for chunk in response.content.iter_chunked(1 << 20):
						handle.write(chunk)
				tmp.rename(dest)
				return 'ok'
		except Exception as error:  # noqa: BLE001 - record and continue
			return f'error {error}'


async def main() -> None:
	parser = argparse.ArgumentParser()
	parser.add_argument('--limit', type=int, default=0, help='cap on video files (0 = all)')
	args = parser.parse_args()

	semaphore = asyncio.Semaphore(CONCURRENCY)
	manifest: dict[str, dict[str, str]] = {'annotations': {}, 'videos': {}}

	async with aiohttp.ClientSession(headers={'User-Agent': 'nu-collector/1.0'}) as session:
		print('[1/3] annotations')
		for remote in ANNOTATION_FILES:
			dest = OUT_DIR / 'annotations' / remote
			status = await download(session, ANNOTATION_REPO, remote, dest, semaphore)
			manifest['annotations'][remote] = status
			print(f'  {remote}: {status}')

		print('[2/3] video list')
		files = [f for f in await list_repo_files(session, VIDEO_REPO) if f.endswith('.mp4')]
		if args.limit > 0:
			files = files[: args.limit]
		print(f'  {len(files)} mp4 files')

		print('[3/3] videos')
		results = await asyncio.gather(*(download(session, VIDEO_REPO, f, OUT_DIR / 'videos' / f, semaphore) for f in files))
		for remote, status in zip(files, results):
			manifest['videos'][remote] = status

	OUT_DIR.mkdir(parents=True, exist_ok=True)
	(OUT_DIR / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
	ok = sum(1 for s in manifest['videos'].values() if s == 'ok')
	cached = sum(1 for s in manifest['videos'].values() if s == 'cached')
	failed = sum(1 for s in manifest['videos'].values() if s not in ('ok', 'cached'))
	print(f'DONE -> {OUT_DIR}  videos: ok={ok} cached={cached} failed={failed}')


if __name__ == '__main__':
	asyncio.run(main())
