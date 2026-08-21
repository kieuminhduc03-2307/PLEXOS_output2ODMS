from __future__ import annotations

import csv
import json
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass
from pathlib import Path


RDF_NS = "http://www.w3.org/1999/02/22-rdf-syntax-ns#"
RDF_ID = f"{{{RDF_NS}}}ID"
RDF_ABOUT = f"{{{RDF_NS}}}about"


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _identifier(element: ET.Element) -> str:
    return (element.get(RDF_ID) or element.get(RDF_ABOUT) or "").lstrip("#")


@dataclass(frozen=True)
class LoadMapping:
    source_bus_id: str
    source_area: str
    source_base_p_mw: float
    source_base_q_mvar: float
    odms_load_name: str
    odms_load_mrid: str
    approved: bool
    mapping_basis: str = "exact RTS bus+load ID and base P/Q"
    source_resource_type: str = "Load"
    source_operating_class: str = "CONFORMING_LOAD"
    target_kind: str = "EnergyConsumer"


def _rts_load_buses(path: str | Path) -> list[dict]:
    result: list[dict] = []
    with Path(path).open("r", encoding="utf-8-sig", newline="") as stream:
        for row in csv.DictReader(stream):
            p = float(row["MW Load"])
            q = float(row["MVAR Load"])
            if p == 0.0 and q == 0.0:
                continue
            result.append(
                {
                    "bus_id": row["Bus ID"].strip(),
                    "area": row["Area"].strip(),
                    "base_p_mw": p,
                    "base_q_mvar": q,
                }
            )
    if not result:
        raise ValueError("RTS bus data contains no non-zero loads")
    return result


def _cim_loads(path: str | Path) -> dict[str, dict]:
    root = ET.parse(path).getroot()
    result: dict[str, dict] = {}
    for element in root:
        if not (_local(element.tag).endswith("Load") or _local(element.tag) == "EnergyConsumer"):
            continue
        values = {_local(child.tag): (child.text or "").strip() for child in element}
        name = values.get("IdentifiedObject.name", "")
        if not name or "EnergyConsumer.p" not in values:
            continue
        if name in result:
            raise ValueError(f"Duplicate ODMS load exact name: {name}")
        result[name] = {
            "mrid": _identifier(element),
            "p_mw": float(values["EnergyConsumer.p"]),
            "q_mvar": float(values.get("EnergyConsumer.q", "0")),
        }
    return result


def build_rts_gmlc_load_crosswalk(
    bus_data: str | Path,
    odms_cim: str | Path,
    *,
    approved: bool = False,
    base_tolerance: float = 1e-6,
) -> list[LoadMapping]:
    source = _rts_load_buses(bus_data)
    targets = _cim_loads(odms_cim)
    mappings: list[LoadMapping] = []
    missing: list[str] = []
    for bus in source:
        target_name = f"{bus['bus_id']}_1"
        target = targets.get(target_name)
        if target is None:
            missing.append(target_name)
            continue
        if abs(target["p_mw"] - bus["base_p_mw"]) > base_tolerance:
            raise ValueError(
                f"Base P mismatch for {target_name}: RTS={bus['base_p_mw']} ODMS={target['p_mw']}"
            )
        if abs(target["q_mvar"] - bus["base_q_mvar"]) > base_tolerance:
            raise ValueError(
                f"Base Q mismatch for {target_name}: RTS={bus['base_q_mvar']} ODMS={target['q_mvar']}"
            )
        mappings.append(
            LoadMapping(
                source_bus_id=bus["bus_id"],
                source_area=bus["area"],
                source_base_p_mw=bus["base_p_mw"],
                source_base_q_mvar=bus["base_q_mvar"],
                odms_load_name=target_name,
                odms_load_mrid=target["mrid"],
                approved=approved,
            )
        )
    if missing:
        raise ValueError(f"RTS-GMLC load crosswalk is incomplete: {missing[:20]}")
    target_ids = [item.odms_load_mrid for item in mappings]
    if len(target_ids) != len(set(target_ids)):
        raise ValueError("RTS-GMLC load crosswalk contains duplicate ODMS targets")
    return sorted(mappings, key=lambda item: int(item.source_bus_id))


def write_load_crosswalk(
    mappings: list[LoadMapping], path: str | Path, *, source_bus_data: str, target_cim: str
) -> None:
    payload = {
        "schema": "plexos-output2odms-load-crosswalk-v1",
        "profile": "rts-gmlc",
        "source_bus_data": source_bus_data,
        "target_cim": target_cim,
        "mapping_count": len(mappings),
        "mappings": [asdict(item) for item in mappings],
    }
    Path(path).write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def load_load_crosswalk(path: str | Path) -> list[LoadMapping]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if payload.get("schema") != "plexos-output2odms-load-crosswalk-v1":
        raise ValueError("Unsupported load crosswalk schema")
    mappings = [LoadMapping(**item) for item in payload.get("mappings", [])]
    if not mappings:
        raise ValueError("Load crosswalk contains no mappings")
    buses = [item.source_bus_id for item in mappings]
    targets = [item.odms_load_mrid for item in mappings]
    if len(buses) != len(set(buses)):
        raise ValueError("Load crosswalk contains duplicate source buses")
    if len(targets) != len(set(targets)):
        raise ValueError("Load crosswalk contains duplicate ODMS targets")
    return mappings
