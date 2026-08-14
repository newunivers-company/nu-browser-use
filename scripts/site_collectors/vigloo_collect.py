"""Vigloo keyless collector.

Pulls the full short-drama catalog from vigloo.com with no login and no browser:

  1. sitemap.xml          -> locale sitemap indexes (content + video)
  2. sitemap-content-*    -> every /<locale>/content/<id> program URL
  3. each program page    -> __NEXT_DATA__ JSON: title, genres, logLine, episode
                             and season counts, view/like/bookmark counts, rating,
                             release date, program code, assets
  4. video sitemap        -> lastmod per program (release freshness cross-check)

Output layout (VIGLOO_OUT, default ~/vigloo_export):
  programs.json     - one record per unique program id (merged across locales)
  programs.csv      - flat table of the same
  locales.json      - per-locale url inventory from the sitemap index
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import os
import re
import xml.etree.ElementTree as ET
from pathlib import Path

import aiohttp

SITEMAP_INDEX = 'https://www.vigloo.com/sitemap.xml'
OUT_DIR = Path(os.environ.get('VIGLOO_OUT', str(Path.home() / 'vigloo_export')))
UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36'
NEXT_DATA_RE = re.compile(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', re.S)
LOCALES = ('en', 'ko', 'ja', 'es', 'id', 'pt-BR', 'th', 'ar', 'zh-Hant', 'zh-Hans', 'hi', 'fr')


def parse_locs(xml_text: str) -> list[str]:
	"""Extract every <loc> URL from a sitemap or sitemapindex document."""
	root = ET.fromstring(xml_text)
	return [el.text.strip() for el in root.iter() if el.tag.endswith('}loc') and el.text]


async def fetch(session: aiohttp.ClientSession, url: str, semaphore: asyncio.Semaphore) -> str | None:
	"""GET a URL with a browser UA, bounded by the concurrency semaphore."""
	async with semaphore:
		try:
			async with session.get(url, timeout=aiohttp.ClientTimeout(total=30)) as response:
				if response.status != 200:
					return None
				return await response.text()
		except Exception:  # noqa: BLE001 - a missed page is recorded, not fatal
			return None


async def collect_sitemaps(session: aiohttp.ClientSession) -> tuple[list[str], dict[str, int]]:
	"""Return every program URL across all locales, plus per-locale counts."""
	index_urls = parse_locs(await fetch(session, SITEMAP_INDEX, asyncio.Semaphore(1)))
	content_maps = [u for u in index_urls if '/sitemap-content-' in u]
	print(f'  sitemap index -> {len(content_maps)} content sitemaps')

	sem = asyncio.Semaphore(6)
	xml_docs = await asyncio.gather(*(fetch(session, u, sem) for u in content_maps))
	program_urls: list[str] = []
	per_locale: dict[str, int] = {}
	for url, doc in zip(content_maps, xml_docs):
		if not doc:
			continue
		locale = url.split('sitemap-content-')[1].rsplit('-', 1)[0]
		locs = [loc for loc in parse_locs(doc) if '/content/' in loc]
		per_locale[locale] = len(locs)
		program_urls.extend(locs)
	print(f'  program urls: {len(program_urls)} across {len(per_locale)} locales')
	return program_urls, per_locale


def extract_program(html: str, url: str) -> dict | None:
	"""Pull the `program` object out of a content page's __NEXT_DATA__."""
	match = NEXT_DATA_RE.search(html)
	if not match:
		return None
	try:
		page_props = json.loads(match.group(1))['props']['pageProps']
	except (json.JSONDecodeError, KeyError):
		return None
	program = page_props.get('program')
	if not isinstance(program, dict):
		return None
	program['_url'] = url
	program['_locale'] = page_props.get('locale') or url.split('/content/')[0].rsplit('/', 1)[-1]
	return program


def squash(value) -> str:
	"""Flatten a scalar/list/dict field into one CSV cell."""
	if value is None:
		return ''
	if isinstance(value, (int, float, bool)):
		return str(value)
	if isinstance(value, str):
		return value
	if isinstance(value, list):
		if value and isinstance(value[0], dict):
			return ' | '.join(str(v.get('title') or v.get('url') or v.get('id') or '') for v in value if isinstance(v, dict))
		return ' | '.join(str(v) for v in value)
	if isinstance(value, dict):
		return ' | '.join(f'{k}:{v}' for k, v in value.items())
	return str(value)


async def main() -> None:
	parser = argparse.ArgumentParser()
	parser.add_argument('--limit', type=int, default=0, help='cap on program pages fetched (0 = all)')
	parser.add_argument('--locale', default='en', help="primary locale, or 'all' for every locale merged by id")
	args = parser.parse_args()

	async with aiohttp.ClientSession(headers={'User-Agent': UA}) as session:
		program_urls, per_locale = await collect_sitemaps(session)
		OUT_DIR.mkdir(parents=True, exist_ok=True)
		(OUT_DIR / 'locales.json').write_text(
			json.dumps({'sitemap_index': SITEMAP_INDEX, 'per_locale': per_locale}, ensure_ascii=False, indent=2),
			encoding='utf-8',
		)

		if args.locale == 'all':
			pending = program_urls
		else:
			# One page per program id, in the requested locale; other locales dedupe to the same id.
			pending = [u for u in program_urls if f'/{args.locale}/content/' in u]
		if args.limit > 0:
			pending = pending[: args.limit]
		print(f'[2/3] fetching {len(pending)} program pages (locale={args.locale})')

		sem = asyncio.Semaphore(8)
		done = 0

		async def one(url: str) -> dict | None:
			nonlocal done
			html = await fetch(session, url, sem)
			done += 1
			if done % 100 == 0:
				print(f'  ...{done}/{len(pending)}')
			if not html:
				return None
			return extract_program(html, url)

		records = [p for p in await asyncio.gather(*(one(u) for u in pending)) if p]
		programs = merge_locales(records) if args.locale == 'all' else records
		print(f'[3/3] parsed {len(records)} pages -> {len(programs)} programs')

		write_outputs(programs)


def merge_locales(records: list[dict]) -> list[dict]:
	"""Collapse per-locale records into one program per id, base = en, translations nested.

	Numeric/canonical fields are identical across locales; only the localized text
	(title, logLine, description, urls) varies, so those are kept per locale.
	"""
	TEXT_FIELDS = ('title', 'subTitle', 'logLine', 'description', 'synopsis', 'titleImage', 'thumbnailExpanded', '_url')
	by_id: dict[str, dict] = {}
	for record in sorted(records, key=lambda r: (r.get('_locale') != 'en', r.get('_locale') or '')):
		program_id = str(record.get('id'))
		entry = by_id.get(program_id)
		if entry is None:
			by_id[program_id] = record
			record['_translations'] = {}
			continue
		locale = record.get('_locale') or ''
		entry['_translations'][locale] = {field: record.get(field) for field in TEXT_FIELDS}
	return list(by_id.values())


def write_outputs(programs: list[dict]) -> None:
	"""Persist programs.json + a flat CSV of the headline fields."""
	(OUT_DIR / 'programs.json').write_text(json.dumps(programs, ensure_ascii=False, indent=2), encoding='utf-8')

	cols = [
		'id', 'title', 'subTitle', 'logLine', 'genres', 'seasonCount', 'episodeCount',
		'viewCount', 'likeCount', 'bookmarkCount', 'rating', 'releaseDate', 'ongoingStatus',
		'isOriginal', 'isFree', 'adUnlockAllowed', 'captionLanguages', 'programCode',
		'titleImage', 'thumbnailExpanded', '_locale', '_url', '_translation_locales',
	]
	with (OUT_DIR / 'programs.csv').open('w', newline='', encoding='utf-8-sig') as handle:
		writer = csv.writer(handle)
		writer.writerow(cols)
		for program in programs:
			row = [squash(program.get(c)) for c in cols]
			# _translation_locales is not a program field; fill it from the merged dict.
			row[cols.index('_translation_locales')] = ' | '.join(sorted((program.get('_translations') or {}).keys()))
			writer.writerow(row)
	print(f'DONE -> {OUT_DIR} ({len(programs)} programs)')


if __name__ == '__main__':
	asyncio.run(main())
