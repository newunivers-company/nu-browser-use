"""App Store Lookup collector for short-drama apps.

itunes.apple.com/lookup is Apple's public, keyless, documented JSON endpoint.
It is the highest-yield T0 source in the registry because one call per app per
day yields four otherwise-unreachable signals:

  1. version + currentVersionReleaseDate -> release cadence. Shipping cadence
     is the cleanest public proxy for engineering investment per brand.
  2. userRatingCount -> monotonic counter. Its daily delta is an install-volume
     proxy, and it is the only public number that moves with UA spend. This is
     what replaces the TikTok/Instagram posting-volume signal we cannot have.
  3. sellerName -> a first-party legal attestation. This is how the registry
     resolves brands to companies (My Drama -> Holy Water Limited, Sereal+ and
     UniReel -> two separate COL entities).
  4. description + releaseNotes -> the app description is where these
     publishers list their own official social URLs, so channel discovery falls
     out of the same call. New URLs are reported against the registry rather
     than silently added: an unknown URL is a claim, not a verified channel.

Per-country lookups are separate records — a title live in `us` but not `kr`
is a market-entry signal, and price/currency differ by storefront.

Output (PROMO_OUT, default ~/promo_export):
  snapshots/YYYY-MM-DD/appstore.json   - full records, per app per country
  app_observations.jsonl               - appended AppObservation rows
  discovered_channels.jsonl            - URLs in descriptions not in the registry
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import hashlib
import json
import os
import re
import sys
from pathlib import Path

if sys.stdout.encoding and sys.stdout.encoding.lower() != 'utf-8':
	sys.stdout.reconfigure(encoding='utf-8', errors='replace')

import aiohttp

sys.path.insert(0, str(Path(__file__).resolve().parent))
from registry.models import load_registry  # noqa: E402

OUT_DIR = Path(os.environ.get('PROMO_OUT', str(Path.home() / 'promo_export')))
LOOKUP = 'https://itunes.apple.com/lookup'
UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36'
DEFAULT_COUNTRIES = ['us', 'kr', 'jp']
BATCH = 12  # lookup accepts comma-joined ids; keep URLs short enough to be safe
KEEP = (
	'trackId',
	'trackName',
	'bundleId',
	'version',
	'currentVersionReleaseDate',
	'releaseDate',
	'sellerName',
	'sellerUrl',
	'artistName',
	'price',
	'currency',
	'formattedPrice',
	'averageUserRating',
	'userRatingCount',
	'averageUserRatingForCurrentVersion',
	'userRatingCountForCurrentVersion',
	'contentAdvisoryRating',
	'minimumOsVersion',
	'fileSizeBytes',
	'genres',
	'languageCodesISO2A',
	'trackViewUrl',
)
# Descriptions are multilingual and publishers run prose straight into a URL
# ("https://sereal.com/をご参照ください。"), so CJK/Hangul terminates the match.
URL_RE = re.compile(r'https?://[^\s<>"\')\]　-〿぀-ヿ㐀-䶿一-鿿가-힯＀-￯]+')
# Bare-domain handles publishers write instead of full URLs.
BARE_RE = re.compile(r'\b((?:www\.)?(?:instagram|tiktok|facebook|youtube|twitter|linkedin|linktr)\.(?:com|ee)/[^\s<>"\')\]]+)')
# Every listing carries privacy/terms links. They are not promotion channels and
# would bury the two or three real finds per run.
LEGAL_RE = re.compile(r'(privacy|terms|term_of|user-?agreement|useragreement|agreement/|eula|subscription-|policy)', re.I)
SOCIAL_HOSTS = (
	'instagram.com',
	'tiktok.com',
	'facebook.com',
	'youtube.com',
	'youtu.be',
	'twitter.com',
	'x.com',
	'linkedin.com',
	'linktr.ee',
	'lnk.bio',
	't.me',
	'discord',
	'reddit.com',
)


def classify(url: str) -> str:
	"""legal | social | site — only the last two are worth a human look."""
	if LEGAL_RE.search(url):
		return 'legal'
	if any(host in url.lower() for host in SOCIAL_HOSTS):
		return 'social'
	return 'site'


def normalize(url: str) -> str:
	"""Loose key for comparing a description URL against the registry."""
	url = url.rstrip('.,);:!')
	url = re.sub(r'^https?://', '', url).lower()
	url = re.sub(r'^www\.', '', url)
	return url.rstrip('/')


def extract_urls(text: str) -> list[str]:
	found = {u.rstrip('.,);:!') for u in URL_RE.findall(text or '')}
	found |= {f'https://{m}' for m in BARE_RE.findall(text or '')}
	return sorted(found)


async def lookup(session: aiohttp.ClientSession, ids: list[int], country: str) -> tuple[list[dict], dict]:
	"""Results plus the cache provenance of the response that carried them.

	Apple serves this endpoint through Akamai with `Cache-Control: max-age=86400`.
	Two runs a day apart can therefore land inside one cache window and return a
	byte-identical payload — which is exactly what happened on 2026-08-14 and
	-08-15: every counter, including DramaBox US at 819,983 ratings, was
	unchanged, while a fresh fetch minutes later read 820,721.

	A zero delta from a cache hit and a zero delta from a quiet market look the
	same in the data and mean opposite things. Recording x-cache/Age/max-age with
	each observation is what makes them tellable apart afterwards, so a flat
	series can be attributed instead of guessed at.
	"""
	params = {'id': ','.join(str(i) for i in ids), 'country': country, 'entity': 'software'}
	try:
		async with session.get(LOOKUP, params=params, timeout=aiohttp.ClientTimeout(total=30)) as response:
			if response.status != 200:
				print(f'  {country}: HTTP {response.status}')
				return [], {}
			x_cache = response.headers.get('x-cache', '')
			provenance = {
				# TCP_HIT/TCP_MISS is the edge's own word for whether this came
				# from the origin; the node id is dropped as noise.
				'cache_state': x_cache.split(' ', 1)[0] or None,
				'cache_age_seconds': int(response.headers['Age']) if response.headers.get('Age', '').isdigit() else None,
				'cache_control': response.headers.get('Cache-Control'),
			}
			# Apple serves this as text/javascript.
			return (await response.json(content_type=None)).get('results', []), provenance
	except Exception as exc:  # noqa: BLE001
		print(f'  {country}: {type(exc).__name__}')
		return [], {}


def previous_records(today: str) -> dict[str, dict]:
	"""Most recent snapshot before today, keyed by '<trackId>:<country>'."""
	snap_root = OUT_DIR / 'snapshots'
	if not snap_root.exists():
		return {}
	earlier = sorted(p for p in snap_root.iterdir() if p.is_dir() and p.name < today and (p / 'appstore.json').exists())
	if not earlier:
		return {}
	rows = json.loads((earlier[-1] / 'appstore.json').read_text(encoding='utf-8'))['apps']
	return {f'{row["trackId"]}:{row["country"]}': row for row in rows}


async def main() -> None:
	parser = argparse.ArgumentParser()
	parser.add_argument('--countries', nargs='*', default=DEFAULT_COUNTRIES)
	args = parser.parse_args()

	registry = load_registry()
	by_app = {b.app_ios: b for b in registry.brands if b.app_ios}
	known = {normalize(str(c.url)) for c in registry.channels}

	today = dt.date.today().isoformat()
	now = dt.datetime.now(dt.timezone.utc).isoformat()
	snap_dir = OUT_DIR / 'snapshots' / today
	snap_dir.mkdir(parents=True, exist_ok=True)

	rows: list[dict] = []
	discoveries: list[dict] = []
	async with aiohttp.ClientSession(headers={'User-Agent': UA}) as session:
		for country in args.countries:
			ids = list(by_app)
			results: list[dict] = []
			provenance: dict = {}
			for start in range(0, len(ids), BATCH):
				batch, batch_provenance = await lookup(session, ids[start : start + BATCH], country)
				results += batch
				# Batches of one country hit the same edge; the last non-empty
				# reading describes the country's fetch well enough.
				provenance = batch_provenance or provenance
				await asyncio.sleep(0.4)
			for result in results:
				brand = by_app.get(result['trackId'])
				if brand is None:
					continue
				description = result.get('description') or ''
				row = {key: result.get(key) for key in KEEP}
				row |= {
					'brand': brand.id,
					'company': brand.company,
					'country': country,
					'description_sha256': hashlib.sha256(description.encode('utf-8')).hexdigest(),
					'description_urls': extract_urls(description),
					'release_notes': (result.get('releaseNotes') or '')[:1500],
					'observed_at': now,
					# Without this, a flat counter cannot be told from a cache hit.
					**{f'http_{k}': v for k, v in provenance.items()},
				}
				rows.append(row)
				for url in row['description_urls']:
					if normalize(url) not in known:
						discoveries.append(
							{
								'url': url,
								'kind': classify(url),
								'brand': brand.id,
								'company': brand.company,
								'evidence': f'appstore_description:{result["trackId"]}:{country}',
								'official_status': 'claimed',
								'observed_at': now,
							}
						)
			missing = sorted(by_app[i].id for i in set(ids) - {r['trackId'] for r in results})
			print(
				f'  {country}: {len(results)}/{len(ids)} apps'
				+ (f'  not on this storefront: {", ".join(missing)}' if missing else '')
			)

	previous = previous_records(today)
	observations = []
	for row in rows:
		before = previous.get(f'{row["trackId"]}:{row["country"]}')
		ratings = row.get('userRatingCount')
		prior_ratings = (before or {}).get('userRatingCount')
		observations.append(
			{
				'source': 'itunes.apple.com',
				'observation_type': 'AppObservation',
				'entity_type': 'app',
				'entity_id': str(row['trackId']),
				'entity_title': row['trackName'],
				'brand': row['brand'],
				'company': row['company'],
				'scope': {'type': 'storefront', 'country': row['country']},
				'version': row['version'],
				'version_released_at': row['currentVersionReleaseDate'],
				'version_bumped': bool(before) and before.get('version') != row['version'],
				'rating': row['averageUserRating'],
				'rating_count': ratings,
				'rating_count_delta': (ratings - prior_ratings) if (ratings is not None and prior_ratings is not None) else None,
				# A zero delta means "no new ratings" only if the reading was fresh.
				# Served from the edge cache, it means "no new reading" — a different
				# statement, and the one that made two identical days look like a
				# stalled market.
				'reading_freshness': 'cached'
				if row.get('http_cache_state') == 'TCP_HIT'
				else 'fresh'
				if row.get('http_cache_state')
				else 'unknown',
				'description_changed': bool(before) and before.get('description_sha256') != row['description_sha256'],
				'seller': row['sellerName'],
				'price': row['formattedPrice'],
				'genres': row['genres'],
				'source_url': row['trackViewUrl'],
				'observed_at': now,
			}
		)

	(snap_dir / 'appstore.json').write_text(
		json.dumps({'collected_at': now, 'apps': rows}, ensure_ascii=False, indent=2), encoding='utf-8'
	)
	with (OUT_DIR / 'app_observations.jsonl').open('a', encoding='utf-8') as handle:
		for observation in observations:
			handle.write(json.dumps(observation, ensure_ascii=False) + '\n')
	if discoveries:
		with (OUT_DIR / 'discovered_channels.jsonl').open('a', encoding='utf-8') as handle:
			for discovery in discoveries:
				handle.write(json.dumps(discovery, ensure_ascii=False) + '\n')

	print(f'{len(rows)} app-country records, {len(observations)} observations')
	for observation in observations:
		if observation['version_bumped']:
			print(f'  RELEASE {observation["brand"]}/{observation["scope"]["country"]}: -> {observation["version"]}')
		if observation['description_changed']:
			print(f'  DESC CHANGED {observation["brand"]}/{observation["scope"]["country"]}')
		if observation['rating_count_delta']:
			print(f'  RATINGS {observation["brand"]}/{observation["scope"]["country"]}: +{observation["rating_count_delta"]}')
	interesting = sorted({d['url'] for d in discoveries if d['kind'] != 'legal'})
	legal_count = len({d['url'] for d in discoveries if d['kind'] == 'legal'})
	if interesting:
		print(f'  {len(interesting)} candidate channels not in registry ({legal_count} legal URLs filtered out):')
		for url in interesting:
			print(f'    {url}')
	print(f'DONE -> {snap_dir}')


if __name__ == '__main__':
	asyncio.run(main())
