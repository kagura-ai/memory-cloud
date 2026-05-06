"""RFC 7231 / RFC 6838 media-type helpers.

Single source of truth for the type/subtype shape regex used by both
``FileStorageService.reserve_upload`` (validates the upload's declared
``content_type``) and ``Settings._validate_allowed_file_content_types``
(validates the operator-supplied allow-list at boot). Sharing the regex
prevents drift between request-time rejection and boot-time misconfig
detection.
"""

import re

# RFC 7231 type/subtype shape: token "/" token, where token is the RFC 7230
# tchar set (alphanumerics + a small symbol set). Compare on the lowercased
# bare type/subtype, so the regex only accepts the lowercase range — RFC 7230
# token is technically case-insensitive but ``normalize_media_type`` already
# lowercases the input.
MEDIA_TYPE_RE = re.compile(r"^[a-z0-9!#$%&'*+\-.^_`|~]+/[a-z0-9!#$%&'*+\-.^_`|~]+$")


def normalize_media_type(value: str) -> str:
    """Strip RFC 7231 parameters and lowercase the bare type/subtype.

    Returns "" when the input is empty/whitespace-only after stripping.
    Use ``MEDIA_TYPE_RE.match(...)`` on the result to validate shape.
    """
    return value.split(";", 1)[0].strip().lower()
