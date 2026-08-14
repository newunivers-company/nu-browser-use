"""Recon DramaBox and netshort entry points for a keyless catalog collector."""

from __future__ import annotations

import asyncio
import re

import aiohttp

UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36'


async def get(session, url, allow_redirects=True):
	try:
		async with session.get(url, allow_redirects=allow_redirects, timeout=aiohttp.ClientTimeout(total=20)) as r:
			body = await r.text(errors='replace')
			return r.status, str(r.url), body, dict(r.headers)
	except Exception as e:  # noqa: BLE001
		return None, url, f'ERR {type(e).__name__}: {e}', {}


async def recon(session, name, base):
	print(f'\n===== {name} ({base}) =====')
	status, final, body, headers = await get(session, base)
	print(f'home: {status} -> {final} ({len(body)} bytes, {headers.get("Content-Type","")})')
	if status is None:
		print('  ', body)
		return
	# __NEXT_DATA__ / framework hints
	has_next = '__NEXT_DATA__' in body
	has_nuxt = '__NUXT__' in body or 'window.__NUXT' in body
	apis = sorted(set(re.findall(r'https?://[\w.-]*api[\w.-]*\.\w+', body)))[:8]
	buildid = re.search(r'"buildId":"([^"]+)"', body)
	print(f'  __NEXT_DATA__: {has_next} | __NUXT__: {has_nuxt} | buildId: {buildid.group(1) if buildid else None}')
	print(f'  api hosts in html: {apis}')
	# robots.txt for sitemap pointers
	rs, _, rbody, _ = await get(session, base.rstrip('/') + '/robots.txt')
	sm = re.findall(r'(?i)sitemap:\s*(\S+)', rbody or '')
	print(f'  robots.txt: {rs} | sitemaps listed: {sm[:5]}')
	# common sitemap/api candidates
	for path in ('/sitemap_index.xml', '/sitemap-index.xml', '/sitemaps.xml', '/en/sitemap.xml', '/api/sitemap'):
		s, _, _, _ = await get(session, base.rstrip('/') + path)
		if s and s == 200:
			print(f'  FOUND sitemap candidate: {path} -> {s}')


async def main():
	async with aiohttp.ClientSession(headers={'User-Agent': UA}) as s:
		for name, base in (
			('DramaBox', 'https://www.dramabox.com/'),
			('DramaBox-app', 'https://www.dramaboxapp.com/'),
			('netshort', 'https://www.netshort.com/'),
			('netshort-alt', 'https://netshort.com/'),
		):
			await recon(s, name, base)


if __name__ == '__main__':
	asyncio.run(main())
