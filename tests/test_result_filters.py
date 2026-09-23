import shutil
import subprocess
from pathlib import Path

import pytest


def test_result_filters_frontend():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js required for frontend regression")
    result = subprocess.run(
        [node, str(Path(__file__).with_name("test_result_filters_ui.cjs"))],
        capture_output=True, text=True, encoding="utf-8", timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
