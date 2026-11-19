"""Bodies of the owner's custom-domain commands for an installation (#2921, experimental).

A command names the binding of the installation the owner last read
(``expected``) and, for verify and remove, the domain row and its state. The
answer is the open domain the command left behind; the domain's facts (the
TXT record, routing, lease, last check) are read from the observation.
"""

from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, Field, StrictInt

from models.sync_feed_schemas import StrictBody
from utils.query_bounds import MAX_INT32

OpenDomainState = Literal["pending", "active", "suspended"]


class PublicationBindingExpectation(StrictBody):
    """The seven identifiers of the installation's publication, as last read."""

    application_id: Annotated[str, Field(min_length=1, max_length=128)]
    resource_id: Annotated[str, Field(min_length=1, max_length=255)]
    resource_pk: UUID
    context_id: UUID
    endpoint_id: UUID
    public_id: Annotated[str, Field(min_length=1, max_length=64)]
    generation: Annotated[StrictInt, Field(ge=1, le=MAX_INT32)]


class DomainRowExpectation(StrictBody):
    """The open domain the command acts on, and the state it was read in."""

    domain_id: UUID
    state: OpenDomainState


class PublicationDomainClaim(StrictBody):
    """Claim a host name for the publication, which must have no open domain."""

    expected: PublicationBindingExpectation
    hostname: Annotated[str, Field(min_length=1, max_length=255)]


class PublicationDomainCommand(StrictBody):
    """Verify or remove the named open domain of the publication."""

    expected: PublicationBindingExpectation
    domain: DomainRowExpectation


class DomainRowState(BaseModel):
    """An open domain: the value a client sends as ``domain`` in its next command."""

    domain_id: UUID
    state: OpenDomainState


class PublicationDomainCommandResult(BaseModel):
    """What the command left behind (experimental).

    ``domain`` is the publication's open domain after the command; ``null``
    after a remove, and after a verify whose own check released the domain.
    """

    schema_version: Literal["public-domain-command-v1"] = "public-domain-command-v1"
    action: Literal["claim", "verify", "remove"]
    domain: DomainRowState | None
