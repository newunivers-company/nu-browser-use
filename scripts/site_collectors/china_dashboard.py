"""China 5-axis intelligence dashboard.

Reads the five keyless China exports (JJWXC / NetEase / Bilibili / 小红书 /
微博热搜) and renders one markdown overview: volume, engagement stats,
category distributions, and cross-axis notes. Pure local analysis — no
network, no new collection.

Output: prints the report; --write saves it next to the exports as
china_dashboard.md.
"""

from __future__ import annotations

import argparse
import collections
import json
import os
import sys
from pathlib import Path

if sys.stdout.encoding and sys.stdout.encoding.lower() != 'utf-8':
	sys.stdout.reconfigure(encoding='utf-8', errors='replace')


def _nas_root() -> Path:
	"""First reachable NAS root, same candidate order as stage_to_nas.sh.

	The WSL mount and the mapped drive letter are one share seen from different
	shells. Hardcoding the WSL path made this unrunnable from the Windows venv
	the cadence actually uses — every source loaded as empty and the report
	rendered zeros, which is the failure the collection policy names by name:
	reading silence as safety. Resolving instead means a missing NAS is loud.
	"""
	candidates = [
		os.environ.get('NAS_ROOT', ''),
		'/mnt/newunivers-sdb/nu-browser-use',
		'X:/nu-browser-use',
		'/x/nu-browser-use',
		'//192.168.0.136/sdb/nu-browser-use',
	]
	for candidate in candidates:
		if candidate and Path(candidate).is_dir():
			return Path(candidate)
	raise SystemExit(f'no NAS root reachable (tried: {[c for c in candidates if c]})')


def sources(nas: Path) -> dict[str, Path]:
	"""Resolved at call time, not import time: importing this module must not
	be able to kill the process just because a share is unmapped."""
	return {
		'jjwxc': nas / 'story_export' / 'jjwxc' / 'novels.json',
		'netease': nas / 'media_export' / 'netease' / 'songs.json',
		'bilibili': nas / 'content_update_export' / 'bilibili' / 'videos.json',
		'xiaohongshu': nas / 'content_update_export' / 'xiaohongshu' / 'posts.json',
		'weibo': nas / 'content_update_export' / 'weibo' / 'hotsearch.json',
	}


def load(path: Path) -> list[dict]:
	try:
		data = json.loads(path.read_text(encoding='utf-8'))
		return data if isinstance(data, list) else []
	except (OSError, json.JSONDecodeError):
		return []


def median(values: list[int]) -> int:
	values = sorted(values)
	return values[len(values) // 2] if values else 0


def build_report(nas: Path) -> str:
	paths = sources(nas)
	jjwxc = load(paths['jjwxc'])
	netease = load(paths['netease'])
	bilibili = load(paths['bilibili'])
	xhs = load(paths['xiaohongshu'])
	weibo = load(paths['weibo'])

	lines: list[str] = []
	lines.append('# 중국 인텔리전스 — 5축 통합 대시보드')
	lines.append('')
	lines.append('| 축 | 소스 | 레코드 | 핵심 지표 |')
	lines.append('|---|---|---|---|')
	lines.append(f'| 원작 IP | JJWXC 晋江 | {len(jjwxc):,} | 랭킹 진입 소설 |')
	lines.append(f'| 음악 | NetEase 网易云 | {len(netease):,} | 다중차트 교차 |')
	lines.append(f'| 콘텐츠 | Bilibili | {len(bilibili):,} | like/coin/danmaku |')
	lines.append(f'| 미감 | 小红书 RED | {len(xhs):,} | 카테고리·좋아요 |')
	lines.append(f'| zeitgeist | 微博热搜 | {len(weibo):,} | 검색 랭킹·카테고리 |')
	lines.append('')

	if weibo:
		lines.append('## 微博热搜 카테고리')
		lines.append('')
		for category, count in collections.Counter(w.get('category') for w in weibo if w.get('category')).most_common(8):
			lines.append(f'- {category}: {count}')
		hot = sorted(weibo, key=lambda w: -int(w.get('hot') or 0))[:3]
		lines.append('')
		lines.append('최고 열도: ' + ' / '.join(f'{w.get("word")}({int(w.get("hot") or 0):,})' for w in hot))
		lines.append('')

	if bilibili:
		likes = [int(v.get('like') or 0) for v in bilibili if v.get('like')]
		lines.append('## Bilibili engagement')
		lines.append('')
		lines.append(f'- like 최대 {max(likes):,} / 중앙값 {median(likes):,} (샘플 {len(likes)})')
		top = sorted(bilibili, key=lambda v: -int(v.get('like') or 0))[:3]
		for v in top:
			lines.append(f'- {v.get("desc", "")[:30]} — like {int(v.get("like") or 0):,}')
		lines.append('')

	if jjwxc:
		ranked = sum(1 for j in jjwxc if j.get('rankings'))
		lines.append('## JJWXC 원작 IP')
		lines.append('')
		lines.append(f'- {len(jjwxc):,}편, 랭킹 데이터 보유 {ranked:,}')
		lines.append('')

	if netease:
		multi_chart = sum(1 for n in netease if len(n.get('charts') or []) > 1)
		lines.append('## NetEase 음악')
		lines.append('')
		lines.append(f'- {len(netease):,}곡 중 {multi_chart}곡이 2개 이상 차트 교차 진입')
		lines.append('')

	if xhs:
		categories = collections.Counter(x.get('category') for x in xhs if x.get('category'))
		lines.append('## 小红书 카테고리')
		lines.append('')
		for category, count in categories.most_common(6):
			lines.append(f'- {category}: {count}')
		lines.append('')

	lines.append('## 활용 지점')
	lines.append('')
	lines.append('- JJWXC 랭킹 → 숏드라마 원작 후보 트래킹 (중국 원작 IP 조기 발견)')
	lines.append('- 微博热搜 카테고리 → 편성 장르 트렌드 (哪种 트로프가 지금 뜨는가)')
	lines.append('- 小红书 → 비주얼 레퍼런스/표지 미감 (Pinterest 중국 보완)')
	lines.append('- NetEase OST → 음악 편성 인텔리전스')
	return '\n'.join(lines)


def main() -> None:
	parser = argparse.ArgumentParser()
	parser.add_argument('--write', action='store_true', help='save as china_dashboard.md next to the exports')
	args = parser.parse_args()

	nas = _nas_root()
	report = build_report(nas)
	print(report)
	if args.write:
		destination = nas / 'china_dashboard.md'
		destination.write_text(report + '\n', encoding='utf-8')
		print(f'\n-> {destination}')


if __name__ == '__main__':
	main()
