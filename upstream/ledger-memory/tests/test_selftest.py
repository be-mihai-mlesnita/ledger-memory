"""The shipped selftest is the install check; it must pass here too. Always the offline form: no service is contacted."""
from __future__ import annotations

from ledger_memory import selftest


def test_selftest_core_passes_offline(capsys):
    assert selftest.run(offline=True) == 0
    out = capsys.readouterr().out
    assert "0 failed" in out and "SKIP  semantic search" in out and "local reader exists" in out


def test_selftest_fails_loudly_when_a_check_fails(monkeypatch, capsys):
    from ledger_memory import api
    monkeypatch.setattr(api.Ledger, "recall", lambda self, q, limit=8: {"query": q, "results": [], "index": {"fulltext_fresh": True}, "warnings": []})
    assert selftest.run(offline=True) == 1
    assert "FAIL  recall returns the current belief only" in capsys.readouterr().out
