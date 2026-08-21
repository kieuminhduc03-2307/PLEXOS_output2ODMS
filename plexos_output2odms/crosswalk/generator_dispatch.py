from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass
from pathlib import Path


RDF_NS = "http://www.w3.org/1999/02/22-rdf-syntax-ns#"
RDF_ID = f"{{{RDF_NS}}}ID"
RDF_ABOUT = f"{{{RDF_NS}}}about"
RDF_RESOURCE = f"{{{RDF_NS}}}resource"


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _ref(value: str | None) -> str:
    value = (value or "").strip()
    if value.startswith("#"):
        return value[1:]
    if value.startswith("urn:uuid:"):
        return value[9:]
    return value


@dataclass(frozen=True)
class GeneratorMapping:
    source_name: str
    source_guid: str
    source_object_id: int
    source_psse_key: str
    odms_synchronous_machine_mrid: str
    odms_generating_unit_mrid: str
    odms_machine_name: str
    approved: bool
    source_node: str = ""
    source_max_capacity_mw: float | None = None
    mapping_basis: str = ""
    min_operating_p_mw: float | None = None
    max_operating_p_mw: float | None = None
    source_resource_type: str = "Generator"
    source_operating_class: str = ""
    status_policy: str = ""
    target_kind: str = "SynchronousMachine"


def _rts_operating_metadata(source_name: str) -> tuple[str, str]:
    if "_SYNC_COND_" in source_name:
        return "SYNCHRONOUS_CONDENSER", "PRESERVE"
    parts = source_name.split("_")
    resource_class = parts[1].upper() if len(parts) >= 3 else "UNKNOWN"
    if resource_class in {"CT", "CC", "STEAM", "NUCLEAR", "HYDRO"}:
        return resource_class, "BINARY_COMMITMENT"
    if resource_class in {"PV", "WIND", "RTPV"}:
        return resource_class, "COMMITMENT_ON_ONLY"
    return resource_class, "PRESERVE"


def _model_generators(path: str | Path) -> list[dict]:
    root = ET.parse(path).getroot()
    classes: dict[str, str] = {}
    objects: dict[int, dict[str, str]] = {}
    collections: dict[int, dict[str, str]] = {}
    memberships: dict[int, dict[str, str]] = {}
    properties: dict[tuple[int, int], str] = {}
    data_rows: list[dict[str, str]] = []
    for element in root:
        values = {_local(child.tag): (child.text or "").strip() for child in element}
        table = _local(element.tag)
        if table == "t_class" and values.get("class_id"):
            classes[values["class_id"]] = values.get("name", "")
        elif table == "t_object":
            objects[int(values["object_id"])] = values
        elif table == "t_collection":
            collections[int(values["collection_id"])] = values
        elif table == "t_membership":
            memberships[int(values["membership_id"])] = values
        elif table == "t_property":
            properties[(int(values["collection_id"]), int(values["property_id"]))] = values.get("name", "")
        elif table == "t_data":
            data_rows.append(values)
    generator_ids = {identifier for identifier, name in classes.items() if name.casefold() == "generator"}
    generator_objects = {
        object_id: item for object_id, item in objects.items() if item.get("class_id") in generator_ids
    }
    values_by_object: dict[int, dict[str, float]] = {identifier: {} for identifier in generator_objects}
    for row in data_rows:
        membership = memberships.get(int(row.get("membership_id", 0)))
        if membership is None:
            continue
        related = {
            int(membership.get("parent_object_id", 0)),
            int(membership.get("child_object_id", 0)),
        } & set(generator_objects)
        if len(related) != 1:
            continue
        property_name = properties.get(
            (int(membership["collection_id"]), int(row.get("property_id", 0))), ""
        )
        try:
            value = float(row["value"])
        except (KeyError, ValueError):
            continue
        values_by_object[next(iter(related))].setdefault(property_name, value)
    result = []
    for object_id, item in generator_objects.items():
        node_names: set[str] = set()
        for membership in memberships.values():
            collection = collections.get(int(membership["collection_id"]), {})
            if collection.get("name", "").casefold() != "nodes":
                continue
            parent = int(membership["parent_object_id"])
            child = int(membership["child_object_id"])
            if parent == object_id and child in objects:
                node_names.add(objects[child].get("name", ""))
            elif child == object_id and parent in objects:
                node_names.add(objects[parent].get("name", ""))
        if len(node_names) != 1:
            raise ValueError(f"Generator {item.get('name')} must have exactly one PLEXOS Node")
        enriched: dict = dict(item)
        enriched["node"] = next(iter(node_names))
        enriched["max_capacity"] = values_by_object[object_id].get("Max Capacity")
        result.append(enriched)
    return result


def _cim_entities(path: str | Path) -> dict[str, dict]:
    root = ET.parse(path).getroot()
    entities: dict[str, dict] = {}
    for element in root:
        identifier = _ref(element.get(RDF_ID) or element.get(RDF_ABOUT))
        if not identifier:
            continue
        values: dict[str, list[str]] = {}
        for child in element:
            name = _local(child.tag)
            value = _ref(child.get(RDF_RESOURCE)) if child.get(RDF_RESOURCE) else (child.text or "").strip()
            values.setdefault(name, []).append(value)
        entities[identifier] = {"class": _local(element.tag), "properties": values}
    return entities


def _first(entity: dict, property_name: str) -> str:
    values = entity["properties"].get(property_name, [])
    return values[0] if values else ""


def list_odms_synchronous_machines(path: str | Path) -> list[dict]:
    entities = _cim_entities(path)
    result = []
    for identifier, entity in entities.items():
        if entity["class"] != "SynchronousMachine":
            continue
        name = _first(entity, "IdentifiedObject.name")
        if name:
            result.append({"target_machine_name": name, "target_machine_mrid": identifier})
    return sorted(result, key=lambda item: item["target_machine_name"])


def _machine_id(ordinal: int) -> str:
    if 1 <= ordinal <= 9:
        return str(ordinal)
    if 10 <= ordinal <= 35:
        return chr(ord("A") + ordinal - 10)
    raise ValueError(f"RTS-GMLC machine ordinal {ordinal} cannot be represented as a PSS/E ID")


def _psse_id_order(value: str) -> int:
    if value.isdigit():
        return int(value)
    if len(value) == 1 and value.isalpha():
        return 10 + ord(value.upper()) - ord("A")
    raise ValueError(f"Unsupported PSS/E machine ID: {value!r}")


def _source_ordinal(value: str) -> int:
    match = re.search(r"_(\d+)$", value)
    if match is None:
        raise ValueError(f"RTS-GMLC Generator has no numeric unit ordinal: {value!r}")
    return int(match.group(1))


def _rts_suffix_key(source_name: str) -> str:
    match = re.fullmatch(r"(?P<bus>\d+)_.+_(?P<ordinal>\d+)", source_name)
    if match is None:
        raise ValueError(f"Generator {source_name!r} does not satisfy the RTS-GMLC naming contract")
    return f"{match.group('bus')}_{_machine_id(int(match.group('ordinal')))}"


def build_rts_gmlc_crosswalk(
    plexos_model: str | Path,
    odms_cim: str | Path,
    *,
    approved: bool = False,
) -> list[GeneratorMapping]:
    source = _model_generators(plexos_model)
    entities = _cim_entities(odms_cim)
    machines: dict[str, tuple[str, dict]] = {}
    for identifier, entity in entities.items():
        if entity["class"] != "SynchronousMachine":
            continue
        name = _first(entity, "IdentifiedObject.name")
        if not name:
            continue
        if name in machines:
            raise ValueError(f"Duplicate ODMS SynchronousMachine exact name: {name}")
        machines[name] = (identifier, entity)

    targets_by_bus: dict[str, list[tuple[str, str, dict, dict]]] = {}
    for name, (machine_id, machine) in machines.items():
        unit_id = _first(machine, "RotatingMachine.GeneratingUnit")
        unit = entities.get(unit_id, {"properties": {}})
        bus = name.split("_", 1)[0]
        targets_by_bus.setdefault(bus, []).append((name, machine_id, machine, unit))

    source_groups: dict[tuple[str, float], list[dict]] = {}
    for generator in source:
        capacity = generator.get("max_capacity")
        if capacity is None:
            raise ValueError(f"Generator {generator.get('name')} has no base Max Capacity")
        source_groups.setdefault((generator.get("node", ""), float(capacity)), []).append(generator)
    target_groups: dict[tuple[str, float], list[tuple[str, str, dict, dict]]] = {}
    for bus, targets in targets_by_bus.items():
        for target in targets:
            maximum = _first(target[3], "GeneratingUnit.maxOperatingP")
            if maximum:
                target_groups.setdefault((bus, float(maximum)), []).append(target)

    pairs: list[tuple[dict, tuple[str, str, dict, dict], str]] = []
    missing: list[str] = []
    for group_key, generators in sorted(source_groups.items()):
        targets = target_groups.get(group_key, [])
        if len(generators) != len(targets):
            missing.append(
                f"bus/capacity {group_key}: {len(generators)} PLEXOS vs {len(targets)} ODMS"
            )
            continue
        ordered_sources = sorted(generators, key=lambda item: (_source_ordinal(item["name"]), item["name"]))
        ordered_targets = sorted(
            targets, key=lambda item: _psse_id_order(item[0].split("_", 1)[1])
        )
        for generator, target in zip(ordered_sources, ordered_targets):
            basis = (
                "exact bus+maxCapacity"
                if len(generators) == 1
                else "exact bus+maxCapacity; ordinal pairing within electrically equivalent group"
            )
            pairs.append((generator, target, basis))

    result: list[GeneratorMapping] = []
    for generator, target, basis in sorted(pairs, key=lambda item: item[0]["name"]):
        name = generator["name"]
        operating_class, status_policy = _rts_operating_metadata(name)
        bus = generator["node"]
        capacity = generator["max_capacity"]
        target_name, machine_id, machine, unit = target
        unit_id = _first(machine, "RotatingMachine.GeneratingUnit")
        minimum = _first(unit, "GeneratingUnit.minOperatingP")
        maximum = _first(unit, "GeneratingUnit.maxOperatingP")
        result.append(
            GeneratorMapping(
                source_name=name,
                source_guid=generator.get("GUID", ""),
                source_object_id=int(generator["object_id"]),
                source_psse_key=target_name,
                odms_synchronous_machine_mrid=machine_id,
                odms_generating_unit_mrid=unit_id,
                odms_machine_name=target_name,
                approved=approved,
                source_node=bus,
                source_max_capacity_mw=float(capacity) if capacity is not None else None,
                mapping_basis=basis,
                min_operating_p_mw=float(minimum) if minimum else None,
                max_operating_p_mw=float(maximum) if maximum else None,
                source_operating_class=operating_class,
                status_policy=status_policy,
            )
        )
    if missing:
        raise ValueError(f"RTS-GMLC crosswalk is incomplete; missing targets: {missing[:20]}")
    targets = [item.odms_synchronous_machine_mrid for item in result]
    if len(targets) != len(set(targets)):
        raise ValueError("RTS-GMLC crosswalk maps multiple PLEXOS generators to one ODMS machine")
    return result


def write_crosswalk(
    mappings: list[GeneratorMapping], path: str | Path, *, source_model: str, target_cim: str
) -> None:
    payload = {
        "schema": "plexos-output2odms-generator-crosswalk-v1",
        "source_system": "PLEXOS",
        "source_model": source_model,
        "target_cim": target_cim,
        "mapping_count": len(mappings),
        "mappings": [asdict(item) for item in mappings],
    }
    Path(path).write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def load_crosswalk(path: str | Path) -> list[GeneratorMapping]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if payload.get("schema") != "plexos-output2odms-generator-crosswalk-v1":
        raise ValueError("Unsupported generator crosswalk schema")
    mappings = [GeneratorMapping(**item) for item in payload.get("mappings", [])]
    if not mappings:
        raise ValueError("Generator crosswalk contains no mappings")
    source_names = [item.source_name for item in mappings]
    targets = [item.odms_synchronous_machine_mrid for item in mappings]
    if len(source_names) != len(set(source_names)):
        raise ValueError("Generator crosswalk contains duplicate source names")
    if len(targets) != len(set(targets)):
        raise ValueError("Generator crosswalk contains duplicate target machines")
    return mappings
