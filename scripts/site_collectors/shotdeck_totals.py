"""Collect the shot count for every left-menu option (all 269) via searchpaneshottotalajax,
then write the totals back into the menu/ folder structure.

Reads the previously collected menu.json to enumerate options, so it never re-scrapes the menu.
The library holds millions of shots per option, so only per-option COUNTS are collected here.
"""

from __future__ import annotations

import asyncio
import csv
import json
import os
import re
from pathlib import Path

import aiohttp
from cdp_use import CDPClient

CDP_HTTP = os.environ.get('BROWSER_USE_CDP_HTTP', 'http://127.0.0.1:9222')
OUT_DIR = Path(os.environ.get('SHOTDECK_OUT', str(Path.home() / 'shotdeck_export')))
MENU_DIR = OUT_DIR / 'menu'

# The site itself skips live counts for these metatypes (loadShotTotal returns false).
SKIP_METATYPES = {'year', 'shade', 'rosco', 'rosco-search', 'shade_distance', 'shade_proportion', 'daterange'}


def _safe_slug(text: str) -> str:
	slug = ''.join(c if c.isalnum() else '_' for c in text.strip().lower())
	return '_'.join(filter(None, slug.split('_'))) or 'category'


async def main() -> None:
	menu = json.loads((MENU_DIR / 'menu.json').read_text(encoding='utf-8'))

	async with aiohttp.ClientSession() as http:
		async with http.get(f'{CDP_HTTP}/json/version') as response:
			ws_url = (await response.json())['webSocketDebuggerUrl']

	async with CDPClient(ws_url) as client:
		targets = await client.send.Target.getTargets()
		page = next(t for t in targets['targetInfos'] if t['type'] == 'page' and 'shotdeck.com/browse' in t.get('url', ''))
		session = await client.send.Target.attachToTarget(params={'targetId': page['targetId'], 'flatten': True})
		sid = session['sessionId']
		await client.send.Runtime.enable(session_id=sid)

		async def post_total(metaname: str, metaval: str) -> str:
			expr = (
				"fetch('/browse/searchpaneshottotalajax', {method:'POST',"
				"headers:{'Content-Type':'application/x-www-form-urlencoded','X-Requested-With':'XMLHttpRequest'},"
				f"body:new URLSearchParams({{metaname:{json.dumps(metaname)},metaval:{json.dumps(metaval)}}}).toString()"
				"}).then(r=>r.text()).then(t=>t.trim()).catch(e=>'ERR '+e)"
			)
			r = await client.send.Runtime.evaluate(
				params={'expression': expr, 'returnByValue': True, 'awaitPromise': True}, session_id=sid
			)
			return r.get('result', {}).get('value') or ''

		semaphore = asyncio.Semaphore(5)
		done = 0
		total_options = sum(len(c['options']) for c in menu['categories'])

		async def fill(option: dict) -> None:
			nonlocal done
			async with semaphore:
				if option['metatype'] in SKIP_METATYPES:
					option['shots'] = ''
					option['shots_num'] = None
				else:
					raw = await post_total(option['metatype'], option['value'])
					digits = re.sub(r'[^\d]', '', raw)
					option['shots'] = f'{int(digits):,}' if digits else ''
					option['shots_num'] = int(digits) if digits else None
			done += 1
			if done % 20 == 0 or done == total_options:
				print(f'  ...{done}/{total_options}')

		# Fill counts for every option across all categories.
		await asyncio.gather(*(fill(opt) for cat in menu['categories'] for opt in cat['options']))

	# --- Rewrite menu outputs with counts -------------------------------------------------
	menu['collected'] = 'per-option shot totals via searchpaneshottotalajax'
	(MENU_DIR / 'menu.json').write_text(json.dumps(menu, ensure_ascii=False, indent=2), encoding='utf-8')

	combined_rows: list[list] = []
	tree = ['# ShotDeck Left-Side Filter Menu (with shot counts)', '',
		f"Total categories: {menu['category_count']}", '',
		'_Counts are total shots in the library for each option. Year/Rosco/Color-picker have no live count (site-side)._', '']

	for cat in menu['categories']:
		folder = MENU_DIR / f"{cat['order']:02d}_{_safe_slug(cat['metatype'])}"
		folder.mkdir(parents=True, exist_ok=True)

		grouped: dict[str, list[dict]] = {}
		for opt in cat['options']:
			grouped.setdefault(opt['subgroup'], []).append(
				{'label': opt['label'], 'value': opt['value'], 'shots': opt.get('shots', ''), 'shots_num': opt.get('shots_num')}
			)
		(folder / 'options.json').write_text(
			json.dumps({'metatype': cat['metatype'], 'header': cat['header'],
				'control_types': cat['control_types'], 'subgroups': grouped}, ensure_ascii=False, indent=2),
			encoding='utf-8',
		)
		with (folder / 'options.csv').open('w', newline='', encoding='utf-8-sig') as handle:
			writer = csv.writer(handle)
			writer.writerow(['subgroup', 'label', 'value', 'shots', 'metatype'])
			for opt in cat['options']:
				writer.writerow([opt['subgroup'], opt['label'], opt['value'], opt.get('shots', ''), opt['metatype']])
				combined_rows.append([cat['header'], cat['metatype'], opt['subgroup'], opt['label'], opt['value'], opt.get('shots', ''), opt.get('shots_num')])

		tree.append(f"- **{cat['header']}** (`{cat['metatype']}`, {cat['option_count']} options)")
		last_sub = None
		for opt in cat['options']:
			if opt['subgroup'] and opt['subgroup'] != last_sub:
				tree.append(f"    - _{opt['subgroup']}_")
				last_sub = opt['subgroup']
			indent = '        ' if opt['subgroup'] else '    '
			count = f" — {opt['shots']} shots" if opt.get('shots') else ''
			tree.append(f"{indent}- {opt['label']}{count}")
	(MENU_DIR / 'menu.md').write_text('\n'.join(tree) + '\n', encoding='utf-8')

	with (MENU_DIR / 'menu_totals.csv').open('w', newline='', encoding='utf-8-sig') as handle:
		writer = csv.writer(handle)
		writer.writerow(['category', 'metatype', 'subgroup', 'label', 'value', 'shots', 'shots_num'])
		writer.writerows(combined_rows)

	counted = [r for r in combined_rows if r[6] is not None]
	print(f'\nDONE -> {MENU_DIR}')
	print(f'  options total: {len(combined_rows)}  |  with counts: {len(counted)}')
	top = sorted(counted, key=lambda r: r[6], reverse=True)[:5]
	for r in top:
		print(f"    {r[0]} / {r[3]}: {r[5]}")


if __name__ == '__main__':
	asyncio.run(main())
