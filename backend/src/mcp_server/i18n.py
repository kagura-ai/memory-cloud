"""MCP Tool Description i18n Service.

Issue #160: Provides i18n support for MCP tool descriptions.
Admin can edit descriptions via Web UI.
"""

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

from models.auth import MCPToolDescription

logger = logging.getLogger(__name__)


class MCPToolDescriptionService:
    """Service for managing MCP tool descriptions with i18n support."""

    def __init__(self, db: "AsyncSession"):
        """Initialize service.

        Args:
            db: Database session
        """
        self.db = db

    async def get_descriptions(self, locale: str = "en") -> dict[str, str]:
        """Get all tool descriptions for specified locale.

        Args:
            locale: Locale code (en, ja, etc.)

        Returns:
            Dict mapping tool_name to description
        """
        from sqlalchemy import select

        stmt = select(MCPToolDescription).where(MCPToolDescription.locale == locale)
        result = await self.db.execute(stmt)
        descriptions = result.scalars().all()

        return {desc.tool_name: desc.description for desc in descriptions}

    async def get_description(self, tool_name: str, locale: str = "en") -> str | None:
        """Get single tool description.

        Args:
            tool_name: Tool name (remember, recall, etc.)
            locale: Locale code

        Returns:
            Description string or None if not found
        """
        from sqlalchemy import select

        stmt = select(MCPToolDescription).where(
            MCPToolDescription.tool_name == tool_name,
            MCPToolDescription.locale == locale,
        )
        result = await self.db.execute(stmt)
        description = result.scalar_one_or_none()

        if description:
            return description.description

        # Fallback to English
        if locale != "en":
            logger.warning(
                f"Description not found for {tool_name} ({locale}), falling back to 'en'"
            )
            return await self.get_description(tool_name, "en")

        return None

    async def update_description(
        self, tool_name: str, locale: str, description: str, user_id: str
    ) -> MCPToolDescription:
        """Update tool description (Admin only).

        Args:
            tool_name: Tool name
            locale: Locale code
            description: New description text
            user_id: Admin user ID (for audit)

        Returns:
            Updated MCPToolDescription
        """
        from sqlalchemy import select

        # Check if exists
        stmt = select(MCPToolDescription).where(
            MCPToolDescription.tool_name == tool_name,
            MCPToolDescription.locale == locale,
        )
        result = await self.db.execute(stmt)
        existing = result.scalar_one_or_none()

        if existing:
            # Update
            existing.description = description
            await self.db.commit()
            await self.db.refresh(existing)
            logger.info(f"mcp_tool_description_updated: {tool_name} ({locale}) by {user_id}")
            return existing
        else:
            # Create new
            new_desc = MCPToolDescription(
                tool_name=tool_name,
                locale=locale,
                description=description,
            )
            self.db.add(new_desc)
            await self.db.commit()
            await self.db.refresh(new_desc)
            logger.info(f"mcp_tool_description_created: {tool_name} ({locale}) by {user_id}")
            return new_desc

    async def list_all_descriptions(self) -> list[MCPToolDescription]:
        """List all tool descriptions across all locales.

        Returns:
            List of all MCPToolDescription records
        """
        from sqlalchemy import select

        stmt = select(MCPToolDescription).order_by(
            MCPToolDescription.tool_name, MCPToolDescription.locale
        )
        result = await self.db.execute(stmt)
        return list(result.scalars().all())
