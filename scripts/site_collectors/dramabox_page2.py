"""Test DramaBox path-based browse pagination + recommendation-graph enumeration fallback."""

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


def nd(html):
	m = NEXT_RE.search(html)
	return json.loads(m.group(1))['props']['pageProps'] if m else None


async def main():
	async with aiohttp.ClientSession(headers={'User-Agent': UA}) as s:
		# Path-based pagination candidates
		print('--- path pagination ---')
		for path in ('/browse/2', '/browse/page/2', '/en/browse/2', '/browse-2'):
			st, final, html = await get(s, 'https://www.dramabox.com' + path)
			pp = nd(html) if st == 200 else None
			ids = [b['bookId'] for b in pp.get('bookList', [])] if pp and 'bookList' in pp else []
			print(f'  {path}: {st} pageNo={pp.get("pageNo") if pp else "-"} ids={ids[:3]}')

		# _next/data path variant with page in path
		for path in (f'/_next/data/{BUILD}/en/browse/2.json', f'/_next/data/{BUILD}/en/browse.json?pageNo=2'):
			st, _, body = await get(s, 'https://www.dramabox.com' + path)
			print(f'  {path[-40:]}: {st} ({len(body)} bytes)')

		# Recommendation graph: does detail have recommends bookIds?
		print('\n--- recommends graph ---')
		st, _, html = await get(s, 'https://www.dramabox.com/drama/41000102472')
		pp = nd(html)
		recs = pp.get('recommends') or []
		rec_ids = [r.get('bookId') for r in recs if isinstance(r, dict)]
		print('recommends count:', len(recs), '| ids:', rec_ids[:8])
		article = pp.get('articleList') or []
		art_ids = [a.get('bookId') for a in article if isinstance(a, dict)]
		print('articleList count:', len(article), '| ids:', art_ids[:8])


if __name__ == '__main__':
	asyncio.run(main())
