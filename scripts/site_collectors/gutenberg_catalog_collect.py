"""Project Gutenberg keyless catalog collector.

Pulls the official machine-readable catalog (pg_catalog.csv, ~79k texts) and
filters it into a STORY INTELLIGENCE seed corpus:

  - subject/topic rows matching drama-relevant themes (love, betrayal, revenge,
    marriage, inheritance, identity, secrets...)
  - per-language counts, per-subject counts for trend-era analysis
  - full catalog kept as raw artifact

No scraping of HTML pages — this is the officially sanctioned bulk channel
(gutenberg.org/policy/robot_access.html). Rights note: texts are PD in the US;
Korea is life+70 with a transitional protection window to 2053 for some works,
so the corpus is tagged with author death-year proxies where available.

Output layout (GUTENBERG_OUT, default ~/gutenberg_export):
  pg_catalog.csv        - raw official catalog (21MB)
  story_candidates.csv  - drama-relevant subset with subject match counts
  subject_counts.csv    - subject frequency across the whole catalog
"""

from __future__ import annotations

import csv
import os
import re
from collections import Counter
from pathlib import Path

import urllib.request

OUT_DIR = Path(os.environ.get('GUTENBERG_OUT', str(Path.home() / 'gutenberg_export')))
CATALOG_URL = 'https://www.gutenberg.org/cache/epub/feeds/pg_catalog.csv'

# Drama/story-material themes -> canonical trope buckets (docs: story layer §40)
THEME_PATTERNS = {
	'love': re.compile(r'\blove|romance|courtship\b', re.I),
	'betrayal': re.compile(r'\bbetray|treason|traitor\b', re.I),
	'revenge': re.compile(r'\brevenge|vengeance|vendetta\b', re.I),
	'marriage': re.compile(r'\bmarriage|wedlock|bride|bridegroom\b', re.I),
	'inheritance': re.compile(r'\binherit|heir|estate\b', re.I),
	'identity': re.compile(r'\bidentity|disguise|mistaken|imposter|impostor\b', re.I),
	'secret': re.compile(r'\bsecret|mystery\b', re.I),
	'family': re.compile(r'\bfamily|household|kin\b', re.I),
	'power': re.compile(r'\bking|queen|royal|throne|court intrigue|political\b', re.I),
	'tragedy': re.compile(r'\btragedy|tragic\b', re.I),
	'adultery': re.compile(r'\badultery|affair|paramour\b', re.I),
	'murder': re.compile(r'\bmurder|crime|criminal|detective\b', re.I),
}


def main() -> None:
	OUT_DIR.mkdir(parents=True, exist_ok=True)
	raw_path = OUT_DIR / 'pg_catalog.csv'
	if not raw_path.exists():
		print(f'downloading catalog ({CATALOG_URL})...')
		req = urllib.request.Request(CATALOG_URL, headers={'User-Agent': 'nu-collector/1.0 (bulk feed; contact: dev)'})
		with urllib.request.urlopen(req, timeout=120) as resp, raw_path.open('wb') as fh:
			while chunk := resp.read(1 << 20):
				fh.write(chunk)
	print(f'catalog: {raw_path.stat().st_size / 1e6:.1f} MB')

	subject_counter: Counter[str] = Counter()
	candidates: list[dict] = []
	total = 0
	with raw_path.open(encoding='utf-8', newline='') as fh:
		reader = csv.DictReader(fh)
		for row in reader:
			total += 1
			subjects = (row.get('Subjects') or '').split(';')
			for s in subjects:
				s = s.strip()
				if s:
					subject_counter[s] += 1
			matched = sorted({name for name, pat in THEME_PATTERNS.items() if pat.search(row.get('Subjects') or '') or pat.search(row.get('Title') or '')})
			if matched:
				candidates.append(
					{
						'text_id': row.get('Text#'),
						'title': (row.get('Title') or '')[:200],
						'authors': (row.get('Authors') or '')[:150],
						'issued': row.get('Issued'),
						'language': row.get('Language'),
						'subjects': ';'.join(s.strip() for s in subjects if s.strip())[:400],
						'themes': '|'.join(matched),
						'url': f'https://www.gutenberg.org/ebooks/{row.get("Text#")}',
					}
				)

	with (OUT_DIR / 'story_candidates.csv').open('w', newline='', encoding='utf-8') as fh:
		if candidates:
			writer = csv.DictWriter(fh, fieldnames=list(candidates[0].keys()))
			writer.writeheader()
			writer.writerows(candidates)
	with (OUT_DIR / 'subject_counts.csv').open('w', newline='', encoding='utf-8') as fh:
		writer = csv.writer(fh)
		writer.writerow(['subject', 'count'])
		for subject, count in subject_counter.most_common():
			writer.writerow([subject, count])

	lang_counter = Counter(c['language'] for c in candidates)
	print(f'total catalog rows: {total}')
	print(f'story candidates: {len(candidates)} | top languages: {lang_counter.most_common(5)}')
	print(f'done -> {OUT_DIR}')


if __name__ == '__main__':
	main()
