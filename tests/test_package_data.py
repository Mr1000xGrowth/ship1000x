"""Packaging tests for runtime data files."""

from __future__ import annotations

import subprocess
import sys
import zipfile
from pathlib import Path


def test_wheel_contains_runtime_data_files(tmp_path):
    """The public wheel must include data/templates used outside editable installs."""
    subprocess.run(
        [
            sys.executable,
            "-m",
            "build",
            "--wheel",
            "--outdir",
            str(tmp_path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    wheels = list(Path(tmp_path).glob("ship1000x-*.whl"))
    assert len(wheels) == 1

    with zipfile.ZipFile(wheels[0]) as wheel:
        names = set(wheel.namelist())

    assert "ship1000x/data/litellm_prices.json" in names
    assert "ship1000x/data/LITELLM_ATTRIBUTION.md" in names
    assert "ship1000x/web/templates/base.html" in names
    assert "ship1000x/web/templates/overview.html" in names
    assert "ship1000x/web/templates/projects.html" in names
