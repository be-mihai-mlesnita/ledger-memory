"""Review fix (0.3.0-rc2): a revision records where the CORRECTED statement comes from."""
import pytest
import sqlite3
from ledger_memory.api import Ledger, LedgerError


def test_agent_revision_of_user_said_fact_is_inferred(home):
    L = Ledger().ensure()
    # Owner creates a fact
    created = L.remember("The user said X", source="user_said", as_owner=True)
    fact_id = created["id"]
    # Agent revises without source
    revised = L.revise(fact_id, "Actually Y", reason="correction")
    assert revised["now"]["source"] == "inferred"
    # Check DB directly
    conn = sqlite3.connect(L.db_path)
    row = conn.execute("SELECT authority_class, asserted_by_actor_id FROM facts WHERE id = ?", (revised["now"]["id"],)).fetchone()
    conn.close()
    assert row[0] == "model_inferred"
    assert row[1] is None


def test_revision_with_user_said_source_is_attributed_to_owner(home):
    L = Ledger().ensure()
    created = L.remember("Original", source="user_said", as_owner=True)
    fact_id = created["id"]
    # Agent revises with source="user_said"
    revised = L.revise(fact_id, "Corrected by user", reason="user corrected", source="user_said")
    assert revised["now"]["source"] == "user_said"
    conn = sqlite3.connect(L.db_path)
    row = conn.execute("SELECT authority_class, asserted_by_actor_id FROM facts WHERE id = ?", (revised["now"]["id"],)).fetchone()
    conn.close()
    assert row[0] == "actor_asserted"
    assert row[1] == L.owner_id


def test_document_revision_needs_a_document_and_links_it(home):
    L = Ledger().ensure()
    # Ingest a document and create a document-backed fact
    doc1 = L.ingest(text="Document one content")
    doc1_id = doc1["document_id"]
    created = L.remember("From doc", source="document", document_id=doc1_id, as_owner=True)
    fact_id = created["id"]

    # Agent revises without source -> inferred, no evidence
    revised = L.revise(fact_id, "Agent correction", reason="agent fix")
    assert revised["now"]["source"] == "inferred"
    conn = sqlite3.connect(L.db_path)
    ev_count = conn.execute("SELECT COUNT(*) FROM fact_evidence WHERE fact_id = ?", (revised["now"]["id"],)).fetchone()[0]
    conn.close()
    assert ev_count == 0

    # A second document-backed fact for the explicit-source cases (fact_id is already revised).
    fact_id = L.remember("From doc, second fact", source="document", document_id=doc1_id, as_owner=True)["id"]
    # Agent revises with source="document" but no document_id -> error
    with pytest.raises(LedgerError, match="source='document' needs the document_id"):
        L.revise(fact_id, "Another try", reason="try", source="document")
    with pytest.raises(LedgerError, match="document_id is only used with source='document'"):
        L.revise(fact_id, "Another try", reason="try", source="inferred", document_id=doc1_id)

    # Ingest a second document
    doc2 = L.ingest(text="Document two content")
    doc2_id = doc2["document_id"]
    # Agent revises with source="document" and document_id
    revised2 = L.revise(fact_id, "Document correction", reason="doc fix", source="document", document_id=doc2_id)
    assert revised2["now"]["source"] == "document"
    conn = sqlite3.connect(L.db_path)
    ev_rows = conn.execute("SELECT evidence_id FROM fact_evidence WHERE fact_id = ?", (revised2["now"]["id"],)).fetchall()
    conn.close()
    assert len(ev_rows) == 1
    assert ev_rows[0][0] == doc2_id


def test_owner_revision_inherits(home):
    L = Ledger().ensure()
    created = L.remember("Owner fact", source="user_said", as_owner=True)
    fact_id = created["id"]
    # Owner revises without source (as_owner=True)
    revised = L.revise(fact_id, "Owner correction", reason="owner fix", as_owner=True)
    assert revised["now"]["source"] == "user_said"
    conn = sqlite3.connect(L.db_path)
    row = conn.execute("SELECT authority_class, asserted_by_actor_id FROM facts WHERE id = ?", (revised["now"]["id"],)).fetchone()
    conn.close()
    assert row[0] == "actor_asserted"
    assert row[1] == L.owner_id


def test_history_keeps_original_attribution(home):
    L = Ledger().ensure()
    created = L.remember("Original user fact", source="user_said", as_owner=True)
    fact_id = created["id"]
    # Agent revises
    L.revise(fact_id, "Agent revision", reason="agent fix")
    # Check original fact unchanged
    conn = sqlite3.connect(L.db_path)
    row = conn.execute("SELECT authority_class, asserted_by_actor_id, superseded_by_fact_id FROM facts WHERE id = ?", (fact_id,)).fetchone()
    conn.close()
    assert row[0] == "actor_asserted"
    assert row[1] == L.owner_id
    assert row[2] is not None  # superseded_by set


def test_revising_twice_gives_a_clear_conflict(home):
    L = Ledger().ensure()
    created = L.remember("Fact to revise", source="user_said", as_owner=True)
    fact_id = created["id"]
    # First revision
    L.revise(fact_id, "First revision", reason="first")
    # Second revision on same original fact_id should fail
    with pytest.raises(LedgerError, match="already revised"):
        L.revise(fact_id, "Second revision", reason="second")


def test_mcp_revise_accepts_optional_source():
    import inspect
    from ledger_memory import mcp_server
    tool = getattr(mcp_server.revise, "fn", mcp_server.revise)
    params = inspect.signature(tool).parameters
    assert params["source"].default is None and params["document_id"].default is None
    assert "user_said" in (tool.__doc__ or "")


def test_agent_correction_of_an_observation_is_inferred(home):
    L = Ledger().ensure()
    f = L.remember("Synthetic disk is 91% full", source="observed")["id"]
    assert L.revise(f, "Synthetic disk is 40% full", reason="agent guess")["now"]["source"] == "inferred"


def test_owner_correction_of_a_document_fact_is_the_owners_statement(home):
    L = Ledger().ensure()
    doc = L.ingest(text="Synthetic manual: the port is 8080.")["document_id"]
    f = L.remember("Synthetic port is 8080", source="document", document_id=doc, as_owner=True)["id"]
    out = L.revise(f, "Synthetic port is 9090", reason="owner knows", as_owner=True)
    assert out["now"]["source"] == "user_said"
    conn = sqlite3.connect(L.db_path)
    row = conn.execute("SELECT authority_class, asserted_by_actor_id FROM facts WHERE id = ?", (out["now"]["id"],)).fetchone()
    evidence = conn.execute("SELECT COUNT(*) FROM fact_evidence WHERE fact_id = ?", (out["now"]["id"],)).fetchone()[0]
    conn.close()
    assert row == ("actor_asserted", L.owner_id) and evidence == 0
