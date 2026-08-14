"""Nail DramaBox /browse pagination + category enumeration."""

from __future__ import annotations

import asyncio
import json
import re

import aiohttp

UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36'
BUILD = 'dramabox_prod_20260720'


async def get(session, url):
	async with session.get(url, timeout=aiohttp.ClientTimeout(total=25)) as r:
		return r.status, await r.text(errors='replace')


async def browse_json(session, query=''):
	st, body = await get(session, f'https://www.dramabox.com/_next/data/{BUILD}/en/browse.json{query}')
	if st != 200:
		return None
	try:
		return json.loads(body)['pageProps']
	except (json.JSONDecodeError, KeyError):
		return None


async def main():
	async with aiohttp.ClientSession(headers={'User-Agent': UA}) as s:
		pp = await browse_json(s)
		print('types:', [(t.get('typeTwoId'), t.get('typeTwoName')) for t in pp.get('types', [])][:20])
		print('pages (total):', pp.get('pages'), '| pageNo:', pp.get('pageNo'), '| typeTwoId:', pp.get('typeTwoId'))
		ids1 = [b['bookId'] for b in pp.get('bookList', [])]
		print('page1 ids:', ids1[:5], '...', len(ids1))

		# Test pagination param variants
		for q in ('?pageNo=2', '?page=2', '?p=2', '?pageNum=2'):
			pp2 = await browse_json(s, q)
			ids2 = [b['bookId'] for b in pp2.get('bookList', [])] if pp2 else []
			same = set(ids1) == set(ids2)
            #
			print(f'  {q}: pageNo={pp2.get("pageNo") if pp2 else "?"} first={ids2[:2]} same_as_p1={same}')

		# Test category filter
		types = pp.get('types', [])
		if types:
			tid = types[1].get('typeTwoId') if len(types) > 1 else types[0].get('typeTwoId')
			pp3 = await browse_json(s, f'?typeTwoId={tid}')
			print(f'  typeTwoId={tid}: name={pp3.get("typeTwoName") if pp3 else "?"} pages={pp3.get("pages") if pp3 else "?"} ids={[b["bookId"] for b in pp3.get("bookList",[])][:3] if pp3 else []}')


if __name__ == '__main__':
	asyncio.run(main())
