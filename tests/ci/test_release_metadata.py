import subprocess
import sys
from pathlib import Path


def test_release_metadata_is_synchronized():
	"""Package and MCP metadata must describe the same shipped implementation."""
	project_root = Path(__file__).resolve().parents[2]
	result = subprocess.run(
		[sys.executable, str(project_root / 'scripts' / 'verify_release_metadata.py')],
		cwd=project_root,
		capture_output=True,
		text=True,
		check=False,
	)
	assert result.returncode == 0, result.stdout + result.stderr
