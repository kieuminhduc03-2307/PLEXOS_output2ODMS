from __future__ import annotations

import csv
import json
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from plexos_output2odms.crosswalk.generator_dispatch import GeneratorMapping, write_crosswalk
from plexos_output2odms.pipeline import SnapshotConfig, build_dispatch_snapshot, write_snapshot_outputs


def mapping(name: str, mrid: str, *, approved: bool = True) -> GeneratorMapping:
    return GeneratorMapping(
        source_name=name,
        source_guid=f"guid-{name}",
        source_object_id=1,
        source_psse_key=name,
        odms_synchronous_machine_mrid=mrid,
        odms_generating_unit_mrid=f"gu-{mrid}",
        odms_machine_name=name,
        approved=approved,
        min_operating_p_mw=10.0,
        max_operating_p_mw=100.0,
    )


def prepare(tmp_path: Path, mappings: list[GeneratorMapping]):
    solution = tmp_path / "generation.csv"
    solution.write_text("time,G1\n2020-07-05 00:00:00,76\n", encoding="utf-8")
    target = tmp_path / "target.xml"
    target.write_text("<rdf:RDF xmlns:rdf='http://www.w3.org/1999/02/22-rdf-syntax-ns#'/>", encoding="utf-8")
    crosswalk = tmp_path / "crosswalk.json"
    write_crosswalk(mappings, crosswalk, source_model="model.xml", target_cim=str(target))
    return solution, target, crosswalk


def test_snapshot_maps_scheduled_mw_and_negative_cim_p(tmp_path: Path):
    solution, target, crosswalk = prepare(tmp_path, [mapping("G1", "machine-1")])
    timestamp = datetime(2020, 7, 5, tzinfo=ZoneInfo("Asia/Ho_Chi_Minh"))
    result = build_dispatch_snapshot(
        solution,
        crosswalk,
        target,
        timestamp=timestamp,
        config=SnapshotConfig(unit="MW"),
        dependent_on="urn:uuid:eq-model",
    )
    assert result.report.ok
    assert result.rows[0]["scheduled_mw"] == 76.0
    assert result.rows[0]["cim_p_mw"] == -76.0
    outputs = write_snapshot_outputs(result, tmp_path / "out", scenario_time=timestamp)
    root = ET.parse(outputs["ssh"]).getroot()
    values = [element.text for element in root.iter() if element.tag.endswith("RotatingMachine.p")]
    assert values == ["-76.0"]
    with Path(outputs["normalized"]).open(newline="", encoding="utf-8") as stream:
        assert list(csv.DictReader(stream))[0]["target_machine_mrid"] == "machine-1"


def test_unapproved_crosswalk_fails_closed(tmp_path: Path):
    solution, target, crosswalk = prepare(tmp_path, [mapping("G1", "machine-1", approved=False)])
    result = build_dispatch_snapshot(
        solution,
        crosswalk,
        target,
        timestamp=datetime(2020, 7, 5, tzinfo=ZoneInfo("UTC")),
        config=SnapshotConfig(unit="MW"),
    )
    assert not result.report.ok
    assert {item.code for item in result.report.errors} == {"CROSSWALK_NOT_APPROVED"}


def test_missing_dispatch_requires_explicit_preserve(tmp_path: Path):
    solution, target, crosswalk = prepare(
        tmp_path, [mapping("G1", "machine-1"), mapping("G2", "machine-2")]
    )
    timestamp = datetime(2020, 7, 5, tzinfo=ZoneInfo("UTC"))
    failed = build_dispatch_snapshot(
        solution, crosswalk, target, timestamp=timestamp, config=SnapshotConfig(unit="MW")
    )
    assert not failed.report.ok
    preserved = build_dispatch_snapshot(
        solution,
        crosswalk,
        target,
        timestamp=timestamp,
        config=SnapshotConfig(unit="MW", missing_dispatch_policy="preserve"),
    )
    assert preserved.report.ok
    assert preserved.audit["mapping"]["preserved_missing_dispatch"] == ["G2"]
