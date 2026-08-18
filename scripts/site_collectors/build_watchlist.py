"""Build the rights-protection watchlist WITH provenance.

The 2026-08-17 watch run matched 14 titles that could not be traced back to a
program, because the 08-14 all-locale programs.json had been overwritten by a
single-locale run and the watchlist stored bare titles. A title alone can't be
adjudicated against a same-name Korean webtoon; a title plus its program id,
platform and locale can.

Watchlist format (one work per line; newtoki_watch.py already reads this):
    <title>|<platform>|<program_id>|<locale>

Aliases per work become additional lines with the same provenance, so a match
on any translation points back to the exact program.

Inputs : vigloo programs.json (all-locale), reelshort books.json
Output : watchlist_provenanced.txt
"""

from __future__ import annotations

import json
from pathlib import Path

VIGLOO = Path(r'C:\Users\USER\vigloo_export\programs.json')
REELSHORT = Path(r'C:\Users\USER\reelshort_export\books.json')
OUT = Path(r'C:\Users\USER\watchlist_provenanced.txt')


def main() -> None:
	lines: list[str] = []
	seen: set[str] = set()

	def add(title: str, platform: str, program_id: str, locale: str) -> None:
		title = (title or '').strip()
		if not title or len(title) < 2:
			return
		key = f'{platform}|{program_id}|{locale}|{title}'
		if key in seen:
			return
		seen.add(key)
		lines.append(f'{title}|{platform}|{program_id}|{locale}')

	programs = json.loads(VIGLOO.read_text(encoding='utf-8'))
	for program in programs:
		program_id = str(program.get('id') or '')
		add(program.get('title'), 'vigloo', program_id, 'en')
		for locale, translation in (program.get('_translations') or {}).items():
			add(translation.get('title'), 'vigloo', program_id, locale)

	books = json.loads(REELSHORT.read_text(encoding='utf-8'))
	for book in books:
		add(book.get('book_title'), 'reelshort', str(book.get('book_id') or ''), 'en')

	OUT.write_text('\n'.join(lines) + '\n', encoding='utf-8')
	print(f'{len(lines)} entries -> {OUT}')
	print('sample:', lines[:3])


if __name__ == '__main__':
	main()
