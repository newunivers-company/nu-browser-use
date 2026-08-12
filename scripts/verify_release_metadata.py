"""Verify package, MCP release, configuration, and tool metadata stay synchronized."""

import ast
import json
import re
import tomllib
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PYPROJECT_PATH = PROJECT_ROOT / 'pyproject.toml'
UV_LOCK_PATH = PROJECT_ROOT / 'uv.lock'
PYTHON_VERSION_PATH = PROJECT_ROOT / '.python-version'
SERVER_METADATA_PATH = PROJECT_ROOT / 'server.json'
MCP_MANIFEST_PATH = PROJECT_ROOT / 'browser_use' / 'mcp' / 'manifest.json'
MCP_SERVER_PATH = PROJECT_ROOT / 'browser_use' / 'mcp' / 'server.py'
USER_CONFIG_REFERENCE = re.compile(r'^\$\{user_config\.([A-Za-z0-9_]+)\}$')


def _load_json(path: Path) -> dict:
	"""Load a JSON object from disk."""
	with path.open(encoding='utf-8') as file:
		return json.load(file)


def _mcp_server_tool_names() -> set[str]:
	"""Read statically declared MCP tool names without importing the server."""
	tree = ast.parse(MCP_SERVER_PATH.read_text(encoding='utf-8'))
	tool_names: set[str] = set()
	for node in ast.walk(tree):
		if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute) or node.func.attr != 'Tool':
			continue
		for keyword_argument in node.keywords:
			if keyword_argument.arg == 'name' and isinstance(keyword_argument.value, ast.Constant):
				if isinstance(keyword_argument.value.value, str):
					tool_names.add(keyword_argument.value.value)
	return tool_names


def validation_errors() -> list[str]:
	"""Return every release-metadata inconsistency found in the repository."""
	with PYPROJECT_PATH.open('rb') as file:
		project_metadata = tomllib.load(file)
	package_version = project_metadata['project']['version']
	requires_python = project_metadata['project']['requires-python']
	with UV_LOCK_PATH.open('rb') as file:
		lock_metadata = tomllib.load(file)
	server_metadata = _load_json(SERVER_METADATA_PATH)
	manifest = _load_json(MCP_MANIFEST_PATH)
	errors: list[str] = []

	versions = {
		'server.json version': server_metadata.get('version'),
		'server.json package version': server_metadata['packages'][0].get('version'),
		'MCP manifest version': manifest.get('version'),
	}
	for label, version in versions.items():
		if version != package_version:
			errors.append(f'{label} is {version!r}, expected {package_version!r}')

	locked_project = next(
		(package for package in lock_metadata.get('package', []) if package.get('name') == project_metadata['project']['name']),
		None,
	)
	if locked_project is None:
		errors.append('uv.lock does not contain the local browser-use package')
	elif locked_project.get('version') != package_version:
		errors.append(f'uv.lock package version is {locked_project.get("version")!r}, expected {package_version!r}')

	if str(lock_metadata.get('requires-python', '')).replace(' ', '') != str(requires_python).replace(' ', ''):
		errors.append(f'uv.lock requires-python is {lock_metadata.get("requires-python")!r}, expected {requires_python!r}')

	configured_python = PYTHON_VERSION_PATH.read_text(encoding='utf-8').strip()
	minimum_python_match = re.match(r'^>=(\d+\.\d+)', requires_python)
	if minimum_python_match and configured_python != minimum_python_match.group(1):
		errors.append(
			f'.python-version is {configured_python!r}, expected minimum supported Python {minimum_python_match.group(1)!r}'
		)

	if 'dev' not in project_metadata.get('dependency-groups', {}):
		errors.append('pyproject.toml must define the development environment in dependency-groups.dev')

	build_targets = project_metadata.get('tool', {}).get('hatch', {}).get('build', {}).get('targets', {})
	for target_name in ('sdist', 'wheel'):
		forced_files = build_targets.get(target_name, {}).get('force-include', {})
		if forced_files.get('browser_use/mcp/manifest.json') != 'browser_use/mcp/manifest.json':
			errors.append(f'Hatch {target_name} build must force-include browser_use/mcp/manifest.json')

	user_config = set(manifest.get('user_config', {}))
	environment_references = {
		match.group(1)
		for value in manifest['server']['mcp_config'].get('env', {}).values()
		if isinstance(value, str) and (match := USER_CONFIG_REFERENCE.fullmatch(value))
	}
	if user_config != environment_references:
		errors.append(
			'MCP user_config must exactly match environment references: '
			f'configured={sorted(user_config)}, referenced={sorted(environment_references)}'
		)

	manifest_tools = {tool['name'] for tool in manifest.get('tools', [])}
	server_tools = _mcp_server_tool_names()
	if manifest_tools != server_tools:
		errors.append(
			'MCP manifest tools differ from server tools: '
			f'manifest_only={sorted(manifest_tools - server_tools)}, server_only={sorted(server_tools - manifest_tools)}'
		)

	return errors


def main() -> int:
	"""Print validation failures and return a non-zero status when metadata drifted."""
	errors = validation_errors()
	if errors:
		for error in errors:
			print(f'- {error}')
		return 1
	return 0


if __name__ == '__main__':
	raise SystemExit(main())
