"""LLM prompt strings for analysis cluster labeling (Stage [G]).

Mirrors ``services/sleep/prompts.py`` style: module-level constants,
no Python string interpolation in the constants themselves —
templates use ``{name}`` placeholders that the labeler fills with
``str.format(**kwargs)``.

The labeler prompt is single-shot per cluster: 5 representative
memory summaries → one ``{label, description, label_confidence}``
JSON object. The system prompt enforces:

- Output MUST be JSON object only (matched by
  ``LLMService.complete_json``'s ``response_format=json_object``).
- Label is 1–3 words ("noun phrase"). Long labels make the scatter
  legend unreadable per the prototype's UX expectation.
- Description is one sentence (≤ 25 words).
- ``label_confidence`` is in [0, 1] — the UI uses this to gate weak
  clusters (the F4 #497 modal greys out the cluster row if < 0.5).

The user prompt embeds the 5 representatives as a bulleted list of
their summaries (Layer 1 only — never Layer 3 content; that would
inflate token cost and risk leaking secrets stored in memory bodies).
"""

from __future__ import annotations

CLUSTER_LABEL_SYSTEM = """You are an expert at giving short, descriptive labels to clusters of memories.

Given 5 representative memory summaries that share a common theme, output a single JSON object:

{
  "label": "<1-3 word noun phrase>",
  "description": "<one sentence, max 25 words>",
  "label_confidence": <float in [0, 1]>
}

Constraints:
- "label" MUST be 1 to 3 words. No verbs, no full sentences. Use a noun phrase.
- "description" is one sentence describing what unifies these memories.
- "label_confidence" is your subjective confidence the theme is coherent: 1.0 = obvious shared theme, 0.0 = no detectable theme.
- Output ONLY the JSON object. No prose before or after.
"""

CLUSTER_LABEL_USER = """Cluster representatives:

{representatives}

Output the JSON object now."""
