# Chunking Best Practices for Kagura Memory Cloud

## Overview

Kagura Memory Cloud uses a **3-layer architecture** where the **summary** field (10-500 characters) is optimized for semantic search using embedding vectors. This design eliminates the need for server-side automatic chunking, giving you full control over how your memories are organized.

This guide helps you make the most of this architecture by adopting effective **client-side chunking strategies**.

---

## The 3-Layer Architecture

```
┌─────────────────────────────────────┐
│  Layer 1: Summary (10-500 chars)   │  ← Embedded for semantic search
│  - Concise, searchable overview    │  ← Primary search target
│  - Optimal: 100-250 characters     │
└─────────────────────────────────────┘
            ↓
┌─────────────────────────────────────┐
│  Layer 2: Context Summary (≤2000)  │  ← NOT embedded (cost optimization)
│  - Explains why & how to use       │  ← BM25 full-text searchable
│  - Rich contextual information     │
└─────────────────────────────────────┘
            ↓
┌─────────────────────────────────────┐
│  Layer 3: Content + Details        │  ← Full document/code
│  - Complete data, code snippets    │  ← Retrieved via reference()
│  - Unbounded length                │
└─────────────────────────────────────┘
```

**Key Insight**: Layer 1 (summary) is already within the optimal chunk size range (100-500 characters ≈ 100-500 tokens) recommended by RAG best practices (2025). No automatic chunking needed!

---

## When to Create Multiple Memories

### ✅ DO create multiple memories for:

1. **Long documents (>2000 characters)**
   - Split by logical sections (introduction, methodology, results)
   - Each memory = one coherent topic/idea

2. **Code files (>500 lines)**
   - One memory per module/class/function
   - Clear, semantic summaries (not "Part 1/5")

3. **Chat conversations with topic shifts**
   - One memory per distinct topic
   - Use tags to link related conversations

4. **Research papers or technical articles**
   - One memory per section (abstract, methods, results, conclusion)
   - Link via tags and context

### ❌ DON'T create memories like:

- ❌ "Document part 1/3", "Document part 2/3", "Document part 3/3"
- ❌ "auth.py file" with 5000 lines in content
- ❌ Generic summaries like "Meeting notes" or "Code snippet"

---

## Chunking Strategies by Use Case

### 1. Code Files

**Bad**:
```python
remember(
    summary="auth.py file",
    content=<entire 5000 line file>,
    type="code"
)
```

**Good**:
```python
# Chunk 1: OAuth2 login
remember(
    summary="OAuth2 login implementation using FastAPI",
    content="""
def oauth2_login(provider: str):
    # Implementation
    ...
""",
    tags=["auth", "oauth2", "login"],
    context={"file": "backend/src/auth.py", "lines": "10-45"},
    type="code"
)

# Chunk 2: JWT validation
remember(
    summary="JWT token validation with expiry check",
    content="""
def validate_jwt(token: str) -> dict:
    # Validation logic
    ...
""",
    tags=["auth", "jwt", "validation"],
    context={"file": "backend/src/auth.py", "lines": "47-82"},
    type="code"
)

# Chunk 3: Session management
remember(
    summary="Session management utilities for Redis",
    content="""
class SessionManager:
    # Session handling
    ...
""",
    tags=["auth", "session", "redis"],
    context={"file": "backend/src/auth.py", "lines": "84-150"},
    type="code"
)
```

**Why this works**:
- Each memory has a **semantic summary** describing its purpose
- Common tags (`["auth"]`) link related memories
- `context` provides file location for reference
- recall("JWT validation") will find the right memory

---

### 2. Technical Documentation

**Bad**:
```python
remember(
    summary="RAG paper",
    content=<entire 20-page paper>,
    type="note"
)
```

**Good**:
```python
# Introduction section
remember(
    summary="RAG systems: Introduction and motivation",
    context_summary="Explains why RAG (Retrieval-Augmented Generation) is needed for LLMs. Covers limitations of pure parametric models and benefits of external knowledge retrieval.",
    content=<introduction section text>,
    tags=["RAG", "LLM", "paper-2024"],
    context={"paper_id": "rag-2024", "section": "intro", "pages": "1-3"},
    importance=0.7,
    type="learning"
)

# Methodology section
remember(
    summary="RAG systems: Hybrid search methodology",
    context_summary="Describes the hybrid search approach combining semantic (60%) and BM25 (40%) retrieval. Includes chunking strategies and overlap ratios.",
    content=<methodology section text>,
    tags=["RAG", "hybrid-search", "paper-2024"],
    context={"paper_id": "rag-2024", "section": "methods", "pages": "4-8"},
    importance=0.9,  # Critical implementation details
    type="learning"
)

# Results section
remember(
    summary="RAG systems: Benchmark results on FinanceBench",
    context_summary="Shows 15% overlap achieves best accuracy/cost balance. Compares fixed-size vs semantic chunking strategies.",
    content=<results section text>,
    tags=["RAG", "benchmarks", "paper-2024"],
    context={"paper_id": "rag-2024", "section": "results", "pages": "9-15"},
    importance=0.8,
    type="learning"
)
```

**Why this works**:
- Each section = one memory with focused summary
- `context_summary` provides overlap (mentions adjacent sections)
- Common `paper_id` in context links all sections
- recall("hybrid search") or recall("chunking strategies") finds relevant memories

---

### 3. Meeting Notes or Conversations

**Bad**:
```python
remember(
    summary="Team meeting",
    content=<2 hours of conversation>,
    type="note"
)
```

**Good**:
```python
# Topic 1: Bug discussion
remember(
    summary="Auth bug: JWT expiry causing 401 errors",
    context_summary="Team discussed JWT token expiry issue. Decided to implement refresh token rotation and add clock skew tolerance.",
    content="[Full conversation about the bug]",
    tags=["bug", "auth", "jwt", "meeting-2024-12"],
    context={"meeting_date": "2024-12-04", "topic": "auth-bug"},
    importance=0.9,
    type="bug-fix"
)

# Topic 2: Feature planning
remember(
    summary="New feature: Dark mode for Web UI",
    context_summary="Planned dark mode implementation. Assigned to Alice, deadline Dec 15. Will use Tailwind dark: classes.",
    content="[Full planning discussion]",
    tags=["feature", "ui", "dark-mode", "meeting-2024-12"],
    context={"meeting_date": "2024-12-04", "topic": "dark-mode", "assignee": "Alice"},
    importance=0.6,
    type="feature"
)

# Topic 3: Performance review
remember(
    summary="Performance: Qdrant search latency optimization",
    context_summary="Reviewed slow search queries. Decided to increase fetch_factor from 3 to 5 and enable reranking for critical searches.",
    content="[Performance metrics and decisions]",
    tags=["performance", "qdrant", "search", "meeting-2024-12"],
    context={"meeting_date": "2024-12-04", "topic": "performance"},
    importance=0.7,
    type="decision"
)
```

**Why this works**:
- Each topic = one memory (natural semantic boundaries)
- Summaries describe the **outcome/decision**, not "we discussed X"
- Tags link related topics across meetings
- Future recall("auth bug") or recall("JWT 401") finds this memory

---

## Linking Related Memories

### Using Tags

Tags are the primary way to link related memories:

```python
# Parent document metadata
tags = ["project-x", "api", "v2"]

# All related memories share these tags
remember(summary="API v2: Authentication endpoint", tags=tags + ["auth"], ...)
remember(summary="API v2: User CRUD endpoints", tags=tags + ["users"], ...)
remember(summary="API v2: Rate limiting implementation", tags=tags + ["rate-limit"], ...)
```

Then search with filters:
```python
recall("API design", filters={"tags": ["project-x", "v2"]})
```

### Using Context Object

For structured relationships:

```python
parent_context = {
    "project": "memory-cloud",
    "document_id": "architecture-2024",
    "version": "1.0"
}

remember(
    summary="Architecture: 3-layer memory model",
    context={**parent_context, "section": "data-model"},
    ...
)

remember(
    summary="Architecture: Hybrid search implementation",
    context={**parent_context, "section": "search"},
    ...
)
```

### Context Overlap Technique

Include 50-100 characters of adjacent context in `context_summary`:

```python
# Section 1
remember(
    summary="RAG Introduction: Problem statement",
    context_summary="LLMs have knowledge cutoff limitations. Next section covers hybrid search as a solution.",
    content=<intro text>,
    ...
)

# Section 2 (includes overlap)
remember(
    summary="RAG Methodology: Hybrid search approach",
    context_summary="Building on the knowledge cutoff problem discussed in intro, we propose combining semantic + BM25 search...",
    content=<methodology text>,
    ...
)
```

This overlap helps:
- Preserve context at chunk boundaries
- Improve recall when query spans multiple chunks
- Mimic 15% overlap recommended by industry (via natural language)

---

## Anti-Patterns to Avoid

### ❌ Anti-Pattern 1: Non-semantic Chunk IDs

**Bad**:
```python
remember(summary="Document chunk 1 of 10", content=..., tags=["doc"])
remember(summary="Document chunk 2 of 10", content=..., tags=["doc"])
remember(summary="Document chunk 3 of 10", content=..., tags=["doc"])
```

**Problem**: Summaries are meaningless for search. recall("authentication") won't match "chunk 2" even if it contains auth code.

**Good**:
```python
remember(summary="OAuth2 authentication flow", content=..., tags=["doc", "auth"])
remember(summary="Database connection pooling", content=..., tags=["doc", "db"])
remember(summary="API rate limiting logic", content=..., tags=["doc", "api"])
```

---

### ❌ Anti-Pattern 2: Storing Entire Files

**Bad**:
```python
remember(
    summary="src/api/routes/memory.py",
    content=<entire 2000-line file>,
    type="code"
)
```

**Problem**:
- Single embedding can't capture all semantic concepts in 2000 lines
- Search quality degrades (diluted relevance)
- Violates optimal chunk size (100-500 chars summary)

**Good**: Split by function/class (see "Code Files" section above)

---

### ❌ Anti-Pattern 3: Redundant Information

**Bad**:
```python
# Storing the same information multiple times with different phrasings
remember(summary="How to fix auth errors", content="Use refresh token", ...)
remember(summary="Authentication error solutions", content="Use refresh token", ...)
remember(summary="JWT token expiry fix", content="Use refresh token", ...)
```

**Problem**: Pollutes search results with duplicates

**Good**: Create **one high-quality memory** with comprehensive tags:
```python
remember(
    summary="JWT expiry fix: Refresh token rotation",
    context_summary="Solves 401 authentication errors caused by expired JWT tokens. Implemented refresh token rotation with clock skew handling.",
    content=<detailed solution>,
    tags=["auth", "jwt", "401-error", "refresh-token", "bug-fix"],
    importance=0.9,
    type="bug-fix"
)
```

Then recall("auth errors"), recall("JWT expiry"), or recall("401 fix") all find this memory.

---

## Summary: Optimal Chunking Checklist

Before calling `remember()`, ask yourself:

- [ ] **Is my summary semantic?** (describes what/why, not "part 1")
- [ ] **Is summary length optimal?** (100-250 chars ideal, max 500)
- [ ] **Does one memory = one concept?** (not mixing multiple ideas)
- [ ] **Have I added relevant tags?** (for linking and filtering)
- [ ] **Is context provided?** (file path, section, project, etc.)
- [ ] **Is importance scored?** (0.9-1.0 for critical, 0.5 for reference)
- [ ] **Is type semantic?** (code, bug-fix, decision, feature, etc.)

If you answer "no" to any of these, refine your memory before storing!

---

## RAG Theory Reference (2025)

This guide is based on industry-standard RAG best practices:

| Parameter | Recommended | Range | Source |
|-----------|------------|-------|--------|
| Chunk Size | 512 tokens (~2000 chars) | 400-1024 tokens | OpenAI, Microsoft Azure |
| Summary Size | 100-250 chars | 10-500 chars | Kagura optimized |
| Overlap Ratio | 15% | 10-20% | NVIDIA FinanceBench |
| Overlap Method | Context in `context_summary` | Natural language | Kagura design |

**Key Findings from Research**:
- **Fixed-size chunking**: Simple, consistent, works well for most cases
- **Semantic chunking**: 3-5x more expensive (extra embeddings), minimal gain for short summaries
- **15% overlap**: Best accuracy/cost balance (NVIDIA benchmarks)
- **Smaller chunks**: Better precision, less context
- **Larger chunks**: More context, diluted relevance

**Kagura's Design Advantage**:
By enforcing a **10-500 character summary constraint**, Kagura keeps every memory within the optimal chunk size naturally, without needing automatic server-side splitting. You get the benefits of optimal chunking **by design**.

---

## Does Chunk Size Change with Embedding Model?

**No.** Optimal chunk size is **independent of embedding dimensions**.

Whether you use:
- **text-embedding-3-small** (512 dimensions)
- **text-embedding-3-large** (3072 dimensions)

The recommended summary length remains **100-250 characters**.

### Why Same Chunk Size for Different Models?

Higher-dimensional embeddings capture **more semantic information** from the **same text length**. They don't require larger chunks - they extract richer features from focused content.

**Analogy**:
- 512D = Standard resolution camera
- 3072D = High-resolution camera

Both capture the same scene (chunk), but high-resolution shows more detail. You don't need a bigger scene for a better camera.

### Performance Comparison

| Model | Dimensions | Cost/1K tokens | Storage/vector | Optimal Chunk |
|-------|-----------|----------------|----------------|---------------|
| text-embedding-3-small | 512 | $0.00002 | 512 bytes | **100-250 chars** |
| text-embedding-3-large | 3072 | $0.00013 | 3072 bytes | **100-250 chars** ✓ |

**Trade-off**: 6.5x cost + 6x storage for superior semantic precision, but **same chunk strategy**.

### Research Evidence

Industry research (Milvus, LlamaIndex, arXiv 2025) confirms:
- Optimal chunk: 128-512 tokens (applies to ALL embedding models)
- Kagura's 100-250 chars ≈ 25-62 tokens (perfectly aligned)
- Larger chunks with high-D models → diluted relevance, no accuracy gain

**Best practice**: Keep chunks small and focused. Let high-dimensional embeddings capture more nuance from that focused content.

---

## Need Help?

- **Web UI**: Access via your deployment's `/docs` page
- **API Reference**: See `/docs/api-reference.md` for chunking examples
- **GitHub Issues**: https://github.com/kagura-ai/memory-cloud/issues

---

**Happy chunking! 🎯**
