"""Tests for UserRepository."""

from datetime import datetime
from uuid import uuid4

import pytest

from models.auth import User
from repositories.user import UserRepository

pytestmark = pytest.mark.asyncio(loop_scope="session")


class TestUserRepository:
    """Test UserRepository for PostgreSQL user operations."""

    @pytest.fixture
    async def repository(self, db_session):
        """Create UserRepository with test DB session."""
        return UserRepository(db_session)

    @pytest.fixture
    async def sample_user(self, db_session):
        """Create sample user in DB with unique identifiers to avoid collisions."""
        uid = str(uuid4())
        user = User(
            user_id=f"test_user_{uid}",
            email=f"test_{uid}@example.com",
            name="Test User",
            role="user",
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
        result = await repository.get_by_email(sample_user.email)

        assert result is not None
        assert result.email == sample_user.email
        assert result.id == sample_user.id

    @pytest.mark.asyncio
    async def test_get_by_email_nonexistent(self, repository):
        """Test getting user by nonexistent email."""
        result = await repository.get_by_email("nonexistent@example.com")

        assert result is None

    @pytest.mark.asyncio
    async def test_get_by_oauth_id(self, repository, sample_user):
        """Test getting user by OAuth ID."""
        result = await repository.get_by_oauth_id(sample_user.user_id)

        assert result is not None
        assert result.user_id == sample_user.user_id
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
        # Create multiple users with unique IDs
        for i in range(3):
            uid = str(uuid4())
            user = User(
                user_id=f"page_user_{uid}_{i}",
                email=f"page_user_{uid}_{i}@example.com",
                name=f"Page User {i}",
                role="user",
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
        uid = str(uuid4())
        new_user = User(
            user_id=f"new_user_{uid}",
            email=f"new_{uid}@example.com",
            name="New User",
            role="user",
            created_at=datetime.utcnow(),
        )

        created = await repository.create(new_user)

        assert created.id is not None
        assert created.email == new_user.email

        # Verify in DB
        fetched = await repository.get_by_email(new_user.email)
        assert fetched is not None

    @pytest.mark.asyncio
    async def test_update(self, repository, sample_user):
        """Test updating existing user."""
        # Update user
        sample_user.name = "Updated Name"
        sample_user.role = "admin"

        updated = await repository.update(sample_user.id, sample_user)

        assert updated.name == "Updated Name"
        assert updated.role == "admin"

        # Verify in DB
        fetched = await repository.get(sample_user.id)
        assert fetched.name == "Updated Name"

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
        uid = str(uuid4())
        user1 = User(
            user_id=f"unique_user_1_{uid}",
            email=f"duplicate_{uid}@example.com",
            name="User 1",
            role="user",
            created_at=datetime.utcnow(),
        )
        db_session.add(user1)
        await db_session.commit()

        # Try to create another user with same email
        user2 = User(
            user_id=f"unique_user_2_{uid}",
            email=f"duplicate_{uid}@example.com",  # Duplicate email
            name="User 2",
            role="user",
            created_at=datetime.utcnow(),
        )

        from sqlalchemy.exc import IntegrityError

        with pytest.raises(IntegrityError):
            db_session.add(user2)
            await db_session.commit()

    @pytest.mark.asyncio
    async def test_user_id_uniqueness(self, repository, db_session):
        """Test that user_id (OAuth ID) must be unique."""
        uid = str(uuid4())
        user1 = User(
            user_id=f"duplicate_oauth_{uid}",
            email=f"user1_{uid}@example.com",
            name="User 1",
            role="user",
            created_at=datetime.utcnow(),
        )
        db_session.add(user1)
        await db_session.commit()

        # Try to create another user with same OAuth ID
        user2 = User(
            user_id=f"duplicate_oauth_{uid}",  # Duplicate OAuth ID
            email=f"user2_{uid}@example.com",
            name="User 2",
            role="user",
            created_at=datetime.utcnow(),
        )

        from sqlalchemy.exc import IntegrityError

        with pytest.raises(IntegrityError):
            db_session.add(user2)
            await db_session.commit()

    @pytest.mark.asyncio
    async def test_users_with_different_roles(self, repository, db_session):
        """Test users with different roles can be created and retrieved."""
        uid = str(uuid4())
        admin_user = User(
            user_id=f"admin_user_{uid}",
            email=f"admin_{uid}@example.com",
            name="Admin User",
            role="admin",
            created_at=datetime.utcnow(),
        )
        regular_user = User(
            user_id=f"regular_user_{uid}",
            email=f"regular_{uid}@example.com",
            name="Regular User",
            role="user",
            created_at=datetime.utcnow(),
        )
        db_session.add_all([admin_user, regular_user])
        await db_session.commit()

        # Get both users
        admin = await repository.get_by_email(admin_user.email)
        regular = await repository.get_by_email(regular_user.email)

        assert admin.role == "admin"
        assert regular.role == "user"
