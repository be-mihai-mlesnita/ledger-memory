"""Review fix (0.3.0-rc2): an upgrade builds and self-tests the new version in its own folder and only then
switches the stable path, so a failed download or self-test leaves the working version in place. Fake uv and ledger
only; the real HOME is never touched."""
import os
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(os.name == "nt", reason="POSIX only")


def _write_fake_uv(bin_dir: Path, fail_sync: Path, fail_selftest: Path, log_file: Path):
    uv_script = bin_dir / "uv"
    uv_script.write_text(textwrap.dedent(f"""\
        #!/usr/bin/env sh
        set -eu
        if [ "$1" = "--version" ]; then
            echo "uv 0.1.0 (fake)"
            exit 0
        fi
        if [ "$1" = "sync" ]; then
            # Log the call
            echo "sync cwd=$(pwd)" >> "{log_file}"
            # Create venv structure
            mkdir -p .venv/bin
            cat > .venv/bin/ledger <<'EOF_LEDGER'
#!/usr/bin/env sh
echo "$@" >> "{log_file}"
if [ "$1" = "selftest" ]; then
    if [ -f "{fail_selftest}" ]; then
        exit 1
    fi
    echo "31 passed, 0 failed, 1 skipped"
    exit 0
fi
if [ "$1" = "backup" ]; then
    echo "backed up"
    exit 0
fi
if [ "$1" = "init" ]; then
    echo "initialized"
    exit 0
fi
exit 0
EOF_LEDGER
            chmod +x .venv/bin/ledger
            cat > .venv/bin/ledger-mcp <<'EOF_MCP'
#!/usr/bin/env sh
echo "ledger-mcp $@" >> "{log_file}"
exit 0
EOF_MCP
            chmod +x .venv/bin/ledger-mcp
            cat > .venv/bin/python <<'EOF_PY'
#!/usr/bin/env sh
echo "python $@" >> "{log_file}"
if [ "$1" = "install/configure_opencode.py" ]; then
    echo "configured opencode"
    exit 0
fi
exit 0
EOF_PY
            chmod +x .venv/bin/python
            if [ -f "{fail_sync}" ]; then
                exit 1
            fi
            exit 0
        fi
        echo "unknown uv command: $@" >&2
        exit 1
    """))
    uv_script.chmod(0o755)


def _create_package(pkg_dir: Path):
    # pyproject.toml
    (pkg_dir / "pyproject.toml").write_text(textwrap.dedent("""\
        [project]
        name = "ledger-memory"
        version = "1.2.3"
        dependencies = []
        [project.optional-dependencies]
        semantic = []
    """))
    # install.sh (will be replaced by test with the new version)
    # MEMORY-RULES.md
    (pkg_dir / "MEMORY-RULES.md").write_text("# Rules\n")
    # install/configure_opencode.py
    (pkg_dir / "install").mkdir()
    (pkg_dir / "install" / "configure_opencode.py").write_text(textwrap.dedent("""\
        #!/usr/bin/env python3
        import argparse, sys
        parser = argparse.ArgumentParser()
        parser.add_argument("--app")
        parser.add_argument("--store")
        args = parser.parse_args()
        print(f"opencode config for {args.app} -> {args.store}")
    """))


def _run_installer(pkg_dir: Path, home: Path, bin_dir: Path, args=None):
    env = os.environ.copy()
    env["HOME"] = str(home)
    env["PATH"] = str(bin_dir) + ":" + env.get("PATH", "/usr/bin:/bin")
    env["LEDGER_APP_DIR"] = str(home / ".local" / "share" / "ledger-memory")
    env["LEDGER_HOME"] = str(home / ".ledger-memory")
    cmd = ["bash", str(pkg_dir / "install.sh")]
    if args:
        cmd.extend(args)
    return subprocess.run(cmd, cwd=pkg_dir, env=env, capture_output=True, text=True)


def test_installer_fresh_install_creates_versioned_dir_and_symlink(tmp_path):
    home = tmp_path / "home"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    home.mkdir()
    pkg_dir = tmp_path / "pkg"
    pkg_dir.mkdir()
    _create_package(pkg_dir)
    fail_sync = tmp_path / "fail_sync"
    fail_selftest = tmp_path / "fail_selftest"
    log_file = tmp_path / "uv.log"
    _write_fake_uv(bin_dir, fail_sync, fail_selftest, log_file)

    # Copy the new install.sh to pkg_dir
    install_sh = Path(__file__).parent.parent / "install.sh"
    if install_sh.exists():
        (pkg_dir / "install.sh").write_text(install_sh.read_text())
    else:
        # Fallback: use the script from the test directory
        pass

    result = _run_installer(pkg_dir, home, bin_dir, ["--no-opencode"])
    assert result.returncode == 0, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"

    app_dir = home / ".local" / "share" / "ledger-memory"
    assert app_dir.is_symlink()
    target = os.readlink(app_dir)
    assert "1.2.3-" in target
    assert "-versions/1.2.3-" in target  # <version>-<UTC stamp>-<unique suffix>
    # Check version dir exists
    versions_dir = app_dir.parent / "ledger-memory-versions"
    assert versions_dir.exists()
    version_dirs = list(versions_dir.glob("1.2.3-*"))
    assert len(version_dirs) == 1
    # Check venv created in version dir
    assert (version_dirs[0] / ".venv" / "bin" / "ledger").exists()
    # Check log shows sync ran in version dir
    log_content = log_file.read_text()
    assert "sync cwd=" in log_content
    assert str(version_dirs[0]) in log_content


def test_installer_failed_sync_keeps_previous_version(tmp_path):
    home = tmp_path / "home"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    home.mkdir()
    pkg_dir = tmp_path / "pkg"
    pkg_dir.mkdir()
    _create_package(pkg_dir)
    fail_sync = tmp_path / "fail_sync"
    fail_selftest = tmp_path / "fail_selftest"
    log_file = tmp_path / "uv.log"
    _write_fake_uv(bin_dir, fail_sync, fail_selftest, log_file)

    install_sh = Path(__file__).parent.parent / "install.sh"
    if install_sh.exists():
        (pkg_dir / "install.sh").write_text(install_sh.read_text())

    # First install: success
    result1 = _run_installer(pkg_dir, home, bin_dir, ["--no-opencode"])
    assert result1.returncode == 0
    app_dir = home / ".local" / "share" / "ledger-memory"
    first_target = os.readlink(app_dir)
    first_ledger = Path(first_target) / ".venv" / "bin" / "ledger"
    assert first_ledger.exists()

    # Second install: fail sync
    fail_sync.touch()
    result2 = _run_installer(pkg_dir, home, bin_dir, ["--no-opencode"])
    assert result2.returncode != 0
    assert "install failed; your current version is unchanged" in result2.stderr

    # APP still points to first version
    assert app_dir.is_symlink()
    assert os.readlink(app_dir) == first_target
    assert first_ledger.exists()
    # Failed version dir exists
    versions_dir = app_dir.parent / "ledger-memory-versions"
    version_dirs = list(versions_dir.glob("1.2.3-*"))
    assert len(version_dirs) == 2


def test_installer_failed_selftest_keeps_previous_version(tmp_path):
    home = tmp_path / "home"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    home.mkdir()
    pkg_dir = tmp_path / "pkg"
    pkg_dir.mkdir()
    _create_package(pkg_dir)
    fail_sync = tmp_path / "fail_sync"
    fail_selftest = tmp_path / "fail_selftest"
    log_file = tmp_path / "uv.log"
    _write_fake_uv(bin_dir, fail_sync, fail_selftest, log_file)

    install_sh = Path(__file__).parent.parent / "install.sh"
    if install_sh.exists():
        (pkg_dir / "install.sh").write_text(install_sh.read_text())

    # First install: success
    result1 = _run_installer(pkg_dir, home, bin_dir, ["--no-opencode"])
    assert result1.returncode == 0
    app_dir = home / ".local" / "share" / "ledger-memory"
    first_target = os.readlink(app_dir)
    first_ledger = Path(first_target) / ".venv" / "bin" / "ledger"
    assert first_ledger.exists()

    # Second install: fail selftest
    fail_selftest.touch()
    result2 = _run_installer(pkg_dir, home, bin_dir, ["--no-opencode"])
    assert result2.returncode != 0
    assert "install failed; your current version is unchanged" in result2.stderr

    # APP still points to first version
    assert app_dir.is_symlink()
    assert os.readlink(app_dir) == first_target
    assert first_ledger.exists()
    versions_dir = app_dir.parent / "ledger-memory-versions"
    version_dirs = list(versions_dir.glob("1.2.3-*"))
    assert len(version_dirs) == 2


def test_installer_switch_points_entry_points_at_final_paths(tmp_path):
    home = tmp_path / "home"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    home.mkdir()
    pkg_dir = tmp_path / "pkg"
    pkg_dir.mkdir()
    _create_package(pkg_dir)
    fail_sync = tmp_path / "fail_sync"
    fail_selftest = tmp_path / "fail_selftest"
    log_file = tmp_path / "uv.log"
    _write_fake_uv(bin_dir, fail_sync, fail_selftest, log_file)

    install_sh = Path(__file__).parent.parent / "install.sh"
    if install_sh.exists():
        (pkg_dir / "install.sh").write_text(install_sh.read_text())

    # First install
    result1 = _run_installer(pkg_dir, home, bin_dir, ["--no-opencode"])
    assert result1.returncode == 0
    app_dir = home / ".local" / "share" / "ledger-memory"
    first_target = os.readlink(app_dir)

    # Second install
    result2 = _run_installer(pkg_dir, home, bin_dir, ["--no-opencode"])
    assert result2.returncode == 0

    # APP points to second version
    second_target = os.readlink(app_dir)
    assert second_target != first_target
    assert "1.2.3-" in second_target

    # Check log: second sync ran in second version dir
    log_content = log_file.read_text()
    sync_lines = [line for line in log_content.splitlines() if line.startswith("sync cwd=")]
    assert len(sync_lines) == 2
    assert first_target in sync_lines[0]
    assert second_target in sync_lines[1]

    # ledger and ledger-mcp exist in second version
    assert (Path(second_target) / ".venv" / "bin" / "ledger").exists()
    assert (Path(second_target) / ".venv" / "bin" / "ledger-mcp").exists()


def test_installer_upgrades_an_old_style_directory_install(tmp_path):
    home = tmp_path / "home"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    home.mkdir()
    pkg_dir = tmp_path / "pkg"
    pkg_dir.mkdir()
    _create_package(pkg_dir)
    fail_sync = tmp_path / "fail_sync"
    fail_selftest = tmp_path / "fail_selftest"
    log_file = tmp_path / "uv.log"
    _write_fake_uv(bin_dir, fail_sync, fail_selftest, log_file)

    install_sh = Path(__file__).parent.parent / "install.sh"
    if install_sh.exists():
        (pkg_dir / "install.sh").write_text(install_sh.read_text())

    # Pre-create old-style directory install
    app_dir = home / ".local" / "share" / "ledger-memory"
    app_dir.mkdir(parents=True)
    (app_dir / ".venv").mkdir()
    (app_dir / ".venv" / "bin").mkdir()
    old_ledger = app_dir / ".venv" / "bin" / "ledger"
    old_ledger.write_text("#!/bin/sh\necho old\n")
    old_ledger.chmod(0o755)
    old_mcp = app_dir / ".venv" / "bin" / "ledger-mcp"
    old_mcp.write_text("#!/bin/sh\necho old-mcp\n")
    old_mcp.chmod(0o755)

    result = _run_installer(pkg_dir, home, bin_dir, ["--no-opencode"])
    assert result.returncode == 0

    # APP is now a symlink
    assert app_dir.is_symlink()
    target = os.readlink(app_dir)
    assert "1.2.3-" in target

    # Old directory moved to versions/legacy-*
    versions_dir = app_dir.parent / "ledger-memory-versions"
    legacy_dirs = list(versions_dir.glob("legacy-*"))
    assert len(legacy_dirs) == 1
    assert (legacy_dirs[0] / ".venv" / "bin" / "ledger").exists()
    assert (legacy_dirs[0] / ".venv" / "bin" / "ledger-mcp").exists()

    # New version has its own venv
    assert (Path(target) / ".venv" / "bin" / "ledger").exists()
    assert (Path(target) / ".venv" / "bin" / "ledger-mcp").exists()
