"""Cover art for the catalogues collected this session.

ReelShort and Vigloo posters were already being pulled; the five catalogues
added since carry the same thing and nobody was fetching it. Each record
already declares a public cover URL, so this is a download pass over data
already held rather than any new crawling — 2,775 covers across goodshort,
dramaboxdb, mydrama, shortmax and flextv.

SCOPE
Promotional cover art only, the category `docs/collection-policy.md` lists as
collectable (포스터, 타이틀 이미지, CDN 무토큰 공개) and the same one
reelshort_collect and vigloo_assets already work in. No episode video, no HLS
segments, no page imagery beyond the declared cover. The five CDN hosts were
checked before any fetch: four publish no robots.txt at all and
akamai-static.shorttv.live allows.

Re-run safe: a cover already on disk **and complete** is skipped, so this costs
one GET per missing title and nothing for the rest. Completeness, not mere
presence, is the test — the first version checked only that the file was
non-empty, and 2,034 of 2,773 covers were 16KB fragments that satisfied it.
Failures are recorded per title rather than retried into a wall.

Output (POSTER_OUT, default ~/catalog_posters):
  <source>/<id>.<ext>
  manifest.json - per source: fetched, cached, failed, with reasons
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import os
import sys
from pathlib import Path
from urllib.parse import urlsplit

if sys.stdout.encoding and sys.stdout.encoding.lower() != 'utf-8':
	sys.stdout.reconfigure(encoding='utf-8', errors='replace')

import aiohttp

OUT_DIR = Path(os.environ.get('POSTER_OUT', str(Path.home() / 'catalog_posters')))
UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36'
HEADERS = {'User-Agent': UA, 'Accept': 'image/avif,image/webp,image/apng,image/*,*/*;q=0.8'}
CONCURRENCY = 6
DELAY = 0.15
MAX_BYTES = 8 * 1024 * 1024  # a cover is not 8MB; anything larger is not cover art

# source -> (export file, id field, cover field)
SOURCES: dict[str, tuple[str, str, str]] = {
	'goodshort': ('goodshort_export/books.json', 'bookId', 'cover'),
	'dramaboxdb': ('dramaboxdb_export/books.json', 'book_id', 'cover'),
	'mydrama': ('mydrama_export/series.json', 'series_id', 'thumbnail'),
	'shortmax': ('shortmax_export/dramas.json', 'drama_id', 'cover'),
	'flextv': ('flextv_export/dramas.json', 'drama_id', 'cover'),
}


CONTENT_TYPE_EXT = {
	'image/jpeg': '.jpg',
	'image/jpg': '.jpg',
	'image/png': '.png',
	'image/webp': '.webp',
	'image/gif': '.gif',
	'image/avif': '.avif',
}


def extension_for(url: str, content_type: str | None = None) -> str:
	"""Prefer what the server says it sent over what the URL implies.

	303 of the first 2,773 covers carried the wrong suffix — 184 JPEGs named
	.webp and 116 WebPs named .jpg — because these CDNs serve a format that has
	nothing to do with the path. The bytes are fine either way, but a file whose
	name lies about its type breaks anything that dispatches on extension.
	"""
	if content_type:
		base = content_type.split(';')[0].strip().lower()
		if base in CONTENT_TYPE_EXT:
			return CONTENT_TYPE_EXT[base]
	path = urlsplit(url).path.lower()
	for ext in ('.jpg', '.jpeg', '.png', '.webp', '.gif'):
		if path.endswith(ext):
			return ext
	return '.jpg'


def looks_complete(payload: bytes) -> bool:
	"""Whole file, not the first chunk of one.

	2,034 of the first 2,773 covers were truncated, clustered at 16KB and 32KB —
	`StreamReader.read(n)` returns what is buffered, not n bytes, so most covers
	were saved one chunk deep. Every one of them passed a magic-byte check,
	which is why the first verification of this collector reported them clean:
	the header is intact, the image is not. Terminators are what distinguishes
	the two.
	"""
	if payload.startswith(b'\xff\xd8'):  # JPEG
		return payload.endswith(b'\xff\xd9')
	if payload.startswith(b'\x89PNG'):
		return b'IEND' in payload[-16:]
	if payload[:4] == b'RIFF' and payload[8:12] == b'WEBP':
		# RIFF declares its own length; a short file is a truncated one.
		import struct

		return len(payload) >= 8 + struct.unpack('<I', payload[4:8])[0]
	if payload.startswith(b'GIF8'):
		return payload.endswith(b'\x3b')
	return bool(payload)  # unknown format: size is all we can judge on


def load_targets(source: str, spec: tuple[str, str, str], root: Path) -> list[tuple[str, str]]:
	path = root / spec[0]
	if not path.exists():
		print(f'  {source}: MISSING ({path})')
		return []
	targets = []
	for record in json.loads(path.read_text(encoding='utf-8')):
		identifier, url = record.get(spec[1]), record.get(spec[2])
		if identifier and isinstance(url, str) and url.startswith('http'):
			targets.append((str(identifier), url))
	return targets


def existing_complete_file(source: str, identifier: str) -> Path | None:
	"""Any suffix: the extension may have been guessed wrong on an earlier run."""
	for ext in ('.jpg', '.jpeg', '.png', '.webp', '.gif', '.avif'):
		candidate = OUT_DIR / source / f'{identifier}{ext}'
		if candidate.exists() and candidate.stat().st_size > 0 and looks_complete(candidate.read_bytes()):
			return candidate
	return None


async def read_body(response: aiohttp.ClientResponse) -> bytes | None:
	"""Drain the response. None means it exceeded MAX_BYTES.

	Explicitly a loop rather than one read(N): a single read returns only what
	has arrived, which is how 73% of the first run ended up as 16KB fragments.
	"""
	chunks: list[bytes] = []
	total = 0
	while True:
		chunk = await response.content.read(65536)
		if not chunk:
			break
		total += len(chunk)
		if total > MAX_BYTES:
			return None
		chunks.append(chunk)
	return b''.join(chunks)


async def fetch_one(session: aiohttp.ClientSession, source: str, identifier: str, url: str, semaphore: asyncio.Semaphore) -> str:
	if existing_complete_file(source, identifier) is not None:
		return 'cached'
	async with semaphore:
		try:
			async with session.get(url, timeout=aiohttp.ClientTimeout(total=45)) as response:
				if response.status != 200:
					return f'http_{response.status}'
				payload = await read_body(response)
				if payload is None:
					return 'oversize'
				if not payload:
					return 'empty'
				extension = extension_for(url, response.headers.get('Content-Type'))
		except Exception as exc:  # noqa: BLE001
			return type(exc).__name__
		if not looks_complete(payload):
			# Better to record the miss than to bank a header with no image
			# behind it, which is what the previous version did 2,034 times.
			return 'incomplete'
		destination = OUT_DIR / source / f'{identifier}{extension}'
		destination.parent.mkdir(parents=True, exist_ok=True)
		# Drop a differently-suffixed copy from an earlier, wronger guess.
		for stale in (OUT_DIR / source).glob(f'{identifier}.*'):
			if stale != destination:
				stale.unlink(missing_ok=True)
		destination.write_bytes(payload)
		await asyncio.sleep(DELAY)
	return 'fetched'


async def main() -> None:
	parser = argparse.ArgumentParser()
	parser.add_argument('--root', type=Path, default=Path.home(), help='directory holding the *_export dirs')
	parser.add_argument('--sources', nargs='*', default=list(SOURCES))
	parser.add_argument('--limit', type=int, help='cap per source (smoke tests)')
	args = parser.parse_args()

	now = dt.datetime.now(dt.timezone.utc).isoformat()
	manifest: dict[str, dict] = {}
	semaphore = asyncio.Semaphore(CONCURRENCY)

	async with aiohttp.ClientSession(headers=HEADERS) as session:
		for source in args.sources:
			spec = SOURCES.get(source)
			if not spec:
				continue
			targets = load_targets(source, spec, args.root)
			if args.limit:
				targets = targets[: args.limit]
			if not targets:
				continue
			results = await asyncio.gather(*(fetch_one(session, source, i, u, semaphore) for i, u in targets))
			tally: dict[str, int] = {}
			for outcome in results:
				tally[outcome] = tally.get(outcome, 0) + 1
			manifest[source] = {'targets': len(targets), 'outcomes': tally, 'collected_at': now}
			summary = ', '.join(f'{k}={v}' for k, v in sorted(tally.items()))
			print(f'  {source:12} {len(targets):>5} covers -> {summary}')

	OUT_DIR.mkdir(parents=True, exist_ok=True)
	(OUT_DIR / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
	on_disk = sum(1 for p in OUT_DIR.rglob('*') if p.is_file() and p.suffix.lower() in ('.jpg', '.jpeg', '.png', '.webp', '.gif'))
	failed = sum(
		count for entry in manifest.values() for key, count in entry['outcomes'].items() if key not in ('fetched', 'cached')
	)
	print(f'\ncovers on disk {on_disk} | failures this run {failed}')
	print(f'DONE -> {OUT_DIR}')


if __name__ == '__main__':
	asyncio.run(main())
