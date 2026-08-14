"""Recon DramaBox /browse: catalog list + pagination for full bookId enumeration."""

from __future__ import annotations

import asyncio
import json
import re

import aiohttp

UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36'
NEXT_RE = re.compile(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', re.S)
BUILD = 'dramabox_prod_20260720'


async def get(session, url):
	async with session.get(url, timeout=aiohttp.ClientTimeout(total=25)) as r:
		return r.status, str(r.url), await r.text(errors='replace')


async def main():
	async with aiohttp.ClientSession(headers={'User-Agent': UA}) as s:
		st, final, html = await get(s, 'https://www.dramabox.com/browse')
		print(f'/browse: {st} -> {final} ({len(html)} bytes)')
		m = NEXT_RE.search(html)
		if m:
			pp = json.loads(m.group(1))['props']['pageProps']
			print('pageProps keys:', list(pp.keys()))
			def walk(o, path='', d=0):
				if d > 5:
					return
				if isinstance(o, list) and o and isinstance(o[0], dict):
					keys = set(o[0].keys())
					if keys & {'bookId', 'bookName'}:
						ids = [x.get('bookId') for x in o][:3]
						print(f'  list {path}: {len(o)} books, sample ids {ids}')
				elif isinstance(o, dict):
					for k, v in o.items():
						walk(v, f'{path}.{k}', d + 1)
			walk(pp)
			# pagination hints
			for k in ('total', 'totalCount', 'pageNo', 'pageSize', 'hasMore', 'totalPage', 'categoryList', 'typeList'):
				found = re.findall(rf'"{k}":\s*([0-9]+|true|false)', html)
				if found:
					print(f'  {k}: {found[:3]}')
		# bookIds present in the browse HTML
		ids = sorted(set(re.findall(r'/drama/(\d+)', html)))
		print(f'\nbookIds in /browse html: {len(ids)}')
		# Try browse pagination via _next/data with query params
		for q in ('/browse?page=2', '/browse?type=all&page=1'):
			st, final, h2 = await get(s, 'https://www.dramabox.com' + q)
			n = len(set(re.findall(r'/drama/(\d+)', h2)))
			print(f'  {q}: {st} -> {n} bookIds')
		# _next/data for browse
		st, _, body = await get(s, f'https://www.dramabox.com/_next/data/{BUILD}/en/browse.json')
		print(f'\n_next/data browse.json: {st} ({len(body)} bytes)')
		if st == 200:
			try:
				pp = json.loads(body)['pageProps']
				ids2 = set()
				def collect(o):
					if isinstance(o, dict):
						if o.get('bookId'):
							ids2.add(o['bookId'])
						for v in o.values():
							collect(v)
					elif isinstance(o, list):
						for v in o:
							collect(v)
				collect(pp)
				print('  bookIds in browse.json:', len(ids2))
			except json.JSONDecodeError:
				print('  (not json)')


if __name__ == '__main__':
	asyncio.run(main())
