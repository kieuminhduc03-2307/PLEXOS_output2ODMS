from __future__ import annotations

import csv
import json
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass(frozen=True)
class BranchRatingMapping:
    source_uid: str
    source_from_bus: int
    source_to_bus: int
    source_r_pu: float
    source_x_pu: float
    source_b_pu: float
    source_cont_rating_mva: float
    source_lte_rating_mva: float
    source_ste_rating_mva: float
    target_name: str
    target_mrid: str
    target_kind: str
    approved: bool
    mapping_basis: str = "exact unordered buses+R+X; ordinal pairing for identical parallels"
    rating_contract: str = "Cont->ConditionA; LTE->ConditionB; STE->ConditionC"


def _source_rows(path: str | Path) -> list[dict]:
    result = []
    with Path(path).open("r", encoding="utf-8-sig", newline="") as stream:
        for row in csv.DictReader(stream):
            result.append(
                {
                    "uid": row["UID"].strip(),
                    "from_bus": int(row["From Bus"]),
                    "to_bus": int(row["To Bus"]),
                    "r": float(row["R"]),
                    "x": float(row["X"]),
                    "b": float(row["B"]),
                    "cont": float(row["Cont Rating"]),
                    "lte": float(row["LTE Rating"]),
                    "ste": float(row["STE Rating"]),
                }
            )
    if not result:
        raise ValueError("RTS branch data contains no rows")
    return result


def _key(row: dict, tolerance_digits: int = 6) -> tuple:
    buses = sorted((int(row["from_bus"]), int(row["to_bus"])))
    return buses[0], buses[1], round(float(row["r"]), tolerance_digits), round(float(row["x"]), tolerance_digits)


def build_rts_branch_rating_crosswalk(
    branch_data: str | Path,
    odms_ac_audit: str | Path,
    *,
    approved: bool = False,
) -> list[BranchRatingMapping]:
    source = _source_rows(branch_data)
    audit = json.loads(Path(odms_ac_audit).read_text(encoding="utf-8"))
    if not audit.get("valid"):
        raise ValueError("ODMS AC audit is not valid")
    targets = []
    for row in audit.get("branches", []):
        targets.append(
            {
                "from_bus": int(row["from_section"]["mapped_bus_number"]),
                "to_bus": int(row["to_section"]["mapped_bus_number"]),
                "r": float(row["r_pu"]),
                "x": float(row["x_pu"]),
                "b": float(row["b_pu"]),
                "name": row["name"],
                "mrid": row["mrid"],
                "kind": row["kind"],
            }
        )
    source_groups: dict[tuple, list[dict]] = {}
    target_groups: dict[tuple, list[dict]] = {}
    for row in source:
        source_groups.setdefault(_key(row), []).append(row)
    for row in targets:
        target_groups.setdefault(_key(row), []).append(row)
    if set(source_groups) != set(target_groups):
        missing = sorted(set(source_groups) - set(target_groups))
        extra = sorted(set(target_groups) - set(source_groups))
        raise ValueError(f"Branch electrical identity mismatch: missing={missing[:20]} extra={extra[:20]}")
    mappings = []
    for key in sorted(source_groups):
        sources = sorted(source_groups[key], key=lambda row: row["uid"])
        target_group = sorted(target_groups[key], key=lambda row: row["name"])
        if len(sources) != len(target_group):
            raise ValueError(f"Branch multiplicity mismatch for {key}")
        if len(sources) > 1:
            ratings = {(row["cont"], row["lte"], row["ste"]) for row in sources}
            if len(ratings) != 1:
                raise ValueError(f"Parallel branch ratings differ and cannot be paired safely for {key}")
        for source_row, target in zip(sources, target_group):
            mappings.append(
                BranchRatingMapping(
                    source_uid=source_row["uid"],
                    source_from_bus=source_row["from_bus"],
                    source_to_bus=source_row["to_bus"],
                    source_r_pu=source_row["r"],
                    source_x_pu=source_row["x"],
                    source_b_pu=source_row["b"],
                    source_cont_rating_mva=source_row["cont"],
                    source_lte_rating_mva=source_row["lte"],
                    source_ste_rating_mva=source_row["ste"],
                    target_name=target["name"],
                    target_mrid=target["mrid"],
                    target_kind=target["kind"],
                    approved=approved,
                )
            )
    if len(mappings) != len(source):
        raise ValueError("RTS branch rating crosswalk is incomplete")
    return sorted(mappings, key=lambda row: row.source_uid)


def write_branch_rating_crosswalk(
    mappings: list[BranchRatingMapping],
    path: str | Path,
    *,
    source_branch_data: str,
    odms_ac_audit: str,
    raw_reference: str | None = None,
) -> None:
    payload = {
        "schema": "plexos-output2odms-branch-rating-crosswalk-v1",
        "profile": "rts-gmlc",
        "source_branch_data": source_branch_data,
        "raw_reference": raw_reference,
        "odms_ac_audit": odms_ac_audit,
        "rating_contract": {
            "ConditionA": "Cont Rating (continuous/normal)",
            "ConditionB": "LTE Rating (long-term emergency)",
            "ConditionC": "STE Rating (short-term emergency)",
            "evidence_note": (
                "Same-case RAW preserves Cont as RATEA but collapses RATEB/RATEC to Cont; "
                "official branch.csv remains authoritative for LTE/STE."
            ),
        },
        "mapping_count": len(mappings),
        "mappings": [asdict(row) for row in mappings],
    }
    Path(path).write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def load_branch_rating_crosswalk(path: str | Path) -> list[BranchRatingMapping]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if payload.get("schema") != "plexos-output2odms-branch-rating-crosswalk-v1":
        raise ValueError("Unsupported branch rating crosswalk schema")
    rows = [BranchRatingMapping(**row) for row in payload.get("mappings", [])]
    if not rows or not all(row.approved for row in rows):
        raise ValueError("Branch rating crosswalk is empty or not fully approved")
    targets = [row.target_mrid for row in rows]
    if len(targets) != len(set(targets)):
        raise ValueError("Branch rating crosswalk contains duplicate ODMS targets")
    return rows
