from __future__ import annotations

from plexos_output2odms.plexos_solution.commitment import build_class_aware_statuses
import pytest


def generator(name: str) -> dict:
    resource_class = name.split("_")[1]
    policy = (
        "PRESERVE" if resource_class == "SYNC" else
        "COMMITMENT_ON_ONLY" if resource_class == "WIND" else
        "BINARY_COMMITMENT"
    )
    return {
        "timestamp": "2020-07-05T00:00:00",
        "source_generator": name,
        "target_machine_name": name,
        "target_machine_mrid": "mrid-" + name,
        "source_operating_class": (
            "SYNCHRONOUS_CONDENSER" if "SYNC_COND" in name else resource_class
        ),
        "status_policy": policy,
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


def test_runtime_refuses_to_infer_status_policy_from_name():
    row = generator("101_CT_1")
    row.pop("status_policy")
    with pytest.raises(ValueError, match="crosswalk metadata"):
        build_class_aware_statuses({"101_CT_1": 1.0}, [row])
