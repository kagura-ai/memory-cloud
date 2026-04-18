"""LLM prompt templates for Sleep Maintenance phases.

Issue #101/#103: All prompts use short labels (A, B, C...) instead of UUIDs
to reduce hallucination risk in batch processing. Each prompt includes an
explicit JSON schema and 1-shot example.

Design notes (academic review):
- Positional bias mitigation: Callers should shuffle batch order before
  substituting into prompts.
- ID confusion mitigation: Short labels mapped back to UUIDs by caller.
- All prompts require JSON-only output via response_format=json_object.
"""

# ============================================================================
# Phase 2: Dedup/Merge
# ============================================================================

DEDUP_JUDGE_SYSTEM = """\
You are a memory deduplication judge. You analyze pairs of memory entries \
and determine if they are duplicates that should be merged.

Rules:
- "duplicate" means the memories convey the same core information, even if \
worded differently.
- If one memory is a strict subset of the other, they are duplicates. \
Keep the more complete one.
- Memories about the same topic but with genuinely different information \
are NOT duplicates.
- When in doubt, mark as "keep_both" — false merges lose information.

You MUST respond with valid JSON only.\
"""

DEDUP_JUDGE_USER = """\
Analyze these memory pairs for duplicates. Each memory has a label (A, B, C...) \
and a summary.

Memories:
{memories}

Pairs to evaluate:
{pairs}

Respond with this exact JSON schema:
{{
  "judgments": [
    {{
      "pair": ["A", "B"],
      "verdict": "merge" | "keep_both",
      "winner": "A" | "B" | null,
      "confidence": 0.0-1.0,
      "reason": "brief explanation"
    }}
  ]
}}

Example:
{{
  "judgments": [
    {{
      "pair": ["A", "B"],
      "verdict": "merge",
      "winner": "A",
      "confidence": 0.92,
      "reason": "B is a subset of A with identical information"
    }}
  ]
}}\
"""

# ============================================================================
# Phase 1: Edge Discovery
# ============================================================================

# Bump on every edit to EDGE_DISCOVERY_SYSTEM or EDGE_DISCOVERY_USER so
# sleep_reports.edge_discovery_result.details can distinguish runs that used
# different prompts.
# v2 (#373): added IMPORTANT directive forbidding pair-order flip for directed
# edge_types and a depends_on example so the LLM can learn the directed pattern.
EDGE_DISCOVERY_PROMPT_REVISION = "v2"

EDGE_DISCOVERY_SYSTEM = """\
You are a knowledge graph edge discovery agent. You analyze pairs of memory \
entries and determine if they are semantically related and should be connected \
in a knowledge graph.

Rules:
- "related" means the memories share a meaningful conceptual connection \
(causal, topical, temporal, or procedural).
- Superficial similarity (e.g., both mention "Python") is NOT sufficient.
- The relationship must be specific and nameable.
- Assign edge_type from: "related_to", "depends_on", "learned_from".

You MUST respond with valid JSON only.\
"""

EDGE_DISCOVERY_USER = """\
Analyze these memory pairs for meaningful relationships.

Memories:
{memories}

Pairs to evaluate (with cosine similarity scores):
{pairs}

IMPORTANT: For directed edge types ("depends_on", "learned_from"), preserve
the input pair order. The first element of "pair" must remain the first
element you received. Direction is encoded by input order; do not flip.
For "related_to" (undirected), order does not matter.

Respond with this exact JSON schema:
{{
  "edges": [
    {{
      "pair": ["A", "B"],
      "related": true | false,
      "edge_type": "related_to" | "depends_on" | "learned_from",
      "confidence": 0.0-1.0,
      "reason": "brief explanation of the relationship"
    }}
  ]
}}

Example:
{{
  "edges": [
    {{
      "pair": ["A", "B"],
      "related": true,
      "edge_type": "related_to",
      "confidence": 0.85,
      "reason": "B describes the implementation of the pattern introduced in A"
    }},
    {{
      "pair": ["C", "D"],
      "related": true,
      "edge_type": "depends_on",
      "confidence": 0.78,
      "reason": "C builds on the abstraction defined in D (C depends_on D)"
    }}
  ]
}}\
"""

# ============================================================================
# Phase 3: Importance Re-evaluation
# ============================================================================

IMPORTANCE_REEVAL_SYSTEM = """\
You are a memory importance evaluator. You assess the long-term value of \
memory entries based on their content, type, and usage patterns.

Scoring guide (0.0-1.0):
- 0.9-1.0: Critical decisions, architecture choices, security fixes
- 0.7-0.8: Useful patterns, important learnings, recurring solutions
- 0.4-0.6: General notes, routine information
- 0.1-0.3: Temporary context, outdated information, trivial details

Consider:
- Is this information likely to be needed again?
- Would losing this memory cause problems?
- Has the information become outdated?
- How specific and actionable is it?

You MUST respond with valid JSON only.\
"""

IMPORTANCE_REEVAL_USER = """\
Re-evaluate the importance of these memories based on their content and metadata.

Memories:
{memories}

Respond with this exact JSON schema:
{{
  "scores": [
    {{
      "label": "A",
      "importance": 0.0-1.0,
      "reason": "brief justification"
    }}
  ]
}}

Example:
{{
  "scores": [
    {{
      "label": "A",
      "importance": 0.85,
      "reason": "Documents a critical architecture decision that affects all future work"
    }}
  ]
}}\
"""

# ============================================================================
# Phase 4: Consolidation (Working → Persistent promotion)
# ============================================================================

CONSOLIDATION_JUDGE_SYSTEM = """\
You are a memory consolidation judge. You decide whether working (short-term) \
memories should be promoted to persistent (long-term) storage.

Promotion criteria:
- The memory contains durable knowledge (not ephemeral context)
- The information is likely to be useful beyond the current session
- The memory is well-formed and self-contained

Archive criteria:
- The memory is purely ephemeral (session state, temporary debugging)
- The information has been superseded by newer memories
- The memory is poorly formed or lacks actionable content

When uncertain, prefer "keep" — premature promotion clutters long-term memory, \
but premature archival loses information permanently.

You MUST respond with valid JSON only.\
"""

CONSOLIDATION_JUDGE_USER = """\
Evaluate these working memories for consolidation.

Memories:
{memories}

Respond with this exact JSON schema:
{{
  "decisions": [
    {{
      "label": "A",
      "action": "promote" | "keep" | "archive",
      "reason": "brief justification"
    }}
  ]
}}

Example:
{{
  "decisions": [
    {{
      "label": "A",
      "action": "promote",
      "reason": "Contains a reusable pattern for error handling that will be needed again"
    }}
  ]
}}\
"""
