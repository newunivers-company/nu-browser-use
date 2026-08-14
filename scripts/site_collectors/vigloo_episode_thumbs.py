"""Vigloo episode thumbnail collector.

For every program x season x episode in the collected catalog, fetches the
episode still via the public thumbnail endpoint the web player itself uses:

  https://api.vigloo.com/thumbnail?seasonId=<sid>&episodeNumber=<n>

which 307-redirects to an unauthenticated CDN JPEG (content.vigloo.com).
These are promotional stills shown to anyone browsing the site, not episode
video; original-video rights are respected (원문 비보관 원칙).

Output (VIGLOO_EP_OUT, default <VIGLOO_OUT>/episode_thumbs):
  <programCode>/s<seasonNumber>/e<NNN>.jpg
  manifest.json  - program -> files -> status map (rerun-safe)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path

import aiohttp

UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36'
DEFAULT_PROGRAMS = Path(os.environ.get('VIGLOO_OUT', str(Path.home() / 'vigloo_export'))) / 'programs.json'
OUT_DIR = Path(os.environ.get('VIGLOO_EP_OUT', str(DEFAULT_PROGRAMS.parent / 'episode_thumbs')))
THUMB_API = 'https://api.vigloo.com/thumbnail?seasonId={season_id}&episodeNumber={episode}'


async def fetch_thumb(session: aiohttp.ClientSession, url: str, dest: Path, semaphore: asyncio.Semaphore) -> str:
	"""Download one episode still, skipping files already on disk."""
	if dest.exists() and dest.stat().st_size > 0:
		return 'cached'
	async with semaphore:
		try:
			# No redirect-follow: the 307 target is an open CDN url we keep.
			async with session.get(url, timeout=aiohttp.ClientTimeout(total=30), allow_redirects=True) as response:
				if response.status != 200:
					return f'http {response.status}'
				dest.parent.mkdir(parents=True, exist_ok=True)
				dest.write_bytes(await response.read())
				return 'ok'
		except Exception as error:  # noqa: BLE001 - record and continue
			return f'error {error}'


async def main() -> None:
	parser = argparse.ArgumentParser()
	parser.add_argument('--programs', type=Path, default=DEFAULT_PROGRAMS)
	parser.add_argument('--limit-programs', type=int, default=0, help='cap on programs (0 = all)')
	parser.add_argument('--limit-episodes', type=int, default=0, help='cap on episodes per season (0 = all)')
	args = parser.parse_args()

	programs = json.loads(args.programs.read_text(encoding='utf-8'))
	if args.limit_programs > 0:
		programs = programs[: args.limit_programs]

	jobs: list[tuple[dict, Path, str]] = []
	for program in programs:
		code = program.get('programCode') or str(program.get('id'))
		for season in program.get('seasons') or []:
			season_id = season.get('id')
			count = int(season.get('episodeCount') or 0)
			if not season_id or count <= 0:
				continue
			if args.limit_episodes > 0:
				count = min(count, args.limit_episodes)
			for episode in range(1, count + 1):
				dest = OUT_DIR / code / f"s{season.get('seasonNumber') or 1}" / f'e{episode:03d}.jpg'
				jobs.append((program, dest, THUMB_API.format(season_id=season_id, episode=episode)))

	print(f'{len(programs)} programs -> {len(jobs)} episode thumbs -> {OUT_DIR}')
	semaphore = asyncio.Semaphore(10)

	async with aiohttp.ClientSession(headers={'User-Agent': UA}) as session:
		statuses = await asyncio.gather(*(fetch_thumb(session, url, dest, semaphore) for _, dest, url in jobs))

	manifest = {}
	for (program, dest, _url), status in zip(jobs, statuses):
		code = program.get('programCode') or str(program.get('id'))
		manifest.setdefault(code, {})[str(dest.relative_to(OUT_DIR))] = status
	OUT_DIR.mkdir(parents=True, exist_ok=True)
	(OUT_DIR / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')

	ok = sum(1 for s in statuses if s == 'ok')
	cached = sum(1 for s in statuses if s == 'cached')
	failed = [s for s in statuses if s not in ('ok', 'cached')]
	print(f'DONE -> {OUT_DIR}  ok={ok} cached={cached} failed={len(failed)}')
	from collections import Counter

	for status, n in Counter(failed).most_common(5):
		print(f'  {status}: {n}')


if __name__ == '__main__':
	asyncio.run(main())
