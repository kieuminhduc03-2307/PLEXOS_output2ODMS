from __future__ import annotations

from plexos_output2odms.plexos_solution.commitment import build_class_aware_statuses


def generator(name: str) -> dict:
    return {
        "timestamp": "2020-07-05T00:00:00+07:00",
        "source_generator": name,
        "target_machine_name": name,
        "target_machine_mrid": "mrid-" + name,
    }


def test_class_aware_commitment_preserves_sync_cond_and_zero_wind():
    names = ["101_CT_1", "114_SYNC_COND_1", "303_WIND_1", "122_HYDRO_1"]
    rows = build_class_aware_statuses(
        {"101_CT_1": 0.0, "114_SYNC_COND_1": 0.0, "303_WIND_1": 0.0, "122_HYDRO_1": 1.0},
        [generator(name) for name in names],
    )
    by_name = {row["source_generator"]: row for row in rows}
    assert by_name["101_CT_1"]["requested_in_service"] is False
    assert by_name["114_SYNC_COND_1"]["action"] == "preserve"
    assert by_name["303_WIND_1"]["action"] == "preserve"
    assert by_name["122_HYDRO_1"]["requested_in_service"] is True


def test_positive_variable_commitment_can_only_turn_on():
    row = build_class_aware_statuses(
        {"303_WIND_1": 1.0}, [generator("303_WIND_1")]
    )[0]
    assert row["action"] == "set"
    assert row["requested_in_service"] is True
