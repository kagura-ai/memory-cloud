"""MCP Tools integration test script.

Direct test of MemoryService methods that MCP tools wrap.

Note: This file is a standalone integration smoke-test script intended to be
run directly (`python tests/test_mcp_tools.py`) against a live environment.
It requires a real DB connection and a valid context_id, so it is skipped
during normal pytest runs.
"""

import asyncio
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skip(
    reason="Integration smoke-test script: requires live DB and context_id"
)

sys.path.insert(0, str(Path(__file__).parent / "src"))

from db.base import get_db
from models.schemas import ExploreRequest, RecallRequest, RememberRequest
from services.memory_service import MemoryService


async def test_remember():
    """Test remember (store memory)."""
    print("\n=== Test 1: remember ===")

    async for db in get_db():
        service = MemoryService(db)

        request = RememberRequest(
            summary="MCP integration test - Claude Code connection successful",
            content="This is a test memory created to verify Neural Memory and graph exploration features",
            type="test",
            importance=0.8,
            tags=["mcp", "integration-test", "phase3"],
            context={"test_type": "mcp_integration", "session": "phase3_verification"},
        )

        result = await service.remember(request, user_id="test_user", client="mcp-test")

        print(f"✅ Memory created: {result.memory_id}")
        print(f"   Scope: {result.scope}")

        await db.commit()

        return str(result.memory_id)


async def test_recall(memory_id: str):
    """Test recall (search with Neural Memory)."""
    print("\n=== Test 2: recall ===")

    async for db in get_db():
        service = MemoryService(db)

        request = RecallRequest(
            query="MCP integration test",
            k=5,
            use_rerank=False,
        )

        result = await service.recall(request, user_id="test_user")

        print(f"✅ Found {len(result.results)} memories")
        for i, memory in enumerate(result.results, 1):
            print(f"   {i}. {memory.summary[:50]}... (score={memory.score:.3f})")

        return result


async def test_explore(memory_id: str):
    """Test explore (graph traversal)."""
    print("\n=== Test 3: explore ===")

    async for db in get_db():
        service = MemoryService(db)

        request = ExploreRequest(
            memory_id=memory_id,
            depth=2,
            min_weight=0.1,
        )

        result = await service.explore(request, user_id="test_user")

        print(f"✅ Seed: {result.seed_memory.summary[:50]}...")
        print(f"   Related memories: {len(result.related_memories)}")

        for i, related in enumerate(result.related_memories, 1):
            print(
                f"   {i}. {related.summary[:40]}... (activation={related.activation:.3f}, hop={related.hop})"
            )

        print(f"   Metadata: {result.metadata}")

        return result


async def main():
    """Run all tests."""
    print("=" * 60)
    print("MCP Tools Integration Test")
    print("=" * 60)

    try:
        # Test 1: remember
        memory_id = await test_remember()

        # Test 2: recall
        await test_recall(memory_id)

        # Test 3: explore
        await test_explore(memory_id)

        print("\n" + "=" * 60)
        print("✅ All tests passed!")
        print("=" * 60)

    except Exception as e:
        print(f"\n❌ Test failed: {e}")
        import traceback

        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
