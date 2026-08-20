from __future__ import annotations

from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from plexos_output2odms.plexos_solution.dispatch import SolutionSelection
from plexos_output2odms.plexos_solution.reader import inspect_solution, read_dispatch


def selection(unit: str | None = "MW") -> SolutionSelection:
    return SolutionSelection(
        "ST", "Interval", datetime(2020, 7, 5, tzinfo=ZoneInfo("Asia/Ho_Chi_Minh")), "Mean", unit
    )


def test_wide_dispatch_extracts_exact_timestamp(tmp_path: Path):
    source = tmp_path / "generation.txt"
    source.write_text(
        '"time","G1","G2"\n2020-07-05 00:00:00,76,12.5\n2020-07-05 01:00:00,80,10\n',
        encoding="utf-8",
    )
    rows = read_dispatch(source, selection())
    assert [(row.generator_name, row.generation_mw) for row in rows] == [("G1", 76.0), ("G2", 12.5)]
    info = inspect_solution(source)
    assert info["generator_columns"] == 2
    assert info["rows"] == 2


def test_wide_dispatch_requires_explicit_unit(tmp_path: Path):
    source = tmp_path / "generation.csv"
    source.write_text("time,G1\n2020-07-05 00:00:00,1\n", encoding="utf-8")
    with pytest.raises(ValueError, match="--unit MW"):
        read_dispatch(source, selection(None))


def test_energy_summary_is_rejected(tmp_path: Path):
    source = tmp_path / "summary.csv"
    source.write_text(
        "child_name,property_name,_date,value,unit_name,phase_name,sample_name\n"
        "G1,Generation,2020-07-05 00:00:00,8,GWh,ST Schedule,Mean\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="Energy summaries"):
        read_dispatch(source, selection())
