"""Tests for UserRepository."""

from datetime import datetime

import pytest

from models.auth import User
from repositories.user import UserRepository


class TestUserRepository:
    """Test UserRepository for PostgreSQL user operations."""

    @pytest.fixture
    async def repository(self, db_session):
        """Create UserRepository with test DB session."""
        return UserRepository(db_session)

    @pytest.fixture
    async def sample_user(self, db_session):
        """Create sample user in DB."""
        user = User(
            user_id="test_user_123",
            email="test@example.com",
            full_name="Test User",
            role="user",
            is_active=True,
            created_at=datetime.utcnow(),
        )
        db_session.add(user)
        await db_session.commit()
        await db_session.refresh(user)
        return user

    @pytest.mark.asyncio
    async def test_get_existing(self, repository, sample_user):
        """Test getting existing user by ID."""
        result = await repository.get(sample_user.id)

        assert result is not None
        assert result.id == sample_user.id
        assert result.email == sample_user.email

    @pytest.mark.asyncio
    async def test_get_nonexistent(self, repository):
        """Test getting nonexistent user."""
        result = await repository.get(99999)

        assert result is None

    @pytest.mark.asyncio
    async def test_get_by_email(self, repository, sample_user):
        """Test getting user by email."""
        result = await repository.get_by_email("test@example.com")

        assert result is not None
        assert result.email == "test@example.com"
        assert result.id == sample_user.id

    @pytest.mark.asyncio
    async def test_get_by_email_nonexistent(self, repository):
        """Test getting user by nonexistent email."""
        result = await repository.get_by_email("nonexistent@example.com")

        assert result is None

    @pytest.mark.asyncio
    async def test_get_by_oauth_id(self, repository, sample_user):
        """Test getting user by OAuth ID."""
        result = await repository.get_by_oauth_id("test_user_123")

        assert result is not None
        assert result.user_id == "test_user_123"
        assert result.id == sample_user.id

    @pytest.mark.asyncio
    async def test_get_by_oauth_id_nonexistent(self, repository):
        """Test getting user by nonexistent OAuth ID."""
        result = await repository.get_by_oauth_id("nonexistent_oauth_id")

        assert result is None

    @pytest.mark.asyncio
    async def test_list(self, repository, sample_user):
        """Test listing users."""
        results = await repository.list()

        assert len(results) > 0
        assert any(u.id == sample_user.id for u in results)

    @pytest.mark.asyncio
    async def test_list_with_pagination(self, repository, db_session):
        """Test listing with pagination."""
        # Create multiple users
        for i in range(3):
            user = User(
                user_id=f"user_{i}",
                email=f"user{i}@example.com",
                full_name=f"User {i}",
                role="user",
                is_active=True,
                created_at=datetime.utcnow(),
            )
            db_session.add(user)
        await db_session.commit()

        # Test pagination
        page1 = await repository.list(skip=0, limit=2)

        assert len(page1) <= 2

    @pytest.mark.asyncio
    async def test_create(self, repository, db_session):
        """Test creating new user."""
        new_user = User(
            user_id="new_user_456",
            email="new@example.com",
            full_name="New User",
            role="user",
            is_active=True,
            created_at=datetime.utcnow(),
        )

        created = await repository.create(new_user)

        assert created.id is not None
        assert created.email == "new@example.com"

        # Verify in DB
        fetched = await repository.get_by_email("new@example.com")
        assert fetched is not None

    @pytest.mark.asyncio
    async def test_update(self, repository, sample_user):
        """Test updating existing user."""
        # Update user
        sample_user.full_name = "Updated Name"
        sample_user.role = "admin"

        updated = await repository.update(sample_user)

        assert updated.full_name == "Updated Name"
        assert updated.role == "admin"

        # Verify in DB
        fetched = await repository.get(sample_user.id)
        assert fetched.full_name == "Updated Name"

    @pytest.mark.asyncio
    async def test_delete(self, repository, sample_user):
        """Test deleting user."""
        user_id = sample_user.id

        await repository.delete(user_id)

        # Verify deleted
        result = await repository.get(user_id)
        assert result is None

    @pytest.mark.asyncio
    async def test_email_uniqueness(self, repository, db_session):
        """Test that email must be unique."""
        user1 = User(
            user_id="unique_user_1",
            email="duplicate@example.com",
            full_name="User 1",
            role="user",
            is_active=True,
            created_at=datetime.utcnow(),
        )
        db_session.add(user1)
        await db_session.commit()

        # Try to create another user with same email
        user2 = User(
            user_id="unique_user_2",
            email="duplicate@example.com",  # Duplicate email
            full_name="User 2",
            role="user",
            is_active=True,
            created_at=datetime.utcnow(),
        )

        from sqlalchemy.exc import IntegrityError

        with pytest.raises(IntegrityError):
            db_session.add(user2)
            await db_session.commit()

    @pytest.mark.asyncio
    async def test_user_id_uniqueness(self, repository, db_session):
        """Test that user_id (OAuth ID) must be unique."""
        user1 = User(
            user_id="duplicate_oauth_id",
            email="user1@example.com",
            full_name="User 1",
            role="user",
            is_active=True,
            created_at=datetime.utcnow(),
        )
        db_session.add(user1)
        await db_session.commit()

        # Try to create another user with same OAuth ID
        user2 = User(
            user_id="duplicate_oauth_id",  # Duplicate OAuth ID
            email="user2@example.com",
            full_name="User 2",
            role="user",
            is_active=True,
            created_at=datetime.utcnow(),
        )

        from sqlalchemy.exc import IntegrityError

        with pytest.raises(IntegrityError):
            db_session.add(user2)
            await db_session.commit()

    @pytest.mark.asyncio
    async def test_active_inactive_users(self, repository, db_session):
        """Test active and inactive users."""
        active_user = User(
            user_id="active_user",
            email="active@example.com",
            full_name="Active User",
            role="user",
            is_active=True,
            created_at=datetime.utcnow(),
        )
        inactive_user = User(
            user_id="inactive_user",
            email="inactive@example.com",
            full_name="Inactive User",
            role="user",
            is_active=False,
            created_at=datetime.utcnow(),
        )
        db_session.add_all([active_user, inactive_user])
        await db_session.commit()

        # Get both users
        active = await repository.get_by_email("active@example.com")
        inactive = await repository.get_by_email("inactive@example.com")

        assert active.is_active is True
        assert inactive.is_active is False
