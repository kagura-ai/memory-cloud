"""Auto-hide Credentials Background Job.

Migration 034: Automatically hide API Keys and OAuth Apps after 7 days.

Schedule: Runs every hour
Purpose: Zero-knowledge security - enforce visibility expiration
"""

from sqlalchemy import and_, select

from db.base import get_db
from models.auth import APIKey, OAuth2Client
from utils.datetime import utcnow
from utils.logger import get_logger

logger = get_logger(__name__)


async def auto_hide_expired_credentials():
    """Auto-hide credentials after visibility window expires.

    Migration 034: Runs hourly to hide credentials after 7 days.

    Process:
    1. Find API keys with visibility_expires_at <= now AND hidden_at IS NULL
    2. Set hidden_at = now, visibility_expires_at = NULL
    3. Same for OAuth clients
    4. Log audit trail

    Returns:
        None
    """
    async for db in get_db():
        now = utcnow()

        # ========================================
        # Auto-hide API Keys
        # ========================================
        api_keys_result = await db.execute(
            select(APIKey).where(
                and_(
                    APIKey.visibility_expires_at <= now,
                    APIKey.hidden_at.is_(None),
                    APIKey.revoked_at.is_(None),  # Don't touch revoked keys
                )
            )
        )
        api_keys = api_keys_result.scalars().all()

        api_key_count = 0
        for key in api_keys:
            key.hidden_at = now
            key.visibility_expires_at = None  # Prevent re-processing
            key.plaintext_encrypted = None  # Migration 035: Delete encrypted plaintext
            api_key_count += 1

            logger.info(
                "api_key_auto_hidden",
                key_id=key.id,
                user_id=key.user_id,
                key_prefix=key.key_prefix,
            )

        # ========================================
        # Auto-hide OAuth Clients
        # ========================================
        oauth_clients_result = await db.execute(
            select(OAuth2Client).where(
                and_(
                    OAuth2Client.visibility_expires_at <= now,
                    OAuth2Client.hidden_at.is_(None),
                )
            )
        )
        oauth_clients = oauth_clients_result.scalars().all()

        oauth_client_count = 0
        for client in oauth_clients:
            client.hidden_at = now
            client.visibility_expires_at = None  # Prevent re-processing
            client.plaintext_secret_encrypted = None  # Migration 035: Delete encrypted secret
            oauth_client_count += 1

            logger.info(
                "oauth_client_auto_hidden",
                client_id=client.client_id,
                owner_id=client.owner_id,
            )

        # Commit all changes
        await db.commit()

        # Summary log
        if api_key_count > 0 or oauth_client_count > 0:
            logger.info(
                "auto_hide_credentials_completed",
                api_keys_hidden=api_key_count,
                oauth_clients_hidden=oauth_client_count,
            )
        else:
            logger.debug("auto_hide_credentials_completed", message="No credentials to hide")

        # Exit generator loop
        return
