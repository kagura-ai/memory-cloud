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

# Step 4: Run migrations BEFORE API starts
# (API auto-creates tables on startup, which conflicts with alembic)
echo ""
echo "==> Step 4/5: Run database migrations"
cd backend && PYTHONPATH=src alembic upgrade head
cd ..

# Start API and web after migrations
echo ""
echo "Starting API and web..."
docker compose up -d
for i in $(seq 1 30); do
  api_health=$(docker compose ps api --format '{{.Health}}' 2>/dev/null || echo "")
  if [ "$api_health" = "healthy" ]; then break; fi
  sleep 1
done
docker compose ps

# Step 5: Create admin
echo ""
echo "==> Step 5/5: Create admin account"
echo "(Interactive — will prompt for login ID, password, and MFA)"
echo ""
cd backend && python3 -m src.cli.create_admin
cd ..

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
