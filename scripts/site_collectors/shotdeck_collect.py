"""ShotDeck authenticated collector.

Drives an already-logged-in Chrome over CDP and pulls, entirely through the page's own fetch()
(so v20 App-Bound cookies apply without extraction):

  1. account.json  - the signed-in user's own account + subscription details
  2. decks.json    - the user's decks (created/saved)
  3. filters.json  - the complete search filter vocabulary (every facet + option value)
  4. shots.json/csv- a bounded, representative sample of browse shots with full DP metadata
  5. thumbs/       - each collected shot's thumbnail image

Scope: the library holds millions of shots; SHOT_LIMIT bounds the per-shot detail+thumbnail pull.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import csv
import json
import os
from pathlib import Path

import aiohttp
from cdp_use import CDPClient

CDP_HTTP = os.environ.get('BROWSER_USE_CDP_HTTP', 'http://127.0.0.1:9222')
OUT_DIR = Path(os.environ.get('SHOTDECK_OUT', str(Path.home() / 'shotdeck_export')))
BATCH = 36


class Page:
	"""Thin CDP page wrapper that evaluates JS in the authenticated ShotDeck tab."""

	def __init__(self, client: CDPClient, session_id: str) -> None:
		self._client = client
		self._sid = session_id

	async def ev(self, expression: str):
		"""Evaluate an expression, awaiting promises and returning JSON-serializable values."""
		result = await self._client.send.Runtime.evaluate(
			params={'expression': expression, 'returnByValue': True, 'awaitPromise': True},
			session_id=self._sid,
		)
		if 'exceptionDetails' in result:
			raise RuntimeError(result['exceptionDetails'].get('text', 'js error'))
		return result.get('result', {}).get('value')


# --- JS payloads (run inside the page) -------------------------------------------------------

JS_PARSE_CARDS = r"""
(html => {
	const doc = new DOMParser().parseFromString(html, 'text/html');
	const out = [];
	doc.querySelectorAll('.outerimage[data-shotid]').forEach(el => {
		const a = el.querySelector('a.gallerythumb');
		const titleLink = el.querySelector('.moviedetails .gallerytitle a');
		const movieHref = titleLink ? (titleLink.getAttribute('href') || '') : '';
		const movieMatch = movieHref.match(/\/movie\/(\d+)~/);
		out.push({
			shotid: el.getAttribute('data-shotid'),
			title: titleLink ? titleLink.textContent.trim() : '',
			movie_id: movieMatch ? movieMatch[1] : '',
			year: el.getAttribute('data-titleyear') || '',
			deckid: el.getAttribute('data-deckid') || '',
			clip: el.getAttribute('data-clip') || '',
			shot_status: el.getAttribute('data-shot-status') || '',
			content_status: el.getAttribute('data-title-content-status') || '',
			resolution: a ? (a.getAttribute('data-size') || '') : '',
			filename: a ? (a.getAttribute('data-filename') || '') : '',
			thumb: (el.querySelector('img.still')||{}).getAttribute ? el.querySelector('img.still').getAttribute('src') : '',
		});
	});
	// The next batch URL is embedded in an inline handler: nextbatchClick('id','<url>')
	const m = html.match(/nextbatchClick\([^,]+,\s*'([^']+)'\)/);
	return JSON.stringify({ cards: out, nextUrl: m ? m[1] : null });
})(arguments0placeholder)
"""

JS_PARSE_DETAIL = r"""
(html => {
	const doc = new DOMParser().parseFromString(html, 'text/html');
	const fields = {};
	doc.querySelectorAll('.detail-group').forEach(g => {
		const key = (g.querySelector('.detail-type')||{}).textContent;
		const val = g.querySelector('.details');
		if (!key || !val) return;
		const k = key.replace(/:\s*$/,'').trim();
		const links = [...val.querySelectorAll('a')].map(a => a.textContent.trim()).filter(Boolean);
		fields[k] = links.length ? links : val.textContent.trim().replace(/\s+/g,' ');
	});
	const palette = [...doc.querySelectorAll('.palette a')].map(a => {
		const m = (a.getAttribute('style')||'').match(/background-color:\s*(#[0-9a-fA-F]+)/);
		return m ? m[1] : null;
	}).filter(Boolean);
	return JSON.stringify({ fields, palette });
})(arguments0placeholder)
"""

JS_FILTERS = r"""
(() => {
	const facets = {};
	document.querySelectorAll('#filterAccordian input[type=checkbox][name]').forEach(i => {
		const name = i.getAttribute('name');
		const value = i.getAttribute('value');
		if (!name || value == null) return;
		(facets[name] = facets[name] || []);
		if (!facets[name].includes(value)) facets[name].push(value);
	});
	return JSON.stringify(facets);
})()
"""

JS_ACCOUNT = r"""
fetch('/account').then(r=>r.text()).then(html => {
	const doc = new DOMParser().parseFromString(html, 'text/html');
	const val = n => (doc.querySelector(`input[name="${n}"]`)||{}).value || '';
	// Detached DOMParser docs have no layout, so innerText is empty; use textContent collapsed.
	const bodyText = doc.body.textContent.replace(/\s+/g,' ');
	const grab = re => ((bodyText.match(re)||[])[1] || '').trim();
	const accountType = (html.match(/accountType\s*=\s*'([^']*)'/)||[])[1] || '';
	const subId = (html.match(/subscriptionId\s*=\s*`([^`]*)`/)||[])[1] || '';
	const subVal = (html.match(/subscriptionValue\s*=\s*"([^"]*)"/)||[])[1] || '';
	const subInt = (html.match(/subscriptionInterval\s*=\s*"([^"]*)"/)||[])[1] || '';
	return JSON.stringify({
		name: val('name'), email: val('email'), phone: val('phone_number'),
		account_type: accountType,
		twofa_enabled: /2FA is enabled/i.test(bodyText),
		subscription_status: grab(/Subscription Status:\s*(\w+)/),
		subscription_plan: grab(/Subscription Plan:\s*([^$]+?)\s*\$/),
		subscription_price: subVal ? `$${subVal} / ${subInt}` : '',
		billing_cycle_end: grab(/Billing Cycle End:\s*(.+?)\s*You will/),
		card: grab(/Card:\s*(.+?)\s*change/),
		subscription_id: subId,
	});
}).catch(e => JSON.stringify({error: String(e)}))
"""

JS_DECKS = r"""
fetch('/browse/decks').then(r=>r.text()).then(html => {
	const doc = new DOMParser().parseFromString(html, 'text/html');
	const tree = doc.querySelector('#offcanvasDecks') || doc;
	const decks = [];
	// The deck tree lists each deck as an <li> carrying shotcount/rights/shared data attributes.
	tree.querySelectorAll('li[data-shotcount]').forEach(li => {
		const name = li.textContent.replace(/\s+/g,' ').trim();
		if (!name) return;
		decks.push({
			name,
			shots: li.getAttribute('data-shotcount') || '',
			rights: li.getAttribute('data-rights') || '',
			shared: li.getAttribute('data-shared') === 'true',
			is_subdeck: li.getAttribute('data-issubdeck') === '1',
			child_decks: li.getAttribute('data-childdecks') || '0',
			deckid: li.getAttribute('data-deckid') || li.id || '',
		});
	});
	return JSON.stringify({ count: decks.length, decks });
}).catch(e => JSON.stringify({error: String(e)}))
"""


JS_MENU = r"""
(() => {
	const acc = document.querySelector('#filterAccordian');
	if (!acc) return JSON.stringify({error: 'no #filterAccordian'});
	const IGNORE = new Set(['accordion-item','lone','show','collapse','collapsed','collapsing','card','active']);
	const categories = [];
	acc.querySelectorAll(':scope > .accordion-item').forEach((item, idx) => {
		// The metatype is the accordion-item's own class token (e.g. genre, shot_type).
		const metatype = item.className.split(/\s+/).find(c => c && !IGNORE.has(c)) || `cat${idx}`;
		const header = (item.querySelector('.accordion-header, .accordion-button, button, h3, h4')
			|| {}).textContent || metatype;
		const options = [];
		const controls = new Set();
		item.querySelectorAll('input[name]').forEach(inp => {
			const type = inp.getAttribute('type') || 'text';
			controls.add(type);
			if (type !== 'checkbox' && type !== 'radio') return;
			const value = inp.getAttribute('value');
			if (value == null || value === '') return;
			const label = (inp.closest('label')?.textContent
				|| inp.nextElementSibling?.textContent || value).replace(/\s+/g,' ').trim();
			// Values like "Movie/TV - Action" encode a subgroup before " - ".
			const dash = value.indexOf(' - ');
			const subgroup = dash > 0 ? value.slice(0, dash) : '';
			options.push({ metatype: inp.getAttribute('name'), value, label, subgroup });
		});
		categories.push({
			order: idx + 1,
			metatype,
			header: header.replace(/\s+/g,' ').trim(),
			control_types: [...controls],
			option_count: options.length,
			options,
		});
	});
	return JSON.stringify({ category_count: categories.length, categories });
})()
"""


async def fetch_html(page: Page, path: str) -> str:
	"""Fetch a URL via the page's own session and return its text body."""
	return await page.ev(f"fetch({json.dumps(path)}).then(r=>r.text()).catch(e=>'ERR '+e)")


async def parse_with(page: Page, js: str, html: str):
	"""Run a parser JS payload against an HTML string passed as a JSON literal."""
	expr = js.replace('arguments0placeholder', json.dumps(html))
	return json.loads(await page.ev(expr))


async def collect_shots(page: Page, limit: int) -> list[dict]:
	"""Follow the searchstillsajax nextbatch chain until `limit` unique shots are gathered."""
	shots: list[dict] = []
	seen: set[str] = set()
	url = '/browse/searchstillsajax'
	while url and len(shots) < limit:
		html = await fetch_html(page, url)
		parsed = await parse_with(page, JS_PARSE_CARDS, html)
		for card in parsed['cards']:
			sid = card['shotid']
			if sid in seen:
				continue
			seen.add(sid)
			shots.append(card)
			if len(shots) >= limit:
				break
		url = parsed['nextUrl']
		print(f'  ...{len(shots)} shots (next: {url})')
		await asyncio.sleep(0.4)
	return shots


async def enrich_details(page: Page, shots: list[dict]) -> None:
	"""Fetch and merge full DP metadata for each shot, a few requests at a time."""
	semaphore = asyncio.Semaphore(4)

	async def one(shot: dict) -> None:
		async with semaphore:
			try:
				html = await fetch_html(page, f"/browse/shotdetailsajax/image/{shot['shotid']}")
				detail = await parse_with(page, JS_PARSE_DETAIL, html)
				shot['metadata'] = detail['fields']
				shot['palette'] = detail['palette']
			except Exception as error:  # noqa: BLE001 - record and continue
				shot['metadata'] = {'__error__': str(error)}
				shot['palette'] = []

	# Sequential-ish batches to stay polite.
	for i in range(0, len(shots), 4):
		await asyncio.gather(*(one(s) for s in shots[i : i + 4]))
		print(f'  ...detail {min(i + 4, len(shots))}/{len(shots)}')
		await asyncio.sleep(0.3)


async def download_thumbs(page: Page, shots: list[dict], thumbs_dir: Path) -> int:
	"""Download each shot's thumbnail via the page session (base64) into thumbs_dir."""
	thumbs_dir.mkdir(parents=True, exist_ok=True)
	saved = 0
	semaphore = asyncio.Semaphore(6)

	async def one(shot: dict) -> None:
		nonlocal saved
		src = shot.get('thumb') or f"/assets/images/stills/smthumb/small_{shot['shotid']}.jpg"
		async with semaphore:
			try:
				data_url = await page.ev(
					f"""
					fetch({json.dumps(src)}).then(r=>r.blob()).then(b=>new Promise(res=>{{
						const fr=new FileReader(); fr.onload=()=>res(fr.result); fr.readAsDataURL(b);
					}})).catch(e=>'ERR '+e)
					"""
				)
				if isinstance(data_url, str) and data_url.startswith('data:'):
					payload = data_url.split(',', 1)[1]
					(thumbs_dir / f"{shot['shotid']}.jpg").write_bytes(base64.b64decode(payload))
					saved += 1
			except Exception:  # noqa: BLE001
				pass

	for i in range(0, len(shots), 6):
		await asyncio.gather(*(one(s) for s in shots[i : i + 6]))
		print(f'  ...thumb {min(i + 6, len(shots))}/{len(shots)}')
	return saved


def _safe_slug(text: str) -> str:
	"""Turn a metatype into a filesystem-safe folder name."""
	slug = ''.join(c if c.isalnum() else '_' for c in text.strip().lower())
	return '_'.join(filter(None, slug.split('_'))) or 'category'


def write_menu(menu: dict) -> None:
	"""Write the left-side filter menu as one folder per category, plus an index and a tree.

	Layout:
	  menu/menu.json                      - full ordered menu
	  menu/menu.md                        - human-readable folder tree
	  menu/<NN>_<metatype>/options.json   - that category's options (grouped by subgroup)
	  menu/<NN>_<metatype>/options.csv    - flat subgroup,label,value rows
	"""
	menu_dir = OUT_DIR / 'menu'
	menu_dir.mkdir(parents=True, exist_ok=True)
	(menu_dir / 'menu.json').write_text(json.dumps(menu, ensure_ascii=False, indent=2), encoding='utf-8')

	tree_lines = ['# ShotDeck Left-Side Filter Menu', '', f"Total categories: {menu.get('category_count', 0)}", '']
	for cat in menu.get('categories', []):
		folder = menu_dir / f"{cat['order']:02d}_{_safe_slug(cat['metatype'])}"
		folder.mkdir(parents=True, exist_ok=True)

		# Group options by subgroup for the per-category JSON.
		grouped: dict[str, list[dict]] = {}
		for opt in cat['options']:
			grouped.setdefault(opt['subgroup'], []).append({'label': opt['label'], 'value': opt['value'], 'metatype': opt['metatype']})
		(folder / 'options.json').write_text(
			json.dumps(
				{'metatype': cat['metatype'], 'header': cat['header'], 'control_types': cat['control_types'], 'subgroups': grouped},
				ensure_ascii=False, indent=2,
			),
			encoding='utf-8',
		)
		with (folder / 'options.csv').open('w', newline='', encoding='utf-8-sig') as handle:
			writer = csv.writer(handle)
			writer.writerow(['subgroup', 'label', 'value', 'metatype'])
			for opt in cat['options']:
				writer.writerow([opt['subgroup'], opt['label'], opt['value'], opt['metatype']])

		# Menu tree lines.
		control_note = '' if cat['control_types'] == ['checkbox'] else f" [{', '.join(cat['control_types'])}]"
		tree_lines.append(f"- **{cat['header']}** (`{cat['metatype']}`, {cat['option_count']} options){control_note}")
		last_sub = None
		for opt in cat['options']:
			if opt['subgroup'] and opt['subgroup'] != last_sub:
				tree_lines.append(f"    - _{opt['subgroup']}_")
				last_sub = opt['subgroup']
			indent = '        ' if opt['subgroup'] else '    '
			tree_lines.append(f"{indent}- {opt['label']}")
	(menu_dir / 'menu.md').write_text('\n'.join(tree_lines) + '\n', encoding='utf-8')


def write_outputs(account, decks, filters, shots) -> None:
	"""Persist all collected data to OUT_DIR as JSON + a flat shots CSV."""
	OUT_DIR.mkdir(parents=True, exist_ok=True)
	(OUT_DIR / 'account.json').write_text(json.dumps(account, ensure_ascii=False, indent=2), encoding='utf-8')
	(OUT_DIR / 'decks.json').write_text(json.dumps(decks, ensure_ascii=False, indent=2), encoding='utf-8')
	(OUT_DIR / 'filters.json').write_text(json.dumps(filters, ensure_ascii=False, indent=2), encoding='utf-8')
	(OUT_DIR / 'shots.json').write_text(json.dumps(shots, ensure_ascii=False, indent=2), encoding='utf-8')

	meta_keys: list[str] = []
	for shot in shots:
		for key in (shot.get('metadata') or {}):
			if key not in meta_keys:
				meta_keys.append(key)
	base_cols = ['shotid', 'title', 'movie_id', 'year', 'resolution', 'filename', 'shot_status', 'content_status', 'deckid']
	with (OUT_DIR / 'shots.csv').open('w', newline='', encoding='utf-8-sig') as handle:
		writer = csv.writer(handle)
		writer.writerow(base_cols + meta_keys + ['palette'])
		for shot in shots:
			meta = shot.get('metadata') or {}
			row = [shot.get(c, '') for c in base_cols]
			for key in meta_keys:
				value = meta.get(key, '')
				row.append(', '.join(value) if isinstance(value, list) else value)
			row.append(' '.join(shot.get('palette') or []))
			writer.writerow(row)


async def main() -> None:
	parser = argparse.ArgumentParser()
	parser.add_argument('--limit', type=int, default=150)
	parser.add_argument('--no-thumbs', action='store_true')
	args = parser.parse_args()

	async with aiohttp.ClientSession() as http:
		async with http.get(f'{CDP_HTTP}/json/version') as response:
			ws_url = (await response.json())['webSocketDebuggerUrl']

	async with CDPClient(ws_url) as client:
		targets = await client.send.Target.getTargets()
		target = next(t for t in targets['targetInfos'] if t['type'] == 'page' and 'shotdeck.com/browse' in t.get('url', ''))
		session = await client.send.Target.attachToTarget(params={'targetId': target['targetId'], 'flatten': True})
		page = Page(client, session['sessionId'])
		await client.send.Runtime.enable(session_id=session['sessionId'])

		print('[1/5] account')
		account = json.loads(await page.ev(JS_ACCOUNT))
		print('[2/5] decks')
		decks = json.loads(await page.ev(JS_DECKS))
		print('[3/6] filters')
		filters = json.loads(await page.ev(JS_FILTERS))
		print(f'      facets: {list(filters.keys())}')
		print('[4/6] left-side menu (categories -> folders)')
		menu = json.loads(await page.ev(JS_MENU))
		if 'error' in menu:
			# Menu lives on the browse page; navigate there if we are elsewhere, then retry.
			await page.ev("location.pathname.includes('/browse/stills') || (location.href='https://shotdeck.com/browse/stills')")
			await asyncio.sleep(5)
			menu = json.loads(await page.ev(JS_MENU))
		print(f"      categories: {menu.get('category_count')} -> {[c['metatype'] for c in menu.get('categories', [])]}")
		print(f'[5/6] shots (limit {args.limit})')
		shots = await collect_shots(page, args.limit)
		await enrich_details(page, shots)
		saved = 0
		if not args.no_thumbs:
			print('[6/6] thumbnails')
			saved = await download_thumbs(page, shots, OUT_DIR / 'thumbs')

		write_outputs(account, decks, filters, shots)
		write_menu(menu)
		print(f'\nDONE -> {OUT_DIR}')
		print(f'  account: {account.get("name")} / {account.get("email")} / {account.get("subscription_plan")}')
		print(f'  decks: {decks.get("count")} -> {[d["name"] for d in decks.get("decks", [])]}')
		print(f'  filter facets: {len(filters)}')
		print(f'  menu categories: {menu.get("category_count")} (folders under menu/)')
		print(f'  shots: {len(shots)} (with metadata)')
		print(f'  thumbnails saved: {saved}')


if __name__ == '__main__':
	asyncio.run(main())
