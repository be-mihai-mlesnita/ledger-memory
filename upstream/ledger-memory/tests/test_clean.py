"""Guard: nothing that ships may carry the source project's internals or anyone's personal data.

Patterns are assembled from fragments so this file never matches itself."""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
NOT_SHIPPED = {".venv", ".git", "__pycache__", ".pytest_cache", "tools", "dist"}
NOT_SHIPPED_FILES = {"BUILD-LOG.md"}
FRAGMENTS = [
    r"\bSL" r"ICE-\d", r"\bAD" r"R-\d", r"\bRE" r"Q-\d", r"\bHA" r"OS", r"\bha" r"os\b", r"\bbr" r"ain\b",
    r"\bCo" r"dex\b", r"iCl" r"oud", r"Tele" r"gram", r"/Us" r"ers/", r"mi" r"hai", r"mles" r"nita", r"Buch" r"arest",
    r"launc" r"hd", r"owner deci" r"sion", r"@gm" r"ail\.",
]
FORBIDDEN = re.compile("|".join(FRAGMENTS))


def shipped_files():
    for p in ROOT.rglob("*"):
        rel = p.relative_to(ROOT)
        if p.is_file() and not (set(rel.parts) & NOT_SHIPPED) and rel.name not in NOT_SHIPPED_FILES \
                and p.suffix not in {".pyc", ".db", ".zip"}:
            yield rel, p


def test_no_internal_references_or_personal_data_ship():
    hits = []
    for rel, p in shipped_files():
        for n, line in enumerate(p.read_text(errors="replace").splitlines(), 1):
            if FORBIDDEN.search(line):
                hits.append(f"{rel}:{n}: {line.strip()[:100]}")
    assert hits == []


def test_the_guard_can_see():
    assert FORBIDDEN.search("see SL" "ICE-0101") and FORBIDDEN.search("/Us" "ers/someone")
    assert len(list(shipped_files())) > 40                    # the walk actually reaches the package


def test_windows_installer_static_safety_contract():
    """Source guard only; does not claim PowerShell or native Windows execution."""
    script = (ROOT / "install.ps1").read_text()
    assert script.startswith("#requires -Version 7.2")
    assert "$info.UseShellExecute = $false" in script
    assert "$info.RedirectStandardOutput = $false" in script
    assert "$info.RedirectStandardError = $false" in script
    assert "$info.ArgumentList.Add($argument)" in script
    assert "'--frozen', '--inexact', '--no-dev', '--project', $root" in script
    assert "if ($SemanticDependencies)" in script
    assert "$info.Environment['UV_PROJECT_ENVIRONMENT'] = $environmentPath" in script
    assert "$info.Environment.Remove($name)" in script
    assert "'UV_WORKING_DIR'" in script
    assert "'UV_WORKING_DIRECTORY'" not in script
    assert "catch [System.Management.Automation.ItemNotFoundException]" in script
    assert "Cannot inspect package .venv; installation refused." in script
    assert "$process.WaitForExit($TimeoutSeconds * 1000)" in script
    assert "$process.Kill($true)" in script and "$process.WaitForExit(5000)" in script
    assert "$process.Dispose()" in script
    assert "FileAttributes]::ReparsePoint" in script
    for forbidden in ("Invoke-Expression", "Invoke-WebRequest", "Remove-Item", "Move-Item",
                      "SetEnvironmentVariable", "ReadToEnd", "ArgumentList.AddRange"):
        assert forbidden not in script
