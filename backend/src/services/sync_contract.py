"""Explicit producer/consumer compatibility; a capability is never a grant."""

from pydantic import ValidationError

from config.settings import Settings
from models.sync_contract import SyncConfigurationV1
from models.sync_feed_schemas import SCHEMA_V1, SCHEMA_V2, SyncSchema

HUMAN_SCOPES = ("openid", "workspaces:read", "integrations:manage", "sync.records:read")
# #2921: the owner's claim, verify and remove for an installation (experimental, optional).
DOMAIN_OPERATIONS_CAPABILITY = "public.domains.operations.v1"
# #2995: the public text of an installation's endpoint (experimental, optional).
TEXT_OPERATIONS_CAPABILITY = "public.text.operations.v1"
# #3035: the usage statistics of an installation's endpoint (experimental, optional).
USAGE_STATS_CAPABILITY = "public.usage.stats.v1"
# #3055: this deployment stores record schema version 2, gives it to new
# installations and serves the upgrade operation. Advertised last.
SCHEMA_V2_CAPABILITY = "sync.schema.v2"
REQUIRED_SYNC_CAPABILITIES = frozenset(
    {
        "identity.oidc.v1",
        "workspace.access.v1",
        "sync.installations.v1",
        "sync.credentials.v1",
        "sync.feeds.v1",
        "sync.managed.v1",
        "sync.activity.v1",
        "public.mcp.v1",
        "public.budget.v1",
    }
)


class SyncContractUnsupported(ValueError):
    """Fixed consumer refusal without reflecting untrusted metadata."""


def sync_configuration(settings: Settings) -> SyncConfigurationV1:
    """Describe enabled profiles without loading secrets, database rows or remote URLs."""
    identity = settings.oidc_enabled
    public = settings.mcp_public_endpoints_enabled
    controls = identity and public and settings.sync_integrations_enabled
    credentials = controls and settings.sync_credentials_enabled
    feeds = credentials and settings.sync_runs_enabled
    budget = public and settings.mcp_public_usage_budget_enabled
    enabled = {
        "identity.oidc.v1": identity,
        "workspace.access.v1": identity,
        "sync.installations.v1": controls,
        "sync.credentials.v1": credentials,
        "sync.feeds.v1": feeds,
        "sync.managed.v1": feeds and settings.sync_managed_enabled,
        "sync.activity.v1": (
            controls and settings.sync_runs_enabled and settings.sync_observability_enabled
        ),
        "public.mcp.v1": public,
        "public.budget.v1": budget,
        "public.domains.v1": budget and settings.mcp_public_domains_enabled,
        "public.domains.observation.v1": controls
        and budget
        and settings.mcp_public_domains_enabled,
        DOMAIN_OPERATIONS_CAPABILITY: controls
        and budget
        and settings.mcp_public_domains_enabled
        and settings.sync_publication_domain_operations_enabled,
        TEXT_OPERATIONS_CAPABILITY: controls and settings.sync_publication_text_operations_enabled,
        USAGE_STATS_CAPABILITY: controls and settings.sync_publication_usage_stats_enabled,
        SCHEMA_V2_CAPABILITY: feeds and settings.sync_schema_v2_enabled,
    }
    # The pair a new installation gets: version 2's exactly while it is advertised.
    schema = SCHEMA_V2 if enabled[SCHEMA_V2_CAPABILITY] else SCHEMA_V1
    return SyncConfigurationV1(
        protocol="kmc.sync.v1",
        schema_version=schema.version,
        schema_hash=schema.schema_hash,
        capabilities=[name for name, active in enabled.items() if active],
        human_scopes=list(HUMAN_SCOPES),
        service_scope="resource.sync",
        assertion_audience="issuer",
    )


def new_installation_schema(settings: Settings) -> SyncSchema:
    """The record schema a first feed configuration stores (the advertised pair)."""
    return (
        SCHEMA_V2
        if SCHEMA_V2_CAPABILITY in sync_configuration(settings).capabilities
        else SCHEMA_V1
    )


def require_sync_contract(
    payload: object,
    *,
    custom_domain: bool = False,
    accepted: tuple[SyncSchema, ...] = (SCHEMA_V1,),
) -> SyncConfigurationV1:
    """Reject unsupported/disabled targets before a consumer queues new work.

    ``accepted`` names the record schema versions the consumer handles; the
    advertised pair must be one of them, and ``sync.schema.v2`` is advertised
    exactly with version 2's pair.

    The caller fetches fresh metadata from its explicitly trusted HTTPS KMC
    origin, with bounded size/time, no redirects or stale-cache fallback. This
    pure helper performs no I/O and does not authorize any operation. Current
    human/service/paid/target admission must still succeed for every request.
    """
    try:
        contract = SyncConfigurationV1.model_validate(payload)
    except ValidationError:
        raise SyncContractUnsupported("unsupported_sync_contract") from None
    required = REQUIRED_SYNC_CAPABILITIES | ({"public.domains.v1"} if custom_domain else set())
    pair = (contract.schema_version, contract.schema_hash)
    if (
        pair not in {(schema.version, schema.schema_hash) for schema in accepted}
        or (pair == (SCHEMA_V2.version, SCHEMA_V2.schema_hash))
        != (SCHEMA_V2_CAPABILITY in contract.capabilities)
        or not required.issubset(contract.capabilities)
        or not set(HUMAN_SCOPES).issubset(contract.human_scopes)
    ):
        raise SyncContractUnsupported("unsupported_sync_contract")
    return contract
