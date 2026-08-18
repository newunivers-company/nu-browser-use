"""netshort keyless catalog collector.

netshort episode pages redirect anonymous visitors to /marketing (content is gated), so no
page-level metadata is retrievable without an account. What IS public is the sitemap set:

  sitemap_netshortcom.xml        -> 112 site_play_N.xml child sitemaps
  site_play_N.xml                -> episode URLs: /episode/<title-slug>-<dramaId>-ep-<n>

From those URLs we derive a catalog: one record per dramaId with a human title (de-slugged),
episode count (max episode number seen), and the landing URL. No video, no login, public-only
(per docs/collection-policy.md).

Output (NETSHORT_OUT, default ~/netshort_export):
  dramas.json / dramas.csv   - one record per dramaId
  episodes.csv               - flat dramaId,episode,url list
  sitemaps.json              - child sitemap inventory
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
from datetime import date
import os
import re
import xml.etree.ElementTree as ET
from pathlib import Path

import aiohttp

INDEX = 'https://netshort.com/sitemap_netshortcom.xml'
OUT_DIR = Path(os.environ.get('NETSHORT_OUT', str(Path.home() / 'netshort_export')))
UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36'
# /episode/<slug>-<dramaId 15+ digits>[-ep-<n>]
EP_RE = re.compile(r'/episode/(?P<slug>.+?)-(?P<drama>\d{12,})(?:-ep-(?P<ep>\d+))?/?$')


def parse_locs(xml_text: str) -> list[str]:
	"""Extract <loc> URLs from a sitemap document."""
	try:
		root = ET.fromstring(xml_text)
	except ET.ParseError:
		return []
	return [e.text.strip() for e in root.iter() if e.tag.endswith('}loc') and e.text]


async def fetch(session: aiohttp.ClientSession, url: str, sem: asyncio.Semaphore) -> str | None:
	async with sem:
		for attempt in range(3):
			try:
				async with session.get(url, timeout=aiohttp.ClientTimeout(total=40)) as r:
					if r.status == 200:
						return await r.text()
					if r.status in (429, 500, 502, 503):
						await asyncio.sleep(2 * (attempt + 1))
						continue
					return None
			except Exception:  # noqa: BLE001
				await asyncio.sleep(1)
	return None


def deslug(slug: str) -> str:
	"""Turn a URL slug into a display title."""
	return re.sub(r'\s+', ' ', slug.replace('-', ' ')).strip().title()


async def main() -> None:
	parser = argparse.ArgumentParser()
	parser.add_argument('--limit-sitemaps', type=int, default=0, help='cap child sitemaps (0 = all)')
	args = parser.parse_args()

	OUT_DIR.mkdir(parents=True, exist_ok=True)
	sem = asyncio.Semaphore(6)
	async with aiohttp.ClientSession(headers={'User-Agent': UA}) as session:
		index_xml = await fetch(session, INDEX, sem)
		child_maps = [u for u in parse_locs(index_xml or '') if 'site_play' in u]
		if args.limit_sitemaps > 0:
			child_maps = child_maps[: args.limit_sitemaps]
		print(f'[1/3] {len(child_maps)} child sitemaps', flush=True)
		(OUT_DIR / 'sitemaps.json').write_text(json.dumps(child_maps, ensure_ascii=False, indent=2), encoding='utf-8')

		# dramaId -> {title, episodes:set, url}
		dramas: dict[str, dict] = {}
		episode_rows: list[tuple[str, str, str]] = []
		total_urls = 0

		async def handle(map_url: str) -> None:
			nonlocal total_urls
			xml = await fetch(session, map_url, sem)
			for loc in parse_locs(xml or ''):
				m = EP_RE.search(loc)
				if not m:
					continue
				total_urls += 1
				drama_id = m.group('drama')
				ep = m.group('ep')
				entry = dramas.setdefault(drama_id, {'dramaId': drama_id, 'title': deslug(m.group('slug')), 'episodes': set(), 'url': ''})
				if ep:
					entry['episodes'].add(int(ep))
					episode_rows.append((drama_id, ep, loc))
				else:
					entry['url'] = loc  # landing url
					episode_rows.append((drama_id, '', loc))

		print('[2/3] parsing episode sitemaps', flush=True)
		for i in range(0, len(child_maps), 12):
			await asyncio.gather(*(handle(u) for u in child_maps[i : i + 12]))
			print(f'  ...{min(i + 12, len(child_maps))}/{len(child_maps)} sitemaps -> {len(dramas)} dramas, {total_urls} urls', flush=True)

		# Finalize records.
		records = []
		for d in dramas.values():
			eps = sorted(d['episodes'])
			records.append({
				'dramaId': d['dramaId'],
				'title': d['title'],
				'episodeCount': (max(eps) if eps else 0),
				'episodesSeen': len(eps),
				'url': d['url'] or f"https://netshort.com/episode/{d['title'].lower().replace(' ', '-')}-{d['dramaId']}",
			})
		records.sort(key=lambda r: r['episodeCount'], reverse=True)

		print('[3/3] writing outputs', flush=True)
		(OUT_DIR / 'dramas.json').write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding='utf-8')

		# Dated snapshot for catalog_state (same local-day-directory convention).
		# dramas.json stays the latest run's view; this is what puts netshort's
		# 50k+ catalog into the time series. Not backfillable — write every run.
		snap_dir = OUT_DIR / 'snapshots' / date.today().isoformat()
		snap_dir.mkdir(parents=True, exist_ok=True)
		(snap_dir / 'dramas.json').write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding='utf-8')
		with (OUT_DIR / 'dramas.csv').open('w', newline='', encoding='utf-8-sig') as h:
			w = csv.writer(h)
			w.writerow(['dramaId', 'title', 'episodeCount', 'episodesSeen', 'url'])
			for r in records:
				w.writerow([r['dramaId'], r['title'], r['episodeCount'], r['episodesSeen'], r['url']])
		with (OUT_DIR / 'episodes.csv').open('w', newline='', encoding='utf-8-sig') as h:
			w = csv.writer(h)
			w.writerow(['dramaId', 'episode', 'url'])
			w.writerows(episode_rows)

	print(f'DONE -> {OUT_DIR} ({len(records)} dramas, {total_urls} episode urls)', flush=True)


if __name__ == '__main__':
	asyncio.run(main())
