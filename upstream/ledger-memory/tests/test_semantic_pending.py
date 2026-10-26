"""Deferred semantic maintenance and truthful readiness, using synthetic stores."""
from __future__ import annotations

import pytest


@pytest.mark.parametrize("late", [False, True])
def test_owned_generation_replacement_refuses_foreign_receipt(home, monkeypatch, late):
    from dataclasses import replace
    import sqlite3
    from ledger_memory.api import Ledger
    from ledger_memory import semantic_rebuild as rebuild
    from ledger_memory.embeddings import DeterministicFakeBackend
    from ledger_memory.semantic_binding import read_receipt, write_receipt

    monkeypatch.setenv("LEDGER_EMBEDDING_BACKEND", "fake")
    ledger = Ledger().ensure()
    ledger.remember("Synthetic owned generation", source="user_said")
    result = rebuild.replace_active_model_rebuild(ledger.db_path, sidecar_path=ledger.vectors_path,
                                                 require_owned=True)
    assert result.receipt_verified is True
    owned = read_receipt(ledger.vectors_path)
    foreign = replace(owned, lineage_id="synthetic-foreign-owner")
    with sqlite3.connect(ledger.vectors_path) as sidecar:
        vectors = sidecar.execute("SELECT * FROM semantic_vectors ORDER BY record_type,record_id").fetchall()
    with sqlite3.connect(ledger.db_path) as core:
        generations = core.execute("SELECT COUNT(*) FROM semantic_index_rebuilds").fetchone()[0]
    calls = []
    class Backend(DeterministicFakeBackend):
        def embed(self, texts):
            calls.append(True)
            write_receipt(ledger.vectors_path, foreign)
            return super().embed(texts)
    monkeypatch.setattr(rebuild, "get_embedding_backend", lambda *args: Backend())
    if not late:
        write_receipt(ledger.vectors_path, foreign)
    with pytest.raises(RuntimeError, match="sidecar (belongs|ownership)"):
        rebuild.replace_active_model_rebuild(ledger.db_path, sidecar_path=ledger.vectors_path,
                                             require_owned=True)
    assert bool(calls) is late
    assert read_receipt(ledger.vectors_path) == foreign
    with sqlite3.connect(ledger.vectors_path) as sidecar:
        assert sidecar.execute("SELECT * FROM semantic_vectors ORDER BY record_type,record_id").fetchall() == vectors
    with sqlite3.connect(ledger.db_path) as core:
        assert core.execute("SELECT COUNT(*) FROM semantic_index_rebuilds").fetchone()[0] == generations


def test_coordinator_prunes_restored_orphans_and_reopens_owned_lane(home, monkeypatch):
    from ledger_memory import api, semantic_pending
    from ledger_memory.embeddings import DeterministicFakeBackend
    from ledger_memory.semantic_rebuild import replace_active_model_rebuild
    from ledger_memory.semantic_search import semantic_lane_blocked
    monkeypatch.setenv("LEDGER_EMBEDDING_BACKEND", "fake")
    ledger = api.Ledger().ensure()
    ledger.remember("Synthetic retained prune baseline", source="user_said")
    replace_active_model_rebuild(ledger.db_path, sidecar_path=ledger.vectors_path)
    assert semantic_lane_blocked(ledger.db_path, ledger.vectors_path) is None
    archive = ledger.backup()["archive_path"]
    ledger.remember("Synthetic removed prune fixture", source="user_said")
    monkeypatch.setattr(api, "resolve_embedding_backend_name", lambda database: "bge-m3")
    monkeypatch.setattr(semantic_pending, "request_drain", lambda *args: False)
    restored = ledger.restore(archive, str(ledger.db_path), force=True)
    assert restored["vectors"]["repair"]["performed"] is False
    assert semantic_lane_blocked(ledger.db_path, ledger.vectors_path) is not None
    backend = DeterministicFakeBackend()
    assert semantic_pending.has_orphaned_embeddings(ledger.db_path, ledger.vectors_path, backend=backend)
    result = semantic_pending.run_pending_worker(ledger.db_path, ledger.vectors_path, backend=backend)
    assert result["status"] == "completed"
    assert not semantic_pending.has_orphaned_embeddings(ledger.db_path, ledger.vectors_path, backend=backend)
    assert semantic_pending.pending_embeddings(ledger.db_path, ledger.vectors_path, backend=backend) == []
    assert semantic_lane_blocked(ledger.db_path, ledger.vectors_path) is None


def test_orphan_pruning_is_model_scoped_and_domain_event_neutral(home, monkeypatch):
    import sqlite3
    from ledger_memory.api import Ledger
    from ledger_memory.embeddings import DeterministicFakeBackend
    from ledger_memory.semantic_pending import prune_orphaned_embeddings, has_orphaned_embeddings
    from ledger_memory.chunk_embeddings import CHUNK_SIDECAR_DDL
    from ledger_memory.semantic_binding import read_receipt
    monkeypatch.setenv("LEDGER_EMBEDDING_BACKEND", "fake")
    ledger = Ledger().ensure()
    current = ledger.remember("Synthetic current prune fixture", source="user_said")
    backend = DeterministicFakeBackend()
    assert read_receipt(ledger.vectors_path) is None
    with sqlite3.connect(ledger.vectors_path) as sidecar:
        sidecar.execute(CHUNK_SIDECAR_DDL)
        before = sidecar.execute("SELECT vector FROM semantic_vectors WHERE record_type='fact' AND record_id=?",
                                 (current["id"],)).fetchone()[0]
        for model in (backend.model_name, "synthetic-other-model"):
            sidecar.execute("INSERT INTO semantic_vectors(record_type,record_id,embedding_model,vector) VALUES('fact',999999,?,?)", (model, before))
            sidecar.execute("INSERT INTO chunk_vectors(chunk_id,embedding_model,vector) VALUES(999999,?,?)", (model, before))
            for kind in ("fact", "chunk"):
                sidecar.execute("INSERT INTO semantic_vector_revisions VALUES(?,999999,?,'ghost','ghost')", (kind, model))
                sidecar.execute("INSERT INTO embedding_failures(record_type,record_id,content_hash,embedding_model,attempts,last_error_class) VALUES(?,999999,'ghost',?,3,'ValueError')", (kind, model))
    with sqlite3.connect(ledger.db_path) as core:
        counts = [core.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in ("facts", "events", "audit_entries")]
        core.execute("INSERT INTO semantic_index_entries(record_type,record_id,content_hash,embedding_model,embedding_dimensions) VALUES('fact',999999,'ghost',?,64)", (backend.model_name,))
    assert has_orphaned_embeddings(ledger.db_path, ledger.vectors_path, backend=backend)
    assert prune_orphaned_embeddings(ledger.db_path, ledger.vectors_path, backend=backend) > 0
    assert not has_orphaned_embeddings(ledger.db_path, ledger.vectors_path, backend=backend)
    assert read_receipt(ledger.vectors_path) is None
    with sqlite3.connect(ledger.vectors_path) as sidecar:
        assert sidecar.execute("SELECT vector FROM semantic_vectors WHERE record_type='fact' AND record_id=?", (current["id"],)).fetchone()[0] == before
        assert sidecar.execute("SELECT COUNT(*) FROM semantic_vectors WHERE record_id=999999 AND embedding_model=?", (backend.model_name,)).fetchone()[0] == 0
        assert sidecar.execute("SELECT COUNT(*) FROM chunk_vectors WHERE chunk_id=999999 AND embedding_model='synthetic-other-model'").fetchone()[0] == 1
        assert sidecar.execute("SELECT COUNT(*) FROM embedding_failures WHERE embedding_model='synthetic-other-model'").fetchone()[0] == 2
    with sqlite3.connect(ledger.db_path) as core:
        assert [core.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in ("facts", "events", "audit_entries")] == counts


def test_coordinator_final_recheck_catches_late_orphan_without_pending_source(home, monkeypatch):
    import sqlite3
    from ledger_memory.api import Ledger
    from ledger_memory.embeddings import DeterministicFakeBackend
    from ledger_memory.semantic_pending import run_pending_worker, has_orphaned_embeddings
    monkeypatch.setenv("LEDGER_EMBEDDING_BACKEND", "fake")
    ledger = Ledger().ensure()
    ledger.remember("Synthetic late orphan fixture", source="user_said")
    backend = DeterministicFakeBackend()
    calls = []
    def late_orphan():
        calls.append(True)
        if len(calls) == 1:
            with sqlite3.connect(ledger.vectors_path) as sidecar:
                blob = sidecar.execute("SELECT vector FROM semantic_vectors LIMIT 1").fetchone()[0]
                sidecar.execute("INSERT INTO semantic_vectors(record_type,record_id,embedding_model,vector) VALUES('fact',999999,?,?)", (backend.model_name, blob))
    result = run_pending_worker(ledger.db_path, ledger.vectors_path, backend=backend, _after_unlock=late_orphan)
    assert result["status"] == "completed"
    assert len(calls) == 2
    assert not has_orphaned_embeddings(ledger.db_path, ledger.vectors_path, backend=backend)


def test_orphan_metadata_cleanup_does_not_create_missing_sidecar(home, tmp_path, monkeypatch):
    import sqlite3
    from ledger_memory.api import Ledger
    from ledger_memory.embeddings import DeterministicFakeBackend
    from ledger_memory.semantic_pending import prune_orphaned_embeddings
    ledger = Ledger().ensure()
    backend = DeterministicFakeBackend()
    monkeypatch.setenv("LEDGER_EMBEDDING_BACKEND", "fake")
    absent = tmp_path / "absent-sidecar.db"
    with sqlite3.connect(ledger.db_path) as core:
        core.execute("INSERT INTO semantic_index_entries(record_type,record_id,content_hash,embedding_model,embedding_dimensions) VALUES('fact',999999,'ghost',?,64)", (backend.model_name,))
    assert prune_orphaned_embeddings(ledger.db_path, absent, backend=backend) == 1
    assert not absent.exists()


@pytest.mark.parametrize("policy", ["disabled", "different-dimensions", "foreign"])
def test_orphan_cleanup_refuses_changed_policy_and_foreign_ownership(home, monkeypatch, policy):
    import sqlite3
    from dataclasses import replace
    from ledger_memory.api import Ledger
    from ledger_memory.embeddings import DeterministicFakeBackend
    from ledger_memory.semantic_pending import prune_orphaned_embeddings
    from ledger_memory.semantic_binding import read_receipt, write_receipt, SidecarOwnershipError
    from ledger_memory.semantic_rebuild import replace_active_model_rebuild
    monkeypatch.setenv("LEDGER_EMBEDDING_BACKEND", "fake")
    ledger = Ledger().ensure()
    ledger.remember("Synthetic policy prune fixture", source="user_said")
    replace_active_model_rebuild(ledger.db_path, sidecar_path=ledger.vectors_path)
    backend = DeterministicFakeBackend()
    with sqlite3.connect(ledger.vectors_path) as sidecar:
        blob = sidecar.execute("SELECT vector FROM semantic_vectors LIMIT 1").fetchone()[0]
        sidecar.execute("INSERT INTO semantic_vectors(record_type,record_id,embedding_model,vector) VALUES('fact',999999,?,?)", (backend.model_name, blob))
    if policy == "disabled":
        monkeypatch.setenv("LEDGER_EMBEDDING_BACKEND", "none")
        assert prune_orphaned_embeddings(ledger.db_path, ledger.vectors_path, backend=backend) == 0
    elif policy == "different-dimensions":
        assert prune_orphaned_embeddings(ledger.db_path, ledger.vectors_path,
                                         backend=DeterministicFakeBackend(32)) == 0
    else:
        receipt = read_receipt(ledger.vectors_path)
        assert receipt is not None
        write_receipt(ledger.vectors_path, replace(receipt, lineage_id="synthetic-foreign"))
        with pytest.raises(SidecarOwnershipError):
            prune_orphaned_embeddings(ledger.db_path, ledger.vectors_path, backend=backend)
    with sqlite3.connect(ledger.vectors_path) as sidecar:
        assert sidecar.execute("SELECT COUNT(*) FROM semantic_vectors WHERE record_id=999999").fetchone()[0] == 1


def test_remember_never_waits_for_embedding(home, monkeypatch):
    from ledger_memory import embeddings, semantic_pending, semantic_rebuild, search_index
    from ledger_memory.api import Ledger
    ledger = Ledger().ensure()
    monkeypatch.setenv("LEDGER_EMBEDDING_BACKEND", "bge-m3")
    calls = []
    monkeypatch.setattr(semantic_pending, "request_drain", lambda database, sidecar: calls.append((database, sidecar)))
    def forbidden(*args, **kwargs):
        raise AssertionError("write attempted synchronous model access")
    monkeypatch.setattr(embeddings, "get_embedding_backend", forbidden)
    monkeypatch.setattr(semantic_rebuild, "rebuild_semantic_index", forbidden)
    result = ledger.remember("Synthetic marmalade deferred fact", source="user_said")
    assert result["warnings"] == []
    assert calls == [(ledger.db_path, ledger.vectors_path)]
    assert any(row.record_id == result["id"] and row.record_type == "fact"
               for row in search_index.fts_search(ledger.db_path, "marmalade"))


def test_ingest_never_waits_for_embedding(home, monkeypatch):
    from ledger_memory import embeddings, semantic_pending, chunk_embeddings
    from ledger_memory.api import Ledger
    from ledger_memory.db import connect_readonly
    ledger = Ledger().ensure()
    monkeypatch.setenv("LEDGER_EMBEDDING_BACKEND", "bge-m3")
    calls = []
    monkeypatch.setattr(semantic_pending, "request_drain", lambda database, sidecar: calls.append((database, sidecar)))
    def forbidden(*args, **kwargs):
        raise AssertionError("ingest attempted synchronous model access")
    monkeypatch.setattr(embeddings, "get_embedding_backend", forbidden)
    monkeypatch.setattr(chunk_embeddings, "embed_chunks", forbidden)
    result = ledger.ingest(text="Synthetic deferred document content.", uri="note://synthetic-deferred")
    assert result["chunks"] > 0
    assert result["embedded"] == 0
    assert result["warnings"] == []
    assert calls == [(ledger.db_path, ledger.vectors_path)]
    with connect_readonly(ledger.db_path) as core:
        assert core.execute("SELECT COUNT(*) FROM chunks WHERE document_id=?",
                            (result["document_id"],)).fetchone()[0] == result["chunks"]


def test_deferred_write_survives_queue_failure_without_echoing_error(home, monkeypatch):
    from ledger_memory import semantic_pending, search_index
    from ledger_memory.api import Ledger
    ledger = Ledger().ensure()
    monkeypatch.setenv("LEDGER_EMBEDDING_BACKEND", "bge-m3")
    def failed(*args):
        raise OSError("SYNTHETIC_PRIVATE_QUEUE_ERROR")
    monkeypatch.setattr(semantic_pending, "request_drain", failed)
    result = ledger.remember("Synthetic cantaloupe durable fact", source="user_said")
    assert result["warnings"]
    assert "SYNTHETIC_PRIVATE_QUEUE_ERROR" not in str(result)
    assert any(row.record_id == result["id"] for row in search_index.fts_search(ledger.db_path, "cantaloupe"))


def test_deferred_explicit_repair_reports_unperformed_binding_repair(home, monkeypatch):
    from dataclasses import replace
    from ledger_memory import api, semantic_pending
    from ledger_memory.semantic_binding import read_receipt, write_receipt
    from ledger_memory.semantic_search import semantic_lane_blocked
    from ledger_memory.semantic_rebuild import replace_active_model_rebuild
    monkeypatch.setenv("LEDGER_EMBEDDING_BACKEND", "fake")
    ledger = api.Ledger().ensure()
    ledger.remember("Synthetic binding repair fixture", source="user_said")
    ledger.refresh(repair=True)
    replace_active_model_rebuild(ledger.db_path, sidecar_path=ledger.vectors_path)
    assert semantic_lane_blocked(ledger.db_path, ledger.vectors_path) is None
    receipt = read_receipt(ledger.vectors_path)
    assert receipt is not None
    write_receipt(ledger.vectors_path, replace(receipt, embedding_model="synthetic-old-model"))
    assert semantic_lane_blocked(ledger.db_path, ledger.vectors_path) is not None
    monkeypatch.setattr(api, "resolve_embedding_backend_name", lambda database: "bge-m3")
    queued = []
    monkeypatch.setattr(semantic_pending, "request_drain", lambda *args: queued.append(args))
    result = ledger.refresh(repair=True)
    assert len(queued) == 1
    assert result["repair"]["requested"] is True
    assert result["repair"]["performed"] is False
    assert "explicit generation rebuild" in result["repair"]["reason"]
    assert result["repair"]["reason"] in result["warnings"]
    assert not result["index"]["semantic_fresh"]
    assert read_receipt(ledger.vectors_path).embedding_model == "synthetic-old-model"
    assert "repaired" not in result


def test_deferred_restore_surfaces_unperformed_generation_repair(home, monkeypatch):
    from ledger_memory import api, semantic_pending
    from ledger_memory.semantic_search import semantic_lane_blocked
    monkeypatch.setenv("LEDGER_EMBEDDING_BACKEND", "fake")
    ledger = api.Ledger().ensure()
    ledger.remember("Synthetic retained restore fixture", source="user_said")
    ledger.refresh(repair=True)
    assert semantic_lane_blocked(ledger.db_path, ledger.vectors_path) is None
    archive = ledger.backup()["archive_path"]
    ledger.remember("Synthetic extra post-backup fixture", source="user_said")
    monkeypatch.setattr(api, "resolve_embedding_backend_name", lambda database: "bge-m3")
    monkeypatch.setattr(semantic_pending, "request_drain", lambda *args: False)
    result = ledger.restore(archive, str(ledger.db_path), force=True)
    assert result["vectors"]["repair"]["performed"] is False
    assert result["vectors"]["warnings"]
    assert not result["vectors"]["index"]["semantic_fresh"]
    assert semantic_lane_blocked(ledger.db_path, ledger.vectors_path) is not None


@pytest.mark.parametrize("lane", ["record", "chunk"])
@pytest.mark.parametrize("error_kind", ["sqlite", "os", "programming"])
def test_query_policy_reselection_does_not_swallow_authority_errors(home, tmp_path, monkeypatch, lane, error_kind):
    import sqlite3
    from ledger_memory import config, embeddings, rag_search as rag, semantic_search as record
    from ledger_memory.api import Ledger
    from ledger_memory.chunk_embeddings import embed_chunks
    from ledger_memory.embeddings import DeterministicFakeBackend
    from ledger_memory.semantic_rebuild import rebuild_semantic_index
    ledger = Ledger().ensure()
    ledger.remember("Synthetic policy boundary fact", source="user_said")
    ledger.ingest(text="Synthetic policy boundary chunk.", uri="note://synthetic-policy")
    config.set_config(ledger.db_path, key="embedding.backend", value="fake", actor_id=ledger.owner_id)
    sidecar = tmp_path / "policy-errors.db"
    rebuild_semantic_index(ledger.db_path, sidecar_path=sidecar)
    embed_chunks(ledger.db_path, actor_id=ledger.owner_id, sidecar_path=sidecar)
    module = record if lane == "record" else rag
    query = record.semantic_search_outcome if lane == "record" else rag.rag_search_outcome
    rows, reason = query(ledger.db_path, "Synthetic", sidecar_path=sidecar)
    assert rows and reason is None
    error = {"sqlite": sqlite3.OperationalError, "os": OSError, "programming": RuntimeError}[error_kind]("synthetic policy failure")
    observed = []
    def failed_policy(database):
        observed.append(True)
        raise error
    class Backend(DeterministicFakeBackend):
        def embed(self, texts):
            monkeypatch.setattr(embeddings, "get_embedding_backend", failed_policy)
            return super().embed(texts)
    monkeypatch.setattr(module, "get_embedding_backend", lambda database: Backend())
    with pytest.raises(type(error)) as caught:
        query(ledger.db_path, "Synthetic", sidecar_path=sidecar)
    assert caught.value is error
    assert observed == [True]


def test_record_query_missing_index_reports_unavailable_without_creating_it(home, tmp_path):
    from ledger_memory import config
    from ledger_memory.api import Ledger
    from ledger_memory.semantic_search import semantic_search_outcome
    ledger = Ledger().ensure()
    ledger.remember("Synthetic not-yet-indexed source", source="user_said")
    config.set_config(ledger.db_path, key="embedding.backend", value="fake", actor_id=ledger.owner_id)
    sidecar = tmp_path / "absent-query.db"
    rows, reason = semantic_search_outcome(ledger.db_path, "Synthetic", sidecar_path=sidecar)
    assert rows == []
    assert reason is not None
    assert not sidecar.exists()


@pytest.mark.parametrize("change", ["source", "metadata", "vector", "receipt", "model", "binding"])
def test_record_query_checks_actual_snapshots_after_inference(home, tmp_path, monkeypatch, change):
    import struct
    from ledger_memory import config, semantic_search as search
    from ledger_memory.api import Ledger
    from ledger_memory.db import connect
    from ledger_memory.embeddings import DeterministicFakeBackend
    from ledger_memory.semantic_binding import GenerationReceipt, write_receipt
    from ledger_memory.semantic_rebuild import rebuild_semantic_index
    ledger = Ledger().ensure()
    ledger.remember("Synthetic record query revision", source="user_said")
    config.set_config(ledger.db_path, key="embedding.backend", value="fake", actor_id=ledger.owner_id)
    sidecar = tmp_path / "record-query.db"
    rebuild_semantic_index(ledger.db_path, sidecar_path=sidecar)
    rows, reason = search.semantic_search_outcome(ledger.db_path, "Synthetic", sidecar_path=sidecar)
    assert rows and reason is None, "positive semantic gate did not open"
    calls = []
    class Backend(DeterministicFakeBackend):
        def embed(self, texts):
            calls.append(True)
            if change in {"source", "metadata"}:
                with connect(ledger.db_path) as connection:
                    if change == "source":
                        connection.execute("UPDATE facts SET statement='Synthetic changed during query'")
                    else:
                        connection.execute("UPDATE semantic_index_entries SET content_hash='synthetic-old-hash'")
            elif change == "model":
                config.set_config(ledger.db_path, key="embedding.backend", value="none", actor_id=ledger.owner_id)
            elif change == "binding":
                write_receipt(sidecar, GenerationReceipt("synthetic-foreign", self.model_name, self.dimensions,
                                                       1, 1, "synthetic-manifest", "synthetic-time"))
            else:
                with connect(sidecar) as connection:
                    if change == "vector":
                        connection.execute("UPDATE semantic_vectors SET vector=?",
                            (struct.pack(f"{self.dimensions}f", *([1.0] + [0.0] * (self.dimensions - 1))),))
                    else:
                        connection.execute("DELETE FROM semantic_vector_revisions WHERE record_type<>'chunk'")
            return super().embed(texts)
    monkeypatch.setattr(search, "get_embedding_backend", lambda database: Backend())
    results, blocked = search.semantic_search_outcome(ledger.db_path, "Synthetic", sidecar_path=sidecar)
    assert calls == [True]
    assert results == []
    assert blocked is not None


@pytest.mark.parametrize("replacement", [False, True])
@pytest.mark.parametrize("prune", [False, True])
def test_manual_record_rebuild_invalidates_existing_ann_when_ann_is_off(home, tmp_path, monkeypatch, replacement, prune):
    from ledger_memory import config, semantic_rebuild as rebuild, semantic_vec
    from ledger_memory.api import Ledger
    from ledger_memory.db import connect, connect_readonly
    from ledger_memory.embeddings import DeterministicFakeBackend
    ledger = Ledger().ensure()
    ledger.remember("Synthetic manual ANN source", source="user_said")
    config.set_config(ledger.db_path, key="embedding.backend", value="fake", actor_id=ledger.owner_id)
    monkeypatch.setenv("LEDGER_SEMANTIC_BACKEND", "bruteforce")
    sidecar = tmp_path / "manual-ann.db"
    rebuild.rebuild_semantic_index(ledger.db_path, sidecar_path=sidecar)
    backend = DeterministicFakeBackend()
    with connect(sidecar) as connection:
        connection.execute(f"CREATE TABLE {semantic_vec.META_TABLE}(model TEXT,dimensions INTEGER)")
        connection.executemany(f"INSERT INTO {semantic_vec.META_TABLE} VALUES(?,?)",
            [(backend.model_name, backend.dimensions), ("synthetic-other-model", 7)])
        # Synthetic stand-in proves the actual metadata/count gate opens first;
        # no native sqlite-vec correctness claim is made by this probe.
        connection.execute(f"CREATE TABLE {semantic_vec.VEC_TABLE}(rowid INTEGER)")
        connection.execute(f"INSERT INTO {semantic_vec.VEC_TABLE} VALUES(1)")
    monkeypatch.setattr(semantic_vec, "load_extension", lambda connection: True)
    with connect_readonly(sidecar) as connection:
        assert semantic_vec.vec_index_row_count(connection, model_name=backend.model_name, dimensions=backend.dimensions) == 1
    if prune:
        with connect(ledger.db_path) as connection:
            identifier = connection.execute("SELECT id FROM facts LIMIT 1").fetchone()[0]
            connection.execute("DELETE FROM facts WHERE id=?", (identifier,))
    operation = rebuild.replace_active_model_rebuild if replacement else rebuild.rebuild_semantic_index
    kwargs = {} if replacement else ({"record_type": "fact", "record_id": identifier} if prune else {"force": True})
    result = operation(ledger.db_path, sidecar_path=sidecar, **kwargs)
    assert result.embedded_rows == 0 if prune else result.embedded_rows > 0
    with connect_readonly(sidecar) as connection:
        assert [tuple(row) for row in connection.execute(f"SELECT * FROM {semantic_vec.META_TABLE}")] == [("synthetic-other-model", 7)]
        assert semantic_vec.vec_index_row_count(connection, model_name=backend.model_name, dimensions=backend.dimensions) is None


def test_record_rebuild_rechecks_skips_after_source_changes_away_and_back(home, tmp_path, monkeypatch):
    from ledger_memory import config, semantic_pending as pending, semantic_rebuild as rebuild
    from ledger_memory.api import Ledger
    from ledger_memory.db import connect, connect_readonly
    from ledger_memory.embeddings import DeterministicFakeBackend
    ledger = Ledger().ensure()
    ledger.remember("Synthetic original skip source", source="user_said")
    config.set_config(ledger.db_path, key="embedding.backend", value="fake", actor_id=ledger.owner_id)
    sidecar = tmp_path / "skip-race.db"
    rebuild.rebuild_semantic_index(ledger.db_path, sidecar_path=sidecar)
    assert rebuild.rebuild_semantic_index(ledger.db_path, sidecar_path=sidecar).skipped_rows > 0
    with connect_readonly(ledger.db_path) as connection:
        fact = connection.execute("SELECT id,statement FROM facts ORDER BY id LIMIT 1").fetchone()
        before_history = connection.execute("SELECT COUNT(*) FROM semantic_index_rebuilds").fetchone()[0]
    original = pending.pending_embeddings
    mutations = []
    def temporary_revision(*args, **kwargs):
        if mutations:
            return original(*args, **kwargs)
        mutations.append(True)
        with connect(ledger.db_path) as connection:
            connection.execute("UPDATE facts SET statement='Synthetic intermediate revision' WHERE id=?", (fact["id"],))
        backend = DeterministicFakeBackend()
        item = next(row for row in original(ledger.db_path, sidecar, backend=backend) if row.record_type == "fact")
        assert pending.publish_embedding(ledger.db_path, sidecar, item, backend.embed([item.text])[0], backend=backend)
        result = original(*args, **kwargs)
        assert item not in result, "intermediate revision was not genuinely current"
        with connect(ledger.db_path) as connection:
            connection.execute("UPDATE facts SET statement=? WHERE id=?", (fact["statement"], fact["id"]))
        return result
    monkeypatch.setattr(pending, "pending_embeddings", temporary_revision)
    with pytest.raises(RuntimeError, match="index changed"):
        rebuild.rebuild_semantic_index(ledger.db_path, sidecar_path=sidecar)
    assert mutations == [True]
    assert any(row.record_type == "fact" for row in original(ledger.db_path, sidecar, backend=DeterministicFakeBackend()))
    with connect_readonly(ledger.db_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM semantic_index_rebuilds").fetchone()[0] == before_history


def test_replacement_rechecks_lineage_after_artifact_before_publication(home, tmp_path, monkeypatch):
    from contextlib import contextmanager
    from ledger_memory import config, semantic_rebuild as rebuild
    from ledger_memory.api import Ledger
    from ledger_memory.db import connect, connect_readonly
    from ledger_memory.semantic_binding import LINEAGE_KEY
    ledger = Ledger().ensure()
    ledger.remember("Synthetic lineage race source", source="user_said")
    config.set_config(ledger.db_path, key="embedding.backend", value="fake", actor_id=ledger.owner_id)
    sidecar = tmp_path / "lineage-race.db"
    baseline = rebuild.replace_active_model_rebuild(ledger.db_path, sidecar_path=sidecar)
    assert baseline.receipt_verified and baseline.gate_open and baseline.embedded_rows > 0
    with connect_readonly(sidecar) as connection:
        before = list(connection.iterdump())
    with connect_readonly(ledger.db_path) as connection:
        history = [tuple(row) for row in connection.execute("SELECT * FROM semantic_index_rebuilds ORDER BY id")]
    original = rebuild.connect_readonly
    changed = []
    @contextmanager
    def mutate_after_artifact(path, **kwargs):
        with original(path, **kwargs) as connection:
            yield connection
        if ".pre-replacement-" in str(path):
            with connect(ledger.db_path) as connection:
                connection.execute("UPDATE config SET value='synthetic-changed-lineage' WHERE key=?", (LINEAGE_KEY,))
            changed.append(True)
    monkeypatch.setattr(rebuild, "connect_readonly", mutate_after_artifact)
    with pytest.raises(RuntimeError, match="lineage"):
        rebuild.replace_active_model_rebuild(ledger.db_path, sidecar_path=sidecar)
    assert changed == [True]
    with connect_readonly(sidecar) as connection:
        assert list(connection.iterdump()) == before
    with connect_readonly(ledger.db_path) as connection:
        assert [tuple(row) for row in connection.execute("SELECT * FROM semantic_index_rebuilds ORDER BY id")] == history


@pytest.mark.parametrize("replacement", [False, True])
@pytest.mark.parametrize("change", ["source", "model"])
def test_record_rebuild_rechecks_source_and_model_after_inference(home, tmp_path, monkeypatch, replacement, change):
    from ledger_memory import config, semantic_rebuild as rebuild
    from ledger_memory.api import Ledger
    from ledger_memory.db import connect, connect_readonly
    from ledger_memory.embeddings import DeterministicFakeBackend
    ledger = Ledger().ensure()
    ledger.remember("Synthetic changing record source", source="user_said")
    config.set_config(ledger.db_path, key="embedding.backend", value="fake", actor_id=ledger.owner_id)
    sidecar = tmp_path / "changing-record.db"
    rebuild.rebuild_semantic_index(ledger.db_path, sidecar_path=sidecar)
    def snapshot():
        with connect_readonly(sidecar) as connection:
            derived = [tuple(row) for row in connection.execute("SELECT * FROM semantic_vectors ORDER BY record_type,record_id")]
            receipts = [tuple(row) for row in connection.execute("SELECT * FROM semantic_vector_revisions ORDER BY record_type,record_id")]
        with connect_readonly(ledger.db_path) as connection:
            entries = [tuple(row) for row in connection.execute("SELECT * FROM semantic_index_entries ORDER BY record_type,record_id")]
            history = [tuple(row) for row in connection.execute("SELECT * FROM semantic_index_rebuilds ORDER BY id")]
        return derived, receipts, entries, history
    before = snapshot()
    assert all(before)
    calls = []
    class Backend(DeterministicFakeBackend):
        def embed(self, texts):
            with connect(ledger.db_path) as connection:
                connection.execute("BEGIN IMMEDIATE")
                if change == "source":
                    connection.execute("UPDATE facts SET statement='Synthetic changed during inference'")
            if change == "model":
                config.set_config(ledger.db_path, key="embedding.backend", value="none", actor_id=ledger.owner_id)
            calls.append(len(texts))
            return super().embed(texts)
    monkeypatch.setattr(rebuild, "get_embedding_backend", lambda database: Backend())
    operation = rebuild.replace_active_model_rebuild if replacement else rebuild.rebuild_semantic_index
    expected = rebuild.SourceChangedDuringEmbeddingError if change == "source" else RuntimeError
    with pytest.raises(expected):
        operation(ledger.db_path, sidecar_path=sidecar, **({} if replacement else {"force": True}))
    assert calls and calls[0] > 0
    assert snapshot() == before


@pytest.mark.parametrize("replacement", [False, True])
@pytest.mark.parametrize("bad", ["missing", "extra", "zero", "nonfinite", "dimension", "boolean", "overflow"])
def test_record_rebuild_rejects_malformed_embeddings_before_publication(home, tmp_path, monkeypatch, replacement, bad):
    from ledger_memory import config, semantic_rebuild as rebuild
    from ledger_memory.api import Ledger
    from ledger_memory.db import connect_readonly
    from ledger_memory.embeddings import DeterministicFakeBackend
    ledger = Ledger().ensure()
    ledger.remember("Synthetic malformed embedding source", source="user_said")
    config.set_config(ledger.db_path, key="embedding.backend", value="fake", actor_id=ledger.owner_id)
    sidecar = tmp_path / "malformed-records.db"
    rebuild.rebuild_semantic_index(ledger.db_path, sidecar_path=sidecar)
    def snapshot():
        with connect_readonly(sidecar) as connection:
            vectors = [tuple(row) for row in connection.execute("SELECT * FROM semantic_vectors ORDER BY record_type,record_id")]
            receipts = [tuple(row) for row in connection.execute("SELECT * FROM semantic_vector_revisions ORDER BY record_type,record_id")]
        with connect_readonly(ledger.db_path) as connection:
            metadata = [tuple(row) for row in connection.execute("SELECT * FROM semantic_index_entries ORDER BY record_type,record_id")]
            rebuilds = [tuple(row) for row in connection.execute("SELECT * FROM semantic_index_rebuilds ORDER BY id")]
        return vectors, receipts, metadata, rebuilds
    before = snapshot()
    assert all(before), "malformed-output guard needs a nonempty preexisting index"
    calls = []
    class Backend(DeterministicFakeBackend):
        def embed(self, texts):
            calls.append(len(texts))
            vectors = super().embed(texts)
            if bad == "missing":
                return vectors[:-1]
            if bad == "extra":
                return vectors + vectors[:1]
            if bad == "dimension":
                vectors[0] = vectors[0][:-1]
            else:
                vectors[0] = [0.0] * self.dimensions
                vectors[0][0] = {"zero": 0.0, "nonfinite": float("nan"), "boolean": True, "overflow": 10**1000}[bad]
            return vectors
    monkeypatch.setattr(rebuild, "get_embedding_backend", lambda database: Backend())
    operation = rebuild.replace_active_model_rebuild if replacement else rebuild.rebuild_semantic_index
    kwargs = {} if replacement else {"force": True}
    with pytest.raises(ValueError, match="embedding"):
        operation(ledger.db_path, sidecar_path=sidecar, **kwargs)
    assert calls and calls[0] > 0
    assert snapshot() == before


@pytest.mark.parametrize("replacement", [False, True])
def test_record_rebuild_prunes_receipts_without_touching_chunk_or_other_model(home, tmp_path, replacement):
    from ledger_memory import config
    from ledger_memory.api import Ledger
    from ledger_memory.chunk_embeddings import embed_chunks
    from ledger_memory.db import connect, connect_readonly
    from ledger_memory.semantic_rebuild import rebuild_semantic_index, replace_active_model_rebuild
    ledger = Ledger().ensure()
    ledger.remember("Synthetic orphan receipt source", source="user_said")
    ledger.ingest(text="Synthetic retained chunk source.", uri="note://synthetic-retained")
    config.set_config(ledger.db_path, key="embedding.backend", value="fake", actor_id=ledger.owner_id)
    sidecar = tmp_path / "prune.db"
    rebuild_semantic_index(ledger.db_path, sidecar_path=sidecar)
    embed_chunks(ledger.db_path, actor_id=ledger.owner_id, sidecar_path=sidecar)
    with connect(sidecar) as connection:
        fact = connection.execute("SELECT * FROM semantic_vector_revisions WHERE record_type='fact'").fetchone()
        assert fact is not None
        identifier = int(fact["record_id"])
        connection.execute("INSERT INTO semantic_vector_revisions VALUES('fact',?,?,?,?)",
                           (identifier, "synthetic-other-model", fact["content_hash"], fact["vector_sha256"]))
        if replacement:
            connection.execute("INSERT INTO semantic_vector_revisions VALUES('fact',999999,?,?,?)",
                               (fact["embedding_model"], "synthetic-orphan", "synthetic-bytes"))
        preserved = [tuple(row) for row in connection.execute(
            "SELECT * FROM semantic_vector_revisions WHERE record_type='chunk' OR embedding_model='synthetic-other-model' "
            "ORDER BY record_type,record_id,embedding_model")]
        assert len(preserved) >= 2
    with connect(ledger.db_path) as connection:
        connection.execute("DELETE FROM facts WHERE id=?", (identifier,))
    if replacement:
        replace_active_model_rebuild(ledger.db_path, sidecar_path=sidecar)
    else:
        rebuild_semantic_index(ledger.db_path, record_type="fact", record_id=identifier, sidecar_path=sidecar)
    with connect_readonly(sidecar) as connection:
        assert connection.execute("SELECT COUNT(*) FROM semantic_vector_revisions WHERE record_type='fact' "
                                  "AND record_id IN (?,999999) AND embedding_model=?",
                                  (identifier, fact["embedding_model"])).fetchone()[0] == 0
        assert [tuple(row) for row in connection.execute(
            "SELECT * FROM semantic_vector_revisions WHERE record_type='chunk' OR embedding_model='synthetic-other-model' "
            "ORDER BY record_type,record_id,embedding_model")] == preserved


@pytest.mark.parametrize("replacement", [False, True])
def test_explicit_record_rebuild_publishes_and_repairs_receipts(home, tmp_path, replacement):
    from ledger_memory import config
    from ledger_memory.api import Ledger
    from ledger_memory.db import connect
    from ledger_memory.embeddings import DeterministicFakeBackend
    from ledger_memory.semantic_pending import pending_embeddings
    from ledger_memory.semantic_rebuild import rebuild_semantic_index, replace_active_model_rebuild
    ledger = Ledger().ensure()
    ledger.remember("Synthetic explicit record receipts", source="user_said")
    config.set_config(ledger.db_path, key="embedding.backend", value="fake", actor_id=ledger.owner_id)
    sidecar = tmp_path / "record-receipts.db"
    backend = DeterministicFakeBackend()
    rebuild = replace_active_model_rebuild if replacement else rebuild_semantic_index
    first = rebuild(ledger.db_path, sidecar_path=sidecar)
    assert first.embedded_rows > 0
    assert pending_embeddings(ledger.db_path, sidecar, backend=backend) == []
    with connect(sidecar) as connection:
        connection.execute("DELETE FROM semantic_vector_revisions WHERE record_type='fact'")
    missing = pending_embeddings(ledger.db_path, sidecar, backend=backend)
    assert missing and all(item.record_type == "fact" for item in missing)
    repaired = rebuild_semantic_index(ledger.db_path, record_type="fact", sidecar_path=sidecar)
    assert repaired.embedded_rows == len(missing)
    assert pending_embeddings(ledger.db_path, sidecar, backend=backend) == []


@pytest.mark.parametrize("failure", ["policy", "index"])
def test_document_api_falls_back_on_policy_reselection_and_index_read_errors(home, monkeypatch, failure):
    from ledger_memory import config, rag_search as rag
    from ledger_memory.api import Ledger
    from ledger_memory.chunk_embeddings import embed_chunks
    from ledger_memory.db import connect
    from ledger_memory.embeddings import DeterministicFakeBackend
    ledger = Ledger().ensure()
    doc = ledger.ingest(text="Synthetic malformed index fallback.", uri="note://synthetic-malformed")
    config.set_config(ledger.db_path, key="embedding.backend", value="fake", actor_id=ledger.owner_id)
    embed_chunks(ledger.db_path, actor_id=ledger.owner_id, sidecar_path=ledger.vectors_path)
    assert ledger.search_documents("Synthetic")["mode"] == "semantic"
    if failure == "index":
        with connect(ledger.vectors_path) as connection:
            connection.execute("ALTER TABLE chunk_vectors RENAME COLUMN vector TO broken_vector")
    else:
        class Backend(DeterministicFakeBackend):
            def embed(self, texts):
                config.set_config(ledger.db_path, key="embedding.backend", value="SYNTHETIC_PRIVATE_INVALID_SETTING",
                                  actor_id=ledger.owner_id)
                return super().embed(texts)
        monkeypatch.setattr(rag, "get_embedding_backend", lambda database: Backend())
    response = ledger.search_documents("Synthetic")
    assert response["mode"] == "keyword (semantic unavailable)"
    expected = "chunk index unavailable" if failure == "index" else "embedding backend unavailable: ValueError"
    assert response["semantic_unavailable_reason"] == expected
    assert "SYNTHETIC_PRIVATE_INVALID_SETTING" not in str(response)
    assert [row["document_id"] for row in response["results"]] == [doc["document_id"]]


def test_document_api_exposes_rag_refusal_and_preserves_keyword_fallback(home):
    from ledger_memory import config
    from ledger_memory.api import Ledger
    from ledger_memory.chunk_embeddings import embed_chunks
    from ledger_memory.db import connect
    ledger = Ledger().ensure()
    doc = ledger.ingest(text="Synthetic fallback source.", uri="note://synthetic-fallback")
    config.set_config(ledger.db_path, key="embedding.backend", value="fake", actor_id=ledger.owner_id)
    embed_chunks(ledger.db_path, actor_id=ledger.owner_id, sidecar_path=ledger.vectors_path)
    with connect(ledger.vectors_path) as connection:
        connection.execute("DELETE FROM semantic_vector_revisions WHERE record_type='chunk'")
    response = ledger.search_documents("Synthetic")
    assert response["mode"] == "keyword (semantic unavailable)"
    assert response["semantic_unavailable_reason"] == "chunk revision receipt mismatch"
    assert [item["document_id"] for item in response["results"]] == [doc["document_id"]]


def test_rag_outcome_distinguishes_empty_permission_and_revision_refusal(home, tmp_path):
    from ledger_memory import config
    from ledger_memory.api import Ledger
    from ledger_memory.chunk_embeddings import embed_chunks
    from ledger_memory.db import connect
    from ledger_memory.rag_search import rag_search, rag_search_outcome
    ledger = Ledger().ensure()
    doc = ledger.ingest(text="Synthetic outcome source.", uri="note://synthetic-outcome")
    config.set_config(ledger.db_path, key="embedding.backend", value="fake", actor_id=ledger.owner_id)
    sidecar = tmp_path / "outcome.db"
    embed_chunks(ledger.db_path, actor_id=ledger.owner_id, sidecar_path=sidecar)
    rows, reason = rag_search_outcome(ledger.db_path, "Synthetic", sidecar_path=sidecar)
    assert len(rows) == doc["chunks"] > 0
    assert reason is None
    assert rag_search(ledger.db_path, "Synthetic", sidecar_path=sidecar) == rows
    assert rag_search_outcome(ledger.db_path, "Synthetic", max_sensitivity="public", sidecar_path=sidecar) == ([], None)
    with connect(sidecar) as connection:
        connection.execute("DELETE FROM semantic_vector_revisions WHERE record_type='chunk'")
    assert rag_search_outcome(ledger.db_path, "Synthetic", sidecar_path=sidecar) == ([], "chunk revision receipt mismatch")
    assert rag_search(ledger.db_path, "Synthetic", sidecar_path=sidecar) == []


def test_rag_outcome_reports_backend_failure_without_private_exception_text(home, tmp_path, monkeypatch):
    from ledger_memory import config, rag_search as rag
    from ledger_memory.api import Ledger
    from ledger_memory.chunk_embeddings import embed_chunks
    from ledger_memory.embeddings import DeterministicFakeBackend
    ledger = Ledger().ensure()
    ledger.ingest(text="Synthetic failing outcome source.", uri="note://synthetic-failure")
    config.set_config(ledger.db_path, key="embedding.backend", value="fake", actor_id=ledger.owner_id)
    sidecar = tmp_path / "failed-outcome.db"
    embed_chunks(ledger.db_path, actor_id=ledger.owner_id, sidecar_path=sidecar)
    class Backend(DeterministicFakeBackend):
        def embed(self, texts):
            raise RuntimeError("SYNTHETIC_PRIVATE_EXCEPTION_TEXT")
    monkeypatch.setattr(rag, "get_embedding_backend", lambda database: Backend())
    rows, reason = rag.rag_search_outcome(ledger.db_path, "Synthetic", sidecar_path=sidecar)
    assert rows == []
    assert reason == "embedding backend unavailable: RuntimeError"
    assert "SYNTHETIC_PRIVATE_EXCEPTION_TEXT" not in reason


def test_rag_binding_is_checked_on_the_vector_snapshot(home, tmp_path, monkeypatch):
    from ledger_memory import config, rag_search as rag
    from ledger_memory.api import Ledger
    from ledger_memory.chunk_embeddings import embed_chunks
    from ledger_memory.embeddings import DeterministicFakeBackend
    from ledger_memory.semantic_binding import GenerationReceipt, write_receipt
    ledger = Ledger().ensure()
    doc = ledger.ingest(text="Synthetic binding snapshot source.", uri="note://synthetic-binding")
    config.set_config(ledger.db_path, key="embedding.backend", value="fake", actor_id=ledger.owner_id)
    sidecar = tmp_path / "binding-rag.db"
    embed_chunks(ledger.db_path, actor_id=ledger.owner_id, sidecar_path=sidecar)
    assert len(rag.rag_search(ledger.db_path, "Synthetic", sidecar_path=sidecar)) == doc["chunks"] > 0
    original = rag._chunk_provenance
    transitions = []
    def change_binding(*args, **kwargs):
        result = original(*args, **kwargs)
        backend = DeterministicFakeBackend()
        write_receipt(sidecar, GenerationReceipt("synthetic-foreign-lineage", backend.model_name,
                      backend.dimensions, 0, 1, "synthetic-manifest", "synthetic-time"))
        transitions.append(True)
        return result
    monkeypatch.setattr(rag, "_chunk_provenance", change_binding)
    assert rag.rag_search(ledger.db_path, "Synthetic", sidecar_path=sidecar) == []
    assert transitions == [True]


@pytest.mark.parametrize("change", ["source", "vector", "model", "receipt"])
def test_rag_checks_actual_revisions_after_query_inference(home, tmp_path, monkeypatch, change):
    import struct
    from ledger_memory import config, rag_search as rag
    from ledger_memory.api import Ledger
    from ledger_memory.chunk_embeddings import embed_chunks
    from ledger_memory.db import connect
    from ledger_memory.embeddings import DeterministicFakeBackend
    ledger = Ledger().ensure()
    doc = ledger.ingest(text="Synthetic RAG revision source.", uri="note://synthetic-rag")
    config.set_config(ledger.db_path, key="embedding.backend", value="fake", actor_id=ledger.owner_id)
    sidecar = tmp_path / "rag.db"
    embed_chunks(ledger.db_path, document_id=doc["document_id"], actor_id=ledger.owner_id, sidecar_path=sidecar)
    baseline = rag.rag_search(ledger.db_path, "Synthetic", sidecar_path=sidecar)
    assert len(baseline) == doc["chunks"] > 0, "positive read gate did not open"
    calls = []
    class Backend(DeterministicFakeBackend):
        def embed(self, texts):
            calls.append(True)
            if change == "source":
                with connect(ledger.db_path) as connection:
                    connection.execute("UPDATE chunks SET content='Synthetic changed source'")
            elif change == "model":
                config.set_config(ledger.db_path, key="embedding.backend", value="none", actor_id=ledger.owner_id)
            else:
                with connect(sidecar) as connection:
                    if change == "vector":
                        connection.execute("UPDATE chunk_vectors SET vector=?",
                            (struct.pack(f"{self.dimensions}f", *([0.0] * self.dimensions)),))
                    else:
                        connection.execute("DELETE FROM semantic_vector_revisions WHERE record_type='chunk'")
            return super().embed(texts)
    monkeypatch.setattr(rag, "get_embedding_backend", lambda database: Backend())
    # Keep the real policy resolver separate from the query backend injection.
    assert rag.rag_search(ledger.db_path, "Synthetic", sidecar_path=sidecar) == []
    assert calls == [True]


def test_explicit_chunk_embedding_publishes_and_repairs_revision_receipts(home, tmp_path):
    from ledger_memory import config
    from ledger_memory.api import Ledger
    from ledger_memory.chunk_embeddings import embed_chunks
    from ledger_memory.db import connect, connect_readonly
    from ledger_memory.embeddings import DeterministicFakeBackend
    from ledger_memory.semantic_pending import pending_embeddings
    ledger = Ledger().ensure()
    document = ledger.ingest(text="Synthetic document for chunk receipts.", uri="note://synthetic-receipts")
    config.set_config(ledger.db_path, key="embedding.backend", value="fake", actor_id=ledger.owner_id)
    sidecar = tmp_path / "chunk-receipts.db"
    backend = DeterministicFakeBackend()
    first = embed_chunks(ledger.db_path, document_id=document["document_id"], actor_id=ledger.owner_id,
                         sidecar_path=sidecar)
    assert first.embedded_rows == document["chunks"] > 0
    assert not [item for item in pending_embeddings(ledger.db_path, sidecar, backend=backend)
                if item.record_type == "chunk"]
    second = embed_chunks(ledger.db_path, document_id=document["document_id"], actor_id=ledger.owner_id,
                          sidecar_path=sidecar)
    assert second.embedded_rows == 0
    assert second.skipped_rows == first.embedded_rows
    with connect(sidecar) as connection:
        connection.execute("DELETE FROM semantic_vector_revisions WHERE record_type='chunk'")
    repaired = embed_chunks(ledger.db_path, document_id=document["document_id"], actor_id=ledger.owner_id,
                            sidecar_path=sidecar)
    assert repaired.embedded_rows == first.embedded_rows
    assert not [item for item in pending_embeddings(ledger.db_path, sidecar, backend=backend)
                if item.record_type == "chunk"]
    with connect_readonly(sidecar) as connection:
        assert connection.execute("SELECT COUNT(*) FROM semantic_vectors WHERE record_type='chunk'").fetchone()[0] == 0


@pytest.mark.parametrize("change", ["source", "model"])
def test_explicit_chunk_embedding_discards_inference_time_revision_changes(home, tmp_path, monkeypatch, change):
    from ledger_memory import chunk_embeddings, config
    from ledger_memory.api import Ledger
    from ledger_memory.db import connect, connect_readonly
    from ledger_memory.embeddings import DeterministicFakeBackend
    ledger = Ledger().ensure()
    document = ledger.ingest(text="Synthetic document changed during inference.", uri="note://synthetic-change")
    config.set_config(ledger.db_path, key="embedding.backend", value="fake", actor_id=ledger.owner_id)
    sidecar = tmp_path / "changed-chunk.db"
    observed = []
    class Backend(DeterministicFakeBackend):
        def embed(self, texts):
            # A separate writer proves inference does not hold a core writer lock.
            with connect(ledger.db_path) as connection:
                connection.execute("BEGIN IMMEDIATE")
                if change == "source":
                    connection.execute("UPDATE chunks SET content='Synthetic replacement' WHERE document_id=?",
                                       (document["document_id"],))
            if change == "model":
                config.set_config(ledger.db_path, key="embedding.backend", value="none", actor_id=ledger.owner_id)
            observed.append(True)
            return super().embed(texts)
    monkeypatch.setattr(chunk_embeddings, "get_embedding_backend", lambda database: Backend())
    with connect_readonly(ledger.db_path) as connection:
        before = connection.execute("SELECT COUNT(*) FROM events WHERE event_type='chunks.embedded'").fetchone()[0]
    with pytest.raises(RuntimeError, match="source or model changed"):
        chunk_embeddings.embed_chunks(ledger.db_path, document_id=document["document_id"],
                                      actor_id=ledger.owner_id, sidecar_path=sidecar)
    assert observed == [True]
    if sidecar.exists():
        with connect_readonly(sidecar) as connection:
            assert connection.execute("SELECT COUNT(*) FROM chunk_vectors").fetchone()[0] == 0
    with connect_readonly(ledger.db_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM events WHERE event_type='chunks.embedded'").fetchone()[0] == before


@pytest.mark.parametrize("extra", [False, True])
def test_explicit_chunk_wrong_vector_count_does_not_publish(home, tmp_path, monkeypatch, extra):
    from ledger_memory import chunk_embeddings, config
    from ledger_memory.api import Ledger
    from ledger_memory.db import connect_readonly
    from ledger_memory.embeddings import DeterministicFakeBackend
    ledger = Ledger().ensure()
    document = ledger.ingest(text="Synthetic vector count document.", uri="note://synthetic-count")
    config.set_config(ledger.db_path, key="embedding.backend", value="fake", actor_id=ledger.owner_id)
    sidecar = tmp_path / "count.db"
    calls = []
    class Backend(DeterministicFakeBackend):
        def embed(self, texts):
            calls.append(len(texts))
            vectors = super().embed(texts)
            return vectors + vectors[:1] if extra else vectors[:-1]
    monkeypatch.setattr(chunk_embeddings, "get_embedding_backend", lambda database: Backend())
    with pytest.raises(RuntimeError, match="invalid chunk vector count"):
        chunk_embeddings.embed_chunks(ledger.db_path, document_id=document["document_id"],
                                      actor_id=ledger.owner_id, sidecar_path=sidecar)
    assert calls == [document["chunks"]]
    assert calls[0] > 0
    assert not sidecar.exists()
    with connect_readonly(ledger.db_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM events WHERE event_type='chunks.embedded'").fetchone()[0] == 0


def test_revision_match_requires_actual_blob_source_and_core_metadata():
    import hashlib
    import struct
    from ledger_memory.semantic_pending import PendingEmbedding, revision_matches
    item = PendingEmbedding("fact", 1, "synthetic-source-hash", "Synthetic source")
    blob = struct.pack("2f", 1.0, 0.0)
    receipt = (item.content_hash, hashlib.sha256(blob).hexdigest())
    metadata = (item.content_hash, 2)
    assert revision_matches(item, blob, receipt, metadata, dimensions=2)
    assert not revision_matches(item, struct.pack("2f", 0.0, 1.0), receipt, metadata, dimensions=2)
    assert not revision_matches(item, blob, ("older-source", receipt[1]), metadata, dimensions=2)
    assert not revision_matches(item, blob, receipt, ("older-source", 2), dimensions=2)
    assert not revision_matches(item, blob, receipt, (item.content_hash, 3), dimensions=2)
    assert not revision_matches(item, bytearray(blob), receipt, metadata, dimensions=2)
    assert not revision_matches(item, blob, receipt, None, dimensions=2)
    assert not revision_matches(item, blob, None, metadata, dimensions=2)
    assert not revision_matches(item, blob, receipt, metadata, dimensions=3)


def test_revision_match_chunks_need_receipt_but_not_record_metadata():
    import hashlib
    import struct
    from ledger_memory.semantic_pending import PendingEmbedding, revision_matches
    item = PendingEmbedding("chunk", 1, "synthetic-chunk-hash", "Synthetic chunk")
    blob = struct.pack("2f", 1.0, 0.0)
    receipt = (item.content_hash, hashlib.sha256(blob).hexdigest())
    assert revision_matches(item, blob, receipt, None, dimensions=2)
    assert not revision_matches(item, blob, None, None, dimensions=2)
    assert not revision_matches(item, blob, ("older-chunk", receipt[1]), None, dimensions=2)


def test_deferred_record_publication_invalidates_only_its_ann_model(home, tmp_path):
    from ledger_memory import config
    from ledger_memory.api import Ledger
    from ledger_memory.db import connect, connect_readonly
    from ledger_memory.embeddings import DeterministicFakeBackend
    from ledger_memory.semantic_pending import pending_embeddings, publish_embedding
    from ledger_memory.semantic_vec import META_TABLE
    ledger = Ledger().ensure()
    ledger.remember("Synthetic ANN invalidation source", source="user_said")
    config.set_config(ledger.db_path, key="embedding.backend", value="fake", actor_id=ledger.owner_id)
    backend = DeterministicFakeBackend()
    sidecar = tmp_path / "ann-receipt.db"
    item = next(row for row in pending_embeddings(ledger.db_path, sidecar, backend=backend)
                if row.record_type == "fact")
    with connect(sidecar) as connection:
        connection.execute(f"CREATE TABLE {META_TABLE}(model TEXT,dimensions INTEGER)")
        connection.executemany(f"INSERT INTO {META_TABLE} VALUES(?,?)",
                               [(backend.model_name, backend.dimensions), ("synthetic-other-model", 7)])
    assert publish_embedding(ledger.db_path, sidecar, item, backend.embed([item.text])[0], backend=backend)
    with connect_readonly(sidecar) as connection:
        rows = connection.execute(f"SELECT model,dimensions FROM {META_TABLE}").fetchall()
    assert [tuple(row) for row in rows] == [("synthetic-other-model", 7)]
    assert item not in pending_embeddings(ledger.db_path, sidecar, backend=backend)


def test_coordinator_rechecks_after_unlock_to_avoid_lost_wakeup(home, tmp_path):
    from ledger_memory import config
    from ledger_memory.api import Ledger
    from ledger_memory.embeddings import DeterministicFakeBackend
    from ledger_memory.semantic_pending import pending_embeddings, run_pending_worker
    ledger = Ledger().ensure()
    ledger.remember("Synthetic initial coordinator source", source="user_said")
    config.set_config(ledger.db_path, key="embedding.backend", value="fake", actor_id=ledger.owner_id)
    backend = DeterministicFakeBackend()
    sidecar = tmp_path / "coordinator.db"
    injected = []
    def after_unlock():
        if not injected:
            injected.append(True)
            # Arrives after the worker observed no eligible work and released.
            ledger.remember("Synthetic late coordinator source", source="user_said")
    result = run_pending_worker(ledger.db_path, sidecar, backend=backend, _after_unlock=after_unlock)
    assert result["status"] == "completed"
    assert result["published"] > 0
    assert injected == [True]
    assert pending_embeddings(ledger.db_path, sidecar, backend=backend) == []


def test_coordinator_rechecks_disabled_selection_after_coalesced_enable(home, tmp_path, monkeypatch):
    import pytest
    from concurrent.futures import ThreadPoolExecutor
    from ledger_memory import config, semantic_pending as pending, worker_spawn
    from ledger_memory.api import Ledger
    from ledger_memory.embeddings import DeterministicFakeBackend, SemanticSearchNotConfiguredError
    ledger = Ledger().ensure()
    sidecar = tmp_path / "reenabled.db"
    original = pending.get_embedding_backend
    coalesced = []
    monkeypatch.setattr(worker_spawn, "spawn_pending_worker", lambda *a: pytest.fail("held owner should coalesce request"))
    def selection(database):
        try:
            return original(database)
        except SemanticSearchNotConfiguredError:
            def producer():
                config.set_config(ledger.db_path, key="embedding.backend", value="fake", actor_id=ledger.owner_id)
                ledger.remember("Synthetic enabled during exit", source="user_said")
                coalesced.append(pending.request_drain(ledger.db_path, sidecar))
            with ThreadPoolExecutor(max_workers=1) as pool:
                pool.submit(producer).result(timeout=3)
            raise
    monkeypatch.setattr(pending, "get_embedding_backend", selection)
    result = pending.run_pending_worker(ledger.db_path, sidecar)
    assert coalesced == [False], "producer did not observe the actual held coordinator lock"
    assert result["status"] == "completed"
    assert result["published"] > 0
    assert pending.pending_embeddings(ledger.db_path, sidecar, backend=DeterministicFakeBackend()) == []


def test_coordinator_follows_current_model_after_stale_backend_injection(home, tmp_path, monkeypatch):
    import pytest
    from concurrent.futures import ThreadPoolExecutor
    from ledger_memory import config, semantic_pending as pending, worker_spawn
    from ledger_memory.api import Ledger
    from ledger_memory.embeddings import DeterministicFakeBackend
    ledger = Ledger().ensure()
    config.set_config(ledger.db_path, key="embedding.backend", value="fake", actor_id=ledger.owner_id)
    sidecar = tmp_path / "current-policy.db"
    original = pending.get_embedding_backend
    coalesced = []
    class OldBackend(DeterministicFakeBackend):
        model_name = "synthetic-old-model"
        def embed(self, texts):
            pytest.fail("stale injected model was used instead of current policy")
    monkeypatch.setattr(worker_spawn, "spawn_pending_worker", lambda *a: pytest.fail("owned job was not coalesced"))
    def selection(database):
        current = original(database)
        if not coalesced:
            def producer():
                ledger.remember("Synthetic current-policy source", source="user_said")
                coalesced.append(pending.request_drain(ledger.db_path, sidecar))
            with ThreadPoolExecutor(max_workers=1) as pool:
                pool.submit(producer).result(timeout=3)
        return current
    monkeypatch.setattr(pending, "get_embedding_backend", selection)
    result = pending.run_pending_worker(ledger.db_path, sidecar, backend=OldBackend())
    assert coalesced == [False]
    assert result["status"] == "completed"
    assert result["published"] > 0
    assert pending.pending_embeddings(ledger.db_path, sidecar, backend=DeterministicFakeBackend()) == []


def test_request_drain_releases_probe_lock_before_fixed_launch(home, tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from ledger_memory import config, semantic_pending as pending, worker_spawn
    from ledger_memory.api import Ledger
    from ledger_memory.locking import exclusive
    ledger = Ledger().ensure()
    config.set_config(ledger.db_path, key="embedding.backend", value="fake", actor_id=ledger.owner_id)
    sidecar = tmp_path / "queued.db"
    launches = []
    def launch(database, target):
        assert database == ledger.db_path
        assert target == sidecar
        def independent_owner():
            with exclusive(pending.pending_lock_path(sidecar), timeout=0):
                launches.append(True)
        with ThreadPoolExecutor(max_workers=1) as pool:
            pool.submit(independent_owner).result(timeout=2)
        return 123
    monkeypatch.setattr(worker_spawn, "spawn_pending_worker", launch)
    assert pending.request_drain(ledger.db_path, sidecar)
    assert launches == [True]


def test_coordinator_busy_lock_does_not_duplicate_work(home, tmp_path):
    import threading
    from ledger_memory import config, semantic_pending as pending
    from ledger_memory.api import Ledger
    from ledger_memory.embeddings import DeterministicFakeBackend
    from ledger_memory.locking import exclusive
    ledger = Ledger().ensure()
    config.set_config(ledger.db_path, key="embedding.backend", value="fake", actor_id=ledger.owner_id)
    sidecar = tmp_path / "busy.db"
    held, release = threading.Event(), threading.Event()
    def owner():
        with exclusive(pending.pending_lock_path(sidecar), timeout=1):
            held.set()
            release.wait(3)
    thread = threading.Thread(target=owner)
    thread.start()
    try:
        assert held.wait(1)
        result = pending.run_pending_worker(ledger.db_path, sidecar, backend=DeterministicFakeBackend())
        assert result == {"status": "busy", "published": 0, "failed": 0, "discarded": 0}
        assert not sidecar.exists()
    finally:
        release.set()
        thread.join(2)
    assert not thread.is_alive()


def test_pending_entry_reports_content_free_failure(tmp_path, monkeypatch, capsys):
    import json
    from ledger_memory import semantic_pending as pending
    def fail(*args, **kwargs):
        raise RuntimeError("SYNTHETIC_PRIVATE_FAILURE_DETAIL")
    monkeypatch.setattr(pending, "run_pending_worker", fail)
    assert pending.main(["--database", str(tmp_path / "core.db"), "--sidecar", str(tmp_path / "vectors.db")]) == 1
    output = capsys.readouterr().out
    assert json.loads(output) == {"status": "failed", "error_class": "RuntimeError"}
    assert "SYNTHETIC_PRIVATE_FAILURE_DETAIL" not in output


def test_pending_module_entry_drains_synthetic_store_with_deadline(home, tmp_path):
    import json
    import os
    import subprocess
    import sys
    from ledger_memory import config
    from ledger_memory.api import Ledger
    from ledger_memory.embeddings import DeterministicFakeBackend
    from ledger_memory.semantic_pending import pending_embeddings
    ledger = Ledger().ensure()
    ledger.remember("Synthetic module entry source", source="user_said")
    config.set_config(ledger.db_path, key="embedding.backend", value="fake", actor_id=ledger.owner_id)
    sidecar = tmp_path / "entry-vectors.db"
    environment = {"HOME": str(home), "LEDGER_EMBEDDING_BACKEND": "fake",
                   "PYTHONDONTWRITEBYTECODE": "1", "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1"}
    if "SYSTEMROOT" in os.environ:
        environment["SYSTEMROOT"] = os.environ["SYSTEMROOT"]
    completed = subprocess.run([sys.executable, "-I", "-m", "ledger_memory.semantic_pending",
                                "--database", str(ledger.db_path), "--sidecar", str(sidecar)],
                               env=environment, capture_output=True, text=True, timeout=15, check=False)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    result = json.loads(completed.stdout)
    assert result["status"] == "completed"
    assert result["published"] > 0, "module did not execute maintenance"
    assert pending_embeddings(ledger.db_path, sidecar, backend=DeterministicFakeBackend()) == []


def test_drain_infers_without_core_writer_and_creates_no_events(home, tmp_path):
    import sqlite3
    from ledger_memory import config
    from ledger_memory.api import Ledger
    from ledger_memory.embeddings import DeterministicFakeBackend
    from ledger_memory.semantic_pending import drain_pending, pending_embeddings
    ledger = Ledger().ensure()
    ledger.remember("Synthetic drained source", source="user_said")
    config.set_config(ledger.db_path, key="embedding.backend", value="fake", actor_id=ledger.owner_id)
    sidecar = tmp_path / "drained.db"
    observed = []
    class Backend(DeterministicFakeBackend):
        def embed(self, texts):
            with sqlite3.connect(ledger.db_path, timeout=0) as connection:
                connection.execute("BEGIN IMMEDIATE")
                connection.rollback()
            observed.extend(texts)
            return super().embed(texts)
    backend = Backend()
    with sqlite3.connect(ledger.db_path) as connection:
        events = connection.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    expected = pending_embeddings(ledger.db_path, sidecar, backend=backend)
    result = drain_pending(ledger.db_path, sidecar, backend=backend)
    assert result == {"published": len(expected), "failed": 0, "discarded": 0}
    assert observed == [item.text for item in expected]
    assert observed, "fixture did not actually run inference"
    assert pending_embeddings(ledger.db_path, sidecar, backend=backend) == []
    with sqlite3.connect(ledger.db_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM events").fetchone()[0] == events


def test_revision_failure_retries_are_bounded(home, tmp_path):
    import sqlite3
    from ledger_memory import config
    from ledger_memory.api import Ledger
    from ledger_memory.embeddings import DeterministicFakeBackend
    from ledger_memory.semantic_pending import drain_pending, pending_embeddings
    ledger = Ledger().ensure()
    ledger.remember("Synthetic failing source", source="user_said")
    config.set_config(ledger.db_path, key="embedding.backend", value="fake", actor_id=ledger.owner_id)
    sidecar = tmp_path / "failures.db"
    calls = []
    class Backend(DeterministicFakeBackend):
        def embed(self, texts):
            calls.extend(texts)
            raise RuntimeError("SYNTHETIC_ERROR_MESSAGE_MUST_NOT_BE_STORED")
    backend = Backend()
    expected = pending_embeddings(ledger.db_path, sidecar, backend=backend)
    assert expected
    for _ in range(3):
        assert drain_pending(ledger.db_path, sidecar, backend=backend)["failed"] == len(expected)
    assert drain_pending(ledger.db_path, sidecar, backend=backend) == {"published": 0, "failed": 0, "discarded": 0}
    assert len(calls) == 3 * len(expected)
    failed = pending_embeddings(ledger.db_path, sidecar, backend=backend, include_failed=True)
    assert len(failed) == len(expected)
    assert all(item.attempts == 3 for item in failed)
    with sqlite3.connect(sidecar) as connection:
        rows = connection.execute("SELECT attempts,last_error_class FROM embedding_failures").fetchall()
        assert rows and all(row == (3, "RuntimeError") for row in rows)
        assert "SYNTHETIC_ERROR_MESSAGE" not in str(rows)


def test_drain_counts_invalid_vector_as_bounded_failure(home, tmp_path):
    from ledger_memory import config
    from ledger_memory.api import Ledger
    from ledger_memory.embeddings import DeterministicFakeBackend
    from ledger_memory.semantic_pending import drain_pending, pending_embeddings
    ledger = Ledger().ensure()
    ledger.remember("Synthetic malformed vector source", source="user_said")
    config.set_config(ledger.db_path, key="embedding.backend", value="fake", actor_id=ledger.owner_id)
    sidecar = tmp_path / "bad-vector.db"
    class Backend(DeterministicFakeBackend):
        def embed(self, texts):
            return [None]
    backend = Backend()
    expected = pending_embeddings(ledger.db_path, sidecar, backend=backend)
    assert expected
    assert drain_pending(ledger.db_path, sidecar, backend=backend)["failed"] == len(expected)
    assert all(item.attempts == 1 for item in pending_embeddings(ledger.db_path, sidecar, backend=backend))


def test_document_chunks_are_drained_separately(tmp_path, monkeypatch):
    import hashlib
    import sqlite3
    import struct
    from types import SimpleNamespace
    from ledger_memory import semantic_pending as pending
    from ledger_memory.chunk_embeddings import CHUNK_SIDECAR_DDL
    from ledger_memory.semantic_rebuild import SIDECAR_DDL
    database, sidecar = tmp_path / "core.db", tmp_path / "vectors.db"
    with sqlite3.connect(database) as connection:
        connection.executescript("CREATE TABLE chunks(id INTEGER PRIMARY KEY,content TEXT);"
                                 "CREATE TABLE semantic_index_entries(record_type,record_id,content_hash,embedding_model,embedding_dimensions);")
        connection.execute("INSERT INTO chunks VALUES(1,'synthetic chunk')")
    monkeypatch.setattr(pending, "_source_rows", lambda connection: [])
    backend = SimpleNamespace(model_name="synthetic-model", dimensions=2)
    item, = pending.pending_embeddings(database, sidecar, backend=backend)
    assert item.record_type == "chunk"
    blob = struct.pack("2f", 1, 0)
    with sqlite3.connect(sidecar) as connection:
        connection.executescript(SIDECAR_DDL + CHUNK_SIDECAR_DDL)
        connection.execute("INSERT INTO semantic_vectors(record_type,record_id,embedding_model,vector) VALUES('chunk',1,?,?)",
                           (backend.model_name, blob))
        connection.execute("INSERT INTO semantic_vector_revisions VALUES('chunk',1,?,?,?)",
                           (backend.model_name, item.content_hash, hashlib.sha256(blob).hexdigest()))
    assert pending.pending_embeddings(database, sidecar, backend=backend) == [item], "wrong table admitted chunk"
    with sqlite3.connect(sidecar) as connection:
        connection.execute("INSERT INTO chunk_vectors(chunk_id,embedding_model,vector) VALUES(1,?,?)",
                           (backend.model_name, blob))
    assert pending.pending_embeddings(database, sidecar, backend=backend) == [], "valid chunk could not be admitted"
    with sqlite3.connect(sidecar) as connection:
        connection.execute("DELETE FROM chunk_vectors")
        connection.execute("INSERT INTO embedding_failures VALUES('chunk',1,?,?,3,'SyntheticError',CURRENT_TIMESTAMP)",
                           (item.content_hash, backend.model_name))
    assert pending.pending_embeddings(database, sidecar, backend=backend) == []
    failed, = pending.pending_embeddings(database, sidecar, backend=backend, include_failed=True)
    assert failed.attempts == 3
    with sqlite3.connect(database) as connection:
        connection.execute("UPDATE chunks SET content='changed synthetic chunk'")
    revised, = pending.pending_embeddings(database, sidecar, backend=backend)
    assert revised.content_hash != item.content_hash
    assert revised.attempts == 0


def test_publish_vector_receipt_then_core_metadata_and_detect_split(home, tmp_path):
    import sqlite3
    import pytest
    from ledger_memory import config
    from ledger_memory.api import Ledger
    from ledger_memory.embeddings import DeterministicFakeBackend
    from ledger_memory.semantic_pending import pending_embeddings, publish_embedding
    ledger = Ledger().ensure()
    ledger.remember("Synthetic publication source", source="user_said")
    config.set_config(ledger.db_path, key="embedding.backend", value="fake", actor_id=ledger.owner_id)
    backend = DeterministicFakeBackend()
    sidecar = tmp_path / "derived.db"
    item = next(row for row in pending_embeddings(ledger.db_path, sidecar, backend=backend) if row.record_type == "fact")
    vector = backend.embed([item.text])[0]
    def split():
        raise RuntimeError("synthetic split publication")
    with pytest.raises(RuntimeError, match="split publication"):
        publish_embedding(ledger.db_path, sidecar, item, vector, backend=backend, _after_sidecar_commit=split)
    assert item in pending_embeddings(ledger.db_path, sidecar, backend=backend)
    with sqlite3.connect(sidecar) as connection:
        assert connection.execute("SELECT COUNT(*) FROM semantic_vectors").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM semantic_vector_revisions").fetchone()[0] == 1
    assert publish_embedding(ledger.db_path, sidecar, item, vector, backend=backend)
    assert item not in pending_embeddings(ledger.db_path, sidecar, backend=backend)


def _fake_store(tmp_path, statement):
    from ledger_memory import config
    from ledger_memory.api import Ledger
    from ledger_memory.embeddings import DeterministicFakeBackend
    from ledger_memory.semantic_pending import pending_embeddings
    ledger = Ledger().ensure()
    ledger.remember(statement, source="user_said")
    config.set_config(ledger.db_path, key="embedding.backend", value="fake", actor_id=ledger.owner_id)
    backend = DeterministicFakeBackend()
    sidecar = tmp_path / "derived.db"
    item = next(row for row in pending_embeddings(ledger.db_path, sidecar, backend=backend) if row.record_type == "fact")
    return ledger, backend, sidecar, item


def test_current_content_hash_checked_at_publication(home, tmp_path):
    import sqlite3
    from ledger_memory.semantic_pending import pending_embeddings, publish_embedding
    ledger, backend, sidecar, item = _fake_store(tmp_path, "Synthetic publication revision")
    vector = backend.embed([item.text])[0]
    with sqlite3.connect(ledger.db_path) as connection:
        connection.execute("UPDATE facts SET statement='Changed synthetic revision' WHERE id=?", (item.record_id,))
    assert not publish_embedding(ledger.db_path, sidecar, item, vector, backend=backend)
    assert not sidecar.exists(), "a stale result must not initialize a sidecar"
    current = next(row for row in pending_embeddings(ledger.db_path, sidecar, backend=backend) if row.record_type == "fact")
    assert current.content_hash != item.content_hash
    assert publish_embedding(ledger.db_path, sidecar, current, backend.embed([current.text])[0], backend=backend)


def test_active_model_checked_at_publication(home, tmp_path):
    from ledger_memory import config
    from ledger_memory.semantic_pending import publish_embedding
    ledger, backend, sidecar, item = _fake_store(tmp_path, "Synthetic model revision")
    vector = backend.embed([item.text])[0]
    config.set_config(ledger.db_path, key="embedding.backend", value="none", actor_id=ledger.owner_id)
    assert not publish_embedding(ledger.db_path, sidecar, item, vector, backend=backend)
    assert not sidecar.exists(), "a result for an inactive model must not initialize a sidecar"
    config.set_config(ledger.db_path, key="embedding.backend", value="fake", actor_id=ledger.owner_id)
    assert publish_embedding(ledger.db_path, sidecar, item, vector, backend=backend)


def test_missing_vector_is_pending(home, tmp_path):
    import sqlite3
    from ledger_memory.semantic_pending import drain_pending, pending_embeddings
    ledger, backend, sidecar, item = _fake_store(tmp_path, "Synthetic vector presence")
    assert drain_pending(ledger.db_path, sidecar, backend=backend)["published"] >= 1
    assert item not in pending_embeddings(ledger.db_path, sidecar, backend=backend)
    with sqlite3.connect(sidecar) as connection:
        connection.execute("DELETE FROM semantic_vectors WHERE record_type='fact' AND record_id=?", (item.record_id,))
    assert item in pending_embeddings(ledger.db_path, sidecar, backend=backend), "a missing vector was not pending"


def test_content_change_resets_retry_budget(home, tmp_path):
    import sqlite3
    from ledger_memory.semantic_pending import pending_embeddings, record_embedding_failure
    ledger, backend, sidecar, item = _fake_store(tmp_path, "Synthetic retry budget")
    for _ in range(3):
        record_embedding_failure(ledger.db_path, sidecar, item, backend=backend, error=ValueError("synthetic"))
    assert item not in pending_embeddings(ledger.db_path, sidecar, backend=backend), "an exhausted revision was retried"
    with sqlite3.connect(ledger.db_path) as connection:
        connection.execute("UPDATE facts SET statement='Synthetic retry budget, revised' WHERE id=?", (item.record_id,))
    revised = next(row for row in pending_embeddings(ledger.db_path, sidecar, backend=backend)
                   if (row.record_type, row.record_id) == (item.record_type, item.record_id))
    assert revised.content_hash != item.content_hash
    assert revised.attempts == 0


def test_derived_pending_work_resumes_after_exit(home, tmp_path):
    from ledger_memory.embeddings import DeterministicFakeBackend
    from ledger_memory.semantic_pending import drain_pending, pending_embeddings
    ledger, backend, sidecar, item = _fake_store(tmp_path, "Synthetic interrupted drain")
    expected = pending_embeddings(ledger.db_path, sidecar, backend=backend)
    class AbruptExit(BaseException):
        """Stands in for a process killed mid-inference: nothing is caught or recorded."""
    class Dying(DeterministicFakeBackend):
        def embed(self, texts):
            raise AbruptExit()
    with pytest.raises(AbruptExit):
        drain_pending(ledger.db_path, sidecar, backend=Dying())
    resumed = pending_embeddings(ledger.db_path, sidecar, backend=backend)
    assert resumed == expected, "pending work must be re-derivable after an exit, with no attempt charged"
    assert all(row.attempts == 0 for row in resumed)
    assert drain_pending(ledger.db_path, sidecar, backend=backend)["published"] == len(expected)
    assert pending_embeddings(ledger.db_path, sidecar, backend=backend) == []


def test_current_revision_opens_read_gate(home, monkeypatch):
    """Anti-vacuity: correct receipts for the current revision do open the semantic lane."""
    from ledger_memory.api import Ledger
    from ledger_memory.semantic_readiness import readiness
    monkeypatch.setenv("LEDGER_EMBEDDING_BACKEND", "fake")
    ledger = Ledger().ensure()
    ledger.remember("Synthetic open gate fixture", source="user_said")
    ledger.refresh(repair=True)
    report = readiness(ledger.db_path, ledger.vectors_path)
    assert report.lane_open and report.state == "ready" and report.pending_rows == 0


def test_split_publication_is_refused_by_read_gate(home, monkeypatch):
    from ledger_memory.api import Ledger
    from ledger_memory.embeddings import DeterministicFakeBackend
    from ledger_memory.semantic_pending import pending_embeddings, publish_embedding
    from ledger_memory.semantic_readiness import readiness
    monkeypatch.setenv("LEDGER_EMBEDDING_BACKEND", "fake")
    ledger = Ledger().ensure()
    ledger.remember("Synthetic first fact", source="user_said")
    ledger.refresh(repair=True)
    assert readiness(ledger.db_path, ledger.vectors_path).lane_open
    monkeypatch.setenv("LEDGER_EMBEDDING_BACKEND", "none")
    ledger.remember("Synthetic split fact", source="user_said")  # written while search is off: left pending
    monkeypatch.setenv("LEDGER_EMBEDDING_BACKEND", "fake")
    backend = DeterministicFakeBackend()
    item = next(row for row in pending_embeddings(ledger.db_path, ledger.vectors_path, backend=backend)
                if row.record_type == "fact")
    vector = backend.embed([item.text])[0]
    def split():
        raise RuntimeError("synthetic split publication")
    with pytest.raises(RuntimeError, match="split publication"):
        publish_embedding(ledger.db_path, ledger.vectors_path, item, vector, backend=backend,
                          _after_sidecar_commit=split)
    report = readiness(ledger.db_path, ledger.vectors_path)
    assert not report.lane_open, "a sidecar receipt without its core metadata opened the lane"
    assert report.pending_rows >= 1
    assert publish_embedding(ledger.db_path, ledger.vectors_path, item, vector, backend=backend)
    assert readiness(ledger.db_path, ledger.vectors_path).lane_open, "the completed publication did not reopen the lane"

def test_publication_ownership_recheck_precedes_all_schema_writes(home, tmp_path, monkeypatch):
    import sqlite3
    import pytest
    from ledger_memory import config, semantic_binding
    from ledger_memory.api import Ledger
    from ledger_memory.embeddings import DeterministicFakeBackend
    from ledger_memory.semantic_pending import pending_embeddings, publish_embedding
    ledger = Ledger().ensure()
    ledger.remember("Synthetic ownership race source", source="user_said")
    config.set_config(ledger.db_path, key="embedding.backend", value="fake", actor_id=ledger.owner_id)
    backend = DeterministicFakeBackend()
    sidecar = tmp_path / "claimed.db"
    item = next(row for row in pending_embeddings(ledger.db_path, sidecar, backend=backend) if row.record_type == "fact")
    checks = []
    def claim_after_first_check(database, target):
        checks.append(True)
        if len(checks) == 1:
            with sqlite3.connect(target) as connection:
                connection.execute("CREATE TABLE foreign_sentinel(value TEXT)")
                connection.execute("INSERT INTO foreign_sentinel VALUES('claimed')")
            return None
        return "synthetic foreign ownership"
    monkeypatch.setattr(semantic_binding, "foreign_binding", claim_after_first_check)
    with pytest.raises(semantic_binding.SidecarOwnershipError):
        publish_embedding(ledger.db_path, sidecar, item, backend.embed([item.text])[0], backend=backend)
    assert len(checks) == 2, "ownership transition was not actually observed"
    with sqlite3.connect(sidecar) as connection:
        names = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert names == {"foreign_sentinel"}
        assert connection.execute("SELECT value FROM foreign_sentinel").fetchall() == [("claimed",)]


def test_pending_detection_is_read_only_and_missing_sidecar_stays_missing(home, tmp_path):
    from ledger_memory.api import Ledger
    from ledger_memory.embeddings import DeterministicFakeBackend
    from ledger_memory.semantic_pending import pending_embeddings
    ledger = Ledger().ensure()
    ledger.remember("Synthetic pending source", source="user_said")
    sidecar = tmp_path / "absent-sidecar.db"
    items = pending_embeddings(ledger.db_path, sidecar, backend=DeterministicFakeBackend())
    assert any(item.record_type == "fact" and "Synthetic pending source" in item.text for item in items)
    assert not sidecar.exists()


def test_pending_detection_checks_source_metadata_receipt_and_actual_blob(home, tmp_path):
    import hashlib
    import sqlite3
    import struct
    from ledger_memory.api import Ledger
    from ledger_memory.embeddings import DeterministicFakeBackend
    from ledger_memory.semantic_pending import pending_embeddings
    from ledger_memory.semantic_rebuild import SIDECAR_DDL
    ledger = Ledger().ensure()
    ledger.remember("Synthetic revision source", source="user_said")
    backend = DeterministicFakeBackend()
    sidecar = tmp_path / "vectors.db"
    item = next(row for row in pending_embeddings(ledger.db_path, sidecar, backend=backend)
                if row.record_type == "fact")
    vector = backend.embed([item.text])[0]
    packed = struct.pack(f"{backend.dimensions}f", *vector)
    key = (item.record_type, item.record_id, backend.model_name)
    with sqlite3.connect(sidecar) as connection:
        connection.executescript(SIDECAR_DDL)
        connection.execute("INSERT INTO semantic_vectors(record_type,record_id,embedding_model,vector) VALUES(?,?,?,?)",
                           (*key, packed))
        connection.execute("INSERT INTO semantic_vector_revisions VALUES(?,?,?,?,?)",
                           (*key, item.content_hash, hashlib.sha256(packed).hexdigest()))
    assert item in pending_embeddings(ledger.db_path, sidecar, backend=backend), "missing core receipt was trusted"
    with sqlite3.connect(ledger.db_path) as connection:
        connection.execute("INSERT INTO semantic_index_entries(record_type,record_id,embedding_model,content_hash,embedding_dimensions) VALUES(?,?,?,?,?)",
                           (*key, item.content_hash, backend.dimensions))
    assert item not in pending_embeddings(ledger.db_path, sidecar, backend=backend)
    with sqlite3.connect(sidecar) as connection:
        connection.execute("UPDATE semantic_vectors SET vector=?", (b"x" * len(packed),))
    assert item in pending_embeddings(ledger.db_path, sidecar, backend=backend), "changed vector bytes were trusted"
    with sqlite3.connect(sidecar) as connection:
        connection.execute("UPDATE semantic_vectors SET vector=?", (packed,))
    with sqlite3.connect(ledger.db_path) as connection:
        connection.execute("UPDATE facts SET statement='Changed synthetic source' WHERE id=?", (item.record_id,))
    current = [row for row in pending_embeddings(ledger.db_path, sidecar, backend=backend)
               if (row.record_type, row.record_id) == (item.record_type, item.record_id)]
    assert len(current) == 1
    assert current[0].content_hash != item.content_hash


def test_derived_sidecar_has_vector_revision_and_bounded_failure_tables(tmp_path):
    import sqlite3
    import pytest
    from ledger_memory.semantic_rebuild import SIDECAR_DDL
    with sqlite3.connect(tmp_path / "synthetic-vectors.db") as connection:
        connection.executescript(SIDECAR_DDL)
        names = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"semantic_vectors", "semantic_vector_revisions", "embedding_failures"} <= names
        connection.execute("INSERT INTO semantic_vector_revisions VALUES (?, ?, ?, ?, ?)",
                           ("fact", 1, "synthetic-model", "source-hash", "vector-hash"))
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("INSERT INTO semantic_vector_revisions VALUES (?, ?, ?, ?, ?)",
                               ("fact", 1, "synthetic-model", "other-source", "other-vector"))
        for attempts in (0, 4):
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute("INSERT INTO embedding_failures VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)",
                                   ("fact", 1, "source-hash", "synthetic-model", attempts, "SyntheticError"))
        connection.execute("INSERT INTO embedding_failures VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)",
                           ("fact", 1, "source-hash", "synthetic-model", 3, "SyntheticError"))
        connection.execute("INSERT INTO embedding_failures VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)",
                           ("fact", 1, "changed-source", "synthetic-model", 1, "SyntheticError"))
        assert connection.execute("SELECT COUNT(*) FROM embedding_failures").fetchone()[0] == 2


def test_legacy_configuration_keeps_keyword_recall_and_explains_upgrade(home, monkeypatch):
    import sys
    from ledger_memory import config
    from ledger_memory.api import Ledger
    monkeypatch.delenv("LEDGER_EMBEDDING_BACKEND", raising=False)
    monkeypatch.setitem(sys.modules, "ledger_memory.adapters.lmstudio", None)
    ledger = Ledger().ensure()
    config.set_config(ledger.db_path, key="embedding.backend", value="lmstudio", actor_id=ledger.owner_id)
    ledger.remember("Synthetic marmalade invoice is paid", source="user_said")
    result = ledger.recall("marmalade")
    assert result["results"], result
    assert any("marmalade" in str(row) for row in result["results"])
    assert "semantic enable" in " ".join(result["warnings"]), result


def test_config_only_backend_readiness_reports_actual_lane(home, monkeypatch):
    from ledger_memory import config
    from ledger_memory.api import Ledger
    from ledger_memory.semantic_readiness import readiness
    from ledger_memory.semantic_search import semantic_lane_blocked
    monkeypatch.delenv("LEDGER_EMBEDDING_BACKEND", raising=False)
    ledger = Ledger().ensure()
    config.set_config(ledger.db_path, key="embedding.backend", value="fake", actor_id=ledger.owner_id)
    ledger.remember("Synthetic readiness fixture", source="user_said")
    ledger.refresh(repair=True)
    assert semantic_lane_blocked(ledger.db_path, ledger.vectors_path) is None, "fixture lane must actually be open"
    report = readiness(ledger.db_path, ledger.vectors_path)
    assert report.lane_open, report.reason
    assert report.binding_ok
    assert report.reason is None
    assert report.pending_rows == 0
    assert report.failed_rows == 0
    assert report.state == "ready"


def test_readiness_does_not_trust_core_metadata_without_physical_receipts(home, monkeypatch):
    import sqlite3
    from ledger_memory.api import Ledger
    from ledger_memory.semantic_readiness import readiness
    from ledger_memory.semantic_search import semantic_lane_blocked
    monkeypatch.setenv("LEDGER_EMBEDDING_BACKEND", "fake")
    ledger = Ledger().ensure()
    ledger.remember("Synthetic physical readiness fixture", source="user_said")
    ledger.refresh(repair=True)
    before = readiness(ledger.db_path, ledger.vectors_path)
    assert before.lane_open, before.reason
    assert before.pending_rows == 0
    with sqlite3.connect(ledger.vectors_path) as connection:
        connection.execute("DELETE FROM semantic_vector_revisions")
    assert semantic_lane_blocked(ledger.db_path, ledger.vectors_path) is None
    after = readiness(ledger.db_path, ledger.vectors_path)
    assert after.core_fresh
    assert not after.lane_open
    assert after.pending_rows > 0
    assert after.failed_rows == 0
    assert after.reason == "semantic projection has unfinished revisions"
    assert after.state == "indexing"


def test_status_reports_pending_failed_and_readiness(home, monkeypatch):
    import sqlite3
    from ledger_memory.api import Ledger
    from ledger_memory.embeddings import DeterministicFakeBackend
    from ledger_memory.semantic_pending import pending_embeddings, record_embedding_failure
    from ledger_memory.semantic_readiness import readiness
    monkeypatch.setenv("LEDGER_EMBEDDING_BACKEND", "fake")
    ledger = Ledger().ensure()
    ledger.remember("Synthetic retry readiness fixture", source="user_said")
    ledger.refresh(repair=True)
    assert readiness(ledger.db_path, ledger.vectors_path).lane_open
    with sqlite3.connect(ledger.vectors_path) as connection:
        connection.execute("DELETE FROM semantic_vector_revisions")
    backend = DeterministicFakeBackend()
    items = pending_embeddings(ledger.db_path, ledger.vectors_path, backend=backend)
    assert items
    target = next(item for item in items if item.record_type == "fact")
    for _ in range(3):
        record_embedding_failure(ledger.db_path, ledger.vectors_path, target,
                                 backend=backend, error=ValueError("synthetic"))
    report = readiness(ledger.db_path, ledger.vectors_path)
    assert report.failed_rows == 1
    assert report.state == "degraded"
    assert report.pending_rows == len(items) - 1
    assert not report.lane_open
    with sqlite3.connect(ledger.db_path) as connection:
        connection.execute("UPDATE facts SET statement='Synthetic new retry revision' WHERE id=?",
                           (target.record_id,))
    revised = readiness(ledger.db_path, ledger.vectors_path)
    assert revised.failed_rows == 0
    assert revised.pending_rows == len(items)
    assert revised.state == "indexing"


def test_readiness_measurement_failure_is_unknown_not_zero(home, monkeypatch):
    from ledger_memory.api import Ledger
    from ledger_memory import semantic_pending
    from ledger_memory.semantic_readiness import readiness
    monkeypatch.setenv("LEDGER_EMBEDDING_BACKEND", "fake")
    ledger = Ledger().ensure()
    ledger.remember("Synthetic measurement readiness fixture", source="user_said")
    ledger.refresh(repair=True)
    assert readiness(ledger.db_path, ledger.vectors_path).lane_open
    def failed_measurement(*args, **kwargs):
        raise OSError("synthetic measurement failure")
    monkeypatch.setattr(semantic_pending, "pending_embeddings", failed_measurement)
    report = readiness(ledger.db_path, ledger.vectors_path)
    assert not report.lane_open
    assert report.pending_rows is None
    assert report.failed_rows is None
    assert report.reason is not None


def test_disabled_readiness_does_not_resolve_model_or_create_indexes(home, tmp_path, monkeypatch):
    from ledger_memory import embeddings
    from ledger_memory.api import Ledger
    from ledger_memory.semantic_readiness import readiness
    monkeypatch.setenv("LEDGER_EMBEDDING_BACKEND", "none")
    ledger = Ledger().ensure()
    def forbidden(*args, **kwargs):
        raise AssertionError("disabled status touched model factory")
    monkeypatch.setattr(embeddings, "get_embedding_backend", forbidden)
    absent = tmp_path / "absent-index.db"
    report = readiness(ledger.db_path, absent)
    assert report.state == "disabled"
    assert report.reason is None
    assert report.pending_rows == report.failed_rows == 0
    assert report.model_fingerprint is None
    assert not report.worker_running
    assert not report.lane_open
    assert not absent.exists()


@pytest.mark.parametrize("available", [False, True])
@pytest.mark.parametrize("downloading", [False, True])
@pytest.mark.parametrize("lifecycle", ["warming", "ready", "unavailable"])
def test_bge_readiness_requires_explicit_passive_worker_probe(tmp_path, monkeypatch, available, downloading, lifecycle):
    from types import SimpleNamespace
    from ledger_memory import (embeddings, embedding_worker as worker, semantic_index, model_artifacts,
                               semantic_binding, semantic_search, semantic_pending)
    from ledger_memory.semantic_readiness import readiness
    monkeypatch.setenv("LEDGER_EMBEDDING_BACKEND", "bge-m3")
    monkeypatch.setattr(embeddings, "get_embedding_backend", lambda path:
                        SimpleNamespace(model_name="bge-m3:onnx:synthetic-fingerprint", dimensions=1024))
    monkeypatch.setattr(semantic_index, "get_semantic_index_status", lambda path: SimpleNamespace(fresh=True))
    monkeypatch.setattr(semantic_binding, "binding_violation", lambda *args, **kwargs: None)
    monkeypatch.setattr(semantic_search, "semantic_lane_blocked", lambda *args: None)
    monkeypatch.setattr(semantic_pending, "pending_embeddings", lambda *args, **kwargs: [])
    profile = worker.WorkerProfile(tmp_path / "absent-profile", tmp_path / "unused-key",
                                   "synthetic-address", "AF_UNIX")
    monkeypatch.setattr(worker, "worker_profile", lambda: profile)
    monkeypatch.setattr(worker, "read_worker_key", lambda value: b"a" * 32)
    observations = []
    def download_status(cache):
        observations.append(cache)
        return downloading
    monkeypatch.setattr(model_artifacts, "download_in_progress", download_status)
    lifecycle_calls = []
    def observe_lifecycle(address, **kwargs):
        lifecycle_calls.append(kwargs)
        assert kwargs["model_fingerprint"] == "synthetic-fingerprint"
        return lifecycle
    monkeypatch.setattr(worker, "worker_lifecycle", observe_lifecycle)
    def forbidden(*args, **kwargs):
        raise AssertionError("status entered model startup")
    monkeypatch.setattr(worker, "ensure_worker", forbidden)
    monkeypatch.setattr(worker, "prepare_worker_profile", forbidden)
    calls = []
    def probe(address, **kwargs):
        calls.append((address, kwargs))
        assert kwargs["probe_only"] is True
        assert kwargs["texts"] == []
        assert kwargs["model_fingerprint"] == "synthetic-fingerprint"
        assert kwargs["timeout"] == 2
        if not available:
            raise worker.WorkerProtocolError("synthetic unavailable")
        return []
    monkeypatch.setattr(worker, "worker_request", probe)
    report = readiness(tmp_path / "synthetic-core", tmp_path / "synthetic-sidecar")
    assert len(calls) == 1
    assert report.worker_running is (available or (not downloading and lifecycle in ("warming", "ready")))
    assert report.lane_open is available
    assert report.state == ("ready" if available else "downloading" if downloading else
                            "warming" if lifecycle == "warming" else "degraded")
    assert len(observations) == (0 if available else 1)
    assert len(lifecycle_calls) == (1 if not available and not downloading else 0)
    assert report.model_fingerprint == "synthetic-fingerprint"
    assert not profile.root.exists()


def test_readiness_config_failure_is_degraded_not_disabled(tmp_path, monkeypatch):
    from ledger_memory import config, embeddings
    from ledger_memory.semantic_readiness import readiness
    monkeypatch.delenv("LEDGER_EMBEDDING_BACKEND", raising=False)
    def unreadable(*args, **kwargs):
        raise OSError("synthetic unreadable configuration")
    monkeypatch.setattr(config, "get_config", unreadable)
    database = tmp_path / "synthetic-core.db"
    assert embeddings.resolve_embedding_backend_name(database) == "none"
    with pytest.raises(OSError):
        embeddings.resolve_embedding_backend_name(database, strict=True)
    report = readiness(database, tmp_path / "synthetic-sidecar.db")
    assert report.state == "degraded"
    assert report.pending_rows is None
    assert report.failed_rows is None
    assert report.reason is not None
    assert not report.lane_open
    assert not database.exists()


def test_readiness_failure_reason_never_contains_exception_payload(tmp_path, monkeypatch):
    from dataclasses import asdict
    from ledger_memory import config
    from ledger_memory.semantic_readiness import readiness
    import json
    monkeypatch.delenv("LEDGER_EMBEDDING_BACKEND", raising=False)
    marker = "SYNTHETIC_PRIVATE_PAYLOAD_NOT_A_REAL_SECRET"
    error_class = type(marker, (OSError,), {})
    def unreadable(*args, **kwargs):
        raise error_class(marker)
    monkeypatch.setattr(config, "get_config", unreadable)
    report = readiness(tmp_path / "absent-core", tmp_path / "absent-index")
    assert report.state == "degraded"
    assert report.reason == "readiness measurement unavailable"
    assert marker not in json.dumps(asdict(report))
    assert report.pending_rows is None
    assert report.failed_rows is None
    assert not report.lane_open


def test_legacy_readiness_retains_fixed_upgrade_guidance_without_model_access(tmp_path, monkeypatch):
    from ledger_memory import embeddings
    from ledger_memory.semantic_readiness import readiness
    monkeypatch.setenv("LEDGER_EMBEDDING_BACKEND", "lmstudio")
    def forbidden(*args, **kwargs):
        raise AssertionError("legacy status touched model factory")
    monkeypatch.setattr(embeddings, "get_embedding_backend", forbidden)
    report = readiness(tmp_path / "absent-core", tmp_path / "absent-index")
    assert report.state == "degraded"
    assert report.reason == "legacy embedding backend unavailable; run ledger semantic enable"
    assert report.pending_rows is None
    assert report.failed_rows is None
    assert not report.worker_running
    assert not report.lane_open


def test_readiness_gate_diagnostics_do_not_echo_arbitrary_payload(home, monkeypatch):
    import json
    from dataclasses import asdict
    from ledger_memory.api import Ledger
    from ledger_memory import semantic_search
    from ledger_memory.semantic_readiness import readiness
    monkeypatch.setenv("LEDGER_EMBEDDING_BACKEND", "fake")
    ledger = Ledger().ensure()
    ledger.remember("Synthetic gate diagnostic fixture", source="user_said")
    ledger.refresh(repair=True)
    assert readiness(ledger.db_path, ledger.vectors_path).lane_open
    marker = "SYNTHETIC_PRIVATE_GATE_DETAIL"
    monkeypatch.setattr(semantic_search, "semantic_lane_blocked", lambda *args: marker)
    report = readiness(ledger.db_path, ledger.vectors_path)
    assert not report.lane_open
    assert report.reason == "semantic index gate unavailable"
    assert marker not in json.dumps(asdict(report))
    assert report.pending_rows == report.failed_rows == 0


def test_service_outage_is_never_charged_to_records(home, tmp_path):
    """Review fix: an unreachable service must not burn every record's three attempts in one outage."""
    import sqlite3
    from ledger_memory.embedding_worker import EmbeddingServiceUnavailable
    from ledger_memory.embeddings import DeterministicFakeBackend
    from ledger_memory.semantic_pending import drain_pending, pending_embeddings, run_pending_worker
    ledger, backend, sidecar, item = _fake_store(tmp_path, "Synthetic outage fact")
    expected = pending_embeddings(ledger.db_path, sidecar, backend=backend)
    calls = []
    class Down(DeterministicFakeBackend):
        def embed(self, texts):
            calls.append(len(texts))
            raise EmbeddingServiceUnavailable("embedding service unavailable")
    with pytest.raises(EmbeddingServiceUnavailable):
        drain_pending(ledger.db_path, sidecar, backend=Down())
    assert calls == [1], "the pass must stop at the first unavailable call"
    result = run_pending_worker(ledger.db_path, sidecar, backend=Down())
    assert result["status"] == "unavailable" and result["failed"] == 0
    assert pending_embeddings(ledger.db_path, sidecar, backend=backend) == expected
    assert all(row.attempts == 0 for row in pending_embeddings(ledger.db_path, sidecar, backend=backend))
    if sidecar.exists():
        with sqlite3.connect(sidecar) as connection:
            tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if "embedding_failures" in tables:
                assert connection.execute("SELECT COUNT(*) FROM embedding_failures").fetchone()[0] == 0
    assert drain_pending(ledger.db_path, sidecar, backend=backend)["published"] == len(expected)


def test_reenable_resets_failures_for_its_model_only(home, tmp_path):
    from ledger_memory.semantic_pending import (
        pending_embeddings, record_embedding_failure, reset_embedding_failures)
    ledger, backend, sidecar, item = _fake_store(tmp_path, "Synthetic reset fact")
    for _ in range(3):
        record_embedding_failure(ledger.db_path, sidecar, item, backend=backend, error=ValueError("synthetic"))
    assert item not in pending_embeddings(ledger.db_path, sidecar, backend=backend)
    assert reset_embedding_failures(sidecar, "other-model") == 0
    assert reset_embedding_failures(sidecar, backend.model_name) == 1
    retried = next(row for row in pending_embeddings(ledger.db_path, sidecar, backend=backend)
                   if (row.record_type, row.record_id) == (item.record_type, item.record_id))
    assert retried.attempts == 0
    assert reset_embedding_failures(tmp_path / "absent.db", backend.model_name) == 0
