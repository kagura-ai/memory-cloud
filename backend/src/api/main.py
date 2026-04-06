"""Kagura Memory Cloud - FastAPI Application Entry Point.

Based on: kagura-ai/src/kagura/api/server.py
"""

import sys
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy.exc import SQLAlchemyError
from starlette.middleware.base import BaseHTTPMiddleware

sys.path.insert(0, str(Path(__file__).parent.parent))

from config.constants import APP_VERSION
from config.settings import get_settings
from utils.exceptions import DatabaseConnectionError, MemoryCloudException
from utils.logger import get_logger, setup_logger

# Setup logger first
setup_logger()
logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan events."""
    # Startup
    logger.info("application_starting", version=app.version)

    # Migrations are managed by Alembic: `alembic upgrade head`

    # Initialize auth routes (Issue #13, #31)
    import auth.roles
    from api.routes.auth import initialize_auth_routes
    from auth.oauth2 import OAuth2Manager
    from auth.session import SessionManager
    from config.database import get_redis_url

    # Initialize RoleManager first (required for user role assignment)
    # Issue #38: Now using PostgreSQL persistence with async sessions
    auth.roles._role_manager = auth.roles.RoleManager(use_postgres=True)
    logger.info("role_manager_initialized (postgresql mode)")

    # Initialize OAuth2 and Session managers
    # Issue #51: OAuth2Manager is optional (only when Google OAuth is configured)
    import os

    global _session_middleware_manager
    google_client_id = os.getenv("GOOGLE_CLIENT_ID", "")
    oauth2_manager = OAuth2Manager(provider="google") if google_client_id else None
    _session_middleware_manager = SessionManager(redis_url=get_redis_url())
    initialize_auth_routes(oauth2_manager, _session_middleware_manager)
    if oauth2_manager:
        logger.info("auth_routes_initialized (google oauth enabled)")
    else:
        logger.info("auth_routes_initialized (password only, google oauth disabled)")

    # Start background task scheduler
    from tasks import (
        get_scheduler,
        schedule_credentials_tasks,
        schedule_embedding_tasks,
        schedule_mcp_tasks,
        schedule_neural_tasks,
        schedule_resource_indexer_jobs,
        schedule_sleep_tasks,
        start_scheduler,
    )

    scheduler = get_scheduler()
    schedule_neural_tasks(scheduler)
    schedule_mcp_tasks(scheduler)
    schedule_credentials_tasks(scheduler)
    schedule_embedding_tasks(scheduler)
    schedule_resource_indexer_jobs(scheduler)
    schedule_sleep_tasks(scheduler)
    start_scheduler()
    logger.info("background_tasks_scheduled")

    logger.info("application_started")

    yield

    # Shutdown
    logger.info("application_shutting_down")

    # Stop scheduler
    from tasks import shutdown_scheduler

    shutdown_scheduler()
    logger.info("scheduler_stopped")

    # Close database
    from db.base import close_db

    await close_db()
    logger.info("application_stopped")


# Create FastAPI app
openapi_tags = [
    # Authentication & Users
    {"name": "authentication", "description": "OAuth2 login and session management"},
    {"name": "users", "description": "User profile and settings"},
    # Workspace
    {"name": "workspace", "description": "Workspace dashboard and stats"},
    {"name": "workspaces", "description": "Workspace CRUD operations"},
    {"name": "workspace-plan", "description": "Workspace plan management (self-service)"},
    {"name": "invitations", "description": "Team member invitations"},
    {"name": "member-credentials", "description": "API key and OAuth credential management"},
    # Memory & Search
    {"name": "contexts", "description": "Context management"},
    {"name": "context-search-config", "description": "Per-context search settings"},
    {
        "name": "memory",
        "description": "MCP memory operations (remember, recall, forget, explore, reference)",
    },
    {
        "name": "attachments",
        "description": "File attachments for memories (upload, download, delete)",
    },
    {"name": "graph", "description": "Neural Memory graph operations"},
    # Integrations
    {"name": "api-keys", "description": "MCP API key management"},
    {"name": "oauth2-server", "description": "OAuth2 client management"},
    {"name": "external-keys", "description": "External API keys (OpenAI, Cohere, etc.)"},
    {"name": "resource-tokens", "description": "Resource token management"},
    {"name": "resource-ingest", "description": "External data ingestion API"},
    {"name": "resource-schema", "description": "Resource schema registry"},
    # Public API
    {"name": "public-search", "description": "Public REST API for search"},
    {"name": "mcp-api", "description": "MCP server tools listing and status"},
    # Admin
    {"name": "admin", "description": "Admin: user management and system stats"},
    {"name": "admin-plans", "description": "Admin: plan tier and quota management"},
    {"name": "neural-config", "description": "Admin: neural memory configuration"},
    {
        "name": "sleep-reports",
        "description": "Admin: Sleep Maintenance execution reports and audit log",
    },
    {"name": "system-admins", "description": "Admin: system administrator management"},
    # System
    {"name": "config", "description": "Environment configuration management (admin)"},
    {"name": "usage", "description": "Usage statistics"},
    {"name": "system", "description": "System health and info"},
    {"name": "well-known", "description": "OAuth2 discovery endpoints"},
]

app = FastAPI(
    title="Kagura Memory Cloud",
    description="Universal AI Memory Platform - Remote MCP Server + Web Management",
    version=APP_VERSION,
    docs_url="/docs",
    redoc_url="/redoc",
    openapi_tags=openapi_tags,
    lifespan=lifespan,
)

# Get settings
settings = get_settings()

# CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Session middleware (Issue #13, #31)
# Note: SessionManager is initialized in lifespan() and assigned to global
_session_middleware_manager = None


# Wrapper middleware to use global SessionManager
class SessionMiddlewareWrapper(BaseHTTPMiddleware):
    """Wrapper to inject SessionManager after lifespan initialization."""

    async def dispatch(self, request: Request, call_next):
        if _session_middleware_manager:
            from api.middleware.session import SessionMiddleware

            middleware = SessionMiddleware(app, _session_middleware_manager)
            return await middleware.dispatch(request, call_next)
        else:
            # Before initialization, skip session check
            return await call_next(request)


app.add_middleware(SessionMiddlewareWrapper)

# Rate Limiting Middleware (Issue #251)
# Must be added AFTER SessionMiddleware (so it runs BEFORE session auth)
# Execution order: RateLimit → Session → Route handler
from api.middleware.rate_limit import RateLimitMiddleware  # noqa: E402

app.add_middleware(RateLimitMiddleware)
logger.info("rate_limit_middleware_registered")

# Request Logging Middleware (Issue #48, #50)
# Must be added LAST (so it runs outermost — records after all other middleware)
# Execution order: RequestLogger → RateLimit → Session → Route handler → (response logged)
from api.middleware.request_logger import RequestLoggingMiddleware  # noqa: E402

app.add_middleware(RequestLoggingMiddleware)
logger.info("request_logging_middleware_registered")


# ============================================================================
# Exception Handlers
# ============================================================================


@app.exception_handler(MemoryCloudException)
async def memory_cloud_exception_handler(
    request: Request, exc: MemoryCloudException
) -> JSONResponse:
    """Handle MemoryCloudException."""
    logger.error(
        "exception_occurred",
        error=exc.error_code,
        message=exc.message,
        status_code=exc.status_code,
        path=request.url.path,
    )

    return JSONResponse(
        status_code=exc.status_code,
        content={
            "error": exc.error_code,
            "message": exc.message,
            "details": exc.details,
        },
    )


def _database_unavailable_response(request: Request, exc: Exception) -> JSONResponse:
    """Shared 503 response for any DB-backend failure (#193).

    Reuses the ``DatabaseConnectionError`` exception class so that the
    status code, error code, and message stay in one place and match the
    shape emitted by ``memory_cloud_exception_handler`` for any code path
    that raises it directly.
    """
    wrapped = DatabaseConnectionError()
    logger.error(
        "database_error",
        error_code=wrapped.error_code,
        error_type=type(exc).__name__,
        path=request.url.path,
        exc_info=True,
    )
    return JSONResponse(
        status_code=wrapped.status_code,
        content={
            "error": wrapped.error_code,
            "message": wrapped.message,
            "details": wrapped.details,
        },
        headers={"Retry-After": "5"},
    )


@app.exception_handler(SQLAlchemyError)
async def sqlalchemy_exception_handler(request: Request, exc: SQLAlchemyError) -> JSONResponse:
    return _database_unavailable_response(request, exc)


@app.exception_handler(ConnectionError)
async def connection_error_handler(request: Request, exc: ConnectionError) -> JSONResponse:
    """Handle raw network errors from the DB driver (#193).

    When the async engine cannot even open a connection, asyncpg raises a
    bare ``ConnectionRefusedError`` (``OSError`` subclass) that SQLAlchemy
    does not wrap, so the ``SQLAlchemyError`` handler above never sees it.
    Before this handler, public routes like ``GET /public/{context_id}/info``
    and ``GET /invitations/{token}`` — which run before any auth dependency
    — would bubble a bare "Internal Server Error" 500 whenever the DB was
    briefly unreachable.

    ``ConnectionError`` is narrow enough (network only: refused / reset /
    aborted / broken pipe) to avoid swallowing unrelated bugs.
    """
    return _database_unavailable_response(request, exc)


# ============================================================================
# Routes
# ============================================================================

from api.routes import (  # noqa: E402
    admin,
    admin_plans,
    api_keys,
    attachments,  # Issue #330: File attachment support
    auth,
    config,
    context_search_config,
    contexts,
    external_keys,
    graph,
    invitations,
    mcp,
    member_credentials,
    memory,
    neural_config,
    oauth,
    public_search,  # Issue #238: Public Search API
    resource_ingest,  # Issue #238: Resource Ingest API
    resource_schema,  # Issue #238: Schema Management API
    resource_tokens,  # Issue #242: Resource Token Management API
    sleep_reports,  # Issue #179: Sleep Report admin UI
    system,
    system_admins,
    usage,
    users,
    well_known,
    workspace,
    workspace_plan,
    workspaces,
)

# Auth routes (Phase 1 - OAuth2 user login)
app.include_router(auth.router, prefix="/api/v1")

# User profile routes (Issue #175 - Timezone settings)
app.include_router(users.router, prefix="/api/v1")

# OAuth2 Server routes (Issue #33 - OAuth2 client management)
app.include_router(oauth.router, prefix="/api/v1")

# API Keys routes (Issue #34 - API Keys management)
app.include_router(api_keys.router, prefix="/api/v1")

# Admin routes (Issue #43 - User management and system-wide stats)
app.include_router(admin.router, prefix="/api/v1")

# Admin Plans routes (Issue #149 - Plan tier management)
app.include_router(admin_plans.router, prefix="/api/v1")

# System Admins routes (Issue #166 - System Admin vs Workspace Admin RBAC)
app.include_router(system_admins.router)

# Workspace routes (Issue #115 - Workspace-level stats)
app.include_router(workspace.router, prefix="/api/v1")

# Workspaces CRUD routes (Issue #115 Phase B - Multi-tenancy)
app.include_router(workspaces.router, prefix="/api/v1/workspaces")

# Workspace Invitation routes (Issue #165 - Team Collaboration)
app.include_router(invitations.router, prefix="/api/v1")

# Workspace Plan routes (Issue #164 - User Management拡張)
app.include_router(workspace_plan.router, prefix="/api/v1")

# Member Credentials routes (Migration 034 - Zero-knowledge credential management)
app.include_router(member_credentials.router)

# Neural Config routes (Issue #107 - Neural Memory configuration)
app.include_router(neural_config.router, prefix="/api/v1")

# Sleep Reports admin routes (Issue #179 - Sleep Report admin UI)
app.include_router(sleep_reports.router, prefix="/api/v1")

# External API Keys routes (Issue #45 - External keys management)
app.include_router(external_keys.router, prefix="/api/v1")

# Config routes (Issue #45 - Configuration management)
app.include_router(config.router, prefix="/api/v1")

# Contexts routes (Issue #82 → #160 - Context-based multi-collection, renamed from projects)
app.include_router(contexts.router, prefix="/api/v1")

# Context Search Config routes (Issue #130 → #160 - Context-scoped search settings, renamed from project)
app.include_router(context_search_config.router)

# MCP API routes (Issue #45 - MCP tools list and status)
app.include_router(mcp.router, prefix="/api/v1")

# Memory routes (Phase 2.2)
app.include_router(memory.router, prefix="/api/v1/memory")

# Attachments routes (Issue #330 - File attachments for memories)
app.include_router(attachments.router, prefix="/api/v1")

# Resource Ingest routes (Issue #238 - Public Context incremental indexing)
app.include_router(resource_ingest.router, prefix="/api/v1")

# Resource Schema routes (Issue #238 - Schema Registry)
app.include_router(resource_schema.router, prefix="/api/v1")

# Resource Token routes (Issue #242 - Resource Token Management)
app.include_router(resource_tokens.router, prefix="/api/v1")

# Public Search routes (Issue #238 - Public REST API)
app.include_router(public_search.router, prefix="/api/v1")

# Graph routes (Issue #46 Phase 5 - Neural Memory stats)
app.include_router(graph.router, prefix="/api/v1")

# Usage routes (Issue #48 - Usage Statistics and Plan Limits)
app.include_router(usage.router, prefix="/api/v1")

# System routes
app.include_router(system.router, prefix="/api/v1")

# Well-known endpoints (OAuth2 metadata)
app.include_router(well_known.router, prefix="/.well-known")

# Billing Plugin (OSS: disabled by default)
# When enabled, registers self-service plan management routes (Stripe integration)
from plugins.billing import is_billing_enabled  # noqa: E402

if is_billing_enabled():
    from plugins.billing.routes import router as billing_router

    app.include_router(billing_router, tags=["billing"])
    logger.info("billing_plugin_enabled")
else:
    logger.info("billing_plugin_disabled (plan changes are admin-only)")

# MCP Remote Server (Phase 3)
# Custom route to avoid 307 redirect from app.mount
from mcp_server.transport import mcp_asgi_app  # noqa: E402


@app.api_route(
    "/mcp/w/{workspace_id}{path:path}",
    methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"],
    include_in_schema=False,
)
async def mcp_workspace_handler(request: Request, workspace_id: str, path: str):
    """Handle workspace-scoped MCP requests.

    Issue #XXX: Allows Claude Desktop to register multiple workspaces
    with unique URLs (same URL limitation workaround).

    URL format: /mcp/w/{workspace_id} or /mcp/w/{workspace_id}/
    The workspace_id is validated against the API key's workspace scope.
    """
    scope = dict(request.scope)
    # Store workspace_id from URL for validation in transport layer
    scope["workspace_id_from_url"] = workspace_id
    # Normalize path to /mcp/ (strip workspace prefix)
    if not path:
        scope["path"] = "/mcp/"
    else:
        scope["path"] = f"/mcp{path}"

    return await mcp_asgi_app(scope, request.receive, request._send)


@app.api_route(
    "/mcp{path:path}",
    methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"],
    include_in_schema=False,
)
async def mcp_handler(request: Request, path: str):
    """Handle MCP requests without 307 redirect.

    FastAPI's app.mount() enforces trailing slash redirect, which causes
    issues with MCP clients. This custom handler allows both /mcp and /mcp/
    to work seamlessly.
    """
    scope = dict(request.scope)
    # Normalize path: /mcp → /mcp/ (FastAPI strips /mcp prefix, leaving empty path)
    if not path:
        scope["path"] = "/mcp/"
    else:
        scope["path"] = f"/mcp{path}"

    return await mcp_asgi_app(scope, request.receive, request._send)


# API documentation endpoint (Issue #39)
# Serve generated OpenAPI spec from file
from pathlib import Path  # noqa: E402

from fastapi.responses import FileResponse  # noqa: E402


@app.get("/static/api/openapi.json", include_in_schema=False)
async def serve_openapi_spec():
    """Serve the generated OpenAPI specification file."""
    openapi_file = Path(__file__).parent.parent.parent / "public" / "api" / "openapi.json"
    if openapi_file.exists():
        return FileResponse(openapi_file, media_type="application/json")
    # Fallback: generate on-the-fly from FastAPI
    return JSONResponse(app.openapi())


@app.get("/")
async def root():
    """Root endpoint."""
    return {
        "name": "Kagura Memory Cloud",
        "version": "0.1.0",
        "description": "Remote MCP Server + Web Management",
        "status": "ok",
    }


@app.get("/health")
async def health():
    """Health check endpoint."""
    return {"status": "ok"}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "api.main:app",
        host=settings.api_host,
        port=settings.api_port,
        reload=True,
        log_level=settings.log_level.lower(),
    )
