"""Eval context stamps carry the deployment's self-hosted rerank model (#2020).

A hard-coded ``reranker_model`` (it was ``qwen3-reranker-4b``) is sent as-is on
the /v1/rerank path, so an endpoint serving another model fails open silently.
``None`` is no escape either: the ORM column default (``rerank-2``) fills it.
The stamps resolve the model the way the production default writers do,
through ``default_reranker_model_for("self_hosted", settings)``.
"""

from __future__ import annotations

import ast
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from models.config import ContextSearchConfig
from tests.eval import _provisioning

_EVAL_ROOT = Path(__file__).resolve().parent


def _settings(**over):
    s = MagicMock()
    s.embedding_model = "text-embedding-3-small"
    s.embedding_dimensions = 512
    s.rerank_base_url = "http://gpu:8002"
    s.rerank_model = "qwen3-reranker-0.6b"
    s.self_hosted_rerank_model = "dengcao/Qwen3-Reranker-8B:Q5_K_M"
    for k, v in over.items():
        setattr(s, k, v)
    return s


async def _stamped_model(monkeypatch, settings) -> str | None:
    monkeypatch.setattr(_provisioning, "get_settings", lambda: settings)
    db = MagicMock()
    db.flush = AsyncMock()
    db.commit = AsyncMock()
    await _provisioning.provision_eval_context(db)
    configs = [
        c.args[0] for c in db.add.call_args_list if isinstance(c.args[0], ContextSearchConfig)
    ]
    assert len(configs) == 1
    return configs[0].reranker_model


async def test_stamp_uses_rerank_model_on_the_v1_rerank_path(monkeypatch):
    model = await _stamped_model(monkeypatch, _settings(rerank_model="served-0.6b"))
    assert model == "served-0.6b"


async def test_stamp_uses_self_hosted_rerank_model_without_rerank_base_url(monkeypatch):
    model = await _stamped_model(
        monkeypatch, _settings(rerank_base_url="", self_hosted_rerank_model="my-ollama-reranker")
    )
    assert model == "my-ollama-reranker"


def test_update_runner_stamps_resolve_the_deployment_model():
    """update_runner provisions inline (inside the live run), so pin it by AST:
    both ContextSearchConfig stamps call default_reranker_model_for."""
    tree = ast.parse((_EVAL_ROOT / "update_runner.py").read_text())
    values = [
        kw.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "ContextSearchConfig"
        for kw in node.keywords
        if kw.arg == "reranker_model"
    ]
    assert len(values) == 2
    for v in values:
        assert isinstance(v, ast.Call), ast.dump(v)
        assert isinstance(v.func, ast.Name) and v.func.id == "default_reranker_model_for"
        assert isinstance(v.args[0], ast.Constant) and v.args[0].value == "self_hosted"
