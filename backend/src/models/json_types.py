"""JSON column types that store a Python ``None`` as SQL NULL (#2026).

SQLAlchemy's ``JSON`` / ``JSONB`` default to ``none_as_null=False``: a Python
``None`` is serialized and stored as the JSON value ``null``, not as SQL
``NULL``. The ORM reads both back as ``None``, so the application cannot tell
them apart, but SQL can: ``WHERE col IS NULL`` misses every JSON-null row, and
``COALESCE(col, '{}') || ...`` merges into the JSON null instead of the empty
object (#2022 found this on ``memories.supersede_candidate``).

Rule: every nullable JSON / JSONB column is declared with one of these types,
so ``None`` means SQL NULL everywhere. ``tests/models/test_json_none_as_null.py``
enforces it over ``Base.metadata``.

Exceptions (plain ``JSONB``, listed in that test): a nullable column that also
has a ``default`` / ``server_default`` keeps ``none_as_null=False``. With
``none_as_null=True`` the ORM leaves an explicit ``None`` out of the INSERT, so
the default fires and the row stores ``{}`` instead of the ``None`` the caller
wrote — a visible change for a column the API returns. Today these are
``resource_events.event_metadata`` and ``indexer_state.metrics``.

NOT NULL JSON columns are out of scope: some of them store JSON ``null`` as a
legitimate value (``agent_states.value``).

Rows written before the switch may still hold JSON ``null``. f15 rewrote the
small tables and ``memories``; in the large or append-only logs (audit_logs,
llm_call_log, memory_access_events, secret_access_log, resource_events,
neural_memory_edges) historical JSON null stays, so a SQL count or merge on
those columns must use ``col IS NULL OR col::text = 'null'`` (and
``NULLIF(col, 'null'::jsonb)`` before ``||``).
"""

from sqlalchemy import JSON
from sqlalchemy.dialects.postgresql import JSONB

# Postgres ``json`` column whose Python ``None`` is SQL NULL.
NullableJSON = JSON(none_as_null=True)

# Postgres ``jsonb`` column whose Python ``None`` is SQL NULL.
NullableJSONB = JSONB(none_as_null=True)
