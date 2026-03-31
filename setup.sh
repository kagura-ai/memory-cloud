#!/bin/bash
# Kagura Memory Cloud - Quick Setup
#
# Usage:
#   chmod +x setup.sh && ./setup.sh
#
# What this does:
#   1. Configure .env.local (generate secrets, prompt for API keys)
#   2. Start Docker services
#   3. Run database migrations
#   4. Create admin account (interactive)

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
cd backend && pip install -q -e ".[dev]" 2>&1 | tail -1
cd ..

# Step 3: Start Docker services
echo ""
echo "==> Step 3/5: Start Docker services"
docker compose up -d
echo "Waiting for services to be healthy..."
# Wait for postgres and qdrant to be healthy (up to 60s)
for i in $(seq 1 60); do
  pg_healthy=$(docker compose ps postgres --format json 2>/dev/null | grep -c '"healthy"' || echo 0)
  qd_healthy=$(docker compose ps qdrant --format json 2>/dev/null | grep -c '"healthy"' || echo 0)
  if [ "$pg_healthy" -ge 1 ] && [ "$qd_healthy" -ge 1 ]; then
    break
  fi
  sleep 1
done
docker compose ps

# Step 4: Run migrations
echo ""
echo "==> Step 4/5: Run database migrations"
cd backend && alembic upgrade head
cd ..

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
