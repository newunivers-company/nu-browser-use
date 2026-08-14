"""Hugging Face Hub inventory — which generative models and datasets are used.

The catalogue's generative-AI sources mostly closed: CivitAI's robots disallows
/api/*, prompthero and promptbase name AI crawlers, generated.photos likewise,
and lexica is unreachable. The Hub is the opposite — robots is a bare
`User-agent: * / Allow: /` with no crawler naming and no Content-Signal, and the
Hub API answers without a key.

WHY THIS IS THE GENERATIVE SIGNAL WORTH HAVING
Prompt libraries tell you what people write; the Hub tells you what actually
runs. Each record carries downloads, likes, pipeline task, library and base
model, so the questions that matter are answerable: which image or video model
is displacing which, how fast a release accumulates use, whether a task category
is growing. Downloads are a monotonic counter, so a daily snapshot yields
velocity the same way app-store rating counts do.

Two axes are collected per entity type, because they answer different questions:
  downloads   the installed base — what the ecosystem actually runs
  trending    what moved this week, which the installed base hides

METADATA ONLY
Model and dataset cards are documentation written by their authors, and are not
fetched. What is kept is the record the Hub itself publishes about each entry:
identifiers, counters, task and library tags, and timestamps.

Output (HF_OUT, default ~/huggingface_export):
  snapshots/YYYY-MM-DD/<kind>-<sort>.json
  models.jsonl / datasets.jsonl / spaces.jsonl - appended, deduped on id
"""

from __future__ import annotations

import argparse
import asyncio
import collections
import datetime as dt
import json
import os
import sys
from pathlib import Path

if sys.stdout.encoding and sys.stdout.encoding.lower() != 'utf-8':
	sys.stdout.reconfigure(encoding='utf-8', errors='replace')

import aiohttp

OUT_DIR = Path(os.environ.get('HF_OUT', str(Path.home() / 'huggingface_export')))
API = 'https://huggingface.co/api'
UA = 'nu-browser-use/1.0 (+https://newunivers.com; nu@newunivers.com)'
HEADERS = {'User-Agent': UA, 'Accept': 'application/json'}
DELAY = 1.0
# Generative tasks this project cares about. Kept explicit rather than "all", so
# the pull stays proportionate to what is actually studied here.
GENERATIVE_TASKS = (
	'text-to-image', 'image-to-image', 'text-to-video', 'image-to-video',
	'text-to-speech', 'text-generation', 'image-text-to-text', 'video-text-to-text',
)
SORTS = ('downloads', 'trendingScore')
KINDS = ('models', 'datasets', 'spaces')


async def fetch(session: aiohttp.ClientSession, url: str) -> list | None:
	try:
		async with session.get(url, timeout=aiohttp.ClientTimeout(total=40)) as response:
			if response.status != 200:
				print(f'  HTTP {response.status}: {url.split("?")[0]}')
				return None
			payload = await response.json(content_type=None)
			return payload if isinstance(payload, list) else None
	except Exception as exc:  # noqa: BLE001
		print(f'  {type(exc).__name__}: {url.split("?")[0]}')
		return None


def flatten(entry: dict, kind: str, sort: str, task: str | None, now: str) -> dict | None:
	identifier = entry.get('id') or entry.get('modelId')
	if not identifier:
		return None
	tags = [t for t in (entry.get('tags') or []) if isinstance(t, str)]
	return {
		'kind': kind,
		'id': identifier,
		'author': entry.get('author'),
		'downloads': entry.get('downloads'),
		'likes': entry.get('likes'),
		'pipeline_tag': entry.get('pipeline_tag'),
		'library': entry.get('library_name'),
		'gated': entry.get('gated'),
		'private': entry.get('private'),
		'created_at': entry.get('createdAt'),
		'last_modified': entry.get('lastModified'),
		# base_model:* and license:* tags carry the lineage and terms the Hub knows.
		'base_model': next((t.split(':', 1)[1] for t in tags if t.startswith('base_model:')), None),
		'license': next((t.split(':', 1)[1] for t in tags if t.startswith('license:')), None),
		'tags': ' | '.join(t for t in tags if ':' not in t)[:400],
		'via_sort': sort,
		'via_task': task,
		'url': f'https://huggingface.co/{"datasets/" if kind == "datasets" else "spaces/" if kind == "spaces" else ""}{identifier}',
		'observed_at': now,
	}


def append_deduped(path: Path, rows: list[dict]) -> int:
	seen: set[str] = set()
	if path.exists():
		for line in path.open(encoding='utf-8'):
			try:
				seen.add(json.loads(line)['id'])
			except Exception:  # noqa: BLE001
				continue
	fresh = []
	for row in rows:
		if row['id'] in seen:
			continue
		seen.add(row['id'])
		fresh.append(row)
	if fresh:
		path.parent.mkdir(parents=True, exist_ok=True)
		with path.open('a', encoding='utf-8') as handle:
			for row in fresh:
				handle.write(json.dumps(row, ensure_ascii=False) + '\n')
	return len(fresh)


async def main() -> None:
	parser = argparse.ArgumentParser()
	parser.add_argument('--limit', type=int, default=100, help='entries per query')
	parser.add_argument('--kinds', nargs='*', default=list(KINDS), choices=list(KINDS))
	parser.add_argument('--tasks', nargs='*', default=list(GENERATIVE_TASKS))
	args = parser.parse_args()

	now = dt.datetime.now(dt.timezone.utc).isoformat()
	snap_dir = OUT_DIR / 'snapshots' / dt.date.today().isoformat()
	snap_dir.mkdir(parents=True, exist_ok=True)
	totals: dict[str, int] = {}

	async with aiohttp.ClientSession(headers=HEADERS) as session:
		for kind in args.kinds:
			rows: list[dict] = []
			for sort in SORTS:
				# Overall leaders, then the same axis narrowed to each generative task.
				queries: list[tuple[str, str | None]] = [(f'{API}/{kind}?sort={sort}&direction=-1&limit={args.limit}', None)]
				if kind == 'models':
					queries += [
						(f'{API}/models?pipeline_tag={task}&sort={sort}&direction=-1&limit={args.limit}', task)
						for task in args.tasks
					]
				for url, task in queries:
					entries = await fetch(session, url) or []
					rows.extend(r for r in (flatten(e, kind, sort, task, now) for e in entries) if r)
					await asyncio.sleep(DELAY)
			unique = {row['id']: row for row in rows}
			(snap_dir / f'{kind}.json').write_text(
				json.dumps({'kind': kind, 'collected_at': now, 'entries': list(unique.values())}, ensure_ascii=False, indent=1),
				encoding='utf-8',
			)
			new = append_deduped(OUT_DIR / f'{kind}.jsonl', list(unique.values()))
			totals[kind] = len(unique)
			print(f'{kind}: {len(unique)} unique (+{new} new)')
			if kind == 'models':
				tasks = collections.Counter(r['pipeline_tag'] for r in unique.values() if r.get('pipeline_tag'))
				for task, count in tasks.most_common(6):
					print(f'    {task:24} {count}')
				top = sorted((r for r in unique.values() if isinstance(r.get('downloads'), int)), key=lambda r: -r['downloads'])[:5]
				for row in top:
					print(f'    {row["id"][:44]:46} {row["downloads"]:>12,} dl  {row["likes"] or 0:>6} likes')

	print(f'\nDONE -> {OUT_DIR} ({", ".join(f"{k}={v}" for k, v in totals.items())})')


if __name__ == '__main__':
	asyncio.run(main())
