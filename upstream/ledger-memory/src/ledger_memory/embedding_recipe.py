"""the embedding recipe: a pinned identity for the embedding lane (RECALL-INTEGRITY
v3 §R1.1; review amendment 3).

Model identity used to be hard-coded: `adapters/lmstudio.py` pinned
`text-embedding-nomic-embed-text-v1.5` as a literal default and `dimensions` returned a literal
`768` regardless of which model_name was actually passed in — so swapping the model_name alone
silently produced dimension-mismatched vectors, and the integrity checker (which scopes
freshness to `backend.model_name`) had no pinned artifact hash, prefix contract, or runtime to
compare against. An `EmbeddingRecipe` makes every one of those facts an explicit, versioned,
per-database config value instead of an assumption baked into the adapter.

Blue/green model (C3): a NEW recipe always builds into a SEPARATE sidecar file
(`var/semantic-<recipe-short>.db`) — never overwrites the currently-active one. The ACTIVE
generation is whichever recipe + sidecar path the per-db config names right now
(`embedding.recipe` / `store.semantic_sidecar_path`, the latter pre-existing from
`about_me_recall`). `activate_embedding_generation` flips BOTH keys in ONE transaction — mirrors
the lineage-id mint's single-transaction mint pattern, not a
get-then-set — so rollback is one flip back to the prior (recipe, sidecar) pair, never a
partially-applied state where one key names the new generation and the other still names the old
one.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from pathlib import Path

from ledger_memory.config import get_config, set_config
from ledger_memory.db import connect
from ledger_memory.emission import emit_audit_and_event

RECIPE_CONFIG_KEY = "embedding.recipe"

# Pre-existing pointer — the ACTIVE
# sidecar for the whole semantic lane. Duplicated here as a plain string (not imported) so this
# module stays a leaf: about_me_recall imports far more than embedding_recipe needs to pull in.
SIDECAR_CONFIG_KEY = "semantic.sidecar_path"


@dataclass(frozen=True)
class EmbeddingRecipe:
    """A pinned, versioned identity for one embedding generation.

    `artifact` is the exact LM Studio model key (what `/v1/embeddings` is called with).
    `revision_hash` is the sha256 of the artifact file(s) on disk (from the LM Studio model dir) —
    the thing that actually changes if the model file is ever replaced under the same key.
    `query_prefix`/`document_prefix` are RECORDED even when empty (an omitted field could mean
    "not applicable" or "forgot to set it"; an explicit `""` means "checked, there is none").
    `pooling`/`normalization`/`max_tokens_policy` describe what the SERVING side does to text
    before/around the vector it returns; `runtime` is the LM Studio version string that served it.
    """

    artifact: str
    revision_hash: str
    query_prefix: str
    document_prefix: str
    pooling: str
    normalization: str
    dimensions: int
    max_tokens_policy: str
    runtime: str

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(data: dict) -> "EmbeddingRecipe":
        return EmbeddingRecipe(
            artifact=str(data["artifact"]),
            revision_hash=str(data["revision_hash"]),
            query_prefix=str(data["query_prefix"]),
            document_prefix=str(data["document_prefix"]),
            pooling=str(data["pooling"]),
            normalization=str(data["normalization"]),
            dimensions=int(data["dimensions"]),
            max_tokens_policy=str(data["max_tokens_policy"]),
            runtime=str(data["runtime"]),
        )

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True)

    @staticmethod
    def from_json(raw: str) -> "EmbeddingRecipe":
        return EmbeddingRecipe.from_dict(json.loads(raw))

    @property
    def short_id(self) -> str:
        """A short, stable identifier for sidecar naming (`var/semantic-<short_id>.db`).

        Derived from the identity-bearing fields (artifact + dimensions + revision_hash) so that
        changing ANY of them — a model swap, or the same model key pointing at a re-downloaded
        file with a different hash — yields a distinct sidecar filename. Two recipes that differ
        only in prefixes/pooling/runtime metadata but share artifact+dimensions+revision_hash are
        treated as the same generation for naming purposes; that combination does not occur for
        any recipe this slice defines.
        """
        basis = f"{self.artifact}:{self.dimensions}:{self.revision_hash}"
        return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:12]


# ---------------------------------------------------------------------------------------------
# The legacy fallback (C1): byte-compatible with today's hard-coding.
#
# Before this slice, `adapters/lmstudio.py` never recorded pooling/normalization/max-token
# policy/runtime at all — those facts were simply never captured. Recording them as invented
# specifics (e.g. "mean pooling") would be fabrication; instead they are recorded as explicit,
# honest "never captured" markers. `revision_hash` is likewise "" (never pinned before this
# slice) rather than a hash computed now against a file that may since have moved/changed —
# recording a hash here would silently claim retroactive provenance that was never established.
# ---------------------------------------------------------------------------------------------

LEGACY_NOMIC_RECIPE = EmbeddingRecipe(
    artifact="text-embedding-nomic-embed-text-v1.5",
    revision_hash="",
    query_prefix="",
    document_prefix="",
    pooling="server_default_unrecorded",
    normalization="server_default_unrecorded",
    dimensions=768,
    max_tokens_policy="unrecorded",
    runtime="unrecorded",
)


def resolve_embedding_recipe(database_path: Path) -> EmbeddingRecipe:
    """The active recipe for *database_path*: whatever `embedding.recipe` config holds, or the
    legacy NOMIC fallback when unset."""
    raw = get_config(database_path, RECIPE_CONFIG_KEY)
    if raw is None:
        return LEGACY_NOMIC_RECIPE
    return EmbeddingRecipe.from_json(raw)


def sidecar_path_for_recipe(recipe: EmbeddingRecipe) -> str:
    """The conventional (not enforced) sidecar path for a generation built from *recipe*."""
    return f"var/semantic-{recipe.short_id}.db"


def store_embedding_recipe(
    database_path: Path, recipe: EmbeddingRecipe, *, actor_id: int = 1, reason: str | None = None
) -> None:
    """Write ONLY the recipe key (audited `set_config`). Exists for building/inspecting a
    generation before it is activated; activation itself must use `activate_embedding_generation`
    so the recipe and sidecar pointer never move independently."""
    set_config(
        database_path,
        key=RECIPE_CONFIG_KEY,
        value=recipe.to_json(),
        actor_id=actor_id,
        reason=reason or "store embedding recipe",
    )


def activate_onnx_backend(database_path: Path, recipe: EmbeddingRecipe, *,
                          expected_backend: str | None, expected_recipe: str | None,
                          actor_id: int) -> None:
    """Publish opt-in policy only if provisioning did not race another policy change."""
    with connect(database_path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        for key, expected in (("embedding.backend", expected_backend), (RECIPE_CONFIG_KEY, expected_recipe)):
            row = connection.execute("SELECT value FROM config WHERE key=?", (key,)).fetchone()
            if (None if row is None else str(row["value"])) != expected:
                raise RuntimeError("semantic configuration changed during provisioning")
        for key, value in (("embedding.backend", "bge-m3"), (RECIPE_CONFIG_KEY, recipe.to_json())):
            connection.execute(
                "INSERT INTO config(key,value,actor_id,reason) VALUES(?,?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, actor_id=excluded.actor_id, "
                "reason=excluded.reason, updated_at=CURRENT_TIMESTAMP",
                (key, value, actor_id, "explicit local semantic enable"))
            emit_audit_and_event(connection, action_type="config.set", target_type="config",
                target_id=None, actor_id=actor_id, reason="explicit local semantic enable",
                event_type="config.set", summary=f"config {key} set")


def _write_config_audited(database_path: Path, key: str, value: str, actor_id: int, reason: str) -> None:
    """One audited UPSERT in its own transaction — the split-topology arm of activation."""
    set_config(database_path, key=key, value=value, actor_id=actor_id, reason=reason)


def activate_embedding_generation(
    database_path: Path,
    recipe: EmbeddingRecipe,
    sidecar_path: str,
    *,
    recipe_database_path: Path | None = None,
    actor_id: int = 1,
    reason: str | None = None,
) -> None:
    """Flip the ACTIVE generation: `embedding.recipe` + `store.semantic_sidecar_path` together.

    Review BLOCK (2026-08-07, empirically reproduced by the lens): in the two-database
    topology the two keys are READ from different files — the sidecar pointer off the LOCAL
    db (`resolve_brain_paths`), the recipe off the BRAIN db (`get_embedding_backend(store)`).
    Writing both to one file "atomically" tears the pair across databases. Contract now:
    `database_path` owns the SIDECAR POINTER; `recipe_database_path` (default: the same db —
    the self-referential single-db case keeps true one-transaction atomicity) owns the
    RECIPE. In the split case the pointer is written FIRST and restored on any failure
    writing the recipe — and the readiness probe reports any torn state loudly.

    Rollback is the same call with the PRIOR (recipe, sidecar_path) pair.
    """
    reason = reason or "activate embedding generation (recipe + sidecar pointer)"
    recipe_json = recipe.to_json()
    sidecar_str = str(sidecar_path)
    recipe_db = Path(recipe_database_path) if recipe_database_path is not None else Path(database_path)

    if recipe_db.resolve() != Path(database_path).resolve():
        prior_sidecar = get_config(database_path, SIDECAR_CONFIG_KEY, None)
        _write_config_audited(database_path, SIDECAR_CONFIG_KEY, sidecar_str, actor_id, reason)
        try:
            _write_config_audited(recipe_db, RECIPE_CONFIG_KEY, recipe_json, actor_id, reason)
        except BaseException:
            if prior_sidecar is not None:
                _write_config_audited(
                    database_path, SIDECAR_CONFIG_KEY, prior_sidecar, actor_id,
                    f"{reason} — ROLLED BACK (recipe write failed)",
                )
            raise
        return

    with connect(database_path) as connection:
        for key, value in ((RECIPE_CONFIG_KEY, recipe_json), (SIDECAR_CONFIG_KEY, sidecar_str)):
            connection.execute(
                """
                INSERT INTO config (key, value, actor_id, reason)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    value      = excluded.value,
                    actor_id   = excluded.actor_id,
                    reason     = excluded.reason,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (key, value, actor_id, reason),
            )
            emit_audit_and_event(
                connection,
                action_type="config.set",
                target_type="config",
                target_id=None,
                actor_id=actor_id,
                reason=reason,
                event_type="config.set",
                summary=f"config {key} set",
            )


__all__ = [
    "RECIPE_CONFIG_KEY",
    "SIDECAR_CONFIG_KEY",
    "EmbeddingRecipe",
    "LEGACY_NOMIC_RECIPE",
    "resolve_embedding_recipe",
    "sidecar_path_for_recipe",
    "store_embedding_recipe",
    "activate_embedding_generation",
]
