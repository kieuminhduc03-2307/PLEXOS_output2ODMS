from __future__ import annotations

import csv
import atexit
import math
import sqlite3
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path
from zipfile import ZipFile

from .dispatch import DispatchRecord, SolutionSelection


POWER_FACTORS_TO_MW = {"MW": 1.0, "kW": 0.001, "GW": 1000.0}
_NATIVE_SOLUTIONS: dict[tuple[str, int, int], object] = {}
_NATIVE_PERIODS: dict[tuple[str, int, int], dict[datetime, int]] = {}


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
    if (actual.tzinfo is None) != (selected.tzinfo is None):
        raise ValueError(
            "Source and selection timestamp timezone metadata differ; implicit timezone attachment is prohibited"
        )
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


def _close_native_solutions() -> None:
    for solution in _NATIVE_SOLUTIONS.values():
        try:
            solution.close()
        except Exception:
            pass
    _NATIVE_SOLUTIONS.clear()
    _NATIVE_PERIODS.clear()


atexit.register(_close_native_solutions)


def _native_connection(path: Path) -> sqlite3.Connection:
    try:
        from plexosdb.solution_reader import PlexosSolution
    except ImportError as exc:
        raise ValueError(
            "Native Solution ZIP support requires the optional dependency: "
            "pip install 'plexos-output2odms[solution-zip]'"
        ) from exc
    resolved = path.resolve()
    stat = resolved.stat()
    key = (str(resolved), stat.st_size, stat.st_mtime_ns)
    solution = _NATIVE_SOLUTIONS.get(key)
    if solution is None:
        stale = [item for item in _NATIVE_SOLUTIONS if item[0] == str(resolved)]
        for item in stale:
            _NATIVE_SOLUTIONS.pop(item).close()
            _NATIVE_PERIODS.pop(item, None)
        solution = PlexosSolution.from_zip(resolved)
        solution.to_sqlite(None, if_exists="replace", decode_bin_values=True)
        connection = solution.connection
        connection.execute("CREATE INDEX IF NOT EXISTS idx_native_values_key ON t_data_values(key_id)")
        connection.execute("CREATE INDEX IF NOT EXISTS idx_native_key_id ON t_key(key_id)")
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_native_membership_id ON t_membership(membership_id)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_native_membership_collection "
            "ON t_membership(collection_id, membership_id)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_native_property_lookup "
            "ON t_property(collection_id, name, property_id)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_native_key_filter "
            "ON t_key(phase_id, period_type_id, membership_id, property_id, key_id)"
        )
        connection.execute("CREATE INDEX IF NOT EXISTS idx_native_period_id ON t_period_0(interval_id)")
        _NATIVE_SOLUTIONS[key] = solution
    return solution.connection


def _native_key(path: Path) -> tuple[str, int, int]:
    resolved = path.resolve()
    stat = resolved.stat()
    return str(resolved), stat.st_size, stat.st_mtime_ns


def _parse_native_period(
    raw: str,
    *,
    year: int,
    month: int,
    day: int,
) -> datetime:
    candidates = []
    try:
        candidates.append(datetime.fromisoformat(raw.replace("Z", "+00:00")))
    except ValueError:
        pass
    for pattern in ("%m/%d/%Y %H:%M:%S", "%d/%m/%Y %H:%M:%S"):
        try:
            candidates.append(datetime.strptime(raw, pattern))
        except ValueError:
            pass
    matches = {
        value
        for value in candidates
        if (value.year, value.month, value.day) == (year, month, day)
    }
    if len(matches) != 1:
        raise ValueError(
            f"Native PLEXOS period {raw!r} is inconsistent with calendar metadata "
            f"{year:04d}-{month:02d}-{day:02d}"
        )
    value = next(iter(matches))
    if value.tzinfo is not None:
        raise ValueError("Native PLEXOS period unexpectedly contains timezone metadata")
    return value


def _native_period_map(path: Path) -> dict[datetime, int]:
    key = _native_key(path)
    cached = _NATIVE_PERIODS.get(key)
    if cached is not None:
        return cached
    connection = _native_connection(path)
    result = {}
    for interval_id, raw, year, month, day in connection.execute(
        "SELECT interval_id, datetime, year, month_of_year, day_of_month FROM t_period_0"
    ):
        timestamp = _parse_native_period(
            str(raw), year=int(year), month=int(month), day=int(day)
        )
        if timestamp in result:
            raise ValueError(f"Duplicate native interval timestamp: {timestamp.isoformat()}")
        result[timestamp] = int(interval_id)
    if not result:
        raise ValueError("Native Solution ZIP contains no interval periods")
    _NATIVE_PERIODS[key] = result
    return result


def _read_native_generator_property(
    path: Path,
    selection: SolutionSelection,
    property_name: str,
) -> list[dict]:
    if selection.timestamp.tzinfo is not None:
        raise ValueError(
            "Native Solution selection must use the timezone-naive source wall clock"
        )
    if selection.phase.upper() != "ST" or selection.period.lower() != "interval":
        raise ValueError(
            f"V1 native Solution ZIP reader supports ST/Interval/Generators/{property_name} only"
        )
    object_names, sample_names = _zip_metadata(path)
    connection = _native_connection(path)
    query = """
        SELECT m.child_object_id, k.sample_id, k.band_id, dv.value, u.value
        FROM t_data_values dv
        JOIN t_key k ON k.key_id = dv.key_id
        JOIN t_membership m ON m.membership_id = k.membership_id
        JOIN t_property property
          ON property.property_id = k.property_id
         AND property.collection_id = m.collection_id
        LEFT JOIN t_unit u ON u.unit_id = property.unit_id
        WHERE k.phase_id = 4
          AND k.period_type_id = 0
          AND property.name = ?
          AND m.collection_id = 1
          AND dv.block_id = ?
    """
    selected_local = selection.timestamp.replace(tzinfo=None)
    interval_id = _native_period_map(path).get(selected_local)
    if interval_id is None:
        raise ValueError(
            f"Timestamp {selected_local.isoformat()} is absent from native Solution ZIP"
        )
    rows = []
    available_samples: set[str] = set()
    for object_id, sample_id, band, value, unit in connection.execute(
        query, (property_name, interval_id)
    ):
        object_id = int(object_id)
        sample_id = int(sample_id)
        band = int(band)
        sample = sample_names.get(sample_id, f"Sample {sample_id}")
        available_samples.add(sample)
        if sample.casefold() != selection.sample.casefold():
            continue
        if band != 1:
            raise ValueError(
                f"{property_name} has unsupported band {band} for object {object_id}"
            )
        rows.append(
            {
                "timestamp": selected_local,
                "object_id": object_id,
                "object_name": object_names.get(object_id, f"object:{object_id}"),
                "sample": sample,
                "value": float(value),
                "unit": unit or "",
                "source_interval_id": interval_id,
            }
        )
    if not rows:
        raise ValueError(
            f"No native {property_name} rows match timestamp/sample; "
            f"available samples are {sorted(available_samples)}"
        )
    names = [row["object_name"] for row in rows]
    if len(names) != len(set(names)):
        raise ValueError(f"Duplicate native {property_name} Generator rows")
    return sorted(rows, key=lambda item: item["object_name"])


def _read_solution_zip(path: Path, selection: SolutionSelection) -> list[DispatchRecord]:
    return [
        DispatchRecord(
            row["timestamp"],
            row["object_name"],
            _to_mw(row["value"], row["unit"]),
            row["unit"],
            "ST",
            "Interval",
            row["sample"],
            row["object_id"],
        )
        for row in _read_native_generator_property(path, selection, "Generation")
    ]


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


def list_solution_timestamps(path: str | Path) -> list[datetime]:
    """List exact source wall-clock timestamps without inventing timezone metadata."""
    source = Path(path)
    if source.suffix.casefold() == ".zip":
        connection = _native_connection(source)
        generation_interval_ids = {
            int(row[0])
            for row in connection.execute(
                """
                SELECT DISTINCT dv.block_id
                FROM t_data_values dv
                JOIN t_key k ON k.key_id = dv.key_id
                JOIN t_membership m ON m.membership_id = k.membership_id
                JOIN t_property property
                  ON property.property_id = k.property_id
                 AND property.collection_id = m.collection_id
                WHERE k.phase_id = 4
                  AND k.period_type_id = 0
                  AND property.name = 'Generation'
                  AND m.collection_id = 1
                """
            )
        }
        values = {
            timestamp
            for timestamp, interval_id in _native_period_map(source).items()
            if interval_id in generation_interval_ids
        }
    else:
        with source.open("r", encoding="utf-8-sig", newline="") as stream:
            reader = csv.DictReader(stream)
            fields = reader.fieldnames or []
            time_field = "time" if "time" in fields else "_date" if "_date" in fields else None
            if time_field is None:
                raise ValueError("PLEXOS result has no time/_date column")
            values = {_parse_datetime(row[time_field]) for row in reader if row.get(time_field)}
    if any(value.tzinfo is not None for value in values):
        raise ValueError("Source carries timezone metadata; explicit aware-source support is required")
    if not values:
        raise ValueError("PLEXOS result contains no timestamps")
    return sorted(values)


def inspect_solution(path: str | Path) -> dict:
    source = Path(path)
    if source.suffix.casefold() == ".zip":
        objects, samples = _zip_metadata(source)
        return {
            "format": "PLEXOS Solution ZIP",
            "objects": len(objects),
            "samples": sorted(set(samples.values())),
            "supported_table": "ST__Interval__Generators__Generation",
            "supported_commitment_table": "ST__Interval__Generators__Units Generating",
            "time_series_timestamp_discovery": True,
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
