"""Isolated bootstrap tests; optional gh GET checks never change authentication."""
from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import pty
import select
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock
import warnings
import zipfile


ROOT = Path(__file__).resolve().parents[2]
HELPER = ROOT / "deployment/bootstrap_verify.py"
SPEC = importlib.util.spec_from_file_location("bootstrap_verify", HELPER)
VERIFY = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(VERIFY)
SOURCE_COMMIT = "a" * 40  # Synthetic fixture only, never a published commit.
KIT_COMMIT = "b" * 40
CORE = "ledger-memory-9.8.7-rc2.zip"
MANAGER = b'''import json, os, pathlib, sys
home = pathlib.Path(os.environ["HOME"])
assert home.name == "isolated-home"
(home / "manager.json").write_text(json.dumps({"argv": sys.argv, "isolated": sys.flags.isolated, "bytecode": sys.dont_write_bytecode, "executable": sys.executable}))
raise SystemExit(int(os.environ.get("FAKE_MANAGER_EXIT", "0")))
'''


def kit_bytes(layout="root", extras=(), missing=()):
    entries = {"manage.py": MANAGER, "bemind_deploy.py": b"# inert fixture\n",
               "install.sh": b"# inert\n", "uninstall.sh": b"# inert\n", "run.sh": b"# inert\n",
               "bootstrap_verify.py": HELPER.read_bytes(), CORE: b"inert core; never executed",
               CORE + ".sha256": b"fixture only\n"}
    prefix = "deployment/" if layout == "deployment" else ""
    output = io.BytesIO()
    with warnings.catch_warnings(), zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        warnings.simplefilter("ignore", UserWarning)
        for name, data in entries.items():
            if name not in missing:
                archive.writestr(prefix + name, data)
        for name, data in extras:
            archive.writestr(name, data)
    return output.getvalue()


STUB = r'''
import hashlib, json, os, pathlib, signal, subprocess, sys
root = pathlib.Path(os.environ["FIXTURE"])
command = pathlib.Path(sys.argv[0]).name
args = sys.argv[1:]
with (root / "calls.jsonl").open("a") as stream:
    stream.write(json.dumps([command, args, "GH_DEBUG" in os.environ]) + "\n")
mode = os.environ.get("FAKE_MODE", "")
def emit_json(data):
    result = subprocess.run([os.environ["REAL_JQ"], "-r", args[args.index("--jq") + 1]], input=json.dumps(data), text=True, capture_output=True)
    sys.stdout.write(result.stdout)
    sys.stderr.write(result.stderr)
    raise SystemExit(result.returncode)
if command == "uname":
    print(os.environ.get("FAKE_OS", "Darwin") if args == ["-s"] else os.environ.get("FAKE_ARCH", "arm64"))
elif command == "gh":
    if args[:2] == ["auth", "status"]:
        assert args == ["auth", "status", "--hostname", "github.com"]
        raise SystemExit(1 if mode == "auth" else 0)
    if args[0] == "api":
        assert args[args.index("--hostname") + 1] == "github.com"
        if "--method" in args:
            assert args[args.index("--method") + 1] == "GET"
        endpoint = next(a for a in args if a.startswith("repos/"))
        assert endpoint.startswith("repos/be-mihai-mlesnita/ledger-memory/")
        if "/contents/" in endpoint:
            assert endpoint.endswith("?ref=" + "a" * 40)
            assert "Accept: application/vnd.github.raw+json" in args
        if "distribution.json?" in endpoint:
            if mode == "repo-auth":
                raise SystemExit(1)
            if mode == "invalid-json":
                raise SystemExit(1)
            emit_json(json.loads((root / "manifest.json").read_text()))
        elif "/commits/" in endpoint:
            emit_json({"sha": "c" * 40 if mode == "provenance" else "b" * 40})
        elif "/releases/tags/" in endpoint:
            m = json.loads((root / "manifest.json").read_text())
            emit_json({"draft": mode == "draft", "tag_name": m["tag"], "assets": [] if mode == "missing-asset" else [{"name": m["asset"]}]})
        elif "bootstrap_verify.py?" in endpoint:
            if mode == "partial-helper":
                sys.stdout.write("raise RuntimeError('MUST NOT EXECUTE PARTIAL HELPER')\n")
                raise SystemExit(1)
            sys.stdout.write(pathlib.Path(os.environ["HELPER"]).read_text())
        else:
            raise AssertionError(endpoint)
    elif args[:2] == ["release", "download"]:
        m = json.loads((root / "manifest.json").read_text())
        assert args[2] == m["tag"]
        assert args[args.index("--repo") + 1] == "github.com/be-mihai-mlesnita/ledger-memory"
        assert args[args.index("--pattern") + 1] == m["asset"]
        target = pathlib.Path(args[args.index("--output") + 1])
        assert stat_mode(target.parent) == 0o700
        target.write_bytes(b"partial" if mode == "partial-kit" else (root / "kit.zip").read_bytes())
        if mode == "terminate-kit":
            os.kill(os.getppid(), signal.SIGTERM)
        raise SystemExit(1 if mode == "partial-kit" else 0)
    else:
        raise AssertionError(args)
elif command.startswith("python"):
    assert args[:2] == ["-I", "-B"]
    if args[2] == "-c":
        raise SystemExit(0 if os.environ.get("FAKE_RUNTIME", "yes") == "yes" else 1)
    assert pathlib.Path(args[2]).name in {"bootstrap_verify.py", "manage.py"}
    if pathlib.Path(args[2]).name == "manage.py" and os.environ.get("FAKE_INTERCEPT_MANAGER") == "yes":
        home = pathlib.Path(os.environ["HOME"])
        assert home.name == "isolated-home"
        (home / "manager.json").write_text(json.dumps({"args": args, "sha256": hashlib.sha256(pathlib.Path(args[2]).read_bytes()).hexdigest()}))
        raise SystemExit(0)
    os.execv(sys.executable, [sys.executable] + args)
elif command == "uv":
    if args[:2] == ["python", "find"]:
        assert os.environ.get("UV_PYTHON_DOWNLOADS") == "never"
        assert "--no-project" in args
        if (root / "python-installed").exists() or os.environ.get("FAKE_UV_RUNTIME") == "yes":
            print(sys.executable)
        else:
            raise SystemExit(1)
    elif args == ["python", "install", "3.13", "--no-bin"]:
        assert os.environ.get("UV_PYTHON_DOWNLOADS") == "manual"
        assert os.environ.get("UV_NO_MODIFY_PATH") == "1"
        (root / "python-installed").write_text("stub only")
    else:
        raise AssertionError(args)
elif command == "brew":
    assert args == ["install", "uv"]
    assert os.environ.get("HOMEBREW_NO_AUTO_UPDATE") == "1"
    assert os.environ.get("HOMEBREW_NO_INSTALL_CLEANUP") == "1"
    if mode == "brew-fail":
        raise SystemExit(1)
    (root / "bin" / args[1]).symlink_to(root / "stub")
else:
    raise AssertionError(command)
'''
# Kept before stub dispatch, without adding a dependency to the production script.
STUB = STUB.replace('root = pathlib.Path', 'def stat_mode(path): return path.stat().st_mode & 0o777\nroot = pathlib.Path')


@unittest.skipUnless(shutil.which("jq"), "Offline gh fixture requires installed jq")
class BootstrapTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="bootstrap-tests-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.home = self.root / "isolated-home"
        self.home.mkdir()
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.temps = self.root / "private-temps"
        self.temps.mkdir()
        (self.temps / "unrelated").write_text("keep")
        stub = self.root / "stub"
        stub.write_text("#!" + sys.executable + "\n" + STUB)
        stub.chmod(0o700)
        for name in ("uname", "gh", "python3.13", "python3.14", "python3.12", "python3.11", "python3"):
            (self.bin / name).symlink_to(stub)
        for name in ("mktemp", "rm", "shasum"):
            (self.bin / name).symlink_to(shutil.which(name))
        self.env = {"HOME": str(self.home), "PATH": str(self.bin), "TMPDIR": str(self.temps),
                    "FIXTURE": str(self.root), "HELPER": str(HELPER), "REAL_JQ": shutil.which("jq"),
                    "GH_DEBUG": "api"}
        self.manifest = {"schema_version": 1, "ready": True,
                         "repository": "be-mihai-mlesnita/ledger-memory", "platform": "darwin",
                         "architectures": ["arm64", "x86_64"], "version": "9.8.7-rc2",
                         "tag": "test-kit-9.8.7-rc2", "asset": "test-kit-9.8.7-rc2.zip",
                         "kit_commit": KIT_COMMIT, "layout": "root"}
        self.set_kit(kit_bytes())

    def set_kit(self, blob):
        (self.root / "kit.zip").write_bytes(blob)
        self.manifest["sha256"] = hashlib.sha256(blob).hexdigest()

    def calls(self):
        log = self.root / "calls.jsonl"
        return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []

    def run_bootstrap(self, responses=None, command=None):
        (self.root / "manifest.json").write_text(json.dumps(self.manifest))
        command = command or ["/bin/bash", str(ROOT / "bootstrap.sh"), "--source-commit", SOURCE_COMMIT]
        if responses is None:
            result = subprocess.run(command, env=self.env, cwd=self.home, text=True,
                                    capture_output=True, start_new_session=True, timeout=20)
            output = result.stdout + result.stderr
            code = result.returncode
        else:
            pid, fd = pty.fork()
            if pid == 0:
                os.chdir(self.home)
                os.execve(command[0], command, self.env)
            output = ""
            pending = list(responses)
            sent = 0
            deadline = time.monotonic() + 20
            status = None
            waited = 0
            try:
                while time.monotonic() < deadline:
                    if select.select([fd], [], [], 0.1)[0]:
                        try:
                            chunk = os.read(fd, 65536)
                        except OSError:
                            break
                        if not chunk:
                            break
                        output += chunk.decode(errors="replace")
                        if output.count("anything else cancels]:") > sent and pending:
                            os.write(fd, (pending.pop(0) + "\n").encode())
                            sent += 1
                    waited, status = os.waitpid(pid, os.WNOHANG)
                    if waited:
                        break
                else:
                    self.fail("Interactive fixture timed out: " + output)
                if not waited:
                    waited, status = os.waitpid(pid, 0)
                code = os.waitstatus_to_exitcode(status)
            finally:
                os.close(fd)
                if not waited:
                    try:
                        os.kill(pid, signal.SIGKILL)
                        os.waitpid(pid, 0)
                    except ProcessLookupError:
                        pass
        self.assertEqual([p.name for p in self.temps.iterdir()], ["unrelated"], output)
        self.assertEqual((self.temps / "unrelated").read_text(), "keep")
        return code, output

    def assert_refused(self, text):
        code, output = self.run_bootstrap()
        self.assertNotEqual(code, 0, output)
        self.assertIn(text, output)
        self.assertFalse((self.home / "manager.json").exists())
        return output

    def test_success_both_architectures_and_layouts(self):
        for arch, layout in (("arm64", "root"), ("x86_64", "deployment")):
            with self.subTest(arch=arch, layout=layout):
                self.env["FAKE_ARCH"] = arch
                self.manifest["layout"] = layout
                self.set_kit(kit_bytes(layout))
                code, output = self.run_bootstrap()
                self.assertEqual(code, 0, output)
                handoff = json.loads((self.home / "manager.json").read_text())
                self.assertEqual(handoff["isolated"], 1)
                self.assertTrue(handoff["bytecode"])
                self.assertEqual(len(handoff["argv"]), 1)
                self.assertTrue(handoff["argv"][0].endswith("/deployment/manage.py" if layout == "deployment" else "/extracted/manage.py"))
                self.assertFalse(Path(handoff["argv"][0]).exists())
        self.assertFalse(any(debug for _, _, debug in self.calls()))
        self.assertFalse(any("latest" in arg for _, args, _ in self.calls() for arg in args))

    def test_new_installer_manifest_enables_model_after_menu_selection(self):
        self.manifest["installer_version"] = "0.3.1"
        code, output = self.run_bootstrap()
        self.assertEqual(code, 0, output)
        self.assertEqual(json.loads((self.home / "manager.json").read_text())["argv"][-1], "--auto-model")

    def test_platform_and_architecture_refusal(self):
        self.env["FAKE_OS"] = "Linux"
        self.assert_refused("macOS only")
        self.env["FAKE_OS"] = "Darwin"
        self.env["FAKE_ARCH"] = "i386"
        self.assert_refused("Supported Mac architectures")
        self.assertFalse(any(name == "gh" for name, _, _ in self.calls()))

    def test_missing_gh_and_authentication_failures(self):
        (self.bin / "gh").unlink()
        self.assert_refused("Browser login alone")
        (self.bin / "gh").symlink_to(self.root / "stub")
        for mode, message in (("auth", "authentication failed"), ("repo-auth", "repository access")):
            with self.subTest(mode=mode):
                self.env["FAKE_MODE"] = mode
                self.assert_refused(message)

    def test_disabled_manifest_stops_before_prerequisites(self):
        self.manifest = json.loads((ROOT / "deployment/distribution.json").read_text())
        self.manifest["ready"] = False
        self.env["FAKE_RUNTIME"] = "no"
        self.assert_refused("disabled/not ready")
        self.assertFalse(any(name in {"brew", "uv"} or name.startswith("python") or args[:2] == ["release", "download"] for name, args, _ in self.calls()))

    def test_invalid_manifest_and_injection(self):
        original = dict(self.manifest)
        for field, value in (("schema_version", 2), ("repository", "other/repo"), ("ready", "true"),
                             ("version", "1\n2"), ("tag", "../../main"), ("tag", "$(touch PWNED)"),
                             ("asset", "-foo.zip"), ("asset", "*.zip"), ("asset", "../foo.zip"),
                             ("sha256", "A" * 64), ("kit_commit", "main"), ("layout", "../../"),
                             ("platform", "linux"), ("architectures", ["arm64"]), ("asset", None),
                             ("tag", "v1\n"), ("tag", "v1\nINJECTED"), ("asset", "good.zip\n"),
                             ("version", "1.2.3\n"), ("sha256", "a" * 64 + "\n")):
            with self.subTest(field=field, value=value):
                self.manifest = dict(original, **{field: value})
                self.assert_refused("retrieve/validate")
        self.assertFalse((self.home / "PWNED").exists())
        self.assertFalse(any(name.startswith("python") for name, _, _ in self.calls()))

    def test_provenance_and_release_metadata(self):
        for mode, message in (("provenance", "provenance"), ("draft", "published release"), ("missing-asset", "absent")):
            with self.subTest(mode=mode):
                self.env["FAKE_MODE"] = mode
                self.assert_refused(message)
        self.assertFalse(any(args[:2] == ["release", "download"] for _, args, _ in self.calls()))

    def test_partial_downloads_and_wrong_digest(self):
        for mode, message in (("partial-helper", "Pinned verifier download failed"), ("partial-kit", "incomplete")):
            with self.subTest(mode=mode):
                self.env["FAKE_MODE"] = mode
                output = self.assert_refused(message)
                self.assertNotIn("RuntimeError", output)
        self.env["FAKE_MODE"] = ""
        self.manifest["sha256"] = "0" * 64
        self.assert_refused("SHA-256 mismatch")

    def test_unsafe_archive_never_hands_off(self):
        self.set_kit(kit_bytes(extras=[("../escaped.py", b"bad")]))
        self.assert_refused("Unsafe or invalid kit")

    def test_no_runtime_no_package_manager(self):
        self.env["FAKE_RUNTIME"] = "no"
        self.assert_refused("approved software channel")

    def test_non_tty_cannot_authorize_prerequisites(self):
        self.env["FAKE_RUNTIME"] = "no"
        (self.bin / "brew").symlink_to(self.root / "stub")
        self.assert_refused("controlling terminal")
        self.assertFalse(any(name == "brew" for name, _, _ in self.calls()))

    def test_cancel_homebrew_prompt(self):
        self.env["FAKE_RUNTIME"] = "no"
        (self.bin / "brew").symlink_to(self.root / "stub")
        code, output = self.run_bootstrap(responses=["no"])
        self.assertNotEqual(code, 0, output)
        self.assertIn("Cancelled", output)
        self.assertFalse(any(name == "brew" for name, _, _ in self.calls()))

    def test_cancel_uv_prompt(self):
        self.env["FAKE_RUNTIME"] = "no"
        (self.bin / "uv").symlink_to(self.root / "stub")
        code, output = self.run_bootstrap(responses=["no"])
        self.assertNotEqual(code, 0, output)
        self.assertFalse((self.root / "python-installed").exists())

    def test_existing_uv_runtime_needs_no_install(self):
        self.env.update(FAKE_RUNTIME="no", FAKE_UV_RUNTIME="yes")
        (self.bin / "uv").symlink_to(self.root / "stub")
        code, output = self.run_bootstrap()
        self.assertEqual(code, 0, output)
        self.assertFalse((self.root / "python-installed").exists())

    def test_explicit_existing_runtime_override(self):
        self.env.update(FAKE_RUNTIME="no", BEMIND_DEPLOY_PYTHON=sys.executable)
        code, output = self.run_bootstrap()
        self.assertEqual(code, 0, output)
        self.assertFalse(any(name.startswith("python") for name, _, _ in self.calls()))

    def test_invalid_runtime_override_does_not_fall_back(self):
        self.env["BEMIND_DEPLOY_PYTHON"] = str(self.root / "missing-python")
        self.assert_refused("BEMIND_DEPLOY_PYTHON must name")
        self.assertFalse(any(args[:2] == ["release", "download"] for _, args, _ in self.calls()))

    def test_explicit_prerequisite_consent_with_stubs_only(self):
        self.env["FAKE_RUNTIME"] = "no"
        (self.bin / "brew").symlink_to(self.root / "stub")
        code, output = self.run_bootstrap(responses=["YeS", "OK"])
        self.assertEqual(code, 0, output)
        self.assertTrue((self.root / "python-installed").exists())
        self.assertTrue((self.home / "manager.json").exists())

    def test_manager_failure_exit_status_and_cleanup(self):
        self.env["FAKE_MANAGER_EXIT"] = "23"
        code, output = self.run_bootstrap()
        self.assertEqual(code, 23, output)

    def test_termination_during_download_cleans_only_owned_temp(self):
        self.env["FAKE_MODE"] = "terminate-kit"
        code, output = self.run_bootstrap()
        self.assertEqual(code, 143, output)
        self.assertFalse((self.home / "manager.json").exists())

    def test_source_ref_must_be_full_commit(self):
        code, output = self.run_bootstrap(command=["/bin/bash", str(ROOT / "bootstrap.sh"), "--source-commit", "main"])
        self.assertNotEqual(code, 0)
        self.assertIn("full 40-character", output)
        self.assertEqual(self.calls(), [])

    @unittest.skipUnless(os.environ.get("LEDGER_BOOTSTRAP_TEST_KIT"), "Set LEDGER_BOOTSTRAP_TEST_KIT to a candidate-built kit")
    def test_complete_candidate_kit_verified_without_executing_real_manager(self):
        kit = Path(os.environ["LEDGER_BOOTSTRAP_TEST_KIT"]).resolve()
        self.assertTrue(kit.is_relative_to(ROOT), "Only candidate-local kit input is permitted")
        self.set_kit(kit.read_bytes())
        self.env.update(FAKE_INTERCEPT_MANAGER="yes")
        code, output = self.run_bootstrap()
        self.assertEqual(code, 0, output)
        handoff = json.loads((self.home / "manager.json").read_text())
        with zipfile.ZipFile(kit) as archive:
            self.assertIn("bootstrap_verify.py", archive.namelist())
            self.assertEqual(handoff["sha256"], hashlib.sha256(archive.read("manage.py")).hexdigest())
        self.assertEqual(handoff["args"][:2], ["-I", "-B"])
        self.assertEqual(len(handoff["args"]), 3)


@unittest.skipUnless(os.environ.get("LEDGER_BOOTSTRAP_LIVE_READONLY") == "1", "Read-only installed gh verification is opt-in")
class LiveReadOnlyTests(unittest.TestCase):
    def setUp(self):
        self.gh = shutil.which("gh")
        self.assertIsNotNone(self.gh, "Installed gh is required; this test never installs or authenticates")
        self.repository = os.environ.get("LEDGER_BOOTSTRAP_API_REPOSITORY", "be-mihai-mlesnita/ledger-memory")
        self.assertIn(self.repository, {"be-mihai-mlesnita/ledger-memory", "cli/cli"})
        self.tmp = tempfile.TemporaryDirectory(prefix="bootstrap-live-readonly-")
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        config = os.environ.get("GH_CONFIG_DIR", str(Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))) / "gh"))
        self.env = {"HOME": str(self.home), "TMPDIR": str(self.home), "GH_CONFIG_DIR": config,
                    "XDG_CACHE_HOME": str(self.home / "cache"), "GH_HOST": "github.com", "GH_PROMPT_DISABLED": "1",
                    "PATH": os.environ.get("PATH", os.defpath)}
        for key in ("GH_TOKEN", "GITHUB_TOKEN"):
            if key in os.environ:
                self.env[key] = os.environ[key]
        self.filter = (ROOT / "bootstrap.sh").read_text().split("manifest_filter='\n", 1)[1].split("'\ngh api", 1)[0]
        self.manifest = {"schema_version": 1, "ready": True, "repository": "be-mihai-mlesnita/ledger-memory",
                         "platform": "darwin", "architectures": ["arm64", "x86_64"], "version": "9.8.7-rc2",
                         "tag": "test-only-9.8.7-rc2", "asset": "test-only.zip", "sha256": "e" * 64,
                         "kit_commit": KIT_COMMIT, "layout": "root"}

    def get(self, suffix="", query=".", raw=False):
        # Only GET; --jq operates on the response locally. No fixture data is sent.
        args = [self.gh, "api", "--hostname", "github.com", "--method", "GET",
                "repos/" + self.repository + suffix, "--jq", query]
        if raw:
            args += ["-H", "Accept: application/vnd.github.raw+json"]
        return subprocess.run(args, cwd=self.home, env=self.env, text=True, capture_output=True, timeout=45)

    def test_installed_gh_manifest_filter_enabled(self):
        result = self.get(query=json.dumps(self.manifest) + " | " + self.filter)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines(), ["ready", "9.8.7-rc2", "test-only-9.8.7-rc2", "test-only.zip", "e" * 64, KIT_COMMIT, "root"])

    def test_installed_gh_manifest_filter_disabled(self):
        self.manifest["ready"] = False
        result = self.get(query=json.dumps(self.manifest) + " | " + self.filter)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "disabled")

    def test_installed_gh_manifest_filter_invalid(self):
        self.manifest["tag"] = "test-only\nINJECTED"
        result = self.get(query=json.dumps(self.manifest) + " | " + self.filter)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Invalid enabled distribution manifest", result.stderr)

    def test_installed_gh_raw_json_contents_at_resolved_ref(self):
        branch = "main"
        if self.repository == "cli/cli":
            default = self.get(query=".default_branch")
            self.assertEqual(default.returncode, 0, default.stderr)
            branch = default.stdout.strip()
            self.assertRegex(branch, r"\A[A-Za-z0-9_-]+\Z")
        result = self.get("/commits/" + branch, ".sha")
        self.assertEqual(result.returncode, 0, result.stderr)
        commit = result.stdout.strip()
        self.assertRegex(commit, r"\A[0-9a-f]{40}\Z")
        tree = self.get("/git/trees/" + commit + "?recursive=1", '[.tree[] | select(.type == "blob" and (.path | endswith(".json")) and (.path | startswith(".") | not)) | .path][0]')
        self.assertEqual(tree.returncode, 0, tree.stderr)
        path = tree.stdout.strip()
        self.assertRegex(path, r"\A[A-Za-z0-9._/-]+\.json\Z")
        contents = self.get("/contents/" + path + "?ref=" + commit, "type", raw=True)
        self.assertEqual(contents.returncode, 0, contents.stderr)
        self.assertIn(contents.stdout.strip(), {"object", "array"})


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="bootstrap-zip-tests-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def reject(self, blob, layout="root", digest=None):
        archive = self.root / "kit.zip"
        archive.write_bytes(blob)
        destination = self.root / "extracted"
        with self.assertRaises((ValueError, zipfile.BadZipFile, OSError)):
            VERIFY.extract(archive, destination, layout, digest or hashlib.sha256(blob).hexdigest())
        self.assertFalse(destination.exists())

    def test_path_injections(self):
        for path in ("../outside", "/tmp/outside", "gateway/../../outside", "gateway\\outside",
                     "gateway//outside", "./manage.py", "deployment/manage.py", "bad\x00.py", "g\u00e4teway/file"):
            with self.subTest(path=path):
                self.reject(kit_bytes(extras=[(path, b"bad")]))

    def test_duplicates_case_collisions_and_unknown_code(self):
        for path in ("manage.py", "Manage.py", "evil.py", "gateway/evil.sh", "gateway"):
            with self.subTest(path=path):
                self.reject(kit_bytes(extras=[(path, b"bad")]))

    def test_links_special_permissions_and_extra_metadata(self):
        for mode, extra in ((stat.S_IFLNK | 0o777, b""), (stat.S_IFIFO | 0o600, b""),
                            (stat.S_IFREG | 0o4600, b""), (stat.S_IFREG | 0o600, b"\x0d\x00\x00\x00")):
            with self.subTest(mode=mode, extra=extra):
                info = zipfile.ZipInfo("README.md")
                info.create_system = 3
                info.external_attr = mode << 16
                info.extra = extra
                self.reject(kit_bytes(extras=[(info, b"manage.py")]))

    def test_executable_documentation_refused(self):
        info = zipfile.ZipInfo("README.md")
        info.external_attr = (stat.S_IFREG | 0o755) << 16
        self.reject(kit_bytes(extras=[(info, b"not code")]))

    def test_missing_manager_and_mismatched_layout(self):
        self.reject(kit_bytes(missing=["manage.py"]))
        self.reject(kit_bytes("deployment"))
        self.reject(kit_bytes(), layout="deployment")

    def test_core_pair_required_and_only_one_core_allowed(self):
        self.reject(kit_bytes(missing=[CORE + ".sha256"]))
        self.reject(kit_bytes(extras=[("ledger-memory-1.0.0.zip", b"bad")]))

    def test_digest_and_corrupt_zip(self):
        self.reject(kit_bytes(), digest="0" * 64)
        self.reject(b"not a zip")
        self.reject(kit_bytes()[:-20])

    def test_crc_mismatch_checked_before_extraction(self):
        blob = io.BytesIO()
        with zipfile.ZipFile(io.BytesIO(kit_bytes())) as original, zipfile.ZipFile(blob, "w") as stored:
            for info in original.infolist():
                stored.writestr(info.filename, original.read(info))
        self.reject(blob.getvalue().replace(b"# inert fixture\n", b"# wrong fixture\n", 1))

    def test_limits_before_extraction(self):
        old = VERIFY.MAX_BYTES
        try:
            VERIFY.MAX_BYTES = 16
            self.reject(kit_bytes())
        finally:
            VERIFY.MAX_BYTES = old

    def test_directory_zipbomb_rejected_before_crc(self):
        for compression in (zipfile.ZIP_DEFLATED, zipfile.ZIP_BZIP2):
            with self.subTest(compression=compression):
                directory = zipfile.ZipInfo("gateway/")
                directory.compress_type = compression
                blob = kit_bytes(extras=[(directory, b"x" * 1024 * 1024)])
                self.assertLess(len(blob), 16384)
                with mock.patch.object(VERIFY, "MAX_BYTES", 16384), mock.patch.object(
                    zipfile.ZipFile, "testzip", side_effect=AssertionError("Directory data must not reach CRC decompression")
                ) as crc:
                    self.reject(blob)
                    crc.assert_not_called()

    def test_nonempty_directory_rejected_below_size_limit(self):
        with mock.patch.object(zipfile.ZipFile, "testzip", side_effect=AssertionError("Nonempty directory reached CRC")) as crc:
            self.reject(kit_bytes(extras=[("gateway/", b"x")]))
            crc.assert_not_called()

    def test_unsupported_compression_on_empty_directory_rejected(self):
        for compression in (zipfile.ZIP_BZIP2, zipfile.ZIP_LZMA):
            with self.subTest(compression=compression):
                directory = zipfile.ZipInfo("gateway/")
                directory.compress_type = compression
                with mock.patch.object(zipfile.ZipFile, "testzip", side_effect=AssertionError("Unsupported directory compression reached CRC")) as crc:
                    self.reject(kit_bytes(extras=[(directory, b"")]))
                    crc.assert_not_called()

    def test_aggregate_uncompressed_limit_before_crc(self):
        blob = kit_bytes(extras=[("README.md", b"x" * 8192), ("VALIDATION.md", b"x" * 8192)])
        self.assertLess(len(blob), 16384)
        with mock.patch.object(VERIFY, "MAX_BYTES", 16384), mock.patch.object(
            zipfile.ZipFile, "testzip", side_effect=AssertionError("Oversized aggregate reached CRC")
        ) as crc:
            self.reject(blob)
            crc.assert_not_called()

    def test_normal_kit_with_empty_directories_accepted(self):
        for layout in ("root", "deployment"):
            for compression in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
                with self.subTest(layout=layout, compression=compression):
                    names = ["gateway/"] if layout == "root" else ["deployment/", "deployment/gateway/"]
                    directories = []
                    for name in names:
                        directory = zipfile.ZipInfo(name)
                        directory.compress_type = compression
                        directories.append((directory, b""))
                    blob = kit_bytes(layout=layout, extras=directories)
                    archive = self.root / "kit.zip"
                    archive.write_bytes(blob)
                    destination = self.root / (layout + "-" + str(compression))
                    VERIFY.extract(archive, destination, layout, hashlib.sha256(blob).hexdigest())
                    prefix = destination if layout == "root" else destination / "deployment"
                    self.assertEqual((prefix / "manage.py").read_bytes(), MANAGER)
                    self.assertTrue((prefix / "gateway").is_dir())

    def test_member_count_limit(self):
        old = VERIFY.MAX_FILES
        try:
            VERIFY.MAX_FILES = 1
            self.reject(kit_bytes())
        finally:
            VERIFY.MAX_FILES = old

    def test_symlink_input_is_refused(self):
        real = self.root / "real.zip"
        real.write_bytes(kit_bytes())
        linked = self.root / "linked.zip"
        linked.symlink_to(real)
        with self.assertRaises(ValueError):
            VERIFY.extract(linked, self.root / "extracted", "root", hashlib.sha256(real.read_bytes()).hexdigest())
        self.assertFalse((self.root / "extracted").exists())

    def test_existing_destination_preserved(self):
        archive = self.root / "kit.zip"
        archive.write_bytes(kit_bytes())
        destination = self.root / "existing"
        destination.mkdir()
        (destination / "keep").write_text("untouched")
        with self.assertRaises(ValueError):
            VERIFY.extract(archive, destination, "root", hashlib.sha256(archive.read_bytes()).hexdigest())
        self.assertEqual((destination / "keep").read_text(), "untouched")

    def test_private_extraction_permissions(self):
        archive = self.root / "kit.zip"
        archive.write_bytes(kit_bytes())
        destination = self.root / "extracted"
        VERIFY.extract(archive, destination, "root", hashlib.sha256(archive.read_bytes()).hexdigest())
        self.assertEqual(stat.S_IMODE(destination.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE((destination / "manage.py").stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE((destination / CORE).stat().st_mode), 0o600)


if __name__ == "__main__":
    unittest.main()
