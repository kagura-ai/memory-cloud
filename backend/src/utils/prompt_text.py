"""Untrusted text placed in an LLM prompt (#2020, #2144).

Stored memory text and a caller's query are untrusted. A chat model's
tokenizer turns some spellings into control tokens whether or not they are
flagged special, so text containing ``<|im_end|>\\n<|im_start|>assistant``
could close the slot it was placed in and write the model's answer itself
(the #2020 reranker finding). :func:`neutralise_control_tokens` breaks every
such spelling before the text enters a prompt; the reranker and the recall
selector share it.
"""

from __future__ import annotations

import re

# Text the Qwen3 tokenizer turns into a control token (#2020 review). Every
# added token of the Qwen3 tokenizer (tokenizer_config.json
# added_tokens_decoder) is matched as ONE token whether or not it is flagged
# special. All the "<|...|>" tokens start with "<|" (ChatML's <|im_start|> /
# <|im_end|> among them); the rest are the tag-shaped ones below. Broken by a
# space after "<": the text stays readable, the token never forms, and a
# single non-overlapping pass cannot reassemble one ("<<|" -> "<< |").
_CONTROL_TOKEN_RE = re.compile(r"<(?=\|)|<(?=/?(?:think|tool_call|tool_response)>)")


def neutralise_control_tokens(text: str, max_chars: int | None = None) -> str:
    """Break every control-token spelling in caller-supplied text.

    Args:
        text: Untrusted text (a memory's summary or content, a query).
        max_chars: When given, the text is cut to this many characters FIRST,
            so a token split by the cut ("<|im_") is no token at all and the
            neutralised text is never cut again.

    Returns:
        The text with a space inserted after each ``<`` that would start a
        control token; otherwise unchanged.
    """
    if max_chars is not None:
        text = text[:max_chars]
    return _CONTROL_TOKEN_RE.sub("< ", text)
