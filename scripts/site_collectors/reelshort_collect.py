"""ReelShort keyless catalog collector.

ReelShort's web player signs every API call client-side. The signer was
recovered from the public _app JS bundle:

  headers : apiVersion=1.0.4, channelId=WEB41001, clientVer=2.4.00,
            devId, lang, ts (unix), uid=0
  sign    = HmacSHA256(sorted "k=v&..." of headers+payload, salt).hex()
  salt    = zj8N6zKEdrK8d1MxwHSvExdgQ868q1yT

List responses come back application-encrypted (text/html content type):

  AES-128-CBC(key=VvRSNGFynLBW7aCP, iv=gLn8sxqpzyNjehDP)
    -> base64 string -> b64decode -> zlib -> JSON

Collected here (anonymous uid=0, exactly what any browser visitor sends):
  1. hall rails     - the 12 homepage rails incl. read/collect counts
  2. search sweeps  - alphabet + common-word webSearch pagination to widen
                      coverage beyond the homepage rails
  3. posters        - book_pic/default_pic per title (public CDN)

Output (REELSHORT_OUT, default ~/reelshort_export):
  books.json / books.csv / rails.json / posters/<book_id>.jpg
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import csv
import datetime as dt
import hashlib
import hmac
import json
import os
import subprocess
import time
import zlib
from pathlib import Path

import aiohttp

SALT = 'zj8N6zKEdrK8d1MxwHSvExdgQ868q1yT'
AES_KEY = 'VvRSNGFynLBW7aCP'
AES_IV = 'gLn8sxqpzyNjehDP'
UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36'
BASE_HEADERS = {'apiVersion': '1.0.4', 'channelId': 'WEB41001', 'clientVer': '2.4.00', 'devId': 'claudecollector01', 'lang': 'en', 'uid': '0'}
OUT_DIR = Path(os.environ.get('REELSHORT_OUT', str(Path.home() / 'reelshort_export')))
SWEEP_WORDS = [chr(c) for c in range(ord('a'), ord('z') + 1)] + ['the', 'my', 'love', 'you', 'boss', 'bride', 'baby', 'alpha', 'wolf', 'secret']


def signed_headers(payload: dict) -> dict[str, str]:
	"""Build the anonymous request headers plus the HMAC sign over params."""
	ts = str(int(time.time()))
	merged = {'session': '', 'ts': ts, **BASE_HEADERS, **payload}
	items = [(k, json.dumps(v, separators=(',', ':')) if isinstance(v, (dict, list)) else str(v)) for k, v in merged.items()]
	items = [(k, v) for k, v in items if v not in ('', 'null', 'None')]
	items.sort()
	query = '&'.join(f'{k}={v}' for k, v in items)
	sign = hmac.new(SALT.encode(), query.encode(), hashlib.sha256).hexdigest()
	headers = {**BASE_HEADERS, 'ts': ts, 'sign': sign}
	return headers


OPENSSL = r'C:\Program Files\Git\usr\bin\openssl.exe'


def decrypt(body: bytes) -> dict:
	"""Undo the client-side response envelope: AES -> b64 -> zlib -> JSON."""
	# Feed raw ciphertext (we b64-decode ourselves) — avoids Git-openssl's
	# flaky handling of very long -a base64 input on Windows.
	ciphertext = base64.b64decode(body.strip())
	proc = subprocess.run(
		[OPENSSL, 'enc', '-d', '-aes-128-cbc', '-K', AES_KEY.encode().hex(), '-iv', AES_IV.encode().hex()],
		input=ciphertext, capture_output=True,
	)
	out = proc.stdout
	if proc.returncode != 0 or not out:
		raise RuntimeError(f'openssl decrypt failed: {proc.stderr[:120]!r}')
	stream = base64.b64decode(out)
	if not stream.startswith(b'x'):
		stream = base64.b64decode(stream)
	return json.loads(zlib.decompress(stream, 47))


async def call(session: aiohttp.ClientSession, path: str, payload: dict) -> dict:
	"""POST one signed API call and decode whatever envelope comes back."""
	headers = {'Content-Type': 'application/json', 'User-Agent': UA, 'Origin': 'https://www.reelshort.com', 'Referer': 'https://www.reelshort.com/', **signed_headers(payload)}
	async with session.post(f'https://www.reelshort.com{path}', json=payload, headers=headers, timeout=aiohttp.ClientTimeout(total=30)) as response:
		body = await response.read()
		if 'json' in response.headers.get('Content-Type', ''):
			return json.loads(body)
		return decrypt(body)


async def collect_rails(session: aiohttp.ClientSession) -> tuple[list[dict], dict[str, dict]]:
	"""Pull homepage rails; return (rails, books-by-id).

	Each book gains `_rail` and `_rail_position` (ordinal within its rail) —
	PLATFORM_INTERNAL ranking observations.
	"""
	result = await call(session, '/api/ms/hall/webInfo', {})
	rails = result.get('data', {}).get('lists', [])
	books: dict[str, dict] = {}
	for rail in rails:
		rail_name = rail.get('title') or f"rail_{rail.get('bs_id')}"
		position = 0
		for book in (rail.get('books') or []) + (rail.get('banners') or []):
			book_id = book.get('book_id') or book.get('t_book_id')
			if not book_id:
				continue
			position += 1
			book['_rail'] = rail_name
			book['_rail_position'] = position
			books.setdefault(book_id, book)
	return rails, books


async def sweep_search(session: aiohttp.ClientSession, books: dict[str, dict]) -> int:
	"""Page webSearch over sweep words, merging every unique title found."""
	added = 0
	for word in SWEEP_WORDS:
		page = 1
		while True:
			result = await call(session, '/api/video/search/webSearch', {'word': word, 'page': page, 'pageSize': 50})
			data = result.get('data', {})
			lists = data.get('lists', [])
			if not lists:
				break
			for book in lists:
				book_id = book.get('book_id') or book.get('t_book_id')
				if book_id and book_id not in books:
					books[book_id] = book
					added += 1
			if page * 50 >= int(data.get('total') or 0):
				break
			page += 1
			await asyncio.sleep(0.4)
		await asyncio.sleep(0.4)
	return added


async def download_posters(session: aiohttp.ClientSession, books: dict[str, dict], posters_dir: Path) -> int:
	"""Save each book's cover image (public CDN) as posters/<book_id>.jpg."""
	posters_dir.mkdir(parents=True, exist_ok=True)
	saved = 0
	semaphore = asyncio.Semaphore(8)

	async def one(book_id: str, url: str) -> None:
		nonlocal saved
		dest = posters_dir / f'{book_id}.jpg'
		if dest.exists() and dest.stat().st_size > 0:
			saved += 1
			return
		async with semaphore:
			try:
				async with session.get(url, timeout=aiohttp.ClientTimeout(total=30)) as response:
					if response.status == 200:
						dest.write_bytes(await response.read())
						saved += 1
			except Exception:  # noqa: BLE001 - skip broken image
				pass

	await asyncio.gather(*(one(bid, b['book_pic']) for bid, b in books.items() if b.get('book_pic')))
	return saved


def write_outputs(rails: list[dict], books: dict[str, dict]) -> None:
	OUT_DIR.mkdir(parents=True, exist_ok=True)
	(OUT_DIR / 'rails.json').write_text(json.dumps(rails, ensure_ascii=False, indent=2), encoding='utf-8')
	records = list(books.values())
	(OUT_DIR / 'books.json').write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding='utf-8')

	# Dated snapshot for catalog_state: the union tool keys on the local-day
	# directory name, same convention as every other collector. books.json stays
	# the latest run's view; this copy is what makes reelshort part of the
	# time series. A day written here cannot be backfilled later.
	snap_dir = OUT_DIR / 'snapshots' / dt.date.today().isoformat()
	snap_dir.mkdir(parents=True, exist_ok=True)
	(snap_dir / 'books.json').write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding='utf-8')

	# Rail-ordinal ranking observations, RankingObservation-style.
	observations = [
		{
			'source': 'reelshort.com', 'ranking_name': f"rail:{b.get('_rail')}", 'rank_type': 'PLATFORM_INTERNAL',
			'entity_type': 'work', 'entity_id': bid, 'entity_title': b.get('book_title'),
			'scope': {'type': 'platform', 'platform': 'reelshort'}, 'period': {'type': 'daily'},
			'rank': b.get('_rail_position'), 'raw_metric_name': 'rail_position',
			'read_count': b.get('read_count'), 'collect_count': b.get('collect_count'),
			'source_url': 'https://www.reelshort.com/', 'observed_at': dt.datetime.now(dt.timezone.utc).isoformat(),
		}
		for bid, b in books.items() if b.get('_rail_position')
	]
	with (OUT_DIR / 'rail_observations.jsonl').open('a', encoding='utf-8') as handle:
		for record in observations:
			handle.write(json.dumps(record, ensure_ascii=False) + '\n')

	cols = ['book_id', 't_book_id', 'book_title', 'theme', 'chapter_count', 'read_count', 'collect_count', 'book_type', 'lang', 'have_trailer', 'is_paid', '_rail', '_rail_position', 'book_pic', 'book_share_url']
	with (OUT_DIR / 'books.csv').open('w', newline='', encoding='utf-8-sig') as handle:
		writer = csv.writer(handle)
		writer.writerow(cols)
		for book in records:
			row = []
			for col in cols:
				value = book.get(col, '')
				row.append(' | '.join(value) if isinstance(value, list) else value)
			writer.writerow(row)


async def main() -> None:
	parser = argparse.ArgumentParser()
	parser.add_argument('--no-posters', action='store_true')
	args = parser.parse_args()

	async with aiohttp.ClientSession() as session:
		print('[1/3] hall rails')
		rails, books = await collect_rails(session)
		print(f'      rails: {len(rails)}, books: {len(books)}')
		print('[2/3] search sweeps')
		added = await sweep_search(session, books)
		print(f'      +{added} from search -> {len(books)} total')
		saved = 0
		if not args.no_posters:
			print('[3/3] posters')
			saved = await download_posters(session, books, OUT_DIR / 'posters')

	write_outputs(rails, books)
	print(f'DONE -> {OUT_DIR}')
	print(f'  books: {len(books)} | posters: {saved}')


if __name__ == '__main__':
	asyncio.run(main())
