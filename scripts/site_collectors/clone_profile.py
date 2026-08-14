"""Clone the logged-in Chrome session cookies into a dedicated CDP-enabled profile.

Chrome 136+ refuses --remote-debugging-port when --user-data-dir is the default one, so the
authenticated state has to be copied into a separate user-data-dir. App-Bound Encryption binds
the cookie key to chrome.exe + the Windows user, not to the profile path, so the same binary
relaunched as the same user can still decrypt the copied cookies.

Requires Chrome to be fully closed (the SQLite files are exclusively locked while it runs).
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

SOURCE_ROOT = Path(os.environ['LOCALAPPDATA']) / 'Google' / 'Chrome' / 'User Data'
TARGET_ROOT = Path.home() / '.browser-use-profiles' / 'shotdeck'
CANDIDATE_PROFILES = ('Default', 'Profile 23')
NEEDLE = 'shotdeck'

# Per-profile state that carries a logged-in session. Caches are deliberately excluded.
PROFILE_ITEMS = (
	'Network',  # Cookies plus any -journal/-wal sidecar that still holds uncommitted rows
	'Local Storage',
	'Session Storage',
	'IndexedDB',
	'Preferences',
	'Secure Preferences',
	'Web Data',
	'Login Data',
)


def shotdeck_cookie_count(cookies_db: Path) -> int | None:
	"""Count shotdeck cookies in a profile, or return None if the database is unreadable."""
	if not cookies_db.is_file():
		return None
	with tempfile.TemporaryDirectory() as tmp:
		copy = Path(tmp) / 'Cookies'
		try:
			shutil.copy2(cookies_db, copy)
			connection = sqlite3.connect(f'file:{copy}?mode=ro', uri=True)
			count = connection.execute(
				'SELECT COUNT(*) FROM cookies WHERE host_key LIKE ?', (f'%{NEEDLE}%',)
			).fetchone()[0]
			connection.close()
		except (OSError, sqlite3.Error) as error:
			print(f'  unreadable ({type(error).__name__}: {error})')
			return None
	return int(count)


def pick_source_profile() -> Path:
	"""Return the profile directory holding shotdeck cookies."""
	best: tuple[int, Path] | None = None
	for name in CANDIDATE_PROFILES:
		profile_dir = SOURCE_ROOT / name
		print(f'checking {name}...')
		count = shotdeck_cookie_count(profile_dir / 'Network' / 'Cookies')
		if count is None:
			print('  -> locked or missing; close Chrome completely and rerun')
			continue
		print(f'  -> {count} shotdeck cookies')
		if count and (best is None or count > best[0]):
			best = (count, profile_dir)
	if best is None:
		sys.exit('no readable profile contained shotdeck cookies')
	return best[1]


def copy_item(source: Path, destination: Path) -> None:
	"""Copy one file or directory, tolerating absent optional state."""
	if not source.exists():
		return
	destination.parent.mkdir(parents=True, exist_ok=True)
	if source.is_dir():
		shutil.copytree(source, destination, dirs_exist_ok=True)
	else:
		shutil.copy2(source, destination)
	print(f'  copied {source.name}')


def main() -> None:
	"""Clone the authenticated profile state into the CDP target directory."""
	source_profile = pick_source_profile()
	print(f'\nsource profile: {source_profile}')

	target_profile = TARGET_ROOT / 'Default'
	if TARGET_ROOT.exists():
		shutil.rmtree(TARGET_ROOT)
	target_profile.mkdir(parents=True)

	# Local State holds the App-Bound-wrapped cookie encryption key and must travel with the cookies.
	copy_item(SOURCE_ROOT / 'Local State', TARGET_ROOT / 'Local State')
	for relative in PROFILE_ITEMS:
		copy_item(source_profile / relative, target_profile / relative)

	print(f'\ncloned into {TARGET_ROOT}')


if __name__ == '__main__':
	main()
