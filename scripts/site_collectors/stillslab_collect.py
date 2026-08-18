"""StillsLab keyless cinematic-reference collector.

stillslab.com is a curated film-stills database for filmmakers — the same layer
as ShotDeck, with friendlier terms: robots.txt allows everything (including
GPTBot) and the site publishes llms.txt / llms-full.txt documenting its filter
syntax FOR machine consumption. Frames carry rich cinematography metadata
(shot_type, frame_size, lighting, color, camera, aspect ratio).

Structure (probed 2026-08-18):
  /movie, /series, /music-video   listing pages, 20 galleries each, no
                                  server-side pagination (?page= is ignored —
                                  deeper pages are client-fetched)
  /gallery/<slug>                 SSR page: title/year, director,
                                  cinematographer, and every frame's CDN path
                                  embedded in the flight payload (Barry Lyndon:
                                  928 frames in one document)
  /filter?<facets>                structured + semantic search, fully crawlable

This collector walks the three listings (deduped), then pulls each gallery's
metadata + frame inventory (URLs only — frame images are NOT downloaded;
metadata-only per policy, same as every other reference source).

Output (STILLSLAB_OUT, default ~/stillslab_export):
  galleries.json / .csv   - per gallery: slug, title, year, director, DP, frame count
  frames.jsonl            - per frame: gallery slug + CDN path (+ index)
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import os
import re
from pathlib import Path

import aiohttp

OUT_DIR = Path(os.environ.get('STILLSLAB_OUT', str(Path.home() / 'stillslab_export')))
BASE = 'https://stillslab.com'
LISTINGS = ('movie', 'series', 'music-video')
UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36'
GALLERY_DELAY = 1.2

GALLERY_RE = re.compile(r'gallery/([a-z0-9-]+)')
SERIES_RE = re.compile(r'series/([a-z0-9-]+)')
CDN_RE = re.compile(r'cdn\.stillslab\.com/([^\s",\\)]+?\.(?:webp|jpg|jpeg|png))')


def parse_gallery(html: str, slug: str, kind: str) -> dict:
	"""Title/year/director/DP from meta tags; frame inventory from the flight payload."""
	title = None
	m = re.search(r'<title>(.*?)</title>', html)
	if m:
		title = re.sub(r'\s*[—-]\s*Film Stills.*$|\s*[—-]\s*StillsLab.*$', '', m.group(1)).strip()
	year = (re.search(r'\((\d{4})\)', title or ''),)
	year = year[0].group(1) if year[0] else None
	director = None
	dp = None
	for m in re.finditer(r'(Director|Cinematographer)[^<]*</span>\s*<[^>]*>([^<]+)', html):
		if m.group(1) == 'Director':
			director = m.group(2).strip()
		else:
			dp = m.group(2).strip()
	og = re.search(r'property="og:image" content="https://cdn\.stillslab\.com/([^/]+)/', html)
	frames = sorted(set(CDN_RE.findall(html)))
	return {
		'slug': slug,
		'kind': kind,
		'title': title,
		'year': year,
		'director': director,
		'cinematographer': dp,
		'frame_count': len(frames),
		'frame_paths': frames,
	}


async def main() -> None:
	parser = argparse.ArgumentParser()
	parser.add_argument('--kinds', default='movie,series,music-video', help='comma list from: movie,series,music-video')
	parser.add_argument('--limit', type=int, help='cap galleries (smoke tests)')
	args = parser.parse_args()
	OUT_DIR.mkdir(parents=True, exist_ok=True)
	kinds = [k for k in args.kinds.split(',') if k in LISTINGS]

	async with aiohttp.ClientSession(headers={'User-Agent': UA}) as session:
		# slug -> (kind, detail path builder). Movies/MVs use /gallery/<slug>;
		# series use /series/<slug> and hold frames per-Episode
		# (/series/<slug>/s<N>/e<M> — probed: Dark s1e1 = 411 frames), so the
		# series pass walks series -> seasons -> episodes.
		galleries: dict[str, tuple[str, str]] = {}
		for kind in kinds:
			async with session.get(f'{BASE}/{kind}', timeout=aiohttp.ClientTimeout(total=30)) as resp:
				html = await resp.text()
			slugs = set(GALLERY_RE.findall(html)) if kind != 'series' else set(SERIES_RE.findall(html))
			slugs.discard('page-a5bd6db200baacc0')  # pagination chunk artifact
			section = 'gallery' if kind != 'series' else 'series'
			for slug in slugs:
				galleries.setdefault(slug, (kind, section))
			print(f'  /{kind}: {len(slugs)} entries')
		print(f'  unique entries: {len(galleries)}')

		todo = list(galleries.items())
		if args.limit:
			todo = todo[: args.limit]

		records = []
		frames_file = OUT_DIR / 'frames.jsonl'
		SEASON_RE = re.compile(rf'href="(/series/[a-z0-9-]+/s\d+)"')
		EPISODE_RE = re.compile(rf'href="(/series/[a-z0-9-]+/s\d+/e\d+)"')
		# Some series link episodes directly from the landing page (Chornobyl:
		# /s1/e1.. with no season intermediate); walk whatever exists.
		EPISODE_ANY_RE = re.compile(rf'href="(/series/[a-z0-9-]+(?:/s\d+)?/e\d+)"')

		async def fetch_html(path: str) -> str | None:
			try:
				async with session.get(f'{BASE}{path}', timeout=aiohttp.ClientTimeout(total=45)) as resp:
					if resp.status != 200:
						return None
					return await resp.text()
			except Exception:  # noqa: BLE001
				return None

		with frames_file.open('w', encoding='utf-8') as fh:
			n = 0
			for slug, (kind, section) in todo:
				n += 1
				html = await fetch_html(f'/{section}/{slug}')
				if not html:
					print(f'  [{n}/{len(todo)}] {slug}: fetch failed')
					continue
				rec = parse_gallery(html, slug, kind)
				# Movies/MVs: all frames are in this one page.
				for path in sorted(set(CDN_RE.findall(html))):
					fh.write(json.dumps({'gallery': slug, 'path': path}, ensure_ascii=False) + '\n')
				if kind == 'series':
					# Series: the landing page holds only a handful of frames;
					# walk seasons -> episodes for the real inventory. Episodes
					# may hang off the landing page directly (no season page).
					episode_paths = set(EPISODE_ANY_RE.findall(html))
					for season_path in sorted(set(SEASON_RE.findall(html))):
						await asyncio.sleep(GALLERY_DELAY)
						shtml = await fetch_html(season_path)
						if not shtml:
							continue
						for path in sorted(set(CDN_RE.findall(shtml))):
							fh.write(json.dumps({'gallery': slug, 'season': season_path.rsplit('/', 1)[-1], 'path': path}, ensure_ascii=False) + '\n')
						episode_paths |= set(EPISODE_RE.findall(shtml))
					for episode_path in sorted(episode_paths):
						await asyncio.sleep(GALLERY_DELAY)
						ehtml = await fetch_html(episode_path)
						if not ehtml:
							continue
						for path in sorted(set(CDN_RE.findall(ehtml))):
							fh.write(json.dumps({'gallery': slug, 'episode': episode_path, 'path': path}, ensure_ascii=False) + '\n')
				rec.pop('frame_paths')
				records.append(rec)
				print(f'  [{n}/{len(todo)}] {slug}: {rec["title"][:46]}')
				await asyncio.sleep(GALLERY_DELAY)

	# frame_count must reflect the episode walk (series landing pages hold a
	# handful of frames; episodes hold the rest), so recompute from the frames
	# file BEFORE writing the summaries.
	frame_counts: dict[str, int] = {}
	for line in open(frames_file, encoding='utf-8'):
		g = json.loads(line)['gallery']
		frame_counts[g] = frame_counts.get(g, 0) + 1
	for r in records:
		r['frame_count'] = frame_counts.get(r['slug'], r['frame_count'])
	total_frames = sum(r['frame_count'] for r in records)
	(OUT_DIR / 'galleries.json').write_text(json.dumps(records, ensure_ascii=False, indent=1), encoding='utf-8')
	with (OUT_DIR / 'galleries.csv').open('w', newline='', encoding='utf-8') as fh:
		writer = csv.DictWriter(fh, fieldnames=['slug', 'kind', 'title', 'year', 'director', 'cinematographer', 'frame_count'], extrasaction='ignore')
		writer.writeheader()
		writer.writerows(records)
	print(f'done: {len(records)} galleries, {total_frames} frame refs -> {OUT_DIR}')


if __name__ == '__main__':
	asyncio.run(main())
