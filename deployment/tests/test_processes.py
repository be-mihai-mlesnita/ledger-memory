"""Only synthetic child processes and throwaway paths are used here."""
import os
from pathlib import Path
import subprocess
import signal
import sys
import time
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import bemind_deploy as deploy

HAS_NONREAPING_WAIT = os.name == "posix" and all(hasattr(os, name) for name in
    ("waitid", "P_PID", "WEXITED", "WNOHANG", "WNOWAIT", "CLD_EXITED"))


@pytest.fixture
def owned_group_signals(monkeypatch):
    popen, killpg = subprocess.Popen, os.killpg
    processes, signals = {}, []
    def create(*args, **kwargs):
        process = popen(*args, **kwargs)
        processes[process.pid] = process
        return process
    def send(pid, sig):
        process = processes[pid]
        assert process.returncode is None, "Never signal a reaped child's PGID"
        exited = os.waitid(os.P_PID, pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
        assert exited is None or exited.si_pid == pid
        signals.append((sig, exited))
        return killpg(pid, sig)
    monkeypatch.setattr(deploy.subprocess, "Popen", create)
    monkeypatch.setattr(deploy.os, "killpg", send)
    return signals


def assert_exited(pid):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        status = subprocess.run(["ps", "-p", str(pid), "-o", "stat="], capture_output=True, text=True)
        if status.returncode == 1 or status.stdout.strip().startswith("Z"):
            return
        time.sleep(0.05)
    pytest.fail("Synthetic child is still running")


@pytest.mark.skipif(not HAS_NONREAPING_WAIT, reason="Requires non-reaping POSIX child ownership")
@pytest.mark.parametrize("ignore_term", [False, True])
def test_timeout_stops_the_child_process_tree(tmp_path, owned_group_signals, ignore_term):
    child_code = ("import signal,time; "
                  + ("signal.signal(signal.SIGTERM,signal.SIG_IGN); " if ignore_term else "")
                  + "print('ready',flush=True); time.sleep(120)")
    code = (
        "import subprocess,sys,time; "
        f"child=subprocess.Popen([sys.executable,'-c',{child_code!r}],stdout=subprocess.PIPE); "
        "child.stdout.readline(); "
        "print(child.pid,flush=True); time.sleep(120)"
    )
    output = tmp_path / "process.log"
    with output.open("wb") as stream:
        # Killing the owned group still cannot prove that no descendant detached.
        with pytest.raises(deploy.ProcessCleanupError):
            deploy.run_process([sys.executable, "-I", "-c", code], cwd=tmp_path,
                               env=os.environ.copy(), stdout=stream,
                               stderr=subprocess.STDOUT, timeout=1)
    child = int(output.read_text().strip())
    assert_exited(child)
    assert [sig for sig, _ in owned_group_signals] == [signal.SIGTERM, signal.SIGKILL]
    assert owned_group_signals[-1][1] is not None  # The exited leader was kept unreaped.


@pytest.mark.skipif(not HAS_NONREAPING_WAIT, reason="Requires non-reaping POSIX child ownership")
@pytest.mark.parametrize("failure,status", [("raise SystemExit(7)", 7),
    ("os.kill(os.getpid(),signal.SIGTERM)", signal.SIGTERM)])
def test_failed_parent_does_not_leave_its_child(tmp_path, owned_group_signals, failure, status):
    code = (
        "import os,signal,subprocess,sys; "
        "child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(120)']); "
        "print(child.pid,flush=True); " + failure
    )
    output = tmp_path / "process.log"
    with output.open("wb") as stream:
        with pytest.raises(deploy.ProcessCleanupError):
            deploy.run_process([sys.executable, "-I", "-c", code], cwd=tmp_path,
                               env=os.environ.copy(), stdout=stream,
                               stderr=subprocess.STDOUT, timeout=10)
    assert_exited(int(output.read_text().strip()))
    assert [sig for sig, _ in owned_group_signals] == [signal.SIGTERM, signal.SIGKILL]
    assert all(exited is not None and exited.si_status == status for _, exited in owned_group_signals)


@pytest.mark.skipif(not HAS_NONREAPING_WAIT, reason="Requires non-reaping POSIX child ownership")
def test_detached_descendant_cannot_be_mistaken_for_verified_cleanup(tmp_path):
    code = (
        "import subprocess,sys,time; "
        "child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(120)'],start_new_session=True); "
        "print(child.pid,flush=True); time.sleep(120)"
    )
    output = tmp_path / "detached.log"
    try:
        with output.open("wb") as stream:
            with pytest.raises(deploy.ProcessCleanupError):
                deploy.run_process([sys.executable, "-I", "-c", code], cwd=tmp_path,
                                   env=os.environ.copy(), stdout=stream,
                                   stderr=subprocess.STDOUT, timeout=1)
    finally:
        if output.exists() and output.read_text().strip():
            child = int(output.read_text().strip())
            try:
                os.kill(child, signal.SIGTERM)
            except ProcessLookupError:
                pass
            assert_exited(child)


def test_successful_command_keeps_its_exit_code(tmp_path):
    with (tmp_path / "process.log").open("wb") as stream:
        result = deploy.run_process([sys.executable, "-I", "-c", "print('synthetic ok')"],
                                    cwd=tmp_path, env=os.environ.copy(), stdout=stream,
                                    stderr=subprocess.STDOUT, timeout=10)
    assert result.returncode == 0


def test_reaped_child_never_receives_a_group_signal(tmp_path, monkeypatch):
    process = subprocess.Popen([sys.executable, "-I", "-c", "raise SystemExit(7)"], cwd=tmp_path)
    assert process.wait(timeout=10) == 7
    monkeypatch.setattr(deploy.os, "killpg", lambda *a: pytest.fail("Reaped PID must not be signalled"), raising=False)
    with pytest.raises(deploy.ProcessCleanupError):
        deploy.stop_process_tree(process)


@pytest.mark.skipif(not HAS_NONREAPING_WAIT, reason="Requires non-reaping POSIX child ownership")
def test_cleanup_reaps_only_after_the_last_group_signal(monkeypatch):
    events = []
    process = SimpleNamespace(pid=12345, returncode=None)
    process.poll = lambda: pytest.fail("Cleanup must not poll/reap before its final signal")
    def wait(timeout):
        events.append("wait")
        process.returncode = 7
        return 7
    def send(pid, sig):
        assert process.returncode is None
        events.append(sig)
    def peek(pid_type, pid, flags):
        assert flags & os.WNOWAIT and flags & os.WNOHANG and flags & os.WEXITED
        assert process.returncode is None
        events.append("peek")
        return SimpleNamespace(si_pid=pid, si_code=os.CLD_EXITED, si_status=7)
    process.wait = wait
    monkeypatch.setattr(deploy.os, "waitid", peek)
    monkeypatch.setattr(deploy.os, "getpgid", lambda pid: pid)
    monkeypatch.setattr(deploy.os, "killpg", send)
    monkeypatch.setattr(deploy.time, "sleep", lambda _: None)
    deploy.stop_process_tree(process)
    assert events == ["peek", signal.SIGTERM, "peek", signal.SIGKILL, "wait"]
    with pytest.raises(deploy.ProcessCleanupError):
        deploy.stop_process_tree(process)
    assert events[-1] == "wait"


@pytest.mark.skipif(not HAS_NONREAPING_WAIT, reason="Requires non-reaping POSIX child ownership")
@pytest.mark.parametrize("lost_after_term", [False, True])
def test_externally_reaped_child_stops_all_later_signals(monkeypatch, lost_after_term):
    process = SimpleNamespace(pid=12345, returncode=None)
    process.wait = process.poll = lambda *a, **k: pytest.fail("Ownership was lost; do not poll or wait")
    signals, peeks = [], []
    def peek(*args):
        peeks.append(args)
        if lost_after_term and len(peeks) == 1:
            return None
        raise ChildProcessError("already reaped elsewhere")
    monkeypatch.setattr(deploy.os, "waitid", peek)
    monkeypatch.setattr(deploy.os, "getpgid", lambda pid: pid)
    monkeypatch.setattr(deploy.os, "killpg", lambda pid, sig: signals.append(sig))
    monkeypatch.setattr(deploy.time, "sleep", lambda _: None)
    with pytest.raises(deploy.ProcessCleanupError):
        deploy.stop_process_tree(process)
    assert signals == ([signal.SIGTERM] if lost_after_term else [])


@pytest.mark.parametrize("outcome", ["failed", "timeout", "success"])
def test_missing_nonreaping_support_never_signals_a_numeric_pid(tmp_path, monkeypatch, outcome):
    process = SimpleNamespace(pid=12345, returncode=None)
    def wait(timeout):
        if outcome == "timeout":
            raise subprocess.TimeoutExpired("synthetic", timeout)
        process.returncode = 7 if outcome == "failed" else 0
        return process.returncode
    process.wait = wait
    monkeypatch.delattr(deploy.os, "waitid", raising=False)
    monkeypatch.setattr(deploy.os, "killpg", lambda *a: pytest.fail("Cannot prove ownership without waitid"), raising=False)
    monkeypatch.setattr(deploy.subprocess, "Popen", lambda *a, **k: process)
    if outcome == "success":
        assert deploy.run_process(["synthetic"], cwd=tmp_path, env={}, stdout=None, stderr=None).returncode == 0
    else:
        with pytest.raises(deploy.ProcessCleanupError):
            deploy.run_process(["synthetic"], cwd=tmp_path, env={}, stdout=None, stderr=None, timeout=1)


@pytest.mark.skipif(not HAS_NONREAPING_WAIT, reason="Requires non-reaping POSIX child ownership")
def test_wait_interruption_after_reaping_does_not_signal(tmp_path, monkeypatch):
    process = SimpleNamespace(pid=12345, returncode=None)
    def wait(timeout):
        process.returncode = 0
        raise KeyboardInterrupt()
    process.wait = wait
    monkeypatch.setattr(deploy.subprocess, "Popen", lambda *a, **k: process)
    monkeypatch.setattr(deploy.os, "waitid", lambda *a: SimpleNamespace(si_pid=process.pid, si_code=os.CLD_EXITED, si_status=0))
    monkeypatch.setattr(deploy.os, "killpg", lambda *a: pytest.fail("Interrupted wait already reaped the child"))
    with pytest.raises(deploy.ProcessCleanupError):
        deploy.run_process(["synthetic"], cwd=tmp_path, env={}, stdout=None, stderr=None)
