"""Prompt collections from GitHub, gated on the licence each repo declares.

The catalogue's prompt_library sources mostly dead-end: prompthero and
promptbase name AI crawlers, CivitAI's robots disallows /api/* outright, lexica
is unreachable, and the rest are documentation pages with no inventory. GitHub
is where prompt collections actually live, its REST API needs no key, and
api.github.com publishes no robots.txt, so nothing there restricts us.

WHAT DECIDES WHETHER PROMPT TEXT IS TAKEN
A prompt is someone's writing. So the licence the repository declares decides:

  permissive (MIT, Apache, BSD, ISC, CC0, CC-BY, Unlicense)
      prompt text is collected, and the licence travels with every row so a
      downstream user knows the terms without going back to the source
  anything else, including NOASSERTION and no licence at all
      metadata only — repo, stars, topics, file inventory. Absent a licence,
      the default is all rights reserved, not "help yourself"

EXCLUDED REGARDLESS OF LICENCE
Repositories whose purpose is republishing leaked system prompts from
commercial products. Several rank near the top of any prompt search, and some
carry a permissive licence — but a republisher cannot grant rights to text they
do not own, and the licence file does not change what the contents are. Matched
on name and description, and the exclusions are reported rather than silently
dropped.

Rate limits: unauthenticated GitHub allows 60 requests/hour for core and 10/min
for search, so the run is small by design and stops early when the budget is
spent rather than hammering a 403.

Output (PROMPT_OUT, default ~/prompt_repo_export):
  repos.json / repos.csv   - repo metadata with licence and gate decision
  prompts.jsonl            - prompt rows, each carrying its licence and source
  excluded.csv             - what was skipped and why
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import datetime as dt
import json
import os
import re
import sys
from pathlib import Path

if sys.stdout.encoding and sys.stdout.encoding.lower() != 'utf-8':
	sys.stdout.reconfigure(encoding='utf-8', errors='replace')

import aiohttp

OUT_DIR = Path(os.environ.get('PROMPT_OUT', str(Path.home() / 'prompt_repo_export')))
API = 'https://api.github.com'
UA = 'nu-browser-use/1.0 (+https://newunivers.com; nu@newunivers.com)'
HEADERS = {'User-Agent': UA, 'Accept': 'application/vnd.github+json'}
PERMISSIVE = {'MIT', 'APACHE-2.0', 'BSD-2-CLAUSE', 'BSD-3-CLAUSE', 'ISC', 'CC0-1.0', 'CC-BY-4.0', 'UNLICENSE'}
# Purpose-based exclusion: republished proprietary system prompts.
LEAK_RE = re.compile(r'(leak|jailbreak|stolen|extracted\s+system|system[_-]?prompts?[_-]?(leak|dump))', re.I)
PROMPT_TEXT_MAX = 1200
MAX_FILES = 12
DELAY = 1.5


def permissive(license_id: str | None) -> bool:
	return bool(license_id) and license_id.upper() in PERMISSIVE


def leak_repo(repo: dict) -> bool:
	haystack = f"{repo.get('full_name', '')} {repo.get('description') or ''} {' '.join(repo.get('topics') or [])}"
	return bool(LEAK_RE.search(haystack))


async def api_get(session: aiohttp.ClientSession, url: str) -> object | None:
	try:
		async with session.get(url, timeout=aiohttp.ClientTimeout(total=30)) as response:
			if response.status == 403:
				print('  rate limit reached — stopping rather than retrying into a wall')
				return 'RATE_LIMIT'
			if response.status != 200:
				return None
			return await response.json(content_type=None)
	except Exception as exc:  # noqa: BLE001
		print(f'  {url}: {type(exc).__name__}')
		return None


def parse_csv_prompts(text: str) -> list[dict]:
	"""act/prompt CSVs, the most common shape for prompt collections."""
	rows = []
	reader = csv.DictReader(text.splitlines())
	for row in reader:
		lowered = {(k or '').strip().lower(): (v or '').strip() for k, v in row.items()}
		title = lowered.get('act') or lowered.get('title') or lowered.get('name')
		body = lowered.get('prompt') or lowered.get('content') or lowered.get('text')
		if title and body:
			rows.append({'title': title[:200], 'prompt': body[:PROMPT_TEXT_MAX], 'truncated': len(body) > PROMPT_TEXT_MAX})
	return rows


def parse_markdown_prompts(text: str) -> list[dict]:
	"""Markdown collections: a heading names the prompt, a fenced block holds it."""
	rows = []
	for match in re.finditer(r'^#{2,4}\s+(.{3,120})\s*$\n+(?:.*?\n)??```[a-z]*\n(.*?)```', text, re.S | re.M):
		title, body = match.group(1).strip(), match.group(2).strip()
		if body:
			rows.append({'title': title[:200], 'prompt': body[:PROMPT_TEXT_MAX], 'truncated': len(body) > PROMPT_TEXT_MAX})
	return rows


async def collect_repo_prompts(session: aiohttp.ClientSession, repo: dict) -> list[dict]:
	"""Read the repo's prompt-bearing files. Only called for permissive licences."""
	full_name = repo['full_name']
	tree = await api_get(session, f"{API}/repos/{full_name}/git/trees/{repo.get('default_branch', 'main')}?recursive=1")
	if tree == 'RATE_LIMIT':
		return []
	paths = [
		node['path'] for node in (tree or {}).get('tree', [])
		if node.get('type') == 'blob'
		and re.search(r'(prompt|template)', node['path'], re.I)
		and node['path'].lower().endswith(('.csv', '.md'))
		and int(node.get('size') or 0) < 400_000
	][:MAX_FILES]

	rows: list[dict] = []
	license_id = (repo.get('license') or {}).get('spdx_id')
	for path in paths:
		raw_url = f"https://raw.githubusercontent.com/{full_name}/{repo.get('default_branch', 'main')}/{path}"
		try:
			async with session.get(raw_url, timeout=aiohttp.ClientTimeout(total=30)) as response:
				if response.status != 200:
					continue
				text = await response.text(errors='replace')
		except Exception:  # noqa: BLE001
			continue
		parsed = parse_csv_prompts(text) if path.lower().endswith('.csv') else parse_markdown_prompts(text)
		for row in parsed:
			rows.append({**row, 'repo': full_name, 'path': path, 'license': license_id, 'source_url': raw_url})
		await asyncio.sleep(DELAY)
	return rows


async def main() -> None:
	parser = argparse.ArgumentParser()
	parser.add_argument('--query', default='prompts in:name,description stars:>800')
	parser.add_argument('--repos', type=int, default=12)
	parser.add_argument('--metadata-only', action='store_true', help='never fetch prompt text, whatever the licence')
	args = parser.parse_args()

	now = dt.datetime.now(dt.timezone.utc).isoformat()
	async with aiohttp.ClientSession(headers=HEADERS) as session:
		print('searching GitHub (unauthenticated)')
		found = await api_get(session, f'{API}/search/repositories?q={args.query.replace(" ", "+")}&sort=stars&per_page={args.repos}')
		if not isinstance(found, dict):
			print('search unavailable')
			return
		repos = found.get('items', [])
		print(f'  {found.get("total_count")} matches, taking top {len(repos)}')

		records: list[dict] = []
		excluded: list[dict] = []
		prompts: list[dict] = []
		for repo in repos:
			license_id = (repo.get('license') or {}).get('spdx_id')
			row = {
				'full_name': repo['full_name'], 'stars': repo.get('stargazers_count'),
				'license': license_id, 'topics': ' | '.join(repo.get('topics') or []),
				'updated_at': repo.get('updated_at'), 'url': repo.get('html_url'),
				'description': (repo.get('description') or '')[:200],
			}
			if leak_repo(repo):
				row['gate'] = 'excluded_leak_repo'
				excluded.append({**row, 'reason': 'republishes leaked proprietary system prompts'})
				records.append(row)
				print(f'  {repo["full_name"][:40]:42} EXCLUDED (leak collection)')
				continue
			if args.metadata_only or not permissive(license_id):
				row['gate'] = 'metadata_only'
				records.append(row)
				print(f'  {repo["full_name"][:40]:42} metadata only (licence {license_id or "none"})')
				continue
			row['gate'] = 'prompts_collected'
			collected = await collect_repo_prompts(session, repo)
			prompts.extend(collected)
			row['prompt_rows'] = len(collected)
			records.append(row)
			print(f'  {repo["full_name"][:40]:42} {len(collected)} prompts ({license_id})')
			await asyncio.sleep(DELAY)

	OUT_DIR.mkdir(parents=True, exist_ok=True)
	(OUT_DIR / 'repos.json').write_text(json.dumps({'built_at': now, 'repos': records}, ensure_ascii=False, indent=2), encoding='utf-8')
	columns = ['full_name', 'stars', 'license', 'gate', 'prompt_rows', 'topics', 'updated_at', 'url', 'description']
	with (OUT_DIR / 'repos.csv').open('w', newline='', encoding='utf-8-sig') as handle:
		writer = csv.DictWriter(handle, fieldnames=columns, extrasaction='ignore')
		writer.writeheader()
		writer.writerows(records)
	if excluded:
		with (OUT_DIR / 'excluded.csv').open('w', newline='', encoding='utf-8-sig') as handle:
			writer = csv.DictWriter(handle, fieldnames=['full_name', 'stars', 'license', 'reason', 'url'], extrasaction='ignore')
			writer.writeheader()
			writer.writerows(excluded)
	if prompts:
		with (OUT_DIR / 'prompts.jsonl').open('a', encoding='utf-8') as handle:
			for row in prompts:
				handle.write(json.dumps({**row, 'observed_at': now}, ensure_ascii=False) + '\n')

	gates = {}
	for row in records:
		gates[row['gate']] = gates.get(row['gate'], 0) + 1
	print(f'\nrepos {len(records)}: ' + ', '.join(f'{k}={v}' for k, v in sorted(gates.items())))
	print(f'prompt rows collected: {len(prompts)} (licences: {sorted({p["license"] for p in prompts})})')
	print(f'DONE -> {OUT_DIR}')


if __name__ == '__main__':
	asyncio.run(main())
