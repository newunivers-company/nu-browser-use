"""Deeper recon: netshort sitemap inventory + DramaBox __NEXT_DATA__ catalog structure."""

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
		# --- netshort sitemap ---
		print('===== netshort sitemap =====')
		st, _, body = await get(s, 'https://netshort.com/sitemap_netshortcom.xml')
		print('status:', st, '| bytes:', len(body))
		urls = locs(body)
		print('total <loc>:', len(urls))
		# Is it an index (nested sitemaps) or a url list?
		nested = [u for u in urls if 'sitemap' in u.lower()]
		print('nested sitemaps:', len(nested), nested[:5])
		# Sample non-sitemap urls + path shapes
		pages = [u for u in urls if 'sitemap' not in u.lower()]
		shapes = {}
		for u in pages:
			seg = re.sub(r'/[0-9]+', '/<id>', u.split('netshort.com')[-1]).split('?')[0]
			shapes[seg] = shapes.get(seg, 0) + 1
		print('page url shapes:', dict(list(sorted(shapes.items(), key=lambda x: -x[1]))[:10]))
		print('sample pages:', pages[:4])

		# --- DramaBox __NEXT_DATA__ ---
		print('\n===== DramaBox __NEXT_DATA__ =====')
		st, _, home = await get(s, 'https://www.dramabox.com/')
		m = NEXT_RE.search(home)
		if m:
			data = json.loads(m.group(1))
			pp = data.get('props', {}).get('pageProps', {})
			print('buildId:', data.get('buildId'))
			print('pageProps keys:', list(pp.keys())[:20])
			# Look for lists of dramas
			def find_lists(o, path='', depth=0):
				out = []
				if depth > 4:
					return out
				if isinstance(o, list) and o and isinstance(o[0], dict):
					keys = set(o[0].keys())
					if keys & {'bookId', 'bookName', 'dramaId', 'title', 'name'}:
						out.append((path, len(o), sorted(keys)[:12]))
				elif isinstance(o, dict):
					for k, v in o.items():
						out += find_lists(v, f'{path}.{k}', depth + 1)
				return out
			for path, n, keys in find_lists(pp)[:10]:
				print(f'  list at {path}: {n} items, keys={keys}')
			# a content/detail url shape from links in home
			links = sorted(set(re.findall(r'/drama/[\w-]+', home)))[:5] + sorted(set(re.findall(r'/detail/\w+', home)))[:5]
			print('  detail-ish links:', links)


if __name__ == '__main__':
	asyncio.run(main())
