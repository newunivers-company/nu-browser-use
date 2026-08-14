"""Recon DramaBox: detail-page metadata + how to enumerate the full catalog (bookIds)."""

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


def next_data(html):
	m = NEXT_RE.search(html)
	return json.loads(m.group(1)) if m else None


async def main():
	async with aiohttp.ClientSession(headers={'User-Agent': UA}) as s:
		# 1. Detail page metadata
		st, final, html = await get(s, 'https://www.dramabox.com/drama/41000102472')
		print(f'detail: {st} -> {final} ({len(html)} bytes)')
		data = next_data(html)
		if data:
			pp = data['props']['pageProps']
			print('detail pageProps keys:', list(pp.keys()))
			book = pp.get('bookInfo') or pp.get('book') or pp.get('detail') or {}
			if not book:
				for k, v in pp.items():
					if isinstance(v, dict) and (v.keys() & {'bookId', 'bookName', 'chapterCount'}):
						book = v
						print('  book found under:', k)
						break
			if book:
				print('  book keys:', sorted(book.keys())[:30])
				for kk in ('bookId', 'bookName', 'bookNameEn', 'author', 'chapterCount', 'introduction', 'labels', 'tags', 'playCount', 'viewCount', 'score', 'cover'):
					if kk in book:
						print(f'    {kk}: {str(book[kk])[:90]}')

		# 2. Enumeration: category/browse pages + _next/data endpoint
		print('\n--- enumeration probes ---')
		for path in ('/browse', '/all', '/category', '/en/browse', '/theater', '/discover', '/ranking'):
			st, final, _ = await get(s, 'https://www.dramabox.com' + path)
			print(f'  {path}: {st} -> {final.split("dramabox.com")[-1][:40]}')
		# _next/data JSON for the home (list source)
		st, _, body = await get(s, f'https://www.dramabox.com/_next/data/{BUILD}/en.json')
		print(f'  _next/data/en.json: {st} ({len(body)} bytes)')
		if st == 200:
			try:
				j = json.loads(body)
				pp = j.get('pageProps', {})
				print('    pageProps keys:', list(pp.keys())[:15])
				bl = pp.get('bigList') or []
				print('    bigList items:', len(bl))
			except json.JSONDecodeError:
				print('    (not json)')
		# sitemap variants specific to dramabox
		for path in ('/sitemap-index.xml', '/sitemap/drama.xml', '/drama/sitemap.xml'):
			st, _, _ = await get(s, 'https://www.dramabox.com' + path)
			print(f'  {path}: {st}')


if __name__ == '__main__':
	asyncio.run(main())
