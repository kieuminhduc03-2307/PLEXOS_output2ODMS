from __future__ import annotations

import csv
import math
import sqlite3
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path
from zipfile import ZipFile

from .dispatch import DispatchRecord, SolutionSelection


POWER_FACTORS_TO_MW = {"MW": 1.0, "kW": 0.001, "GW": 1000.0}


def _parse_datetime(value: str) -> datetime:
    value = value.strip()
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        pass
    for pattern in ("%m/%d/%Y %H:%M:%S", "%d/%m/%Y %H:%M:%S"):
        try:
            return datetime.strptime(value, pattern)
        except ValueError:
            continue
    raise ValueError(f"Unsupported PLEXOS timestamp: {value!r}")


def _same_time(actual: datetime, selected: datetime) -> bool:
    if actual.tzinfo is None and selected.tzinfo is not None:
        actual = actual.replace(tzinfo=selected.tzinfo)
    if selected.tzinfo is None and actual.tzinfo is not None:
        selected = selected.replace(tzinfo=actual.tzinfo)
    return actual == selected


def _to_mw(value: str | float, unit: str) -> float:
    normalized = unit.strip()
    factor = POWER_FACTORS_TO_MW.get(normalized)
    if factor is None:
        raise ValueError(
            f"Unit {unit!r} is not an instantaneous power unit; accepted units are "
            f"{sorted(POWER_FACTORS_TO_MW)}. Energy summaries such as MWh/GWh are rejected."
        )
    result = float(value) * factor
    if not math.isfinite(result):
        raise ValueError("PLEXOS Generation contains a non-finite value")
    return result


def _read_csv(path: Path, selection: SolutionSelection) -> list[DispatchRecord]:
    with path.open("r", encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        fields = reader.fieldnames or []
        if not fields:
            raise ValueError("PLEXOS result file has no header")
        if "time" in fields:
            if selection.unit is None:
                raise ValueError(
                    "Wide PLEXOS result files do not carry unit metadata; pass --unit MW explicitly"
                )
            if selection.phase.upper() != "ST" or selection.period.lower() != "interval":
                raise ValueError("Wide dispatch input is accepted only as ST/Interval data")
            rows = []
            matched_timestamp = False
            for row in reader:
                timestamp = _parse_datetime(row["time"])
                if not _same_time(timestamp, selection.timestamp):
                    continue
                if matched_timestamp:
                    raise ValueError(f"Duplicate dispatch timestamp: {row['time']}")
                matched_timestamp = True
                if timestamp.tzinfo is None:
                    timestamp = timestamp.replace(tzinfo=selection.timestamp.tzinfo)
                for name in fields:
                    if name == "time":
                        continue
                    value = row.get(name, "")
                    if value in (None, ""):
                        raise ValueError(f"Missing Generation for {name!r} at {row['time']}")
                    rows.append(
                        DispatchRecord(
                            timestamp,
                            name.strip(),
                            _to_mw(value, selection.unit),
                            selection.unit,
                            selection.phase,
                            selection.period,
                            selection.sample,
                        )
                    )
            if not matched_timestamp:
                raise ValueError(f"Timestamp {selection.timestamp.isoformat()} is absent from {path}")
            return rows

        long_fields = {"child_name", "property_name", "_date", "value", "unit_name"}
        if not long_fields.issubset(fields):
            raise ValueError(
                "Unsupported result CSV. Expected wide 'time' format or Energy Exemplar query columns "
                f"{sorted(long_fields)}"
            )
        rows = []
        for row in reader:
            if row["property_name"].strip().casefold() != "generation":
                continue
            if row.get("phase_name") and selection.phase.casefold() not in row["phase_name"].casefold():
                continue
            sample = (row.get("sample_name") or "Mean").strip() or "Mean"
            if sample.casefold() != selection.sample.casefold():
                continue
            timestamp = _parse_datetime(row["_date"])
            if not _same_time(timestamp, selection.timestamp):
                continue
            if timestamp.tzinfo is None:
                timestamp = timestamp.replace(tzinfo=selection.timestamp.tzinfo)
            rows.append(
                DispatchRecord(
                    timestamp,
                    row["child_name"].strip(),
                    _to_mw(row["value"], row["unit_name"]),
                    row["unit_name"].strip(),
                    selection.phase,
                    selection.period,
                    sample,
                    int(row["child_id"]) if row.get("child_id") else None,
                )
            )
        if not rows:
            raise ValueError("No Generation rows match the requested phase/period/timestamp/sample")
        return rows


def _zip_metadata(path: Path) -> tuple[dict[int, str], dict[int, str]]:
    with ZipFile(path) as archive:
        xml_names = [name for name in archive.namelist() if name.lower().endswith("solution.xml")]
        if len(xml_names) != 1:
            raise ValueError(f"Expected exactly one Solution.xml in {path}, found {len(xml_names)}")
        with archive.open(xml_names[0]) as stream:
            root = ET.parse(stream).getroot()
    objects: dict[int, str] = {}
    samples: dict[int, str] = {0: "Mean"}
    for element in root:
        local = element.tag.rsplit("}", 1)[-1]
        values = {child.tag.rsplit("}", 1)[-1]: (child.text or "").strip() for child in element}
        if local == "t_object" and values.get("object_id"):
            objects[int(values["object_id"])] = values.get("name", "")
        elif local == "t_sample" and values.get("sample_id"):
            sample_id = int(values["sample_id"])
            samples[sample_id] = values.get("sample_name") or values.get("name") or (
                "Mean" if sample_id == 0 else f"Sample {sample_id}"
            )
    return objects, samples


def _read_solution_zip(path: Path, selection: SolutionSelection) -> list[DispatchRecord]:
    if selection.phase.upper() != "ST" or selection.period.lower() != "interval":
        raise ValueError("V1 native Solution ZIP reader supports ST/Interval/Generators/Generation only")
    try:
        from plexosdb.solution_reader import PlexosSolution
    except ImportError as exc:
        raise ValueError(
            "Native Solution ZIP support requires the optional dependency: pip install 'plexos-output2odms[solution-zip]'"
        ) from exc

    object_names, sample_names = _zip_metadata(path)
    solution = PlexosSolution.from_zip(path)
    try:
        solution.to_sqlite(None, if_exists="replace", decode_bin_values=True)
        connection: sqlite3.Connection = solution.connection
        connection.execute("CREATE INDEX IF NOT EXISTS idx_dispatch_values_key ON t_data_values(key_id)")
        connection.execute("CREATE INDEX IF NOT EXISTS idx_dispatch_key_id ON t_key(key_id)")
        connection.execute("CREATE INDEX IF NOT EXISTS idx_dispatch_membership_id ON t_membership(membership_id)")
        connection.execute("CREATE INDEX IF NOT EXISTS idx_dispatch_period_id ON t_period_0(interval_id)")
        query = """
            SELECT m.child_object_id, k.sample_id, k.band_id, p.datetime, dv.value, u.value
            FROM t_data_values dv
            JOIN t_key k ON k.key_id = dv.key_id
            JOIN t_membership m ON m.membership_id = k.membership_id
            JOIN t_property property
              ON property.property_id = k.property_id
             AND property.collection_id = m.collection_id
            LEFT JOIN t_unit u ON u.unit_id = property.unit_id
            JOIN t_period_0 p ON p.interval_id = dv.block_id
            WHERE k.phase_id = 4
              AND k.period_type_id = 0
              AND property.name = 'Generation'
              AND m.collection_id = 1
              AND p.datetime IN (?, ?, ?)
        """
        selected_local = selection.timestamp.replace(tzinfo=None)
        timestamp_parameters = (
            selected_local.strftime("%m/%d/%Y %H:%M:%S"),
            selected_local.strftime("%d/%m/%Y %H:%M:%S"),
            selected_local.isoformat(sep=" "),
        )
        rows = []
        available_samples: set[str] = set()
        for object_id, sample_id, band, raw_timestamp, value, unit in connection.execute(
            query, timestamp_parameters
        ):
            object_id = int(object_id)
            sample_id = int(sample_id)
            band = int(band)
            sample = sample_names.get(sample_id, f"Sample {sample_id}")
            available_samples.add(sample)
            if sample.casefold() != selection.sample.casefold():
                continue
            # Native PLEXOS XML may use locale-ambiguous dd/mm vs mm/dd text. The SQL
            # predicate already matched an exact representation of the selected local time.
            timestamp = selected_local
            if band != 1:
                raise ValueError(f"Generation has unsupported band {band} for object {object_id}")
            if timestamp.tzinfo is None:
                timestamp = timestamp.replace(tzinfo=selection.timestamp.tzinfo)
            rows.append(
                DispatchRecord(
                    timestamp,
                    object_names.get(object_id, f"object:{object_id}"),
                    _to_mw(value, unit or ""),
                    unit or "",
                    "ST",
                    "Interval",
                    sample,
                    object_id,
                )
            )
        if not rows:
            raise ValueError(
                f"No native Solution rows match timestamp/sample; available samples are {sorted(available_samples)}"
            )
        return rows
    finally:
        solution.close()


def read_dispatch(path: str | Path, selection: SolutionSelection) -> list[DispatchRecord]:
    source = Path(path)
    if not source.exists():
        raise ValueError(f"PLEXOS result file does not exist: {source}")
    if source.suffix.casefold() == ".zip":
        records = _read_solution_zip(source, selection)
    else:
        records = _read_csv(source, selection)
    names = [record.generator_name for record in records]
    if len(names) != len(set(names)):
        duplicates = sorted({name for name in names if names.count(name) > 1})
        raise ValueError(f"Duplicate Generator dispatch rows: {duplicates[:20]}")
    return sorted(records, key=lambda item: item.generator_name)


def inspect_solution(path: str | Path) -> dict:
    source = Path(path)
    if source.suffix.casefold() == ".zip":
        objects, samples = _zip_metadata(source)
        return {
            "format": "PLEXOS Solution ZIP",
            "objects": len(objects),
            "samples": sorted(set(samples.values())),
            "supported_table": "ST__Interval__Generators__Generation",
        }
    with source.open("r", encoding="utf-8-sig", newline="") as stream:
        reader = csv.reader(stream)
        header = next(reader)
        first = next(reader, None)
        last = first
        count = 1 if first else 0
        for row in reader:
            last = row
            count += 1
    return {
        "format": "wide dispatch" if header and header[0] == "time" else "query CSV",
        "columns": len(header),
        "generator_columns": len(header) - 1 if header and header[0] == "time" else None,
        "rows": count,
        "first_timestamp": first[0] if first else None,
        "last_timestamp": last[0] if last else None,
    }
