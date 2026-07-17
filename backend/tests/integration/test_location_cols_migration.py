"""WHERE-axis generated columns round-trip (#1331, e68_1331_location_cols).

Mirrors test_time_trigger_migration.py: raw INSERT → generated-column SELECT,
including the regex guard's malformed-value → NULL behavior and the
valid_location_range CHECK (raw-SQL defense).
"""

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

_INSERT = text(
    """
    INSERT INTO memories
      (id, user_id, summary, content, type, importance, confidence,
       scope, embedding_status, client, source, long_term,
       access_count, details)
    VALUES
      (:id, 'u1', 'geo row', 'geo row', 'note', 0.5, 1.0,
       'working', 'success', 'test', 'mcp_remember', false, 0,
       CAST(:details AS JSONB))
    """
)


async def _insert(db_session, details_json: str) -> uuid.UUID:
    mem_id = uuid.uuid4()
    await db_session.execute(_INSERT, {"id": mem_id, "details": details_json})
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
        '{"location": {"lat": "one", "lon": "1e2"}}',
    ):
        mem_id = await _insert(db_session, details)
        row = await _cols(db_session, mem_id)
        assert row.location_lat is None
        assert row.location_lon is None


@pytest.mark.asyncio
async def test_numeric_looking_string_populates_via_raw_sql(db_session):
    # ``->>`` renders JSON strings and numbers identically, so a raw-SQL
    # insert of {"lat": "35.6"} DOES populate the column (regex matches).
    # The service-layer 422 (normalize_location rejects string numerics) is
    # the real contract; this pins the raw-path behavior so nobody assumes
    # the regex guard distinguishes the two.
    mem_id = await _insert(db_session, '{"location": {"lat": "35.6", "lon": "139.7"}}')
    row = await _cols(db_session, mem_id)
    assert row.location_lat == pytest.approx(35.6)
    assert row.location_lon == pytest.approx(139.7)


@pytest.mark.asyncio
async def test_negative_and_integer_coordinates_cast(db_session):
    mem_id = await _insert(db_session, '{"location": {"lat": -33, "lon": -70.65}}')
    row = await _cols(db_session, mem_id)
    assert row.location_lat == pytest.approx(-33.0)
    assert row.location_lon == pytest.approx(-70.65)


@pytest.mark.asyncio
async def test_tiny_coordinate_survives_jsonb_rendering(db_session):
    # The regex guard's contract rests on JSONB rendering numerics via
    # ``numeric`` (never exponent notation): a ~1e-7-scale coordinate — whose
    # Python repr IS exponential — must still populate the column. If details
    # ever migrates off JSONB or serialization changes, this pin catches the
    # silent NULL-out.
    mem_id = await _insert(db_session, '{"location": {"lat": 1e-7, "lon": -0.0000001}}')
    row = await _cols(db_session, mem_id)
    assert row.location_lat == pytest.approx(1e-7)
    assert row.location_lon == pytest.approx(-1e-7)


@pytest.mark.asyncio
async def test_out_of_range_value_rejected_by_check(db_session):
    # valid_location_range: numeric-but-impossible coordinates written via
    # raw SQL must fail the CHECK, not become queryable garbage.
    with pytest.raises(IntegrityError, match="valid_location_range"):
        await _insert(db_session, '{"location": {"lat": 95.0, "lon": 10.0}}')
    await db_session.rollback()
