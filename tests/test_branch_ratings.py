from __future__ import annotations

import json
from pathlib import Path

import pytest

from plexos_output2odms.crosswalk.branch_ratings import (
    build_rts_branch_rating_crosswalk,
    load_branch_rating_crosswalk,
    write_branch_rating_crosswalk,
)


def test_branch_rating_crosswalk_maps_static_ratings_to_conditions(tmp_path: Path):
    source = tmp_path / "branch.csv"
    source.write_text(
        "UID,From Bus,To Bus,R,X,B,Cont Rating,LTE Rating,STE Rating\n"
        "A1,101,102,0.003,0.014,0.461,175,193,200\n",
        encoding="utf-8",
    )
    audit = tmp_path / "audit.json"
    audit.write_text(
        json.dumps(
            {
                "valid": True,
                "branches": [
                    {
                        "name": "101_102_1",
                        "mrid": "line-1",
                        "kind": "Line",
                        "r_pu": 0.003,
                        "x_pu": 0.014,
                        "b_pu": 0.461,
                        "from_section": {"mapped_bus_number": 101},
                        "to_section": {"mapped_bus_number": 102},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    rows = build_rts_branch_rating_crosswalk(source, audit, approved=True)
    assert rows[0].source_cont_rating_mva == 175.0
    assert rows[0].source_lte_rating_mva == 193.0
    assert rows[0].source_ste_rating_mva == 200.0
    output = tmp_path / "crosswalk.json"
    write_branch_rating_crosswalk(
        rows, output, source_branch_data=str(source), odms_ac_audit=str(audit)
    )
    assert load_branch_rating_crosswalk(output)[0].target_mrid == "line-1"


def test_parallel_branches_with_different_ratings_fail_closed(tmp_path: Path):
    source = tmp_path / "branch.csv"
    source.write_text(
        "UID,From Bus,To Bus,R,X,B,Cont Rating,LTE Rating,STE Rating\n"
        "A1-1,101,102,0.003,0.014,0.461,175,193,200\n"
        "A1-2,101,102,0.003,0.014,0.461,200,220,240\n",
        encoding="utf-8",
    )
    audit = tmp_path / "audit.json"
    audit.write_text(
        json.dumps(
            {
                "valid": True,
                "branches": [
                    {
                        "name": f"101_102_{index}", "mrid": f"line-{index}",
                        "kind": "Line", "r_pu": 0.003, "x_pu": 0.014, "b_pu": 0.461,
                        "from_section": {"mapped_bus_number": 101},
                        "to_section": {"mapped_bus_number": 102},
                    }
                    for index in (1, 2)
                ],
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="Parallel branch ratings differ"):
        build_rts_branch_rating_crosswalk(source, audit, approved=True)
