# Architecture Overview

Kagura Memory Cloud is built with a modern, scalable architecture designed for production use.

## System Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                     Client Applications                      │
│  (Claude Desktop, Claude Code, ChatGPT, Web UI, Custom)     │
└─────────────────────────────────────────────────────────────┘
                              ↓
┌─────────────────────────────────────────────────────────────┐
│                    Authentication Layer                      │
│  OAuth2 (Google, GitHub) │ API Keys │ JWT Tokens            │
└─────────────────────────────────────────────────────────────┘
                              ↓
┌──────────────────────┬──────────────────────────────────────┐
│   MCP Server (SSE)   │          REST API (FastAPI)          │
│  - 5 MCP Tools       │  - Memory CRUD                       │
│  - Session Mgmt      │  - OAuth2 endpoints                  │
│  - JSON-RPC          │  - API Key management                │
└──────────────────────┴──────────────────────────────────────┘
                              ↓
┌─────────────────────────────────────────────────────────────┐
│                      Service Layer                           │
│  MemoryService │ SearchService │ EmbeddingService           │
│  GraphService │ NeuralMemoryEngine │ AuthService            │
└─────────────────────────────────────────────────────────────┘
                              ↓
┌─────────────────────────────────────────────────────────────┐
│                    Repository Layer                          │
│  MemoryRepository │ GraphRepository │ UserRepository        │
└─────────────────────────────────────────────────────────────┘
                              ↓
┌──────────────┬──────────────┬──────────────┬───────────────┐
│  PostgreSQL  │   Qdrant     │    Redis     │  External API │
│  - Memories  │  - Vectors   │  - Sessions  │  - OpenAI     │
│  - Users     │  - Full-text │  - Cache     │  - Cohere     │
│  - Graph     │  (1u=1coll)  │  - Rate Lmt  │               │
└──────────────┴──────────────┴──────────────┴───────────────┘
```

## 3-Layer Memory Architecture

### Layer 1: Summary
- **Purpose**: Quick search and retrieval
- **Content**: Concise summary (10-500 characters)
- **Storage**: PostgreSQL + Qdrant vector
- **Use case**: Initial search results

### Layer 2: Context Summary
- **Purpose**: Understanding context
- **Content**: Medium explanation (max 2000 characters)
- **Storage**: PostgreSQL
- **Use case**: Showing search result previews

### Layer 3: Details
- **Purpose**: Complete information
- **Content**: Full content + structured metadata (JSON)
- **Storage**: PostgreSQL
- **Use case**: Detailed view after selection

## Hybrid Search System

```
User Query
    ↓
┌─────────────────────────────┐
│  Query Processing           │
│  - Tokenization             │
│  - OpenAI Embedding         │
└─────────────────────────────┘
    ↓
┌──────────────┬──────────────┐
│  Semantic    │   BM25       │
│  (Vector)    │  (Keyword)   │
│  60% weight  │  40% weight  │
└──────────────┴──────────────┘
    ↓               ↓
Results A       Results B
    └───────┬───────┘
            ↓
    ┌───────────────┐
    │  Fusion       │
    │  (Weighted)   │
    └───────────────┘
            ↓
    ┌───────────────┐
    │  Cohere       │
    │  Reranking    │
    └───────────────┘
            ↓
      Final Results
```

### Search Weights
- **Semantic Search**: 60% (better for meaning)
- **BM25 Full-text**: 40% (better for exact keywords)
- **Reranking**: Cohere multilingual-v3.0

## Neural Memory Engine

### Hebbian Learning
Automatic relationship learning based on co-activation:

```python
# When memories A and B are accessed together
weight(A, B) += learning_rate * activation(A) * activation(B)
```

**Features**:
- Decays over time (forgetting curve)
- Strengthens with repeated co-access
- Creates knowledge graph automatically

### Activation Spreading
Graph-based exploration from seed memory:

```
Seed Memory
    ↓
  [depth=1]
    ├── Related Memory 1 (weight=0.9)
    ├── Related Memory 2 (weight=0.7)
    └── Related Memory 3 (weight=0.5)
         ↓
       [depth=2]
         ├── Sub-related 1 (weight=0.8)
         └── Sub-related 2 (weight=0.6)
```

**Parameters**:
- `depth`: Max hops (default: 2, max: 5)
- `min_weight`: Minimum edge weight (default: 0.5)
- `relation_types`: Filter by relation types

### Unified Scoring
Combines multiple signals:

```python
score = (
    0.4 * semantic_score +
    0.3 * graph_score +
    0.2 * temporal_score +
    0.1 * trust_score
)
```

## Database Design

### PostgreSQL Tables

1. **users** - User accounts
2. **api_keys** - API key management (SHA256 hashed)
3. **external_api_keys** - OpenAI/Cohere keys (Fernet encrypted)
4. **memories** - 3-layer memory storage
5. **graph_memory** - Neural memory relationships (NetworkX JSON)
6. **oauth_clients** - OAuth2 client applications
7. **oauth_authorization_codes** - OAuth2 auth codes
8. **oauth_tokens** - OAuth2 access/refresh tokens

### Qdrant Collections

**Design**: 1 user = 1 collection

- Collection name: `kagura_user_{user_id}`
- Vector size: 512 (OpenAI text-embedding-3-small)
- Distance metric: Cosine
- Tokenizer: Multilingual (auto Japanese support)

**Features**:
- Semantic vector search
- Full-text BM25 search (MatchText)
- Metadata filtering

### Redis Storage

1. **Sessions**: Session-based authentication (7 days TTL)
2. **Cache**: Embedding cache, search results
3. **Rate Limiting**: Per-key request counters

## Security Architecture

### Authentication Flow

```
User → OAuth2 Login → Session Cookie → Access Token
                                    ↓
                            API Key Creation
                                    ↓
                        External API Access
```

### Authorization Levels

1. **Admin**: Full access (user management, system config)
2. **User**: Standard access (own memories, API keys)
3. **Read-only**: View-only access

### Encryption

- **API Keys**: SHA256 hash storage
- **External API Keys**: Fernet symmetric encryption
- **JWT Tokens**: HS256 signing (1 hour expiration)
- **OAuth2 Secrets**: SHA256 hash storage

## Scalability Considerations

### Horizontal Scaling
- **Backend**: Stateless FastAPI (scale with replicas)
- **Frontend**: Next.js static export (CDN-ready)
- **Database**: PostgreSQL connection pooling (asyncpg)
- **Redis**: Cluster mode support

### Performance Optimizations
- **Async I/O**: All database operations async
- **Connection Pooling**: PostgreSQL (20 max), Redis (10 max)
- **Caching**: Redis cache for embeddings (60 min TTL)
- **Batch Processing**: Background tasks with APScheduler

### Resource Usage (per instance)
- **CPU**: 2-4 cores (recommended)
- **Memory**: 4-8 GB (recommended)
- **Storage**: ~100 MB per 1000 memories (PostgreSQL + Qdrant)

## Deployment Architecture

```
┌──────────────────────────────────────┐
│         Load Balancer (GCP)          │
└──────────────────────────────────────┘
              ↓
┌──────────────────────────────────────┐
│      Backend Instances (Docker)      │
│  - your-domain.com (production)      │
│  - localhost:8080 (development)      │
└──────────────────────────────────────┘
              ↓
┌──────────────────────────────────────┐
│     Managed Services (GCP)           │
│  - Cloud SQL (PostgreSQL)            │
│  - Qdrant Cloud                      │
│  - Memorystore (Redis)               │
└──────────────────────────────────────┘
```

## Technology Stack

| Layer | Technology | Version |
|-------|-----------|---------|
| **Backend** | FastAPI | 0.115+ |
| **Database** | PostgreSQL | 15+ |
| **Vector DB** | Qdrant | 1.15+ |
| **Cache** | Redis | 7+ |
| **Frontend** | Next.js | 16 |
| **ORM** | SQLAlchemy | 2.0+ (async) |
| **Auth** | Authlib | 1.3+ |
| **AI** | OpenAI API | Latest |
| **Reranking** | Cohere API | Latest |
| **Graph** | NetworkX | 3.0+ |

## Monitoring & Observability

### Logging
- **Format**: Structured JSON logs (structlog)
- **Levels**: DEBUG, INFO, WARNING, ERROR
- **Destinations**: stdout (Docker), CloudWatch (prod)

### Metrics (Future)
- Request latency (p50, p95, p99)
- Error rates
- Database query performance
- Memory usage per user

### Health Checks
- `/health` - Basic health check
- `/api/v1/info` - Detailed system info
- Database connection status
- Qdrant connection status
- Redis connection status

## Future Enhancements

1. **Multi-region Deployment**: Global CDN + regional databases
2. **Real-time Collaboration**: WebSocket support for shared memories
3. **Advanced Analytics**: User behavior insights, usage patterns
4. **Custom Embeddings**: Fine-tuned models for specific domains
5. **GraphQL API**: Alternative to REST for complex queries
