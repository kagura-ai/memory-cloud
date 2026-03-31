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
echo "==> Step 1/4: Configure environment"
cd backend && python3 -m src.cli.setup_env
cd ..

# Step 2: Start Docker services
echo ""
echo "==> Step 2/4: Start Docker services"
docker compose up -d
echo "Waiting for services to be healthy..."
sleep 5
docker compose ps

# Step 3: Run migrations
echo ""
echo "==> Step 3/4: Run database migrations"
cd backend && alembic upgrade head
cd ..

# Step 4: Create admin
echo ""
echo "==> Step 4/4: Create admin account"
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
