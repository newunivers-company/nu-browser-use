"""Recon netshort: site_play sitemap contents + drama page metadata surface."""

from __future__ import annotations

import asyncio
import json
import re
import xml.etree.ElementTree as ET

import aiohttp

UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36'
NEXT_RE = re.compile(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', re.S)


async def get(session, url):
	async with session.get(url, timeout=aiohttp.ClientTimeout(total=25)) as r:
		return r.status, str(r.url), await r.text(errors='replace')


def locs(xml_text):
	try:
		root = ET.fromstring(xml_text)
		return [e.text.strip() for e in root.iter() if e.tag.endswith('}loc') and e.text]
	except ET.ParseError:
		return []


async def main():
	async with aiohttp.ClientSession(headers={'User-Agent': UA}) as s:
		# One play sitemap
		st, _, body = await get(s, 'https://netshort.com/site_play_1.xml')
		urls = locs(body)
		print(f'site_play_1.xml: {st} | {len(urls)} urls')
		shapes = {}
		for u in urls:
			seg = re.sub(r'/[0-9]+', '/<id>', u.split('netshort.com')[-1]).split('?')[0]
			shapes[seg] = shapes.get(seg, 0) + 1
		print('url shapes:', dict(sorted(shapes.items(), key=lambda x: -x[1])[:8]))
		print('samples:', urls[:4])

		# Fetch one drama page -> metadata surface
		drama_url = next((u for u in urls if 'sitemap' not in u), None)
		if drama_url:
			st, final, html = await get(s, drama_url)
			print(f'\ndrama page: {st} -> {final} ({len(html)} bytes)')
			m = NEXT_RE.search(html)
			print('has __NEXT_DATA__:', bool(m))
			if m:
				data = json.loads(m.group(1))
				pp = data.get('props', {}).get('pageProps', {})
				print('buildId:', data.get('buildId'))
				print('pageProps keys:', list(pp.keys())[:25])
				# Find the drama/book object
				for k, v in pp.items():
					if isinstance(v, dict) and (v.keys() & {'id', 'title', 'name', 'bookName', 'shortPlayName', 'introduction', 'description'}):
						print(f'  {k} -> keys: {sorted(v.keys())[:20]}')
						for kk in ('title', 'name', 'shortPlayName', 'introduction', 'episodeCount', 'chapterCount', 'playCount', 'tags', 'categories'):
							if kk in v:
								print(f'      {kk}: {str(v[kk])[:80]}')
			else:
				# JSON-LD?
				ld = re.findall(r'<script type="application/ld\+json">(.*?)</script>', html, re.S)
				print('ld+json blocks:', len(ld))
				if ld:
					print(ld[0][:300])


if __name__ == '__main__':
	asyncio.run(main())
