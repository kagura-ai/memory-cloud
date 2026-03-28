"""Tests for Session Management (Issue #114).

Tests:
- Session creation and retrieval
- Session deletion (single)
- Session deletion by user (invalidate all user sessions)
- Session expiration handling
- SCAN-based iteration (non-blocking)
- Pipeline-based atomic deletion
"""

import json
from unittest.mock import MagicMock, patch

import pytest

from auth.session import SessionManager


class TestSessionManager:
    """Test SessionManager class."""

    @pytest.fixture
    def mock_redis(self):
        """Create mock Redis client."""
        return MagicMock()

    @pytest.fixture
    def session_manager(self, mock_redis):
        """Create SessionManager with mocked Redis."""
        with patch.object(SessionManager, "_get_or_create_redis_client", return_value=mock_redis):
            manager = SessionManager(redis_url="redis://localhost:6379", session_ttl=3600)
            return manager

    def test_create_session(self, session_manager, mock_redis):
        """Test session creation."""
        user_info = {
            "sub": "user_123",
            "email": "test@example.com",
            "name": "Test User",
        }

        session_id = session_manager.create_session(user_info)

        # Session ID should be generated
        assert session_id is not None
        assert len(session_id) > 20  # URL-safe base64 of 32 bytes

        # Redis setex should be called
        mock_redis.setex.assert_called_once()
        call_args = mock_redis.setex.call_args
        assert call_args[0][0].startswith("session:")
        assert call_args[0][1] == 3600  # TTL

    def test_get_session_exists(self, session_manager, mock_redis):
        """Test getting an existing session."""
        session_data = {
            "sub": "user_123",
            "email": "test@example.com",
            "created_at": "2025-01-01T00:00:00",
            "last_accessed": "2025-01-01T00:00:00",
        }
        mock_redis.get.return_value = json.dumps(session_data)

        result = session_manager.get_session("test_session_id")

        assert result is not None
        assert result["sub"] == "user_123"
        assert result["email"] == "test@example.com"
        mock_redis.get.assert_called_with("session:test_session_id")

    def test_get_session_not_exists(self, session_manager, mock_redis):
        """Test getting a non-existent session."""
        mock_redis.get.return_value = None

        result = session_manager.get_session("nonexistent_session")

        assert result is None

    def test_delete_session(self, session_manager, mock_redis):
        """Test deleting a session."""
        mock_redis.delete.return_value = 1

        result = session_manager.delete_session("test_session_id")

        assert result is True
        mock_redis.delete.assert_called_with("session:test_session_id")

    def test_delete_session_not_exists(self, session_manager, mock_redis):
        """Test deleting a non-existent session."""
        mock_redis.delete.return_value = 0

        result = session_manager.delete_session("nonexistent_session")

        assert result is False


class TestDeleteUserSessions:
    """Test delete_user_sessions method (Issue #114).

    Tests verify:
    - SCAN-based iteration (non-blocking, O(1) per call)
    - Pipeline-based atomic deletion
    - Proper user session filtering
    """

    @pytest.fixture
    def mock_redis(self):
        """Create mock Redis client."""
        redis = MagicMock()
        # Setup default pipeline mock
        mock_pipe = MagicMock()
        mock_pipe.execute.return_value = []
        redis.pipeline.return_value = mock_pipe
        return redis

    @pytest.fixture
    def session_manager(self, mock_redis):
        """Create SessionManager with mocked Redis."""
        with patch.object(SessionManager, "_get_or_create_redis_client", return_value=mock_redis):
            manager = SessionManager(redis_url="redis://localhost:6379", session_ttl=3600)
            return manager

    def test_delete_user_sessions_single_session(self, session_manager, mock_redis):
        """Test deleting single session for a user using SCAN + pipeline."""
        user_id = "user_123"
        session_data = json.dumps(
            {
                "sub": user_id,
                "user_id": user_id,
                "email": "test@example.com",
            }
        )

        # Mock SCAN to return one session, then cursor 0 to stop
        mock_redis.scan.return_value = (0, ["session:abc123"])
        mock_redis.get.return_value = session_data

        # Mock pipeline
        mock_pipe = MagicMock()
        mock_pipe.execute.return_value = [1]  # 1 deletion succeeded
        mock_redis.pipeline.return_value = mock_pipe

        deleted_count = session_manager.delete_user_sessions(user_id)

        assert deleted_count == 1
        mock_redis.scan.assert_called_once_with(0, match="session:*", count=100)
        mock_pipe.delete.assert_called_once_with("session:abc123")
        mock_pipe.execute.assert_called_once()

    def test_delete_user_sessions_multiple_sessions(self, session_manager, mock_redis):
        """Test deleting multiple sessions for a user using pipeline."""
        user_id = "user_123"
        session_data = json.dumps(
            {
                "sub": user_id,
                "user_id": user_id,
                "email": "test@example.com",
            }
        )

        # Mock SCAN to return multiple sessions
        mock_redis.scan.return_value = (0, ["session:abc123", "session:def456", "session:ghi789"])
        mock_redis.get.return_value = session_data

        # Mock pipeline with 3 successful deletions
        mock_pipe = MagicMock()
        mock_pipe.execute.return_value = [1, 1, 1]
        mock_redis.pipeline.return_value = mock_pipe

        deleted_count = session_manager.delete_user_sessions(user_id)

        assert deleted_count == 3
        assert mock_pipe.delete.call_count == 3

    def test_delete_user_sessions_no_sessions(self, session_manager, mock_redis):
        """Test when user has no sessions."""
        # Mock SCAN to return empty
        mock_redis.scan.return_value = (0, [])

        deleted_count = session_manager.delete_user_sessions("user_123")

        assert deleted_count == 0
        mock_redis.pipeline.assert_not_called()

    def test_delete_user_sessions_mixed_users(self, session_manager, mock_redis):
        """Test that only the specified user's sessions are deleted."""
        target_user = "user_123"
        other_user = "user_456"

        # Sessions belonging to different users
        session1 = json.dumps(
            {"sub": target_user, "user_id": target_user, "email": "user1@example.com"}
        )
        session2 = json.dumps(
            {"sub": other_user, "user_id": other_user, "email": "user2@example.com"}
        )
        session3 = json.dumps(
            {"sub": target_user, "user_id": target_user, "email": "user1@example.com"}
        )

        mock_redis.scan.return_value = (0, ["session:s1", "session:s2", "session:s3"])
        mock_redis.get.side_effect = [session1, session2, session3]

        # Mock pipeline with 2 successful deletions
        mock_pipe = MagicMock()
        mock_pipe.execute.return_value = [1, 1]
        mock_redis.pipeline.return_value = mock_pipe

        deleted_count = session_manager.delete_user_sessions(target_user)

        # Only 2 sessions should be deleted (session1 and session3)
        assert deleted_count == 2
        assert mock_pipe.delete.call_count == 2

    def test_delete_user_sessions_with_sub_only(self, session_manager, mock_redis):
        """Test deletion works with sessions that only have 'sub' key (OAuth2 standard)."""
        user_id = "user_123"
        # Session with only 'sub' (no 'user_id')
        session_data = json.dumps(
            {
                "sub": user_id,
                "email": "test@example.com",
            }
        )

        mock_redis.scan.return_value = (0, ["session:abc123"])
        mock_redis.get.return_value = session_data

        mock_pipe = MagicMock()
        mock_pipe.execute.return_value = [1]
        mock_redis.pipeline.return_value = mock_pipe

        deleted_count = session_manager.delete_user_sessions(user_id)

        assert deleted_count == 1

    def test_delete_user_sessions_with_user_id_only(self, session_manager, mock_redis):
        """Test deletion works with sessions that only have 'user_id' key (internal)."""
        user_id = "user_123"
        # Session with only 'user_id' (no 'sub')
        session_data = json.dumps(
            {
                "user_id": user_id,
                "email": "test@example.com",
            }
        )

        mock_redis.scan.return_value = (0, ["session:abc123"])
        mock_redis.get.return_value = session_data

        mock_pipe = MagicMock()
        mock_pipe.execute.return_value = [1]
        mock_redis.pipeline.return_value = mock_pipe

        deleted_count = session_manager.delete_user_sessions(user_id)

        assert deleted_count == 1

    def test_delete_user_sessions_invalid_json(self, session_manager, mock_redis):
        """Test that invalid JSON session data is skipped."""
        user_id = "user_123"
        valid_session = json.dumps({"sub": user_id, "email": "test@example.com"})

        mock_redis.scan.return_value = (0, ["session:valid", "session:invalid"])
        mock_redis.get.side_effect = [valid_session, "not valid json"]

        mock_pipe = MagicMock()
        mock_pipe.execute.return_value = [1]
        mock_redis.pipeline.return_value = mock_pipe

        deleted_count = session_manager.delete_user_sessions(user_id)

        # Only the valid session should be deleted
        assert deleted_count == 1

    def test_delete_user_sessions_redis_error(self, session_manager, mock_redis):
        """Test handling of Redis errors."""
        mock_redis.scan.side_effect = Exception("Redis connection failed")

        deleted_count = session_manager.delete_user_sessions("user_123")

        # Should return 0 on error, not raise exception
        assert deleted_count == 0

    def test_delete_user_sessions_scan_pagination(self, session_manager, mock_redis):
        """Test SCAN pagination with multiple iterations."""
        user_id = "user_123"
        session_data = json.dumps({"sub": user_id, "email": "test@example.com"})

        # Mock SCAN to return results in multiple batches
        # First call returns cursor 42 (more data), second call returns cursor 0 (done)
        mock_redis.scan.side_effect = [
            (42, ["session:batch1_1", "session:batch1_2"]),
            (0, ["session:batch2_1"]),
        ]
        mock_redis.get.return_value = session_data

        mock_pipe = MagicMock()
        mock_pipe.execute.return_value = [1, 1, 1]
        mock_redis.pipeline.return_value = mock_pipe

        deleted_count = session_manager.delete_user_sessions(user_id)

        assert deleted_count == 3
        # SCAN should be called twice
        assert mock_redis.scan.call_count == 2
        mock_redis.scan.assert_any_call(0, match="session:*", count=100)
        mock_redis.scan.assert_any_call(42, match="session:*", count=100)

    def test_delete_user_sessions_pipeline_partial_failure(self, session_manager, mock_redis):
        """Test pipeline with partial deletion failures."""
        user_id = "user_123"
        session_data = json.dumps({"sub": user_id, "email": "test@example.com"})

        mock_redis.scan.return_value = (0, ["session:s1", "session:s2", "session:s3"])
        mock_redis.get.return_value = session_data

        # Mock pipeline where one deletion fails (session expired between scan and delete)
        mock_pipe = MagicMock()
        mock_pipe.execute.return_value = [1, 0, 1]  # Middle one failed
        mock_redis.pipeline.return_value = mock_pipe

        deleted_count = session_manager.delete_user_sessions(user_id)

        # Only 2 deletions succeeded
        assert deleted_count == 2
