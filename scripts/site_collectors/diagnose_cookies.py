"""Diagnose whether copied Chrome cookies are portable across user-data-dir.

Chrome cookie values carry a version prefix: v10 = AES-GCM under the DPAPI-wrapped key in
Local State (portable to any dir for the same Windows user), v20 = App-Bound Encryption, whose
key is unwrapped by the elevated Chrome service and is NOT recoverable outside Chrome itself.
"""

from __future__ import annotations

import base64
import json
import os
import shutil
import sqlite3
import tempfile
from collections import Counter
from pathlib import Path

SOURCE_ROOT = Path(os.environ['LOCALAPPDATA']) / 'Google' / 'Chrome' / 'User Data'
TARGET_ROOT = Path.home() / '.browser-use-profiles' / 'shotdeck'


def cookie_version_histogram(cookies_db: Path) -> Counter[str]:
	"""Bucket shotdeck cookie values by their encryption version prefix."""
	histogram: Counter[str] = Counter()
	with tempfile.TemporaryDirectory() as tmp:
		copy = Path(tmp) / 'Cookies'
		shutil.copy2(cookies_db, copy)
		connection = sqlite3.connect(f'file:{copy}?mode=ro', uri=True)
		for (blob,) in connection.execute('SELECT encrypted_value FROM cookies WHERE host_key LIKE ?', ('%shotdeck%',)):
			prefix = bytes(blob)[:3].decode('ascii', errors='replace') if blob else 'empty'
			histogram[prefix] += 1
		connection.close()
	return histogram


def local_state_keys(local_state: Path) -> dict[str, str]:
	"""Report which encryption keys the Local State file carries."""
	payload = json.loads(local_state.read_text(encoding='utf-8', errors='replace'))
	os_crypt = payload.get('os_crypt', {})
	report: dict[str, str] = {}
	for key_name in ('encrypted_key', 'app_bound_encrypted_key'):
		raw = os_crypt.get(key_name)
		if raw is None:
			report[key_name] = 'absent'
			continue
		decoded = base64.b64decode(raw)
		report[key_name] = f'present, {len(decoded)} bytes, prefix={decoded[:5]!r}'
	return report


for label, root, profile in (
	('SOURCE Default', SOURCE_ROOT, SOURCE_ROOT / 'Default'),
	('SOURCE Profile 23', SOURCE_ROOT, SOURCE_ROOT / 'Profile 23'),
	('CLONE', TARGET_ROOT, TARGET_ROOT / 'Default'),
):
	print(f'--- {label} ---')
	cookies_db = profile / 'Network' / 'Cookies'
	if cookies_db.is_file():
		try:
			print(f'  shotdeck cookie versions: {dict(cookie_version_histogram(cookies_db))}')
		except (OSError, sqlite3.Error) as error:
			print(f'  cookies unreadable: {type(error).__name__}: {error}')
	else:
		print('  no cookies db')
	local_state = root / 'Local State'
	if local_state.is_file():
		for key_name, description in local_state_keys(local_state).items():
			print(f'  {key_name}: {description}')
	else:
		print('  no Local State')
