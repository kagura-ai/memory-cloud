#!/bin/bash
# Kagura Memory Cloud - Quick Setup
#
# Usage:
#   chmod +x setup.sh && ./setup.sh
#
# What this does:
#   1. Configure .env.local (generate secrets, prompt for API keys)
#   2. Install Python dependencies
#   3. Start Docker services
#   4. Run database migrations
#   5. Create admin account (interactive)

set -euo pipefail

echo ""
echo "  ██╗  ██╗ █████╗  ██████╗ ██╗   ██╗██████╗  █████╗ "
echo "  ██║ ██╔╝██╔══██╗██╔════╝ ██║   ██║██╔══██╗██╔══██╗"
echo "  █████╔╝ ███████║██║  ███╗██║   ██║██████╔╝███████║"
echo "  ██╔═██╗ ██╔══██║██║   ██║██║   ██║██╔══██╗██╔══██║"
echo "  ██║  ██╗██║  ██║╚██████╔╝╚██████╔╝██║  ██║██║  ██║"
echo "  ╚═╝  ╚═╝╚═╝  ╚═╝ ╚═════╝  ╚═════╝ ╚═╝  ╚═╝╚═╝  ╚═╝"
echo ""
echo "  Memory Cloud — Universal AI Memory Platform"
echo "  Give your AI assistants persistent memory."
echo ""
echo "=================================================="

# Check prerequisites
echo ""
echo "Checking prerequisites..."
command -v docker >/dev/null 2>&1 || { echo "✗ Docker not found. Install Docker first."; exit 1; }
command -v python3 >/dev/null 2>&1 || { echo "✗ Python 3 not found. Install Python 3.11+."; exit 1; }
docker compose version >/dev/null 2>&1 || { echo "✗ Docker Compose not found."; exit 1; }
echo "✓ Prerequisites OK"

# Step 1: Configure environment
echo ""
echo "==> Step 1/5: Configure environment"
cd backend && python3 -m src.cli.setup_env
cd ..

# Step 2: Install Python dependencies
echo ""
echo "==> Step 2/5: Install Python dependencies"
cd backend && pip install -e ".[dev]"
cd ..
echo "Installing kagura-memory SDK..."
pip install kagura-memory

# Check for port conflicts before starting services
echo ""
echo "Checking for port conflicts..."
for port in 5432 6333 6334 6379 8080; do
  # Exclude our own kagura containers
  conflict=$(docker ps --format '{{.Names}} {{.Ports}}' 2>/dev/null | grep -v "^kagura-" | grep ":${port}->" || true)
  if [ -n "$conflict" ]; then
    echo "✗ Port $port is already in use by another container:"
    echo "  $conflict"
    echo "  Stop it first: docker stop <container-name>"
    exit 1
  fi
done
echo "✓ No port conflicts"

# Step 3: Start infrastructure (postgres, qdrant, redis — NOT API yet)
echo ""
echo "==> Step 3/5: Start Docker services"
docker compose up -d postgres qdrant redis
echo "Waiting for infrastructure to be healthy..."
for i in $(seq 1 60); do
  pg_health=$(docker compose ps postgres --format '{{.Health}}' 2>/dev/null || echo "")
  qd_health=$(docker compose ps qdrant --format '{{.Health}}' 2>/dev/null || echo "")
  if [ "$pg_health" = "healthy" ] && [ "$qd_health" = "healthy" ]; then
    break
  fi
  sleep 1
done

# Start API and web
echo ""
echo "Starting API and web..."
docker compose up -d api web
echo "Waiting for API to be healthy..."
for i in $(seq 1 30); do
  api_health=$(docker compose ps api --format '{{.Health}}' 2>/dev/null || echo "")
  if [ "$api_health" = "healthy" ]; then break; fi
  sleep 1
done

# Step 4: Run migrations INSIDE the API container
# (Host Python may connect to a different DB instance due to Docker networking)
echo ""
echo "==> Step 4/5: Run database migrations"
docker compose exec api bash -c "cd /app && PYTHONPATH=src alembic upgrade head"
docker compose ps

# Step 5: Create admin (inside container for consistent DB connection)
# MCP_JSON_DIR=/app overrides .mcp.json output path for container environment
echo ""
echo "==> Step 5/5: Create admin account"
echo "(Interactive — will prompt for login ID, password, and MFA)"
echo ""
docker compose exec -e MCP_JSON_DIR=/app -it api python3 -m src.cli.create_admin
# Copy .mcp.json from container to project root for Claude Code
if docker compose cp api:/app/.mcp.json ./.mcp.json 2>/dev/null; then
  echo "✓ .mcp.json copied to project root"
else
  echo "⚠ .mcp.json not found in container — run create_admin manually"
fi

echo ""
echo "=================================================="
echo " Setup complete!"
echo ""
echo " Backend API:  http://localhost:8080"
echo " Frontend UI:  http://localhost:3000"
echo " API docs:     http://localhost:8080/redoc"
echo ""
echo " To use with Claude Code:"
echo "   Restart Claude Code to load .mcp.json"
echo "   then use remember/recall tools"
echo "=================================================="
