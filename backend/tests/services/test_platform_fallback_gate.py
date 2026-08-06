"""One definition of "may this plan use the platform key" (#1030 / #1495).

Two surfaces need that rule and they must agree:

- the embedding path, which DENIES the call when the answer is no;
- ``GET /workspaces/{id}/openai-key-status``, which the web UI asks before
  deciding whether to warn that embedding is unavailable.

They disagreed once already, in the other direction: the UI counted the
workspace's own key rows and never asked the server at all, so every workspace
served by the platform credential was shown a red "OpenAI API key required"
banner and a warning triangle while embedding 100% successfully (#1495, live in
production). Fixing that by re-implementing the plan gate in the endpoint would
have rebuilt the same defect one layer down — a second copy of a rule, free to
drift from the one actually enforced.

Hence a shared helper, and hence these tests: they pin the rule itself, and that
both callers route through it rather than carrying their own copy.
"""

from __future__ import annotations

import ast
import inspect
import textwrap

import pytest

from services import embedding_service
from services.embedding_service import platform_fallback_allowed


@pytest.fixture
def restriction(monkeypatch):
    """Toggle #1030 without touching the process-wide settings singleton."""

    def _set(enabled: bool):
        from config import settings as settings_mod

        real = settings_mod.get_settings()

        class Patched:
            def __getattr__(self, name):
                if name == "embedding_platform_fallback_requires_managed_plan":
                    return enabled
                return getattr(real, name)

        monkeypatch.setattr(settings_mod, "get_settings", lambda: Patched())

    return _set


class TestTheRule:
    @pytest.mark.parametrize("plan", ["basic", "pro"])
    def test_paid_plans_may_use_the_platform_key(self, plan, restriction):
        restriction(True)
        assert platform_fallback_allowed(plan) is True

    def test_free_may_not(self, restriction):
        """Free is "BYOK required or self-host Ollama" once #1030 is on."""
        restriction(True)
        assert platform_fallback_allowed("free") is False

    @pytest.mark.parametrize("plan", ["free", "basic", "pro"])
    def test_with_the_restriction_off_every_tier_may(self, plan, restriction):
        """The default, and it must stay the default.

        OSS / dev / self-host deployments set a key and expect it to serve
        everything; flipping this on is a managed-SaaS decision.
        """
        restriction(False)
        assert platform_fallback_allowed(plan) is True

    def test_an_unknown_plan_is_refused_when_restricted(self, restriction):
        """`has_feature` answers False for a name it does not know.

        Documented here because it is load-bearing and unobvious: a plan_name
        that does not round-trip (casing, a display name like "M", a tier added
        to the DB but not to PLAN_TIERS) reads as Free and loses embedding
        SILENTLY. Pinning it makes the behaviour a decision rather than an
        accident, and marks the spot if it should later become an error.
        """
        restriction(True)
        assert platform_fallback_allowed("M") is False
        assert platform_fallback_allowed("nonexistent-tier") is False


class TestBothCallersShareIt:
    """Neither surface may carry its own copy of the condition."""

    @staticmethod
    def _calls(fn) -> set[str]:
        tree = ast.parse(textwrap.dedent(inspect.getsource(fn)))
        out = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                f = node.func
                if isinstance(f, ast.Name):
                    out.add(f.id)
                elif isinstance(f, ast.Attribute):
                    out.add(f.attr)
        return out

    def test_the_embedding_gate_routes_through_the_helper(self):
        assert "platform_fallback_allowed" in self._calls(
            embedding_service.EmbeddingService._prepare_spend_cap_gate
        )

    def test_the_key_status_endpoint_routes_through_the_helper(self):
        """Measured as a CALL, not a substring.

        The first version searched the source text and passed against code that
        had dropped the call but kept the now-unused import — the exact
        regression it was written to catch. Found by mutation.
        """
        from api.routes import workspaces

        assert "platform_fallback_allowed" in self._calls(workspaces.check_openai_key_status), (
            "the key-status endpoint no longer CALLS the shared plan gate; it "
            "will report embedding_available=true for a Free workspace the "
            "embedding path refuses (#1495)"
        )

    def test_the_endpoint_does_not_reimplement_the_condition(self):
        """Calling the helper AND keeping a private copy is the drift case.

        Measured on the AST, not the text: the first version of this assertion
        searched the source for "managed_embeddings" and failed against CORRECT
        code, because the comment explaining the rule names it. An assertion a
        prose edit can break is not measuring the code.
        """
        from api.routes import workspaces

        tree = ast.parse(textwrap.dedent(inspect.getsource(workspaces.check_openai_key_status)))
        called = {
            n.func.id
            for n in ast.walk(tree)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
        }
        assert "has_feature" not in called, (
            "the endpoint calls has_feature directly instead of deferring to "
            "platform_fallback_allowed — a second copy of the plan gate, free "
            "to drift from the one the embedding path enforces"
        )
        attrs = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
        assert "embedding_platform_fallback_requires_managed_plan" not in attrs, (
            "the endpoint reads the #1030 setting itself; that decision belongs "
            "to platform_fallback_allowed"
        )


class TestAvailabilityNeedsBothHalves:
    """`embedding_available` is "allowed to" AND "actually configured"."""

    @staticmethod
    def _src() -> str:
        from api.routes import workspaces

        return inspect.getsource(workspaces.check_openai_key_status)

    def test_a_permitted_plan_still_needs_a_key_to_exist(self):
        """Pro is allowed the platform fallback — but on a deployment that never
        set OPENAI_API_KEY there is nothing to fall back to, and claiming
        availability would be the original bug with the sign flipped."""
        src = self._src()
        assert 'os.getenv("OPENAI_API_KEY")' in src

    def test_a_workspace_key_alone_is_enough(self):
        """BYOK does not consult the plan gate at all — the workspace is paying
        for its own embeddings, on any tier."""
        src = self._src()
        assert "openai_key is not None or" in src
