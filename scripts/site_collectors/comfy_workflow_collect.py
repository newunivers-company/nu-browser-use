"""comfy.org official workflow collector (keyless).

comfy.org/workflows/comfyui/ is a static Astro page embedding the full official
workflow listing (409 templates) as an RSC payload. Each entry carries
name/shareId/title/description/tags/models/usage/date/username, and every
workflow's runnable JSON is a direct download:

    https://comfy.org/workflows/download/<shareId>.json

No login, robots.txt allows all crawlers (disallows only /_astro/ etc.).

Scope control: VIDEO_ONLY defaults to true — only workflows whose tags mention
video are downloaded (the JSON files are 50-300KB each and non-video templates
are out of scope for the AI-video intelligence layer).

Output layout (COMFY_OUT, default ~/comfy_export):
  workflows.json   - all listing records (metadata for every workflow)
  workflows.csv    - flat table
  json/<sid>.json  - downloaded workflow JSON (video-tagged only by default)

License note: comfy.org does not state a workflow-level license; these files go
to the internal-reference partition only, not the commercial/dataset partition.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import html
import json
import os
import re
from pathlib import Path

import aiohttp

OUT_DIR = Path(os.environ.get('COMFY_OUT', str(Path.home() / 'comfy_export')))
LIST_URL = 'https://comfy.org/workflows/comfyui/'
DOWNLOAD_URL = 'https://comfy.org/workflows/download/{sid}.json'
UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36'
DL_SLEEP = 0.6
DL_CONCURRENCY = 4


def parse_listing(html_text: str) -> list[dict]:
	"""Extract every workflow record from the RSC payload of the listing page."""
	decoded = html.unescape(html_text)
	spans: list[str] = []
	for match in re.finditer(r'\{"name":\[0,', decoded):
		start = match.start()
		depth = 0
		for idx in range(start, min(start + 100_000, len(decoded))):
			ch = decoded[idx]
			if ch == '{':
				depth += 1
			elif ch == '}':
				depth -= 1
				if depth == 0:
					spans.append(decoded[start : idx + 1])
					break

	def field(span: str, key: str) -> str | None:
		m = re.search(r'"' + key + r'":\[0,"?(.*?)"?\][,\]]', span)
		return m.group(1).rstrip('"') if m else None

	records: dict[str, dict] = {}
	for span in spans:
		sid = field(span, 'shareId')
		if not sid:
			continue
		tag_match = re.search(r'"tags":\[1,\[(.*?)\]\]', span)
		tags = re.findall(r'\[0,"([^"]+)"\]', tag_match.group(1)) if tag_match else []
		model_match = re.search(r'"models":\[1,\[(.*?)\]\]', span)
		models = re.findall(r'\[0,"([^"]+)"\]', model_match.group(1)) if model_match else []
		usage = re.search(r'"usage":\[0,(\d+)\]', span)
		records[sid] = {
			'share_id': sid,
			'title': field(span, 'title'),
			'description': field(span, 'description'),
			'tags': tags,
			'models': models,
			'username': field(span, 'username'),
			'creator': field(span, 'creatorDisplayName'),
			'date': field(span, 'date'),
			'usage': int(usage.group(1)) if usage else None,
		}
	return list(records.values())


def summarize_workflow(doc: dict) -> dict:
	"""Node-level summary: class types and counts, per the Workflow Registry schema."""
	nodes = doc.get('nodes', [])
	classes: dict[str, int] = {}
	for node in nodes:
		ct = node.get('type')
		if ct:
			classes[ct] = classes.get(ct, 0) + 1
	return {
		'node_count': len(nodes),
		'link_count': len(doc.get('links', [])),
		'class_types': dict(sorted(classes.items(), key=lambda x: -x[1])),
	}


async def main() -> None:
	parser = argparse.ArgumentParser()
	parser.add_argument('--all', action='store_true', help='download every workflow, not just video-tagged')
	parser.add_argument('--limit', type=int, default=0, help='max downloads (0 = all matched)')
	args = parser.parse_args()

	json_dir = OUT_DIR / 'json'
	json_dir.mkdir(parents=True, exist_ok=True)

	connector = aiohttp.TCPConnector(limit=DL_CONCURRENCY)
	async with aiohttp.ClientSession(connector=connector, headers={'User-Agent': UA}) as session:
		async with session.get(LIST_URL, timeout=aiohttp.ClientTimeout(total=60)) as response:
			response.raise_for_status()
			listing_html = await response.text()
		records = parse_listing(listing_html)
		print(f'listing: {len(records)} workflows parsed')

		video_recs = [r for r in records if any('video' in t.lower() for t in r.get('tags', []))]
		targets = records if args.all else video_recs
		print(f'video-tagged: {len(video_recs)} | download targets: {len(targets)}')

		sem = asyncio.Semaphore(DL_CONCURRENCY)

		async def download(rec: dict) -> None:
			sid = rec['share_id']
			async with sem:
				try:
					async with session.get(DOWNLOAD_URL.format(sid=sid), timeout=aiohttp.ClientTimeout(total=45)) as resp:
						if resp.status != 200:
							rec['download_status'] = f'http_{resp.status}'
							return
						doc = await resp.json(content_type=None)
				except Exception as exc:  # noqa: BLE001 - record the miss
					rec['download_status'] = f'error:{type(exc).__name__}'
					return
				(json_dir / f'{sid}.json').write_text(json.dumps(doc, ensure_ascii=False), encoding='utf-8')
				rec['workflow_summary'] = summarize_workflow(doc)
				rec['download_status'] = 'ok'
				await asyncio.sleep(DL_SLEEP)

		if args.limit:
			await asyncio.gather(*(download(r) for r in targets[: args.limit]))
		else:
			await asyncio.gather(*(download(r) for r in targets))

	ok = sum(1 for r in targets if r.get('download_status') == 'ok')
	print(f'downloaded: {ok}/{len(targets)} workflow JSONs')

	fieldnames = ['share_id', 'title', 'tags', 'models', 'username', 'creator', 'date', 'usage', 'download_status']
	with (OUT_DIR / 'workflows.csv').open('w', newline='', encoding='utf-8') as fh:
		writer = csv.DictWriter(fh, fieldnames=fieldnames, extrasaction='ignore')
		writer.writeheader()
		for r in records:
			row = dict(r)
			row['tags'] = '|'.join(r.get('tags', []))
			row['models'] = '|'.join(r.get('models', []))
			writer.writerow(row)
	(OUT_DIR / 'workflows.json').write_text(json.dumps(records, ensure_ascii=False, indent=1), encoding='utf-8')
	print(f'done -> {OUT_DIR}')


if __name__ == '__main__':
	asyncio.run(main())
