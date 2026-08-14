"""fal.ai keyless model-registry collector.

No login, no API key — everything below is the public site surface:

  1. /api/models?keywords=video   -> paged listing (~650 models incl. non-video; filtered)
  2. each model page's HTML       -> the RSC payload embeds a complete OpenAPI 3 doc
                                      (request/response schemas, endpoints, param enums)
  3. normalized registry          -> fal_models.json + fal_models.csv + openapi/<id>.json

Only video-capable categories are collected (text-to-video, image-to-video,
video-to-video, audio-to-video, video-to-audio, lipsync, video-edit...).
Individual OpenAPI docs are large, so page fetches are semaphored and polite.

Output layout (FAL_OUT, default ~/fal_export):
  models.json        - listing records + normalized schema summary per model
  models.csv         - flat table
  openapi/<slug>.json - raw embedded OpenAPI doc per model (schemas for prompt
                        compilation + Model Registry normalization)
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import os
import re
from pathlib import Path

import aiohttp

OUT_DIR = Path(os.environ.get('FAL_OUT', str(Path.home() / 'fal_export')))
LIST_URL = 'https://fal.ai/api/models'
UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36'
LIST_PAGE_SIZE = 50
VIDEO_CATEGORIES = {
	'text-to-video',
	'image-to-video',
	'video-to-video',
	'audio-to-video',
	'video-to-audio',
}
VIDEO_TAG_HINTS = ('video', 'lipsync', 'avatar', 'camera', 'text-to-video', 'image-to-video')
PAGE_SLEEP = 1.0
DETAIL_SLEEP = 0.8


def slugify(model_id: str) -> str:
	"""Turn a fal model id (owner/name/sub) into a safe filename."""
	return re.sub(r'[^a-zA-Z0-9._-]+', '_', model_id)


def is_video_model(item: dict) -> bool:
	"""Keep a listing record if its category or tags mark it video-capable."""
	if item.get('category') in VIDEO_CATEGORIES:
		return True
	tags = ' '.join(item.get('tags') or []).lower()
	return any(hint in tags for hint in VIDEO_TAG_HINTS) and item.get('category') not in ('image', 'audio', 'text')


def extract_openapi_span(html: str) -> str | None:
	"""Locate the escaped JSON span that carries the embedded OpenAPI doc.

	The span is brace-balanced on the escaped level (quotes are backslash-
	escaped there, so braces inside strings cannot break the walk). Decoding
	happens in decode_embedded_doc.
	"""
	anchor = re.search(r'\{"openapi', html)
	if not anchor:
		anchor = re.search(r'\{\\"openapi', html)
	if not anchor:
		return None
	start = anchor.start()
	depth = 0
	for idx in range(start, min(start + 3_000_000, len(html))):
		ch = html[idx]
		if ch == '{':
			depth += 1
		elif ch == '}':
			depth -= 1
			if depth == 0:
				return html[start : idx + 1]
	return None


def is_openapi_doc(doc: object) -> bool:
	"""Accept only documents that actually look like an OpenAPI doc."""
	return isinstance(doc, dict) and 'paths' in doc and 'openapi' in doc


def decode_embedded_doc(raw: str) -> dict | None:
	"""Decode an HTML-attr-escaped JSON span that may nest one or two more levels.

	Pages embed the OpenAPI doc as: html-attr escapes > JSON string > (sometimes
	another JSON string) > {openapi: <doc>} or the doc itself. Try each depth.
	"""
	candidates: list[str] = [raw]
	try:
		once = json.loads('"' + raw + '"')
		candidates.append(once)
		if isinstance(once, str):
			twice = json.loads('"' + once + '"')
			candidates.append(twice)
	except json.JSONDecodeError:
		pass

	for text in candidates:
		if not isinstance(text, str):
			continue
		try:
			obj = json.loads(text)
		except json.JSONDecodeError:
			continue
		if is_openapi_doc(obj):
			return obj
		if isinstance(obj, dict) and is_openapi_doc(obj.get('openapi')):
			return obj['openapi']
	return None


def summarize_openapi(model_id: str, doc: dict) -> dict:
	"""Reduce an OpenAPI doc to the fields the Model Registry cares about."""
	paths = doc.get('paths', {})
	endpoints = []
	for path, ops in paths.items():
		if not isinstance(ops, dict):
			continue
		for method, op in ops.items():
			if method not in ('post', 'get') or not isinstance(op, dict):
				continue
			request_ref = None
			try:
				body = op.get('requestBody', {}).get('content', {}).get('application/json', {})
				schema = body.get('schema', {})
				request_ref = schema.get('$ref') or schema.get('title')
			except AttributeError:
				pass
			endpoints.append(
				{
					'method': method,
					'path': path,
					'operation_id': op.get('operationId'),
					'summary': (op.get('summary') or '')[:200],
					'request_schema': request_ref,
				}
			)
	schemas = doc.get('components', {}).get('schemas', {})
	request_props = {}
	for name, schema in schemas.items():
		if 'Request' in name and isinstance(schema, dict):
			props = schema.get('properties', {})
			request_props[name] = {
				key: {
					'type': val.get('type'),
					'enum': val.get('enum'),
					'default': val.get('default'),
					'description': (val.get('description') or '')[:160],
				}
				for key, val in props.items()
				if isinstance(val, dict)
			}
	return {
		'model_id': model_id,
		'openapi_version': doc.get('openapi'),
		'endpoints': endpoints,
		'request_schemas': request_props,
		'schema_names': list(schemas.keys()),
	}


async def fetch_listing_page(session: aiohttp.ClientSession, page: int) -> dict | None:
	"""Fetch one page of the models listing."""
	params = {'keywords': 'video', 'size': LIST_PAGE_SIZE, 'page': page}
	try:
		async with session.get(LIST_URL, params=params, timeout=aiohttp.ClientTimeout(total=30)) as response:
			if response.status != 200:
				return None
			return await response.json()
	except Exception:  # noqa: BLE001 - record the miss, keep collecting
		return None


async def fetch_model_page(session: aiohttp.ClientSession, model_id: str) -> str | None:
	"""Fetch a model detail page's HTML."""
	url = f'https://fal.ai/models/{model_id}'
	try:
		async with session.get(url, timeout=aiohttp.ClientTimeout(total=45)) as response:
			if response.status != 200:
				return None
			return await response.text()
	except Exception:  # noqa: BLE001
		return None


async def main() -> None:
	parser = argparse.ArgumentParser()
	parser.add_argument('--pages', type=int, default=0, help='listing pages to walk (0 = all)')
	parser.add_argument('--limit', type=int, default=0, help='max detail pages to fetch (0 = all matched)')
	parser.add_argument('--skip-openapi', action='store_true', help='listing only, no detail pages')
	parser.add_argument('--retry-missing', action='store_true', help='only fetch models lacking a downloaded OpenAPI doc')
	args = parser.parse_args()

	openapi_dir = OUT_DIR / 'openapi'
	openapi_dir.mkdir(parents=True, exist_ok=True)

	connector = aiohttp.TCPConnector(limit=6)
	async with aiohttp.ClientSession(connector=connector, headers={'User-Agent': UA}) as session:
		# --- 1. listing -----------------------------------------------------
		matched: list[dict] = []
		page = 1
		pages_total = None
		while True:
			data = await fetch_listing_page(session, page)
			if not data:
				print(f'  listing page {page}: fetch failed, stopping')
				break
			if pages_total is None:
				pages_total = data.get('pages', 1)
				print(f'  listing: {data.get("total")} models across {pages_total} pages')
			items = data.get('items', [])
			video_items = [it for it in items if is_video_model(it)]
			matched.extend(video_items)
			print(f'  page {page}/{pages_total}: {len(items)} items, {len(video_items)} video-matched')
			page += 1
			if pages_total and page > pages_total:
				break
			if args.pages and page > args.pages:
				break
			await asyncio.sleep(PAGE_SLEEP)

		# dedupe by id
		by_id: dict[str, dict] = {}
		for it in matched:
			by_id.setdefault(it['id'], it)
		matched = list(by_id.values())
		print(f'  unique video-capable models: {len(matched)}')
		if args.retry_missing:
			matched = [it for it in matched if not (openapi_dir / f'{slugify(it["id"])}.json').exists()]
			print(f'  retry-missing: {len(matched)} models without a downloaded doc')

		# --- 2. detail pages -> embedded OpenAPI ----------------------------
		records_path = OUT_DIR / 'models.json'
		records: list[dict] = []
		if args.retry_missing and records_path.exists():
			# merge into the existing registry: only refetch candidates replace their rows
			records = json.loads(records_path.read_text(encoding='utf-8'))
		records_by_id = {r['fal_model_id']: r for r in records}
		if not args.skip_openapi:
			for n, it in enumerate(matched, 1):
				if args.limit and n > args.limit:
					break
				model_id = it['id']
				html = await fetch_model_page(session, model_id)
				summary: dict = {}
				if html:
					raw_span = extract_openapi_span(html)
					doc = decode_embedded_doc(raw_span) if raw_span else None
					if doc:
						(openapi_dir / f'{slugify(model_id)}.json').write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding='utf-8')
						summary = summarize_openapi(model_id, doc)
				record = {
					'fal_model_id': model_id,
					'title': it.get('title'),
					'category': it.get('category'),
					'tags': it.get('tags'),
					'license_type': it.get('licenseType'),
					'status': it.get('status'),
					'deprecated': it.get('deprecated'),
					'pricing': it.get('pricingInfoOverride'),
					'run_url': it.get('modelUrl'),
					'endpoint_count': len(summary.get('endpoints', [])),
					'schema_names': summary.get('schema_names'),
					'request_schemas': summary.get('request_schemas'),
				}
				records_by_id[model_id] = record
				flag = 'openapi' if summary else ('page-fetched' if html else 'MISS')
				print(f'  [{n}/{len(matched)}] {model_id}: {flag}')
				await asyncio.sleep(DETAIL_SLEEP)
		else:
			for it in matched:
				records_by_id.setdefault(
					it['id'],
					{
						'fal_model_id': it['id'],
						'title': it.get('title'),
						'category': it.get('category'),
						'tags': it.get('tags'),
						'license_type': it.get('licenseType'),
						'status': it.get('status'),
						'deprecated': it.get('deprecated'),
						'pricing': it.get('pricingInfoOverride'),
						'run_url': it.get('modelUrl'),
					},
				)
		records = list(records_by_id.values())

	# --- 3. outputs ---------------------------------------------------------
	(OUT_DIR / 'models.json').write_text(json.dumps(records, ensure_ascii=False, indent=1), encoding='utf-8')
	fieldnames = ['fal_model_id', 'title', 'category', 'tags', 'license_type', 'status', 'deprecated', 'pricing', 'run_url', 'endpoint_count']
	with (OUT_DIR / 'models.csv').open('w', newline='', encoding='utf-8') as fh:
		writer = csv.DictWriter(fh, fieldnames=fieldnames, extrasaction='ignore')
		writer.writeheader()
		writer.writerows(records)
	with_schema = sum(1 for r in records if r.get('endpoint_count'))
	print(f'done: {len(records)} models, {with_schema} with OpenAPI schemas -> {OUT_DIR}')


if __name__ == '__main__':
	asyncio.run(main())
