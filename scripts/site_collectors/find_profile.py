"""Locate which Chrome profile holds shotdeck.com cookies (host_key is plaintext)."""

import os
import shutil
import sqlite3
import tempfile
from pathlib import Path

ROOT = Path(os.environ['LOCALAPPDATA']) / 'Google' / 'Chrome' / 'User Data'
NEEDLE = 'shotdeck'

for profile_dir in sorted(ROOT.iterdir()):
	if not profile_dir.is_dir():
		continue
	if profile_dir.name != 'Default' and not profile_dir.name.startswith('Profile '):
		continue
	cookies_db = profile_dir / 'Network' / 'Cookies'
	if not cookies_db.is_file():
		continue
	with tempfile.TemporaryDirectory() as tmp:
		copy = Path(tmp) / 'Cookies'
		try:
			shutil.copy2(cookies_db, copy)
		except OSError as error:
			print(f'{profile_dir.name}: copy failed: {error}')
			continue
		try:
			connection = sqlite3.connect(f'file:{copy}?mode=ro', uri=True)
			rows = connection.execute(
				'SELECT host_key, name, expires_utc FROM cookies WHERE host_key LIKE ?',
				(f'%{NEEDLE}%',),
			).fetchall()
			connection.close()
		except sqlite3.Error as error:
			print(f'{profile_dir.name}: sqlite error: {error}')
			continue
	if rows:
		names = sorted({row[1] for row in rows})
		hosts = sorted({row[0] for row in rows})
		print(f'>>> {profile_dir.name}: {len(rows)} shotdeck cookies')
		print(f'    hosts: {hosts}')
		print(f'    names: {names}')
