from __future__ import annotations

import csv
import json
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path

from plexos_output2odms.crosswalk.generator_dispatch import GeneratorMapping, write_crosswalk
from plexos_output2odms.pipeline import SnapshotConfig, build_dispatch_snapshot, write_snapshot_outputs
import plexos_output2odms.pipeline as pipeline_module
from plexos_output2odms.plexos_solution.dispatch import DispatchRecord


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
    timestamp = datetime(2020, 7, 5)
    result = build_dispatch_snapshot(
        solution,
        crosswalk,
        target,
        timestamp=timestamp,
        config=SnapshotConfig(unit="MW", analysis_timezone="Asia/Ho_Chi_Minh"),
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
        timestamp=datetime(2020, 7, 5),
        config=SnapshotConfig(unit="MW"),
    )
    assert not result.report.ok
    assert {item.code for item in result.report.errors} == {"CROSSWALK_NOT_APPROVED"}


def test_missing_dispatch_requires_explicit_preserve(tmp_path: Path):
    solution, target, crosswalk = prepare(
        tmp_path, [mapping("G1", "machine-1"), mapping("G2", "machine-2")]
    )
    timestamp = datetime(2020, 7, 5)
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


def test_reactive_limits_are_validate_only_by_default_and_apply_requires_opt_in(tmp_path: Path):
    ac_mapping = GeneratorMapping(
        **{
            **mapping("G1", "machine-1").__dict__,
            "source_base_q_mvar": 5.0,
            "source_voltage_setpoint_pu": 1.01,
            "source_q_min_mvar": -20.0,
            "source_q_max_mvar": 30.0,
            "ac_control_policy": "ODMS_REGULATING_ONLY",
        }
    )
    solution, target, crosswalk = prepare(tmp_path, [ac_mapping])
    timestamp = datetime(2020, 7, 5)
    default_result = build_dispatch_snapshot(
        solution, crosswalk, target, timestamp=timestamp, config=SnapshotConfig(unit="MW")
    )
    default_path = tmp_path / "validate-only.json"
    default_result.write_operating_snapshot(default_path)
    default_payload = json.loads(default_path.read_text(encoding="utf-8"))
    assert default_payload["reactive_capabilities"][0]["policy"] == (
        "VALIDATE_ONLY_STATIC_CAPABILITY"
    )
    assert not default_result.audit["generator_ac_controls"]["runtime_mutation_authorized"]

    apply_result = build_dispatch_snapshot(
        solution,
        crosswalk,
        target,
        timestamp=timestamp,
        config=SnapshotConfig(unit="MW", q_limit_policy="apply_source"),
    )
    apply_path = tmp_path / "apply-source.json"
    apply_result.write_operating_snapshot(apply_path)
    apply_payload = json.loads(apply_path.read_text(encoding="utf-8"))
    assert apply_payload["reactive_capabilities"][0]["policy"] == (
        "APPLY_SOURCE_STATIC_CAPABILITY"
    )
    assert apply_result.audit["generator_ac_controls"]["runtime_mutation_authorized"]


def test_native_zip_uses_embedded_units_generating_when_commitment_is_omitted(
    tmp_path: Path, monkeypatch
):
    source = tmp_path / "Case Solution.zip"
    source.write_bytes(b"native-fixture")
    target = tmp_path / "target.xml"
    target.write_text(
        "<rdf:RDF xmlns:rdf='http://www.w3.org/1999/02/22-rdf-syntax-ns#'/>",
        encoding="utf-8",
    )
    native_mapping = GeneratorMapping(
        **{
            **mapping("G1", "machine-1").__dict__,
            "source_operating_class": "CT",
            "status_policy": "BINARY_COMMITMENT",
        }
    )
    crosswalk = tmp_path / "crosswalk.json"
    write_crosswalk([native_mapping], crosswalk, source_model="model.xml", target_cim=str(target))
    timestamp = datetime(2024, 4, 5)
    monkeypatch.setattr(
        pipeline_module,
        "read_dispatch",
        lambda path, selected: [
            DispatchRecord(timestamp, "G1", 76.0, "MW", "ST", "Interval", "Mean", 101)
        ],
    )
    observed = {}

    def embedded_commitment(path, selected_timestamp, **kwargs):
        observed["path"] = Path(path)
        observed["timestamp"] = selected_timestamp
        observed.update(kwargs)
        return {"G1": 1.0}

    monkeypatch.setattr(pipeline_module, "read_commitment", embedded_commitment)
    result = build_dispatch_snapshot(
        source,
        crosswalk,
        target,
        timestamp=timestamp,
        config=SnapshotConfig(unit=None),
    )
    assert result.report.ok
    assert observed == {
        "path": source,
        "timestamp": timestamp,
        "phase": "ST",
        "period": "Interval",
        "sample": "Mean",
    }
    assert result.status_rows[0]["requested_in_service"] is True
    assert result.audit["sources"]["commitment"]["property"] == (
        "Generator.Units Generating"
    )
