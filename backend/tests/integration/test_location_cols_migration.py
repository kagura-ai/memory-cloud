"""WHERE-axis generated columns round-trip (#1331, e69_1331_location_cols).

Mirrors test_time_trigger_migration.py: raw INSERT → generated-column SELECT,
including the regex guard's malformed-value → NULL behavior and the
valid_location_range CHECK (raw-SQL defense).

Two insert paths on purpose (#1344): ``memories.details`` is PostgreSQL json
(NOT jsonb), which stores inserted text VERBATIM. ``CAST(:details AS JSON)``
reproduces the app write path (json.dumps text lands unchanged — exponent
notation included), while ``CAST(:details AS JSONB)`` canonicalizes numerics
before the json assignment and models raw-SQL writers that round-trip
through jsonb. The guard must behave on BOTH.
"""

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError


def _insert_stmt(cast: str):
    return text(
        f"""
        INSERT INTO memories
          (id, user_id, summary, content, type, importance, confidence,
           scope, embedding_status, client, source, long_term,
           access_count, details)
        VALUES
          (:id, 'u1', 'geo row', 'geo row', 'note', 0.5, 1.0,
           'working', 'success', 'test', 'mcp_remember', false, 0,
           CAST(:details AS {cast}))
        """
    )


_INSERT = _insert_stmt("JSONB")
_INSERT_VERBATIM = _insert_stmt("JSON")


async def _insert(db_session, details_json: str, *, verbatim: bool = False) -> uuid.UUID:
    mem_id = uuid.uuid4()
    stmt = _INSERT_VERBATIM if verbatim else _INSERT
    await db_session.execute(stmt, {"id": mem_id, "details": details_json})
    return mem_id


async def _cols(db_session, mem_id):
    return (
        await db_session.execute(
            text("SELECT location_lat, location_lon FROM memories WHERE id = :id"),
            {"id": mem_id},
        )
    ).one()


@pytest.mark.asyncio
async def test_generated_location_columns_populate_from_details(db_session):
    mem_id = await _insert(
        db_session, '{"location": {"lat": 35.6812345, "lon": 139.7671234, "label": "Tokyo"}}'
    )
    row = await _cols(db_session, mem_id)
    assert row.location_lat == pytest.approx(35.6812345)
    assert row.location_lon == pytest.approx(139.7671234)


@pytest.mark.asyncio
async def test_no_location_key_yields_null_columns(db_session):
    mem_id = await _insert(db_session, '{"other": 1}')
    row = await _cols(db_session, mem_id)
    assert row.location_lat is None
    assert row.location_lon is None


@pytest.mark.asyncio
async def test_malformed_values_null_instead_of_failing_insert(db_session):
    # Raw-SQL defense: free-form or structurally wrong location values must
    # not break the INSERT — the regex guard NULLs the generated columns.
    for details in (
        '{"location": "Tokyo office"}',
        '{"location": {"lat": {"deep": 1}, "lon": []}}',
        '{"location": {"lat": "one", "lon": "north"}}',
    ):
        mem_id = await _insert(db_session, details)
        row = await _cols(db_session, mem_id)
        assert row.location_lat is None
        assert row.location_lon is None


@pytest.mark.asyncio
async def test_numeric_looking_string_populates_via_raw_sql(db_session):
    # ``->>`` renders JSON strings and numbers identically, so a raw-SQL
    # insert of {"lat": "35.6"} DOES populate the column (regex matches) —
    # and since #1344 widened the guard, exponent-notation strings ("1e2")
    # do too. The service-layer 422 (normalize_location rejects string
    # numerics) is the real contract; this pins the raw-path behavior so
    # nobody assumes the regex guard distinguishes the two.
    mem_id = await _insert(db_session, '{"location": {"lat": "35.6", "lon": "1e2"}}')
    row = await _cols(db_session, mem_id)
    assert row.location_lat == pytest.approx(35.6)
    assert row.location_lon == pytest.approx(100.0)


@pytest.mark.asyncio
async def test_negative_and_integer_coordinates_cast(db_session):
    mem_id = await _insert(db_session, '{"location": {"lat": -33, "lon": -70.65}}')
    row = await _cols(db_session, mem_id)
    assert row.location_lat == pytest.approx(-33.0)
    assert row.location_lon == pytest.approx(-70.65)


@pytest.mark.asyncio
async def test_tiny_coordinate_survives_jsonb_canonicalization(db_session):
    # jsonb round-trip path: CAST(... AS JSONB) canonicalizes 1e-7 to
    # 0.0000001 before the json-column assignment, so the plain-decimal arm
    # of the regex matches. Models raw-SQL writers that go through jsonb.
    mem_id = await _insert(db_session, '{"location": {"lat": 1e-7, "lon": -0.0000001}}')
    row = await _cols(db_session, mem_id)
    assert row.location_lat == pytest.approx(1e-7)
    assert row.location_lon == pytest.approx(-1e-7)


@pytest.mark.asyncio
async def test_exponent_notation_survives_json_verbatim_storage(db_session):
    # #1344 regression pin — the APP path. details is json (not jsonb): the
    # inserted text is stored verbatim, and json.dumps renders any
    # 0 < |value| < 1e-4 coordinate in exponent notation ("5e-05"). The regex
    # guard must accept it or the row silently vanishes from recall_nearby
    # (write succeeds, generated column NULL). Pre-#1344 this pin fails with
    # location_lon NULL.
    mem_id = await _insert(
        db_session,
        '{"location": {"lat": 51.4779, "lon": 5e-05}}',
        verbatim=True,
    )
    row = await _cols(db_session, mem_id)
    assert row.location_lat == pytest.approx(51.4779)
    assert row.location_lon == pytest.approx(5e-05)

    mem_id = await _insert(
        db_session,
        '{"location": {"lat": 1e-07, "lon": -1.23e-05}}',
        verbatim=True,
    )
    row = await _cols(db_session, mem_id)
    assert row.location_lat == pytest.approx(1e-07)
    assert row.location_lon == pytest.approx(-1.23e-05)


@pytest.mark.asyncio
async def test_exponent_cap_nulls_absurd_exponents_instead_of_erroring(db_session):
    # The 2-digit exponent cap classifies 3+-digit exponents as malformed →
    # NULL. Without the cap, "1e309" would raise an out-of-range error at the
    # ::double precision cast (breaking "malformed → NULL, never error"),
    # and "1e123" — within double range but absurd as a coordinate — would
    # cast and then abort the INSERT on the range CHECK instead of NULLing.
    for lexeme in ("1e123", "1e309"):
        mem_id = await _insert(
            db_session,
            f'{{"location": {{"lat": {lexeme}, "lon": 5.0}}}}',
            verbatim=True,
        )
        row = await _cols(db_session, mem_id)
        assert row.location_lat is None, lexeme
        assert row.location_lon == pytest.approx(5.0)


@pytest.mark.asyncio
async def test_out_of_range_value_rejected_by_check(db_session):
    # valid_location_range: numeric-but-impossible coordinates written via
    # raw SQL must fail the CHECK, not become queryable garbage.
    with pytest.raises(IntegrityError, match="valid_location_range"):
        await _insert(db_session, '{"location": {"lat": 95.0, "lon": 10.0}}')
    await db_session.rollback()
