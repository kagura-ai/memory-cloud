"""APIKeyCreate request schema — expiry contract (#1537).

The request layer is where "omitted = server default" and "0 = explicit
never" meet the wire: 0 used to be rejected (ge=1) and is now the opt-in.
"""

import pytest
from pydantic import ValidationError

from api.routes.api_keys import APIKeyCreate


def test_omitted_expires_days_is_none_so_the_server_default_applies():
    assert APIKeyCreate(name="k").expires_days is None


def test_zero_is_accepted_as_the_explicit_never_expires_opt_in():
    assert APIKeyCreate(name="k", expires_days=0).expires_days == 0


@pytest.mark.parametrize("days", [-1, 3651])
def test_out_of_range_values_are_rejected(days):
    with pytest.raises(ValidationError, match="expires_days"):
        APIKeyCreate(name="k", expires_days=days)
