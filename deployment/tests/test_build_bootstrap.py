"""The real builder must produce a kit accepted by the pinned bootstrap verifier."""
import hashlib
import importlib.util
from pathlib import Path
import subprocess
import sys
import zipfile


def test_built_kit_passes_bootstrap_verification(tmp_path):
    deployment = Path(__file__).resolve().parents[1]
    output = tmp_path / "candidate.zip"
    subprocess.run([sys.executable, "-I", "-B", str(deployment / "build_bundle.py"),
                    "--output", str(output)], check=True, capture_output=True, text=True)
    digest = hashlib.sha256(output.read_bytes()).hexdigest()
    assert output.with_suffix(".zip.sha256").read_text().split()[0] == digest
    spec = importlib.util.spec_from_file_location("candidate_bootstrap_verify", deployment / "bootstrap_verify.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    destination = tmp_path / "verified"
    module.extract(output, destination, "root", digest)
    with zipfile.ZipFile(output) as archive:
        assert {"manage.py", "bootstrap_verify.py", "bemind_deploy.py", "gateway/ledger_backend.py"} <= set(archive.namelist())
    for relative in ("manage.py", "bootstrap_verify.py", "bemind_deploy.py", "gateway/ledger_backend.py"):
        assert (destination / relative).read_bytes() == (deployment / relative).read_bytes()
    assert not (destination / "distribution.json").exists(), "The enabled manifest must stay outside the asset it hashes"
