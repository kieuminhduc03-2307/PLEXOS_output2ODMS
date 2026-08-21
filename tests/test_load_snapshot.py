from __future__ import annotations

import csv
import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from plexos_output2odms.crosswalk.load_snapshot import (
    LoadMapping,
    build_rts_gmlc_load_crosswalk,
    load_load_crosswalk,
)
from plexos_output2odms.plexos_solution.regional_load import (
    allocate_rts_nodal_load,
    read_rts_regional_load,
)


def mappings() -> list[LoadMapping]:
    return [
        LoadMapping("101", "1", 100.0, 20.0, "101_1", "load-101", True),
        LoadMapping("102", "1", 300.0, 90.0, "102_1", "load-102", True),
        LoadMapping("201", "2", 200.0, 40.0, "201_1", "load-201", True),
    ]


def test_regional_load_period_one_is_midnight(tmp_path: Path):
    path = tmp_path / "load.csv"
    path.write_text(
        "Year,Month,Day,Period,1,2\n2020,7,5,1,200,100\n2020,7,5,2,300,200\n",
        encoding="utf-8",
    )
    values = read_rts_regional_load(
        path, datetime(2020, 7, 5, 0, 0, tzinfo=ZoneInfo("Asia/Ho_Chi_Minh"))
    )
    assert values == {"1": 200.0, "2": 100.0}


def test_nodal_allocation_preserves_region_sums_and_base_power_factor():
    rows = allocate_rts_nodal_load({"1": 200.0, "2": 100.0}, mappings())
    by_region = {}
    for row in rows:
        by_region[row["source_region"]] = by_region.get(row["source_region"], 0.0) + row["load_p_mw"]
        assert row["load_q_mvar"] / row["load_p_mw"] == pytest.approx(
            row["base_q_mvar"] / row["base_p_mw"]
        )
        assert row["q_provenance"] == "RTS_DERIVED_AC_EMBEDDING"
    assert by_region == pytest.approx({"1": 200.0, "2": 100.0})


def test_duplicate_load_target_fails_closed(tmp_path: Path):
    path = tmp_path / "crosswalk.json"
    payload = {
        "schema": "plexos-output2odms-load-crosswalk-v1",
        "mappings": [
            {**mappings()[0].__dict__, "source_bus_id": "101"},
            {**mappings()[0].__dict__, "source_bus_id": "102"},
        ],
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="duplicate ODMS targets"):
        load_load_crosswalk(path)


def test_load_crosswalk_requires_exact_base_p_q_and_materializes_mrid(tmp_path: Path):
    bus = tmp_path / "bus.csv"
    bus.write_text(
        "Bus ID,MW Load,MVAR Load,Area\n101,108,22,1\n",
        encoding="utf-8",
    )
    cim = tmp_path / "case.xml"
    cim.write_text(
        """<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
        xmlns:cim="http://iec.ch/TC57/CIM100#">
        <cim:ConformLoad rdf:ID="load-101">
          <cim:IdentifiedObject.name>101_1</cim:IdentifiedObject.name>
          <cim:EnergyConsumer.p>108</cim:EnergyConsumer.p>
          <cim:EnergyConsumer.q>22</cim:EnergyConsumer.q>
        </cim:ConformLoad></rdf:RDF>""",
        encoding="utf-8",
    )
    result = build_rts_gmlc_load_crosswalk(bus, cim, approved=True)
    assert result[0].odms_load_mrid == "load-101"
    assert result[0].approved


def test_golden_regional_total_is_4474_979379_mw():
    golden = {"1": 1525.828798, "2": 1752.258775, "3": 1196.891806}
    rows = allocate_rts_nodal_load(
        golden,
        [
            LoadMapping("101", "1", 2850.0, 580.0, "101_1", "l1", True),
            LoadMapping("201", "2", 2850.0, 580.0, "201_1", "l2", True),
            LoadMapping("301", "3", 2850.0, 580.0, "301_1", "l3", True),
        ],
    )
    assert sum(row["load_p_mw"] for row in rows) == pytest.approx(4474.979379)
