"""ShortDramaCast bilingual title bridge collector.

The NU Rank join ceiling (84 multi-source titles) exists because the Chinese
ranking source (duanju007) speaks zh and everything else speaks en. A bridge
needs pairs of (zh title, en title) for the same work.

shortdramacast.com (SceneTrace) is exactly that bridge: every drama has /dramas/<slug>
(en) and /zh-cn/dramas/<slug> (zh-CN) pages under the same slug, declared as
hreflang alternates in sitemap-dramas.xml (3,339 urls). Fetching both language
pages per slug yields the zh-en title pairs, plus genre/platform metadata as a
bonus.

Collection:
  1. sitemap-dramas.xml -> unique slugs (skip listing pages)
  2. en page  -> og:title / h1 (EN title)
  3. zh page  -> og:title / h1 (ZH title)
Pairs where both sides parse become title_bridge.json.

Output (SDC_OUT, default ~/ranking_export):
  title_bridge.json     - [{slug, en, zh, ...}]
  bridge_join.json      - duanju007 zh titles joined to EN titles
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
from pathlib import Path

import aiohttp

if sys.stdout.encoding and sys.stdout.encoding.lower() != 'utf-8':
	sys.stdout.reconfigure(encoding='utf-8', errors='replace')

BASE = 'https://shortdramacast.com'
UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36'
OUT_DIR = Path(os.environ.get('SDC_OUT', str(Path.home() / 'ranking_export')))
PAGE_WAIT = 1.2
PAUSE_EVERY = 60
PAUSE_SECONDS = 20.0

TITLE_RE = re.compile(r'<h1[^>]*>([^<]{2,120})</h1>')
OG_RE = re.compile(r'<meta property="og:title" content="([^"]{2,150})"')


async def fetch(session: aiohttp.ClientSession, url: str) -> str | None:
	try:
		async with session.get(url, timeout=aiohttp.ClientTimeout(total=25)) as response:
			if response.status != 200:
				return None
			return await response.text()
	except Exception:  # noqa: BLE001
		return None


def extract_title(html: str) -> str | None:
	for regex in (OG_RE, TITLE_RE):
		match = regex.search(html)
		if match:
			title = match.group(1).strip()
			# Strip the page-type suffix both locales append ("... Cast & Episodes",
			# "... 演员表、集数、别名与观看入口") and HTML entities.
			title = re.sub(
				r'\s*(\.\.\.)?\s*((Cast|Episodes|Plot|播放源|观看入口|演员表|剧情|集数|别名|与|、)[^|]*(\s*\|\s*)?)+\s*$',
				'',
				title,
			)
			title = re.sub(r'\s*\|\s*SceneTrace.*$', '', title)
			title = re.sub(r'&#38;', '&', title).strip()
			if title:
				return title
	return None


async def main() -> None:
	parser = argparse.ArgumentParser()
	parser.add_argument('--limit', type=int, default=0, help='cap slugs (smoke tests)')
	args = parser.parse_args()

	async with aiohttp.ClientSession(headers={'User-Agent': UA}) as session:
		xml = await fetch(session, f'{BASE}/sitemap-dramas.xml')
		if not xml:
			raise SystemExit('sitemap fetch failed')
		locs = re.findall(r'<loc>([^<]+)</loc>', xml)
		slugs = sorted({loc.rstrip('/').rsplit('/', 1)[-1] for loc in locs if '/dramas/' in loc})
		if args.limit > 0:
			slugs = slugs[: args.limit]
		print(f'slugs: {len(slugs)}')

		bridge = []
		for index, slug in enumerate(slugs, 1):
			if index > 1 and (index - 1) % PAUSE_EVERY == 0:
				print(f'  ...pause {PAUSE_SECONDS:.0f}s after {index - 1} ({len(bridge)} pairs)')
				await asyncio.sleep(PAUSE_SECONDS)
			en_html = await fetch(session, f'{BASE}/dramas/{slug}')
			zh_html = await fetch(session, f'{BASE}/zh-cn/dramas/{slug}')
			en = extract_title(en_html) if en_html else None
			zh = extract_title(zh_html) if zh_html else None
			if en and zh and en != zh:
				bridge.append({'slug': slug, 'en': en, 'zh': zh})
			if index % 100 == 0:
				print(f'  {index}/{len(slugs)} -> {len(bridge)} pairs')
			await asyncio.sleep(PAGE_WAIT)

	OUT_DIR.mkdir(parents=True, exist_ok=True)
	(OUT_DIR / 'title_bridge.json').write_text(json.dumps(bridge, ensure_ascii=False, indent=2), encoding='utf-8')

	# Join duanju007 zh titles through the bridge.
	observations = []
	for line in (OUT_DIR / 'observations.jsonl').read_text(encoding='utf-8').splitlines():
		try:
			observations.append(json.loads(line))
		except json.JSONDecodeError:
			continue
	duanju_zh = {(o.get('entity_title') or '').strip() for o in observations if o.get('source') == 'duanju007.com'}
	zh_to_en = {b['zh']: b['en'] for b in bridge}
	matched = [(zh, zh_to_en[zh]) for zh in duanju_zh if zh in zh_to_en]
	(OUT_DIR / 'bridge_join.json').write_text(
		json.dumps(
			{'duanju_titles': len(duanju_zh), 'bridge_pairs': len(bridge), 'matched': matched}, ensure_ascii=False, indent=2
		),
		encoding='utf-8',
	)
	print(f'DONE: {len(bridge)} bridge pairs | duanju007 matched: {len(matched)}/{len(duanju_zh)}')
	print(f'-> {OUT_DIR / "title_bridge.json"}')


if __name__ == '__main__':
	asyncio.run(main())
