"""#2986: the LanceDB full-text column follows a point's rewritten token fields.

The keyword rebuild writes a summary point's token fields through the store's
payload update; the store rebuilds the point's ``search_text`` from them. A
point indexed with the dictionary-form token of 引越し is not found by a query
for 引越し (now the normalized token 引っ越し) until its tokens are rewritten.
Runs against an on-disk store under ``tmp_path``; skipped without the ``lite``
extra.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

pytest.importorskip("lancedb")

from db.lance_store import DEFAULT_COLLECTION, LanceVectorStore  # noqa: E402
from utils.tokenizer import tokenize_for_search  # noqa: E402

WS = "11111111-1111-4111-8111-111111111111"
CTX = "22222222-2222-4222-8222-222222222222"
USER = "google-oauth2|456"


async def test_rewritten_token_fields_rebuild_the_full_text_column(tmp_path):
    store = LanceVectorStore(str(tmp_path / "lance"))
    memory_id = uuid4()
    await store.ensure_collection(4, DEFAULT_COLLECTION)
    await store.add_memory(
        user_id=USER,
        memory_id=memory_id,
        vector=[0.1, 0.2, 0.3, 0.4],
        # As written before #2986: the dictionary form of 引越し.
        payload={
            "summary": "引越しの手続き",
            "summary_tokens": "引越し 手続き",
            "type": "note",
            "scope": "working",
        },
        workspace_id=WS,
        context_id=CTX,
    )

    async def hits() -> list[str]:
        results = await store.search_fulltext(USER, "引越し", WS, CTX, limit=5)
        return [str(r["id"]) for r in results]

    assert str(memory_id) not in await hits()

    await store.update_payload(
        memory_id, {"summary_tokens": tokenize_for_search("引越しの手続き")}, DEFAULT_COLLECTION
    )

    assert str(memory_id) in await hits()


async def test_retrieve_payloads_reads_stored_payloads_and_skips_absent_rows(tmp_path):
    """The keyword rebuild compares the stored token fields before a rewrite
    (``db.qdrant.retrieve_keyword_points`` reads them here on LanceDB)."""
    store = LanceVectorStore(str(tmp_path / "lance"))
    memory_id = uuid4()
    await store.ensure_collection(4, DEFAULT_COLLECTION)
    await store.add_memory(
        user_id=USER,
        memory_id=memory_id,
        vector=[0.1, 0.2, 0.3, 0.4],
        payload={"summary": "手続き", "summary_tokens": "手続き", "type": "note"},
        workspace_id=WS,
        context_id=CTX,
    )

    found = await store.retrieve_payloads(DEFAULT_COLLECTION, [str(memory_id), str(uuid4())])

    assert list(found) == [str(memory_id)]
    assert found[str(memory_id)]["summary_tokens"] == "手続き"
    assert found[str(memory_id)]["context_id"] == CTX
    assert await store.retrieve_payloads(DEFAULT_COLLECTION, []) == {}


async def test_retrieve_keyword_points_reads_the_lance_store(monkeypatch, tmp_path):
    from db import qdrant

    store = LanceVectorStore(str(tmp_path / "lance"))
    memory_id = uuid4()
    await store.ensure_collection(4, DEFAULT_COLLECTION)
    await store.add_memory(
        user_id=USER,
        memory_id=memory_id,
        vector=[0.1, 0.2, 0.3, 0.4],
        payload={"summary": "手続き", "summary_tokens": "手続き"},
        workspace_id=WS,
        context_id=CTX,
    )
    monkeypatch.setattr(qdrant, "_active_store", lambda: store)

    points = await qdrant.retrieve_keyword_points([memory_id], DEFAULT_COLLECTION)

    assert points[str(memory_id)].payload["summary_tokens"] == "手続き"
    assert points[str(memory_id)].vector is None
