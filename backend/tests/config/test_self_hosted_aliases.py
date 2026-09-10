"""SELF_HOSTED_MODEL_ALIASES parsing (#1525)."""

from config.self_hosted_aliases import (
    parse_self_hosted_model_aliases,
    resolve_self_hosted_model_id,
)


class TestParse:
    def test_empty_means_no_aliases(self):
        assert parse_self_hosted_model_aliases(None) == {}
        assert parse_self_hosted_model_aliases("") == {}
        assert parse_self_hosted_model_aliases("  ") == {}

    def test_pairs_are_split_on_the_first_equals_only(self):
        # An upstream id may itself contain '=' (query-ish ids); only the
        # first '=' separates the registry name from the upstream id.
        assert parse_self_hosted_model_aliases("a=b=c") == {"a": "b=c"}

    def test_whitespace_is_tolerated(self):
        assert parse_self_hosted_model_aliases(
            " qwen3-embedding:4b = Qwen/Qwen3-Embedding-4B , qwen3-embedding:8b=x "
        ) == {"qwen3-embedding:4b": "Qwen/Qwen3-Embedding-4B", "qwen3-embedding:8b": "x"}

    def test_malformed_entries_are_dropped_not_guessed(self):
        # A typo must not silently map to *something*; the request then goes
        # out with the registry name and the backend rejects it visibly.
        assert parse_self_hosted_model_aliases("no-equals,=x,y=,ok=fine") == {"ok": "fine"}


class TestResolve:
    def test_unaliased_models_pass_through(self):
        assert resolve_self_hosted_model_id("qwen3-embedding:4b", "") == "qwen3-embedding:4b"
        assert resolve_self_hosted_model_id("qwen3-embedding:4b", "other=x") == "qwen3-embedding:4b"

    def test_aliased_model_is_rewritten(self):
        assert (
            resolve_self_hosted_model_id(
                "qwen3-embedding:4b", "qwen3-embedding:4b=Qwen/Qwen3-Embedding-4B"
            )
            == "Qwen/Qwen3-Embedding-4B"
        )
