from __future__ import annotations

import hashlib
import uuid
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable


RDF = "http://www.w3.org/1999/02/22-rdf-syntax-ns#"
CIM = "http://iec.ch/TC57/CIM100#"
MD = "http://iec.ch/TC57/61970-552/ModelDescription/1#"
SSH_PROFILE = "http://entsoe.eu/CIM/SteadyStateHypothesis/3/1"

ET.register_namespace("rdf", RDF)
ET.register_namespace("cim", CIM)
ET.register_namespace("md", MD)


def _q(namespace: str, name: str) -> str:
    return f"{{{namespace}}}{name}"


def _format(value: float) -> str:
    # Python's repr is the shortest decimal that round-trips to the same IEEE-754 value.
    return repr(float(value))


def write_ssh(
    rows: Iterable[dict],
    path: str | Path,
    *,
    scenario_time: datetime,
    dependent_on: str,
) -> str:
    ordered = sorted(rows, key=lambda item: item["target_machine_mrid"])
    fingerprint = "\n".join(
        f"{item['target_machine_mrid']}={_format(item['cim_p_mw'])}" for item in ordered
    )
    model_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"{scenario_time.isoformat()}\n{fingerprint}"))
    root = ET.Element(_q(RDF, "RDF"))
    full_model = ET.SubElement(root, _q(MD, "FullModel"), {_q(RDF, "about"): f"urn:uuid:{model_id}"})
    # Use the scenario timestamp to keep identical dispatch snapshots byte-for-byte deterministic.
    ET.SubElement(full_model, _q(MD, "Model.created")).text = scenario_time.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    ET.SubElement(full_model, _q(MD, "Model.scenarioTime")).text = scenario_time.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    ET.SubElement(full_model, _q(MD, "Model.profile")).text = SSH_PROFILE
    ET.SubElement(full_model, _q(MD, "Model.DependentOn"), {_q(RDF, "resource"): dependent_on})
    for item in ordered:
        identifier = item["target_machine_mrid"]
        reference = identifier if identifier.startswith(("urn:", "http:")) else f"#{identifier}"
        machine = ET.SubElement(root, _q(CIM, "SynchronousMachine"), {_q(RDF, "about"): reference})
        ET.SubElement(machine, _q(CIM, "RotatingMachine.p")).text = _format(item["cim_p_mw"])
    tree = ET.ElementTree(root)
    ET.indent(tree, space="  ")
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    tree.write(target, encoding="utf-8", xml_declaration=True)
    return hashlib.sha256(target.read_bytes()).hexdigest()
