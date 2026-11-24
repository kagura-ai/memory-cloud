"""Bodies of the public-text command for an installation's endpoint (#2995, experimental).

A managing application, acting for the workspace's canonical owner through
the installation's registered browser client, sets the endpoint's public
description and usage guide. The command names the binding the owner last
read (``expected``), as the custom-domain commands do. The public name is
not part of it: it stays the owner's.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field, model_validator

from models.api_base import TZAwareBaseModel
from models.public_mcp import PUBLIC_DESCRIPTION_MAX_CHARS, PUBLIC_USAGE_GUIDE_MAX_CHARS
from models.sync_feed_schemas import StrictBody
from models.sync_publication_domain_schemas import PublicationBindingExpectation

PublicTextField = Literal["description", "usage_guide"]


class PublicationTextUpdate(StrictBody):
    """Set the public description, the usage guide, or both.

    An omitted field keeps its value; ``""`` or ``null`` clears it, as in the
    owner API. At least one of the two is present. Any other key, the public
    name included, is refused.
    """

    expected: PublicationBindingExpectation
    public_description: Annotated[str | None, Field(max_length=PUBLIC_DESCRIPTION_MAX_CHARS)] = None
    public_usage_guide: Annotated[str | None, Field(max_length=PUBLIC_USAGE_GUIDE_MAX_CHARS)] = None

    @model_validator(mode="after")
    def _one_field_at_least(self) -> PublicationTextUpdate:
        if not self.model_fields_set & {"public_description", "public_usage_guide"}:
            raise ValueError("public_description or public_usage_guide is required")
        return self


class PublicationTextResult(TZAwareBaseModel):
    """The endpoint's public text after the command (experimental).

    ``changed_fields`` names what this command changed (empty when the text
    already was as sent; nothing is then recorded).
    """

    endpoint_id: UUID
    public_id: str
    public_description: str | None
    public_usage_guide: str | None
    updated_at: datetime | None
    changed_fields: list[PublicTextField]
