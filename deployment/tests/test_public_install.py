"""Offline public launcher fixtures; never run real brew, downloads or models."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_bootstrap as legacy

ROOT = legacy.ROOT
BREW_URL = "https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh"
RAW = "https://raw.githubusercontent.com/be-mihai-mlesnita/ledger-memory/"
# Explicit proposed paste commands, independent of concurrently edited README.
CURL_COMMAND = "/bin/bash -c 's=$(curl --disable -fsSL --proto =https --proto-redir =https " + RAW + "main/install.sh) && test -n \"$s\" && /bin/bash -c \"$s\"'"
WGET_COMMAND = "/bin/bash -c 's=$(wget --no-config --no-netrc --https-only -qO- " + RAW + "main/install.sh) && test -n \"$s\" && /bin/bash -c \"$s\"'"

STUB = r'''
import hashlib, json, os, pathlib, signal, sys
root = pathlib.Path(os.environ["FIXTURE"])
name = pathlib.Path(sys.argv[0]).name
args = sys.argv[1:]
mode = os.environ.get("FAKE_MODE", "")
with (root / "calls.jsonl").open("a") as stream:
    stream.write(json.dumps([name, args, os.environ["PATH"]]) + "\n")
def link(command, parent):
    target = parent / command
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.exists():
        target.symlink_to(root / "stub")
if name == "uname":
    print(os.environ.get("FAKE_OS", "Darwin") if args == ["-s"] else os.environ.get("FAKE_ARCH", "arm64"))
elif name == "id":
    assert args == ["-u"]
    print(os.environ.get("FAKE_UID", "501"))
elif name in {"curl", "wget"}:
    url = args[-1]
    assert url.startswith("https://")
    assert "api.github.com" not in url
    if name == "curl":
        assert args[0] == "--disable"
        assert "--proto" in args and "--proto-redir" in args
        assert args[args.index("--proto") + 1] == "=https"
        assert args[args.index("--proto-redir") + 1] == "=https"
        assert not any(a in args for a in ("-k", "--insecure", "--netrc", "-u"))
        target = pathlib.Path(args[args.index("--output") + 1]) if "--output" in args else None
    else:
        assert "--no-config" in args and "--no-netrc" in args and "--https-only" in args
        assert "--no-check-certificate" not in args
        target = next((pathlib.Path(a.split("=", 1)[1]) for a in args if a.startswith("--output-document=")), None)
    raw = "https://raw.githubusercontent.com/be-mihai-mlesnita/ledger-memory/"
    if url == "https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh":
        kind, data = "homebrew", b"#!/bin/bash\nhomebrew-install\n"
    elif url == raw + "main/install.sh":
        kind, data = "entry", pathlib.Path(os.environ["ENTRY_SCRIPT"]).read_bytes()
    elif url == raw + "main/deployment/distribution.json":
        kind, data = "manifest", (root / "manifest.json").read_bytes()
    else:
        m = json.loads((root / "manifest.json").read_text())
        if url == raw + m["kit_commit"] + "/deployment/bootstrap_verify.py":
            kind, data = "verifier", pathlib.Path(os.environ["HELPER"]).read_bytes()
        elif url == "https://github.com/be-mihai-mlesnita/ledger-memory/releases/download/" + m["tag"] + "/" + m["asset"]:
            kind, data = "kit", (root / "kit.zip").read_bytes()
        else:
            raise AssertionError(url)
    if mode == "partial-" + kind:
        data = b"homebrew-install\n" if kind in {"entry", "homebrew"} else b"raise RuntimeError('PARTIAL_EXECUTED')\n"
    elif mode == "empty-" + kind:
        data = b""
    elif mode == "syntax-homebrew" and kind == "homebrew":
        data = b"homebrew-install\nif ;\n"
    elif mode == "invalid-json" and kind == "manifest":
        data = b"not json"
    elif mode == "duplicate-json" and kind == "manifest":
        data = data[:-1] + b', "ready": true}'
    elif mode == "oversized-json" and kind == "manifest":
        data += b" " * 65537
    elif mode == "deep-json" and kind == "manifest":
        data = b"[" * 2000 + b"]" * 2000
    elif mode == "invalid-utf8" and kind == "manifest":
        data = b"\xff"
    if target:
        assert target.parent.parent == pathlib.Path(os.environ["TMPDIR"])
        assert target.parent.stat().st_mode & 0o777 == 0o700
        target.write_bytes(data)
    else:
        sys.stdout.buffer.write(data)
        sys.stdout.flush()
    if mode in {"terminate-kit", "interrupt-kit", "hangup-kit"} and kind == "kit":
        os.kill(os.getppid(), {"terminate-kit": signal.SIGTERM, "interrupt-kit": signal.SIGINT, "hangup-kit": signal.SIGHUP}[mode])
    raise SystemExit(1 if mode == "partial-" + kind else 0)
elif name == "homebrew-install":
    assert all(os.isatty(fd) for fd in (0, 1, 2))
    assert os.environ.get("INTERACTIVE") == "1"
    assert "NONINTERACTIVE" not in os.environ and "CI" not in os.environ
    (root / "homebrew-ran").write_text("inert fixture")
    if mode == "homebrew-fail":
        raise SystemExit(1)
    if mode != "homebrew-unavailable":
        link("brew", pathlib.Path(os.environ["BREW_PREFIX"]) / "bin")
elif name == "brew":
    if args == ["--prefix"]:
        if mode == "prefix-fail":
            raise SystemExit(1)
        print(os.environ.get("FAKE_PREFIX", os.environ["BREW_PREFIX"]))
    else:
        assert args[0] == "install" and set(args[1:]) <= {"python@3.13", "uv"}
        assert all(os.isatty(fd) for fd in (0, 1, 2))
        for key in ("HOMEBREW_NO_AUTO_UPDATE", "HOMEBREW_NO_INSTALL_CLEANUP", "HOMEBREW_NO_ANALYTICS"):
            assert os.environ.get(key) == "1"
        if mode == "formula-fail":
            raise SystemExit(1)
        prefix = pathlib.Path(os.environ["BREW_PREFIX"])
        if "python@3.13" in args and mode != "python-unavailable":
            link("python3.13", prefix / "opt/python@3.13/bin")
            (root / "python-installed").touch()
        if "uv" in args and mode != "uv-unavailable":
            link("uv", prefix / "bin")
            (root / "uv-installed").touch()
elif name.startswith("python"):
    assert args[:2] == ["-I", "-B"]
    if args[2] == "-c" and "sys.version_info" in args[3]:
        assert "== (3, 13)" in args[3]
        raise SystemExit(1 if os.environ.get("FAKE_PYTHON") == "broken" and not (root / "python-installed").exists() else 0)
    if pathlib.Path(args[2]).name == "manage.py":
        assert all(os.isatty(fd) for fd in (0, 1, 2))
        assert os.environ["HOME"] == str(root / "isolated-home")
        if os.environ.get("FAKE_INTERCEPT_MANAGER") == "yes":
            (root / "isolated-home/manager.json").write_text(json.dumps({
                "args": args, "sha256": hashlib.sha256(pathlib.Path(args[2]).read_bytes()).hexdigest()}))
            raise SystemExit(0)
    os.execv(sys.executable, [sys.executable] + args)
elif name == "uv":
    assert args in (["--version"], ["sync", "--help"])
    if os.environ.get("FAKE_UV") == "broken" and not (root / "uv-installed").exists():
        raise SystemExit(1)
    if args == ["--version"]:
        print("uv 0.9.0 (inert fixture)")
    elif os.environ.get("FAKE_UV") == "sync-broken" or (
        os.environ.get("FAKE_UV") == "pre-project" and not (root / "uv-installed").exists()
    ):
        raise SystemExit(1)
else:
    raise AssertionError(name)
'''


class PublicInstallTests(unittest.TestCase):
    def test_readme_contains_the_tested_one_line_commands(self):
        blocks = (ROOT / "README.md").read_text().split("```bash\n")[1:]
        commands = [block.split("```", 1)[0].strip() for block in blocks]
        self.assertEqual(commands, [CURL_COMMAND, WGET_COMMAND])
        self.assertTrue(all(len(command.splitlines()) == 1 for command in commands))

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="public-install-tests-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.home = self.root / "isolated-home"
        self.bin = self.root / "bin"
        self.temps = self.root / "private-temps"
        for directory in (self.home, self.bin, self.temps):
            directory.mkdir()
        (self.temps / "unrelated").write_text("keep")
        self.profiles = (".zprofile", ".zshrc", ".bash_profile", ".profile")
        for name in self.profiles:
            (self.home / name).write_text("unchanged\n")
        self.prefixes = [self.root / "opt/homebrew", self.root / "usr/local"]
        for prefix in self.prefixes:
            (prefix / "bin").mkdir(parents=True)
        self.prefix = self.prefixes[0]
        self.entry = self.root / "install.sh"
        source = (ROOT / "install.sh").read_text()
        # Only private test copies replace standard prefixes; real brew is unreachable.
        for original, prefix in zip(("/opt/homebrew/bin/brew", "/usr/local/bin/brew"), self.prefixes):
            self.assertIn(original, source)
            source = source.replace(original, shlex.quote(str(prefix / "bin/brew")))
        self.entry.write_text(source)
        stub = self.root / "stub"
        stub.write_text("#!" + sys.executable + "\n" + STUB)
        stub.chmod(0o700)
        for name in ("uname", "id", "curl", "wget", "homebrew-install", "brew", "python3.13", "uv"):
            (self.bin / name).symlink_to(stub)
        for name in ("mktemp", "rm"):
            (self.bin / name).symlink_to(shutil.which(name))
        self.env = {"HOME": str(self.home), "PATH": str(self.bin), "TMPDIR": str(self.temps),
                    "FIXTURE": str(self.root), "HELPER": str(legacy.HELPER),
                    "ENTRY_SCRIPT": str(self.entry), "BREW_PREFIX": str(self.prefix)}
        self.manifest = {"schema_version": 1, "ready": True,
                         "repository": "be-mihai-mlesnita/ledger-memory", "platform": "darwin",
                         "architectures": ["arm64", "x86_64"], "version": "9.8.7-rc2",
                         "tag": "test-kit-9.8.7-rc2", "asset": "test-kit-9.8.7-rc2.zip",
                         "kit_commit": legacy.KIT_COMMIT, "layout": "root"}
        self.set_kit(legacy.kit_bytes())

    set_kit = legacy.BootstrapTests.set_kit
    calls = legacy.BootstrapTests.calls

    def run_install(self, responses=(), command=None):
        result = legacy.BootstrapTests.run_bootstrap(
            self, responses=responses, command=command or ["/bin/bash", str(self.entry)])
        for name in self.profiles:
            self.assertEqual((self.home / name).read_text(), "unchanged\n")
        self.assertEqual(self.env["PATH"], str(self.bin))
        self.assertFalse((self.home / "PWNED").exists())
        return result

    def refuse(self, text, responses=()):
        code, output = self.run_install(responses)
        self.assertNotEqual(code, 0, output)
        self.assertIn(text, output)
        self.assertFalse((self.home / "manager.json").exists())
        self.assertNotIn("PARTIAL_EXECUTED", output)
        return output

    def test_existing_tools_both_architectures_and_layouts(self):
        for arch, layout in (("arm64", "root"), ("x86_64", "deployment")):
            with self.subTest(arch=arch):
                self.env["FAKE_ARCH"] = arch
                self.manifest["layout"] = layout
                self.set_kit(legacy.kit_bytes(layout))
                code, output = self.run_install()
                self.assertEqual(code, 0, output)
                handoff = json.loads((self.home / "manager.json").read_text())
                self.assertEqual(handoff["isolated"], 1)
                self.assertTrue(handoff["bytecode"])
                self.assertEqual(len(handoff["argv"]), 1)
                self.assertFalse(Path(handoff["argv"][0]).exists())
        self.assertFalse(any(name == "gh" or args[:1] == ["install"] for name, args, _ in self.calls()))

    def test_curl_preferred_and_verifier_commit_pinned(self):
        code, output = self.run_install()
        self.assertEqual(code, 0, output)
        urls = [args[-1] for name, args, _ in self.calls() if name == "curl"]
        self.assertEqual(urls, [RAW + "main/deployment/distribution.json",
                               RAW + legacy.KIT_COMMIT + "/deployment/bootstrap_verify.py",
                               "https://github.com/be-mihai-mlesnita/ledger-memory/releases/download/" + self.manifest["tag"] + "/" + self.manifest["asset"]])
        self.assertFalse(any(name in {"wget", "gh"} for name, _, _ in self.calls()))

    def test_wget_fallback(self):
        (self.bin / "curl").unlink()
        code, output = self.run_install()
        self.assertEqual(code, 0, output)
        self.assertEqual(sum(name == "wget" for name, _, _ in self.calls()), 3)

    def test_no_downloader(self):
        for name in ("curl", "wget"):
            (self.bin / name).unlink()
        self.refuse("curl or wget is required")

    def test_root_os_arch_and_non_tty_stop_before_prerequisites(self):
        for key, value, text in (("FAKE_UID", "0", "normal user"), ("FAKE_OS", "Linux", "macOS only"),
                                 ("FAKE_ARCH", "i386", "Supported Mac architectures")):
            with self.subTest(key=key):
                self.env[key] = value
                self.refuse(text)
                del self.env[key]
        self.refuse("real /dev/tty", responses=None)
        self.assertFalse(any(name in {"brew", "curl", "wget"} for name, _, _ in self.calls()))

    def test_hidden_brew_both_standard_prefixes(self):
        (self.bin / "brew").unlink()
        for prefix in self.prefixes:
            with self.subTest(prefix=prefix):
                self.env["BREW_PREFIX"] = str(prefix)
                brew = prefix / "bin/brew"
                brew.symlink_to(self.root / "stub")
                code, output = self.run_install()
                self.assertEqual(code, 0, output)
                brew.unlink()
        self.assertFalse((self.root / "homebrew-ran").exists())

    def test_invalid_brew_candidates_are_not_executed(self):
        (self.bin / "brew").unlink()
        candidates = [self.bin / "brew"] + [p / "bin/brew" for p in self.prefixes]
        for kind in ("directory", "nonexecutable"):
            with self.subTest(kind=kind):
                for candidate in candidates:
                    if kind == "directory":
                        candidate.mkdir(mode=0o700)
                    else:
                        candidate.write_text("must not execute\n")
                        candidate.chmod(0o600)
                self.refuse("Cancelled", ["no"])
                for candidate in candidates:
                    candidate.rmdir() if kind == "directory" else candidate.unlink()
        self.assertFalse(any(name in {"brew", "homebrew-install", "curl", "wget"} for name, _, _ in self.calls()))

    def test_relative_path_brew_is_reused(self):
        relative_bin = self.home / "relative-tools"
        relative_bin.mkdir()
        (relative_bin / "brew").symlink_to(self.root / "stub")
        command = 'export PATH="relative-tools:$PATH"; /bin/bash ' + shlex.quote(str(self.entry))
        code, output = self.run_install(command=["/bin/bash", "-c", command])
        self.assertEqual(code, 0, output)
        self.assertFalse((self.root / "homebrew-ran").exists())
        self.assertTrue(any(name == "brew" and args == ["--prefix"] for name, args, _ in self.calls()))

    def test_missing_homebrew_decline_or_eof_even_with_working_tools(self):
        (self.bin / "brew").unlink()
        for response in ("no", "", "y", "\x04"):
            with self.subTest(response=response):
                output = self.refuse("Cancelled", [response])
                self.assertIn(BREW_URL, output)
                self.assertIn("not checksum-pinned", output)
                self.assertIn("Command Line Tools", output)
        self.assertFalse(any(name in {"curl", "wget", "homebrew-install"} for name, _, _ in self.calls()))

    def test_missing_homebrew_install_is_interactive_and_separate(self):
        for name in ("brew", "python3.13", "uv"):
            (self.bin / name).unlink()
        self.env.update(CI="1", NONINTERACTIVE="1")
        code, output = self.run_install(["YeS", "oK"])
        self.assertEqual(code, 0, output)
        self.assertTrue((self.root / "homebrew-ran").exists())
        self.assertEqual(sum("anything else cancels]:" in line for line in output.splitlines()), 2)
        self.assertIn("may remain", output)
        self.assertIn("even for uninstall", output)
        self.assertIn("python@3.13", output)
        self.assertIn("HOMEBREW_NO_ANALYTICS=1", output)

    def test_homebrew_partial_empty_or_invalid_script_never_executes(self):
        (self.bin / "brew").unlink()
        for mode, text in (("partial-homebrew", "will not"), ("empty-homebrew", "Empty download"),
                           ("syntax-homebrew", "will not")):
            with self.subTest(mode=mode):
                self.env["FAKE_MODE"] = mode
                self.refuse(text, ["yes"])
                self.assertFalse((self.root / "homebrew-ran").exists())

    def test_homebrew_failure_or_missing_result_stops(self):
        (self.bin / "brew").unlink()
        for mode, text in (("homebrew-fail", "failed or was cancelled"), ("homebrew-unavailable", "still unavailable")):
            with self.subTest(mode=mode):
                self.env["FAKE_MODE"] = mode
                self.refuse(text, ["yes"])
        self.assertFalse(any(name == "brew" for name, _, _ in self.calls()))

    def install_missing(self, missing):
        for name in missing:
            (self.bin / name).unlink()
        code, output = self.run_install(["OK"])
        self.assertEqual(code, 0, output)
        installs = [args for name, args, _ in self.calls() if name == "brew" and args[:1] == ["install"]]
        self.assertEqual(installs, [["install"] + ["python@3.13" if n == "python3.13" else n for n in missing]])

    def test_only_missing_python_installed(self):
        self.install_missing(("python3.13",))

    def test_only_missing_uv_installed(self):
        self.install_missing(("uv",))

    def test_both_missing_formulae_installed(self):
        self.install_missing(("python3.13", "uv"))

    def test_broken_tools_are_rechecked_after_install(self):
        self.env.update(FAKE_PYTHON="broken", FAKE_UV="broken")
        code, output = self.run_install(["yes"])
        self.assertEqual(code, 0, output)
        self.assertTrue((self.root / "python-installed").exists())
        self.assertTrue((self.root / "uv-installed").exists())

    def test_existing_uv_sync_help_is_probed_and_reused(self):
        code, output = self.run_install()
        self.assertEqual(code, 0, output)
        self.assertEqual([args for name, args, _ in self.calls() if name == "uv"],
                         [["--version"], ["sync", "--help"], ["--version"], ["sync", "--help"]])
        self.assertFalse(any(name == "brew" and args[:1] == ["install"] for name, args, _ in self.calls()))

    def test_pre_project_uv_requires_consent_and_successful_recheck(self):
        self.env["FAKE_UV"] = "pre-project"
        self.refuse("Cancelled", ["no"])
        self.assertFalse((self.root / "uv-installed").exists())
        self.assertFalse(any(name in {"curl", "wget"} for name, _, _ in self.calls()))
        code, output = self.run_install(["yes"])
        self.assertEqual(code, 0, output)
        self.assertEqual([args for name, args, _ in self.calls() if name == "brew" and args[:1] == ["install"]],
                         [["install", "uv"]])

    def test_uv_sync_help_failure_after_install_stops(self):
        self.env["FAKE_UV"] = "sync-broken"
        self.refuse("uv is still unavailable", ["yes"])
        self.assertTrue((self.root / "uv-installed").exists())
        self.assertFalse(any(name in {"curl", "wget"} for name, _, _ in self.calls()))

    def test_formula_decline_eof_failure_and_missing_executable(self):
        (self.bin / "python3.13").unlink()
        (self.bin / "uv").unlink()
        for response in ("no", "\x04"):
            self.refuse("Cancelled", [response])
        self.assertFalse(any(name == "brew" and args[:1] == ["install"] for name, args, _ in self.calls()))
        for mode, text in (("formula-fail", "installation failed"), ("python-unavailable", "Python 3.13 is still unavailable")):
            with self.subTest(mode=mode):
                self.env["FAKE_MODE"] = mode
                self.refuse(text, ["yes"])
        self.assertFalse(any(name in {"curl", "wget"} for name, _, _ in self.calls()))

    def test_uv_install_missing_executable_stops(self):
        (self.bin / "uv").unlink()
        self.env["FAKE_MODE"] = "uv-unavailable"
        self.refuse("uv is still unavailable", ["yes"])

    def test_prefix_is_data_not_code_or_path_list(self):
        for prefix in ("", "/", "relative", "/tmp\nINJECTED", "/tmp:/evil", "/tmp/../evil", "/tmp/./evil", "/tmp/$(touch PWNED)", "/nonexistent-fixture"):
            with self.subTest(prefix=prefix):
                self.env["FAKE_PREFIX"] = prefix
                self.refuse("prefix")
        self.env.pop("FAKE_PREFIX")
        self.env["FAKE_MODE"] = "prefix-fail"
        self.refuse("Could not read Homebrew prefix")

    def test_prefix_with_spaces_only_changes_child_path(self):
        prefix = self.root / "brew prefix"
        prefix.mkdir()
        self.env["BREW_PREFIX"] = str(prefix)
        code, output = self.run_install()
        self.assertEqual(code, 0, output)
        manager_paths = [path for name, args, path in self.calls()
                         if name.startswith("python") and Path(args[2]).name == "manage.py"]
        self.assertEqual(manager_paths, [f"{prefix}/bin:{prefix}/sbin:{prefix}/opt/python@3.13/bin:{self.bin}"])

    def test_strict_manifest_types_and_injections(self):
        original = self.manifest.copy()
        for field, value in (("schema_version", True), ("schema_version", 1.0), ("schema_version", 2),
                             ("ready", False), ("ready", 1), ("ready", "true"), ("repository", "evil.example/repo"),
                             ("platform", "linux"), ("architectures", ["arm64"]), ("architectures", "arm64,x86_64"),
                             ("layout", "../../"), ("layout", None), ("version", "1.2.3\n"), ("version", 123),
                             ("tag", "../main"), ("tag", "$(touch PWNED)"), ("tag", "https://evil.example"),
                             ("tag", "v1?ref=main"), ("tag", "v1\n"), ("tag", "a" * 129),
                             ("asset", "../x.zip"), ("asset", "-x.zip"), ("asset", "*.zip"), ("asset", "x.zip\n"),
                             ("sha256", "A" * 64), ("sha256", "0" * 63), ("sha256", "0" * 64 + "\n"),
                             ("kit_commit", "main"), ("kit_commit", "b" * 39), ("kit_commit", "B" * 40),
                             ("kit_commit", "b" * 40 + "/evil"), ("unknown", "extra"), ("reason", False)):
            with self.subTest(field=field, value=value):
                self.manifest = dict(original, **{field: value})
                self.refuse("Invalid distribution manifest")
        self.manifest = {k: v for k, v in original.items() if k != "ready"}
        self.refuse("Invalid distribution manifest")
        self.assertFalse(any(args[-1].endswith("bootstrap_verify.py") for name, args, _ in self.calls() if name == "curl"))

    def test_published_manifest_shape_with_inert_fixture_kit(self):
        self.manifest = json.loads((ROOT / "deployment/distribution.json").read_text())
        self.assertEqual(self.manifest["schema_version"], 1)
        self.assertEqual(self.manifest["layout"], "root")
        self.assertIsInstance(self.manifest.get("reason"), str)
        # Preserve the schema and source/tag routes, substitute ONLY inert kit bytes/hash.
        self.set_kit(legacy.kit_bytes())
        code, output = self.run_install()
        self.assertEqual(code, 0, output)
        self.assertTrue((self.home / "manager.json").exists())

    @unittest.skipUnless(os.environ.get("LEDGER_BOOTSTRAP_TEST_KIT"), "Set LEDGER_BOOTSTRAP_TEST_KIT to the local current release kit")
    def test_current_release_kit_verified_without_executing_real_manager(self):
        kit = Path(os.environ["LEDGER_BOOTSTRAP_TEST_KIT"]).resolve()
        self.assertTrue(kit.is_relative_to(ROOT), "Only project-local kit input is permitted")
        self.manifest = json.loads((ROOT / "deployment/distribution.json").read_text())
        blob = kit.read_bytes()
        self.assertEqual(hashlib.sha256(blob).hexdigest(), self.manifest["sha256"])
        (self.root / "kit.zip").write_bytes(blob)
        self.env["FAKE_INTERCEPT_MANAGER"] = "yes"
        with zipfile.ZipFile(kit) as archive:
            self.assertEqual(archive.read("bootstrap_verify.py"), legacy.HELPER.read_bytes())
            manager_digest = hashlib.sha256(archive.read("manage.py")).hexdigest()
        code, output = self.run_install()
        self.assertEqual(code, 0, output)
        handoff = json.loads((self.home / "manager.json").read_text())
        self.assertEqual(handoff["sha256"], manager_digest)
        self.assertEqual(handoff["args"][:2], ["-I", "-B"])
        self.assertEqual(len(handoff["args"]), 3)
        self.assertFalse(Path(handoff["args"][2]).exists())

    def test_malformed_duplicate_bounded_and_deep_json(self):
        for mode in ("invalid-json", "duplicate-json", "oversized-json", "deep-json", "invalid-utf8"):
            with self.subTest(mode=mode):
                self.env["FAKE_MODE"] = mode
                self.refuse("Invalid distribution manifest")

    def test_all_partial_empty_downloads_stop_for_both_clients(self):
        for client in ("curl", "wget"):
            if client == "wget":
                (self.bin / "curl").unlink()
            for kind in ("manifest", "verifier", "kit"):
                for failure in ("partial", "empty"):
                    with self.subTest(client=client, kind=kind, failure=failure):
                        self.env["FAKE_MODE"] = failure + "-" + kind
                        self.refuse("download" if failure == "empty" else "Download failed")
        self.assertFalse(any(args[2:3] and Path(args[2]).name == "bootstrap_verify.py" for name, args, _ in self.calls() if name.startswith("python")))

    def test_wrong_digest_stops_before_verifier_execution(self):
        self.manifest["sha256"] = "0" * 64
        self.refuse("SHA-256 mismatch")
        self.assertFalse(any(Path(args[2]).name == "bootstrap_verify.py" for name, args, _ in self.calls() if name.startswith("python")))

    def test_unsafe_archive_never_hands_off(self):
        self.set_kit(legacy.kit_bytes(extras=[("../escaped.py", b"bad")]))
        self.refuse("Unsafe or invalid kit")

    def test_manager_cancellation_failure_and_signal_cleanup(self):
        for status in (0, 23, 130):
            self.env["FAKE_MANAGER_EXIT"] = str(status)
            code, output = self.run_install()
            self.assertEqual(code, status, output)
        (self.home / "manager.json").unlink()
        for mode, status in (("terminate-kit", 143), ("interrupt-kit", 130), ("hangup-kit", 143)):
            self.env["FAKE_MODE"] = mode
            code, output = self.run_install()
            self.assertEqual(code, status, output)
            self.assertFalse((self.home / "manager.json").exists())

    def test_public_paste_commands_bash_and_zsh(self):
        for shell in ("/bin/bash", shutil.which("zsh")):
            if not shell:
                continue
            for command in (CURL_COMMAND, WGET_COMMAND):
                with self.subTest(shell=shell, command=command):
                    code, output = self.run_install(command=[shell, "-f", "-c", command])
                    self.assertEqual(code, 0, output)
                    self.assertTrue((self.home / "manager.json").exists())

    def test_public_paste_never_executes_failed_or_empty_entry(self):
        for command in (CURL_COMMAND, WGET_COMMAND):
            for mode in ("partial-entry", "empty-entry"):
                with self.subTest(command=command, mode=mode):
                    self.env["FAKE_MODE"] = mode
                    code, output = self.run_install(command=["/bin/bash", "-c", command])
                    self.assertNotEqual(code, 0, output)
                    self.assertFalse((self.root / "homebrew-ran").exists())
                    self.assertFalse((self.home / "manager.json").exists())

    def test_bash_syntax(self):
        result = subprocess.run(["/bin/bash", "-n", str(ROOT / "install.sh")], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
