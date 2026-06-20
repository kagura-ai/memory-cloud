# =============================================================================
# Kagura Memory Cloud — Caddy reverse proxy (production, blue-green template)
# =============================================================================
# This is the SOURCE TEMPLATE — edit THIS file (e.g. to set your domain).
# scripts/deploy.sh renders it to ./Caddyfile via envsubst. That generated
# ./Caddyfile is gitignored and must NOT be edited or committed directly.
#
# Placeholder:
#   ${API_UPSTREAM} — replaced with "api-blue" or "api-green" at deploy time
#
# TLS is terminated here using a Cloudflare Origin CA certificate. Cloudflare
# sits in front in "Full (strict)" mode, so client → Cloudflare and
# Cloudflare → Caddy are both TLS, but the origin cert doesn't need to be
# publicly trusted — only trusted by Cloudflare, which Cloudflare Origin CA
# certs are by construction.
#
# Place the cert and key at:
#   /etc/caddy/origin-ca/cert.pem
#   /etc/caddy/origin-ca/key.pem
# (These are mounted into the caddy container from the host.)
# =============================================================================

{
	# Use a safe email if ACME is ever reached, but with explicit Origin CA
	# TLS below, Caddy shouldn't attempt ACME.
	email admin@example.com

	# Production sizing
	servers {
		max_header_size 16KB
	}
}

# Replace memory.kagura-ai.com with your domain in this template file.
# scripts/deploy.sh generates Caddyfile from this via envsubst.
memory.kagura-ai.com {
	tls /etc/caddy/origin-ca/cert.pem /etc/caddy/origin-ca/key.pem

	encode zstd gzip

	# /api/v1/workers/* is internal-only — must precede the /api/* catch-all.
	# The co-resident ai-worker reaches this via the Docker internal network
	# directly on the API container port; it must never be reachable through
	# public ingress. Return 404 (not 403) to avoid confirming the path exists.
	handle /api/v1/workers* {
		respond 404
	}

	# /internal/* is the billing-service entitlement-push surface (#954): it
	# writes plan tier + addon quota, authenticated only by BILLING_SERVICE_TOKEN.
	# Like /api/v1/workers/*, it is reachable only over the internal Docker
	# network and must never be served through public ingress — block it at the
	# edge as defense-in-depth on top of the token. 404 (not 403) so the path is
	# not confirmed. deploy.sh `verify_internal_blocked` fails the deploy if this
	# block ever goes missing.
	handle /internal* {
		respond 404
	}

	# -------------------------------------------------------------------------
	# Backend API (FastAPI on :8080) — blue-green upstream
	# -------------------------------------------------------------------------
	handle /api/* {
		reverse_proxy ${API_UPSTREAM}:8080
	}

	handle /health {
		reverse_proxy ${API_UPSTREAM}:8080
	}

	# /readiness is intentionally NOT exposed through Caddy.
	# deploy.sh probes it from inside the container via `docker compose exec`.

	handle /docs* {
		reverse_proxy ${API_UPSTREAM}:8080
	}

	handle /redoc* {
		reverse_proxy ${API_UPSTREAM}:8080
	}

	handle /openapi.json {
		reverse_proxy ${API_UPSTREAM}:8080
	}

	handle /.well-known/* {
		reverse_proxy ${API_UPSTREAM}:8080
	}

	handle /oauth/* {
		reverse_proxy ${API_UPSTREAM}:8080
	}

	# -------------------------------------------------------------------------
	# MCP Streamable HTTP / SSE
	#   flush_interval -1 is CRITICAL for SSE: without it Caddy buffers chunks
	#   and the client never sees events until the stream closes.
	# -------------------------------------------------------------------------
	handle /mcp* {
		reverse_proxy ${API_UPSTREAM}:8080 {
			flush_interval -1
			transport http {
				read_timeout  24h
				write_timeout 24h
			}
		}
	}

	# -------------------------------------------------------------------------
	# Frontend (Next.js standalone on :3000) — catch-all
	# -------------------------------------------------------------------------
	handle {
		reverse_proxy web:3000
	}

	log {
		output stdout
		format json
	}
}

# =============================================================================
# Extension point for sibling services (one-time, do not remove)
# =============================================================================
# Sibling services co-resident on this VM (e.g. kagura-memory-ai-worker's
# webhook receiver at aw.kagura-ai.com) plug their own top-level vhost blocks
# in here by dropping a `*.caddy` file into /opt/kagura-caddy-extra/ on the
# host. That directory is bind-mounted read-only into the caddy container
# (see docker-compose.prod.yml). This import sits at TOP LEVEL — outside the
# site block above — so imported files can define full vhost blocks of their
# own (e.g. `aw.kagura-ai.com { ... }`), not just snippets.
#
# Glob safety: Caddy tolerates this import matching zero files, so the initial
# (empty) state is valid. The literal path survives deploy.sh's envsubst, which
# uses an explicit allow-list (`envsubst '${API_UPSTREAM}'`) — no interpolation.
#
# Applying a NEW *.caddy file (or this mount on first rollout) takes more than
# `caddy reload` — see README.md "Caddy extension point" for the exact
# recreate-vs-reload procedure.
import /opt/kagura-caddy-extra/*.caddy
