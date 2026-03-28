# Getting Started

This guide will help you get started with Kagura Memory Cloud.

## Prerequisites

- Docker and Docker Compose
- Python 3.12+ (for local development)
- Node.js 18+ (for frontend development)

## Installation

### 1. Clone the Repository

```bash
git clone https://github.com/kagura-ai/memory-cloud.git
cd memory-cloud
```

### 2. Environment Setup

Create `.env` file in the project root:

```bash
# Database
POSTGRES_USER=kagura
POSTGRES_PASSWORD=your_secure_password
POSTGRES_DB=kagura_memory_cloud

# Qdrant
QDRANT_URL=http://qdrant:6333
QDRANT_API_KEY=your_qdrant_api_key

# Redis
REDIS_URL=redis://redis:6379/0

# OpenAI (for embeddings)
OPENAI_API_KEY=sk-...

# Cohere (for reranking)
COHERE_API_KEY=...

# JWT Secret
JWT_SECRET_KEY=your_random_secret_key_here
```

### 3. Start Services

```bash
docker compose up -d
```

This will start:
- Backend API (port 8000)
- Frontend (port 3000)
- PostgreSQL (port 5432)
- Qdrant (port 6333)
- Redis (port 6379)

### 4. Access the Application

- **Web UI**: http://localhost:3000
- **API Docs**: http://localhost:8000/docs
- **Health Check**: http://localhost:8000/health

## Quick Test

### Create an API Key

1. Navigate to http://localhost:3000
2. Log in with OAuth2 (Google)
3. Go to "API Keys" page
4. Click "Create API Key"
5. Copy and save the key (shown only once)

### Test the API

```bash
export KAGURA_API_KEY="kagura_xxxxxxxxxxxx"

# Remember a memory
curl -X POST http://localhost:8000/api/v1/memory/remember \
  -H "Authorization: Bearer $KAGURA_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "summary": "Test memory",
    "content": "This is a test memory",
    "type": "note",
    "importance": 0.8
  }'

# Recall memories
curl -X POST http://localhost:8000/api/v1/memory/recall \
  -H "Authorization: Bearer $KAGURA_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "query": "test",
    "k": 5
  }'
```

## MCP Integration

### Claude Desktop / Claude Code

Add to your `claude_desktop_config.json` or create `.mcp.json`:

```json
{
  "mcpServers": {
    "kagura": {
      "url": "http://localhost:8080/mcp/w/{workspace_id}",
      "headers": {
        "Authorization": "Bearer YOUR_API_KEY"
      }
    }
  }
}
```

Replace `YOUR_API_KEY` with your API key from the Kagura dashboard.

## Next Steps

- Read the [API Reference](api-reference.md) for detailed API documentation
- Explore [Architecture](architecture.md) to understand the system design
- Check out [Guides](guides/mcp-integration.md) for advanced usage

## Troubleshooting

### Docker containers not starting

```bash
# Check logs
docker compose logs backend

# Restart services
docker compose restart
```

### Authentication errors

If you see "Not authenticated" errors in development:

1. Open http://localhost:3000 in your browser
2. Log in with Google, GitHub, or Azure OAuth
3. After login, your session cookie will be set

For API testing, create an API key from the web UI and use it in the Authorization header:
```bash
curl -H "Authorization: Bearer kagura_your_api_key" http://localhost:8080/api/v1/health
```

### Database migration issues

```bash
# Run migrations manually
docker compose exec backend \
  alembic upgrade head
```

## Support

- **GitHub Issues**: [Report bugs](https://github.com/kagura-ai/memory-cloud/issues)
- **Documentation**: [Full documentation](http://localhost:8080/docs)
