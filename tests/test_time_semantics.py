from __future__ import annotations

from datetime import datetime

import pytest

from plexos_output2odms.time_semantics import SourceTimeContext, parse_source_wall_clock


def test_unknown_source_time_does_not_claim_utc():
    value = SourceTimeContext(
        datetime(2020, 7, 5),
        source_time_basis="unknown_local",
        analysis_timezone="UTC",
    ).to_dict()
    assert value["source_wall_clock"] == "2020-07-05T00:00:00"
    assert value["timestamp_utc"] is None
    assert value["analysis_timestamp_utc"] == "2020-07-05T00:00:00Z"


def test_known_iana_source_can_produce_source_utc():
    value = SourceTimeContext(
        datetime(2020, 7, 5),
        source_time_basis="iana_timezone",
        source_timezone="America/Denver",
        analysis_timezone="UTC",
    ).to_dict()
    assert value["timestamp_utc"] == "2020-07-05T06:00:00Z"


def test_offset_timestamp_must_not_be_silently_accepted_as_wall_clock():
    with pytest.raises(ValueError, match="must not contain an offset"):
        parse_source_wall_clock("2020-07-05T00:00:00+07:00")
