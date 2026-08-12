"""Security tests for pinned default Chromium extension artifacts."""

import hashlib
import json
import threading
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from browser_use.browser.extensions import DEFAULT_EXTENSION_SPECS, DefaultExtensionSpec, ExtensionArtifactLock
from browser_use.browser.profile import BrowserProfile


def test_default_extension_pins_are_complete_and_unique() -> None:
	"""Every shipped extension must have a unique version and SHA-256 pin."""
	extension_ids = {extension.extension_id for extension in DEFAULT_EXTENSION_SPECS}

	assert len(extension_ids) == len(DEFAULT_EXTENSION_SPECS)
	assert all(extension.version for extension in DEFAULT_EXTENSION_SPECS)
	assert all(len(extension.sha256) == 64 for extension in DEFAULT_EXTENSION_SPECS)


def test_cached_extension_requires_matching_hash_and_manifest_version(tmp_path: Path) -> None:
	"""A cache entry is reusable only when both artifact and extracted metadata match."""
	crx_file = tmp_path / 'extension.crx'
	crx_file.write_bytes(b'pinned-extension-artifact')
	digest = hashlib.sha256(crx_file.read_bytes()).hexdigest()
	extension = DefaultExtensionSpec(
		name='Pinned test extension',
		extension_id='a' * 32,
		version='1.2.3',
		sha256=digest,
		url='https://clients2.google.com/test.crx',
	)
	extension_dir = tmp_path / extension.extension_id
	extension_dir.mkdir()
	(extension_dir / 'manifest.json').write_text(
		json.dumps({'manifest_version': 3, 'version': extension.version}),
		encoding='utf-8',
	)
	profile = BrowserProfile(enable_default_extensions=False)

	assert profile._extension_artifact_is_valid(extension, crx_file, extension_dir)

	crx_file.write_bytes(b'tampered-extension-artifact')
	assert not profile._extension_artifact_is_valid(extension, crx_file, extension_dir)


def test_extension_zip_rejects_path_traversal(tmp_path: Path) -> None:
	"""Extension extraction must not write outside its dedicated cache directory."""
	archive_path = tmp_path / 'unsafe.zip'
	with zipfile.ZipFile(archive_path, 'w') as archive:
		archive.writestr('../outside.txt', 'unsafe')

	with zipfile.ZipFile(archive_path) as archive, pytest.raises(ValueError, match='unsafe path'):
		BrowserProfile._safe_extract_extension_zip(archive, tmp_path / 'extension')

	assert not (tmp_path / 'outside.txt').exists()


def test_extension_lock_write_is_safe_across_concurrent_browser_launches(tmp_path: Path) -> None:
	"""Concurrent launches must leave one valid lock without shared temporary-file races."""
	extension = DefaultExtensionSpec(
		name='Concurrent lock extension',
		extension_id='b' * 32,
		version='4.5.6',
		sha256='c' * 64,
		url='https://clients2.google.com/test.crx',
	)
	lock_file = tmp_path / f'{extension.extension_id}.lock.json'

	with ThreadPoolExecutor(max_workers=16) as executor:
		list(executor.map(lambda _: BrowserProfile._write_extension_lock(extension, lock_file), range(64)))

	lock = ExtensionArtifactLock.model_validate_json(lock_file.read_text(encoding='utf-8'))
	assert lock.extension_id == extension.extension_id
	assert lock.version == extension.version
	assert lock.sha256 == extension.sha256
	assert list(tmp_path.glob(f'.{lock_file.name}.*.tmp')) == []


def test_default_extension_cache_preparation_is_serialized_across_profiles(monkeypatch) -> None:
	"""Concurrent profiles must not mutate shared download and extraction paths together."""
	active_preparations = 0
	maximum_active_preparations = 0
	counter_lock = threading.Lock()

	def prepare_locked(profile: BrowserProfile) -> list[str]:
		nonlocal active_preparations, maximum_active_preparations
		with counter_lock:
			active_preparations += 1
			maximum_active_preparations = max(maximum_active_preparations, active_preparations)
		try:
			time.sleep(0.01)
			return []
		finally:
			with counter_lock:
				active_preparations -= 1

	monkeypatch.setattr(BrowserProfile, '_ensure_default_extensions_downloaded_locked', prepare_locked)
	profiles = [BrowserProfile(enable_default_extensions=True) for _ in range(12)]

	with ThreadPoolExecutor(max_workers=len(profiles)) as executor:
		list(executor.map(lambda profile: profile.prepare_default_extensions(), profiles))

	assert maximum_active_preparations == 1
