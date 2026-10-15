"""#1990: dense vector layout — settings → create_collection / update_collection.

Pins what the reconciler sends to qdrant-client for every combination of
"what the server reports" × "what the settings ask for", that a matching
collection costs no update, that layouts set outside this service are left
alone, and that the dense search carries the rescore block.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from qdrant_client.models import (
    BinaryQuantization,
    BinaryQuantizationConfig,
    Disabled,
    ScalarQuantization,
    ScalarQuantizationConfig,
    ScalarType,
    VectorParamsDiff,
)

from config.settings import get_settings
from db import qdrant as qmod
from db.qdrant import (
    DENSE_QUANTIZATION_QUANTILE,
    DENSE_RESCORE_OVERSAMPLING,
    KAGURA_MEMORIES_BM25_VECTOR_NAME,
    KAGURA_MEMORIES_COLLECTION,
    KAGURA_MEMORIES_VECTOR_NAME,
    DenseVectorConfig,
    DenseVectorState,
    apply_dense_vector_config,
    apply_dense_vector_config_to_all,
    ensure_kagura_memories_collection,
    search_memories_fulltext,
    search_memories_qdrant,
)
from utils.exceptions import QdrantError

WS = "11111111-1111-4111-8111-111111111111"
CTX = "22222222-2222-4222-8222-222222222222"
UID = "google-oauth2|user-123"
INT8 = ScalarQuantization(
    scalar=ScalarQuantizationConfig(type=ScalarType.INT8, quantile=0.99, always_ram=True)
)
BINARY = BinaryQuantization(binary=BinaryQuantizationConfig(always_ram=True))


@pytest.fixture
def mock_client(monkeypatch):
    client = AsyncMock()
    monkeypatch.setattr(qmod, "get_qdrant_client", lambda: client)
    monkeypatch.setattr(qmod, "_active_store", lambda: None)
    client.query_points.return_value = SimpleNamespace(points=[])
    return client


@pytest.fixture
def dense_settings(monkeypatch):
    """Set QDRANT_DENSE_QUANTIZATION / QDRANT_DENSE_ON_DISK on the Settings instance."""

    def _set(quantization: str = "none", on_disk: bool = False) -> None:
        monkeypatch.setattr(get_settings(), "qdrant_dense_quantization", quantization)
        monkeypatch.setattr(get_settings(), "qdrant_dense_on_disk", on_disk)

    _set()
    return _set


def _info(*, quantization=None, on_disk=None, named=True, dense_quantization=None):
    """A get_collection() result with the named-vector layout and sparse bm25."""
    dense = SimpleNamespace(size=8, on_disk=on_disk, quantization_config=dense_quantization)
    vectors = {KAGURA_MEMORIES_VECTOR_NAME: dense} if named else dense
    params = SimpleNamespace(
        vectors=vectors, sparse_vectors={KAGURA_MEMORIES_BM25_VECTOR_NAME: object()}
    )
    return SimpleNamespace(
        config=SimpleNamespace(params=params, quantization_config=quantization),
        payload_schema={"tags": object(), "location": object()},
    )


def _existing(mock_client, info, name=KAGURA_MEMORIES_COLLECTION):
    mock_client.get_collections.return_value = SimpleNamespace(
        collections=[SimpleNamespace(name=name)]
    )
    mock_client.get_collection.return_value = info


class TestDenseVectorConfig:
    def test_from_settings_reads_both_values(self, dense_settings):
        dense_settings("int8", True)
        assert DenseVectorConfig.from_settings() == DenseVectorConfig("int8", True)

    def test_default_is_fp32_in_ram(self, dense_settings):
        config = DenseVectorConfig.from_settings()
        assert config == DenseVectorConfig("none", False)
        assert config.is_default is True
        assert DenseVectorConfig("int8", False).is_default is False
        assert DenseVectorConfig("none", True).is_default is False

    def test_settings_reject_unknown_quantization(self, monkeypatch):
        from config.settings import Settings

        monkeypatch.setenv("QDRANT_DENSE_QUANTIZATION", "binary")
        with pytest.raises(ValueError, match="qdrant_dense_quantization"):
            Settings()

    def test_settings_read_env(self, monkeypatch):
        from config.settings import Settings

        monkeypatch.setenv("QDRANT_DENSE_QUANTIZATION", "int8")
        monkeypatch.setenv("QDRANT_DENSE_ON_DISK", "true")
        settings = Settings()
        assert settings.qdrant_dense_quantization == "int8"
        assert settings.qdrant_dense_on_disk is True

    @pytest.mark.parametrize("raw", ["INT8", " int8 ", "Int8\n"])
    def test_settings_normalise_case_and_whitespace(self, monkeypatch, raw):
        """A compose .env line must not keep the API from booting (code review)."""
        from config.settings import Settings

        monkeypatch.setenv("QDRANT_DENSE_QUANTIZATION", raw)
        assert Settings().qdrant_dense_quantization == "int8"


class TestReadDenseState:
    def test_unset_collection(self):
        assert qmod._read_dense_state(_info()) == DenseVectorState("none", False)

    def test_int8_on_disk(self):
        assert qmod._read_dense_state(_info(quantization=INT8, on_disk=True)) == DenseVectorState(
            "int8", True
        )

    def test_on_disk_false_and_none_are_the_same(self):
        assert qmod._read_dense_state(_info(on_disk=False)) == DenseVectorState("none", False)

    def test_other_quantization_kinds(self):
        assert qmod._read_dense_state(_info(quantization=BINARY)).quantization == "other"
        assert qmod._read_dense_state(_info(dense_quantization=INT8)).quantization == "other"

    def test_int8_with_always_ram_false_is_its_own_kind(self):
        """always_ram is a bool and is compared; only the quantile float is not."""
        served = ScalarQuantization(
            scalar=ScalarQuantizationConfig(type=ScalarType.INT8, quantile=0.99, always_ram=False)
        )
        assert qmod._read_dense_state(_info(quantization=served)).quantization == "int8_on_disk"

    def test_anonymous_vectors_are_not_ours(self):
        assert qmod._read_dense_state(_info(named=False)) is None
        assert qmod._read_dense_state(SimpleNamespace(config=None)) is None


class TestCreateCollection:
    async def test_default_layout_is_unchanged(self, mock_client, dense_settings):
        mock_client.get_collections.return_value = SimpleNamespace(collections=[])
        await ensure_kagura_memories_collection(512)
        kwargs = mock_client.create_collection.await_args.kwargs
        dense = kwargs["vectors_config"][KAGURA_MEMORIES_VECTOR_NAME]
        assert dense.size == 512
        assert dense.on_disk is None
        assert kwargs["quantization_config"] is None
        mock_client.update_collection.assert_not_awaited()

    async def test_int8_on_disk_layout(self, mock_client, dense_settings):
        dense_settings("int8", True)
        mock_client.get_collections.return_value = SimpleNamespace(collections=[])
        await ensure_kagura_memories_collection(2560)
        kwargs = mock_client.create_collection.await_args.kwargs
        dense = kwargs["vectors_config"][KAGURA_MEMORIES_VECTOR_NAME]
        assert dense.size == 2560
        assert dense.on_disk is True
        quantization = kwargs["quantization_config"]
        assert isinstance(quantization, ScalarQuantization)
        assert quantization.scalar.type == ScalarType.INT8
        assert quantization.scalar.quantile == DENSE_QUANTIZATION_QUANTILE
        assert quantization.scalar.always_ram is True
        # The sparse bm25 vector is untouched by the dense layout.
        assert KAGURA_MEMORIES_BM25_VECTOR_NAME in kwargs["sparse_vectors_config"]


class TestReconcile:
    async def test_converts_fp32_to_int8_on_disk_in_one_update(self, mock_client, dense_settings):
        dense_settings("int8", True)
        mock_client.get_collection.return_value = _info()

        assert await apply_dense_vector_config(KAGURA_MEMORIES_COLLECTION) is True

        mock_client.update_collection.assert_awaited_once()
        kwargs = mock_client.update_collection.await_args.kwargs
        assert kwargs["collection_name"] == KAGURA_MEMORIES_COLLECTION
        assert isinstance(kwargs["quantization_config"], ScalarQuantization)
        assert kwargs["quantization_config"].scalar.type == ScalarType.INT8
        assert kwargs["vectors_config"] == {
            KAGURA_MEMORIES_VECTOR_NAME: VectorParamsDiff(on_disk=True)
        }

    async def test_matching_collection_sends_nothing(self, mock_client, dense_settings):
        dense_settings("int8", True)
        mock_client.get_collection.return_value = _info(quantization=INT8, on_disk=True)

        assert await apply_dense_vector_config(KAGURA_MEMORIES_COLLECTION) is False

        mock_client.update_collection.assert_not_awaited()

    async def test_default_settings_on_unset_collection_send_nothing(
        self, mock_client, dense_settings
    ):
        mock_client.get_collection.return_value = _info()
        assert await apply_dense_vector_config(KAGURA_MEMORIES_COLLECTION) is False
        mock_client.update_collection.assert_not_awaited()

    async def test_back_to_none_sends_disabled_and_on_disk_false(self, mock_client, dense_settings):
        mock_client.get_collection.return_value = _info(quantization=INT8, on_disk=True)

        assert await apply_dense_vector_config(KAGURA_MEMORIES_COLLECTION) is True

        kwargs = mock_client.update_collection.await_args.kwargs
        assert kwargs["quantization_config"] is Disabled.DISABLED
        assert kwargs["vectors_config"] == {
            KAGURA_MEMORIES_VECTOR_NAME: VectorParamsDiff(on_disk=False)
        }

    async def test_only_the_differing_part_is_sent(self, mock_client, dense_settings):
        dense_settings("int8", True)
        mock_client.get_collection.return_value = _info(quantization=INT8, on_disk=False)

        assert await apply_dense_vector_config(KAGURA_MEMORIES_COLLECTION) is True

        kwargs = mock_client.update_collection.await_args.kwargs
        assert "quantization_config" not in kwargs
        assert kwargs["vectors_config"] == {
            KAGURA_MEMORIES_VECTOR_NAME: VectorParamsDiff(on_disk=True)
        }

    async def test_quantile_difference_alone_is_not_a_change(self, mock_client, dense_settings):
        """Kind is compared, not the float: a normalised quantile must not re-kick the optimizer."""
        dense_settings("int8", False)
        served = ScalarQuantization(
            scalar=ScalarQuantizationConfig(type=ScalarType.INT8, quantile=0.9899, always_ram=True)
        )
        mock_client.get_collection.return_value = _info(quantization=served)
        assert await apply_dense_vector_config(KAGURA_MEMORIES_COLLECTION) is False
        mock_client.update_collection.assert_not_awaited()

    async def test_int8_copy_on_disk_is_moved_back_to_ram(self, mock_client, dense_settings):
        """Scalar int8 with always_ram=False does not match the layout the service promises."""
        dense_settings("int8", True)
        served = ScalarQuantization(
            scalar=ScalarQuantizationConfig(type=ScalarType.INT8, quantile=0.99, always_ram=False)
        )
        mock_client.get_collection.return_value = _info(quantization=served, on_disk=True)

        assert await apply_dense_vector_config(KAGURA_MEMORIES_COLLECTION) is True

        kwargs = mock_client.update_collection.await_args.kwargs
        assert kwargs["quantization_config"].scalar.always_ram is True
        assert "vectors_config" not in kwargs

    async def test_update_log_names_the_changed_keys(
        self, mock_client, dense_settings, monkeypatch
    ):
        dense_settings("none", True)
        mock_client.get_collection.return_value = _info()
        events: list[dict] = []
        monkeypatch.setattr(
            qmod.logger, "info", lambda event, **kw: events.append({"event": event, **kw})
        )

        await apply_dense_vector_config(KAGURA_MEMORIES_COLLECTION)

        assert events[-1]["event"] == "collection_dense_config_updated"
        assert events[-1]["changed"] == ["vectors_config"]

    @pytest.mark.parametrize(
        "info",
        [_info(quantization=BINARY), _info(dense_quantization=INT8), _info(named=False)],
        ids=["binary", "per-vector", "anonymous"],
    )
    async def test_layouts_set_outside_the_service_are_left_alone(
        self, mock_client, dense_settings, info
    ):
        dense_settings("int8", True)
        mock_client.get_collection.return_value = info
        assert await apply_dense_vector_config(KAGURA_MEMORIES_COLLECTION) is False
        mock_client.update_collection.assert_not_awaited()

    async def test_server_error_is_wrapped(self, mock_client, dense_settings):
        mock_client.get_collection.side_effect = RuntimeError("down")
        with pytest.raises(QdrantError, match="Failed to apply dense vector config"):
            await apply_dense_vector_config(KAGURA_MEMORIES_COLLECTION)

    async def test_lance_backend_is_a_no_op(self, mock_client, dense_settings, monkeypatch):
        dense_settings("int8", True)
        monkeypatch.setattr(qmod, "_active_store", lambda: object())
        assert await apply_dense_vector_config(KAGURA_MEMORIES_COLLECTION) is False
        mock_client.get_collection.assert_not_awaited()


class TestEnsureExistingCollection:
    async def test_existing_collection_is_reconciled_before_index_retrofit(
        self, mock_client, dense_settings
    ):
        dense_settings("int8", True)
        _existing(mock_client, _info())

        await ensure_kagura_memories_collection()

        mock_client.create_collection.assert_not_awaited()
        mock_client.update_collection.assert_awaited_once()
        assert isinstance(
            mock_client.update_collection.await_args.kwargs["quantization_config"],
            ScalarQuantization,
        )

    async def test_existing_matching_collection_is_untouched(self, mock_client, dense_settings):
        _existing(mock_client, _info())
        await ensure_kagura_memories_collection()
        mock_client.create_collection.assert_not_awaited()
        mock_client.update_collection.assert_not_awaited()
        mock_client.create_payload_index.assert_not_awaited()

    async def test_layout_update_failure_does_not_fail_ensure(
        self, mock_client, dense_settings, monkeypatch
    ):
        """A usable collection stays usable: context creation must not fail on the layout (code review)."""
        dense_settings("int8", True)
        _existing(mock_client, _info())
        mock_client.update_collection.side_effect = RuntimeError("403 read-only key")
        warnings: list[dict] = []
        monkeypatch.setattr(
            qmod.logger, "warning", lambda event, **kw: warnings.append({"event": event, **kw})
        )

        await ensure_kagura_memories_collection()

        assert [w["event"] for w in warnings] == ["collection_dense_config_apply_failed"]
        mock_client.create_collection.assert_not_awaited()


class TestStartupPass:
    async def test_every_kagura_collection_in_turn_and_failures_do_not_stop_it(
        self, mock_client, dense_settings
    ):
        dense_settings("int8", False)
        variant = KAGURA_MEMORIES_COLLECTION + "_qwen3_2560"
        broken = KAGURA_MEMORIES_COLLECTION + "_broken"
        mock_client.get_collections.return_value = SimpleNamespace(
            collections=[
                SimpleNamespace(name=KAGURA_MEMORIES_COLLECTION),
                SimpleNamespace(name=broken),
                SimpleNamespace(name="other_collection"),
                SimpleNamespace(name=variant),
            ]
        )

        async def get_collection(name):
            if name == broken:
                raise RuntimeError("segment load failed")
            if name == variant:
                return _info(quantization=INT8)
            return _info()

        mock_client.get_collection.side_effect = get_collection

        results = await apply_dense_vector_config_to_all()

        assert results == {KAGURA_MEMORIES_COLLECTION: True, broken: False, variant: False}
        mock_client.update_collection.assert_awaited_once()
        assert mock_client.update_collection.await_args.kwargs["collection_name"] == (
            KAGURA_MEMORIES_COLLECTION
        )
        touched = [c.args[0] for c in mock_client.get_collection.await_args_list]
        assert "other_collection" not in touched

    async def test_collection_list_failure_propagates(self, mock_client, dense_settings):
        mock_client.get_collections.side_effect = RuntimeError("conn refused")
        with pytest.raises(RuntimeError, match="conn refused"):
            await apply_dense_vector_config_to_all()

    async def test_lance_backend_skips_and_warns_on_non_default(
        self, mock_client, dense_settings, monkeypatch
    ):
        dense_settings("int8", True)
        monkeypatch.setattr(qmod, "_active_store", lambda: object())
        warnings: list[dict] = []
        monkeypatch.setattr(
            qmod.logger, "warning", lambda event, **kw: warnings.append({"event": event, **kw})
        )

        assert await apply_dense_vector_config_to_all() == {}

        mock_client.get_collections.assert_not_awaited()
        assert [w["event"] for w in warnings] == ["dense_vector_config_ignored"]

    async def test_lance_backend_with_default_settings_is_silent(
        self, mock_client, dense_settings, monkeypatch
    ):
        monkeypatch.setattr(qmod, "_active_store", lambda: object())
        warnings: list[str] = []
        monkeypatch.setattr(qmod.logger, "warning", lambda event, **kw: warnings.append(event))
        assert await apply_dense_vector_config_to_all() == {}
        assert warnings == []


class TestSearchParams:
    async def test_dense_search_rescores_quantized_candidates(self, mock_client):
        await search_memories_qdrant(UID, [0.1], WS, CTX)
        params = mock_client.query_points.await_args.kwargs["search_params"]
        assert params.quantization.ignore is False
        assert params.quantization.rescore is True
        assert params.quantization.oversampling == DENSE_RESCORE_OVERSAMPLING

    async def test_bm25_search_has_no_quantization_block(self, mock_client):
        await search_memories_fulltext(UID, "hello", WS, CTX)
        assert "search_params" not in mock_client.query_points.await_args.kwargs
