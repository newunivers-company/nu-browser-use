"""Vigloo promotional asset downloader.

Complements vigloo_collect.py: pulls the public marketing assets each program
already declares in its catalog record — poster (titleImage), expanded
thumbnail, and the default thumbnails list — from asset.vigloo.com, which
serves them unauthenticated.

These are promotional materials, not episode video; original-video rights are
respected (원문 비보관 원칙).

Output (VIGLOO_ASSETS_OUT, default <VIGLOO_OUT>/assets):
  assets/manifest.json      - program -> downloaded files map
  assets/<programCode>/...  - title/en.png, expanded/en.png, thumb_default/en.png
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
OUT_DIR = Path(os.environ.get('VIGLOO_ASSETS_OUT', str(DEFAULT_PROGRAMS.parent / 'assets')))


def asset_plan(program: dict) -> list[tuple[str, str]]:
	"""Map a program record to (relative_path, url) pairs for its public assets."""
	code = program.get('programCode') or str(program.get('id'))
	plan: list[tuple[str, str]] = []
	if program.get('titleImage'):
		plan.append((f'{code}/title_en.png', program['titleImage']))
	if program.get('thumbnailExpanded'):
		plan.append((f'{code}/expanded_en.png', program['thumbnailExpanded']))
	for thumb in program.get('thumbnails') or []:
		url = thumb.get('url')
		if url:
			kind = {'0': 'thumb_default', '1': 'thumb_motion'}.get(str(thumb.get('type')), f"thumb_{thumb.get('type')}")
			plan.append((f'{code}/{kind}_en.png', url))
	return plan


async def main() -> None:
	parser = argparse.ArgumentParser()
	parser.add_argument('--programs', type=Path, default=DEFAULT_PROGRAMS, help='programs.json from vigloo_collect.py')
	parser.add_argument('--limit', type=int, default=0, help='cap on programs (0 = all)')
	args = parser.parse_args()

	programs = json.loads(args.programs.read_text(encoding='utf-8'))
	if args.limit > 0:
		programs = programs[: args.limit]
	plans = [(p, asset_plan(p)) for p in programs]
	total = sum(len(urls) for _, urls in plans)
	print(f'{len(programs)} programs -> {total} assets -> {OUT_DIR}')

	sem = asyncio.Semaphore(8)
	manifest: list[dict] = []
	done = 0

	async with aiohttp.ClientSession(headers={'User-Agent': UA}) as session:

		async def one(program: dict, rel: str, url: str) -> tuple[str, str | None]:
			nonlocal done
			path = OUT_DIR / rel
			if path.exists() and path.stat().st_size > 0:
				done += 1
				return rel, 'cached'
			async with sem:
				try:
					async with session.get(url, timeout=aiohttp.ClientTimeout(total=30)) as response:
						if response.status != 200:
							done += 1
							return rel, f'http {response.status}'
						path.parent.mkdir(parents=True, exist_ok=True)
						path.write_bytes(await response.read())
						done += 1
						return rel, 'ok'
				except Exception as error:  # noqa: BLE001 - record and continue
					done += 1
					return rel, f'error {error}'

		jobs = [one(p, rel, url) for p, urls in plans for rel, url in urls]
		results = await asyncio.gather(*jobs)

	# Rebuild manifest keyed by program using job order (jobs were emitted program-major).
	index = 0
	for program, urls in plans:
		files = {}
		for rel, url in urls:
			rel_result, status = results[index]
			files[rel_result] = status
			index += 1
		manifest.append({'id': program.get('id'), 'programCode': program.get('programCode'), 'files': files})

	OUT_DIR.mkdir(parents=True, exist_ok=True)
	(OUT_DIR / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
	ok = sum(1 for r in results if r[1] == 'ok')
	cached = sum(1 for r in results if r[1] == 'cached')
	failed = [r for r in results if r[1] not in ('ok', 'cached')]
	print(f'DONE -> {OUT_DIR}  ok={ok} cached={cached} failed={len(failed)}')
	for rel, status in failed[:10]:
		print(f'  FAIL {rel}: {status}')


if __name__ == '__main__':
	asyncio.run(main())
