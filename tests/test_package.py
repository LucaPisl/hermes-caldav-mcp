"""Public wheel contents and a cold isolated installation, without a real keyring."""

import os
import subprocess
import sys
import zipfile
from pathlib import Path


def test_wheel_contains_only_product_files_and_installs_cleanly(tmp_path):
    root = Path(__file__).resolve().parents[1]

    def run(args, env=None):
        result = subprocess.run(
            args, cwd=root, env=env, capture_output=True, text=True, timeout=60
        )
        assert result.returncode == 0, result.stderr
        return result

    run(["uv", "build", "--offline", "--out-dir", str(tmp_path / "dist")])
    wheel = next((tmp_path / "dist").glob("*.whl"))
    with zipfile.ZipFile(wheel) as archive:
        names = archive.namelist()
        assert all(
            n.startswith("hermes_caldav_mcp/")
            or n.startswith("hermes_caldav_mcp-0.1.0.dist-info/")
            for n in names
        )
        assert not any("test" in n or ".env" in n or "fixture" in n for n in names)
        metadata = archive.read("hermes_caldav_mcp-0.1.0.dist-info/METADATA").decode()
        assert "License-Expression: AGPL-3.0-only" in metadata
        license_text = archive.read(
            "hermes_caldav_mcp-0.1.0.dist-info/licenses/LICENSE"
        ).decode()
        assert "GNU AFFERO GENERAL PUBLIC LICENSE" in license_text
        assert "Version 3, 19 November 2007" in license_text
        assert "A small local MCP" in metadata
    environment = tmp_path / "venv"
    install_env = {**os.environ, "UV_PROJECT_ENVIRONMENT": str(environment)}
    run(
        [
            "uv",
            "sync",
            "--frozen",
            "--offline",
            "--no-dev",
            "--no-install-project",
            "--python",
            sys.executable,
        ],
        env=install_env,
    )
    python = str(environment / "bin/python")
    run(
        [
            "uv",
            "pip",
            "install",
            "--offline",
            "--python",
            python,
            "--no-deps",
            str(wheel),
        ]
    )
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in {"DBUS_SESSION_BUS_ADDRESS", "PYTHONPATH"}
    }
    result = subprocess.run(
        [python, "-m", "hermes_caldav_mcp", "--help"],
        cwd=tmp_path,
        env=env,
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert "setup" in result.stdout
    assert not result.stderr
