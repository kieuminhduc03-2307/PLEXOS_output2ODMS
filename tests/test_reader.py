from __future__ import annotations

from datetime import datetime
from pathlib import Path
import sqlite3
from zipfile import ZipFile

import pytest

from plexos_output2odms.plexos_solution.dispatch import SolutionSelection
from plexos_output2odms.plexos_solution.reader import (
    inspect_solution,
    list_solution_timestamps,
    read_dispatch,
)
import plexos_output2odms.plexos_solution.reader as reader_module
from plexos_output2odms.plexos_solution.commitment import read_commitment


def selection(unit: str | None = "MW") -> SolutionSelection:
    return SolutionSelection(
        "ST", "Interval", datetime(2020, 7, 5), "Mean", unit
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
    assert list_solution_timestamps(source) == [
        datetime(2020, 7, 5, 0, 0),
        datetime(2020, 7, 5, 1, 0),
    ]


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


@pytest.fixture
def native_solution(tmp_path: Path, monkeypatch):
    source = tmp_path / "Native Solution.zip"
    xml = """
    <Solution>
      <t_object><object_id>101</object_id><name>G1</name></t_object>
      <t_sample><sample_id>0</sample_id><sample_name>Mean</sample_name></t_sample>
    </Solution>
    """
    with ZipFile(source, "w") as archive:
        archive.writestr("Native Solution.xml", xml)

    connection = sqlite3.connect(":memory:")
    connection.executescript(
        """
        CREATE TABLE t_data_values(key_id INTEGER, period_type_id INTEGER, block_id INTEGER, value REAL);
        CREATE TABLE t_key(
          band_id INTEGER, key_id INTEGER, membership_id INTEGER, period_type_id INTEGER,
          phase_id INTEGER, property_id INTEGER, sample_id INTEGER
        );
        CREATE TABLE t_membership(child_object_id INTEGER, collection_id INTEGER, membership_id INTEGER);
        CREATE TABLE t_property(collection_id INTEGER, name TEXT, property_id INTEGER, unit_id INTEGER);
        CREATE TABLE t_unit(unit_id INTEGER, value TEXT);
        CREATE TABLE t_period_0(
          interval_id INTEGER, datetime TEXT, year INTEGER, month_of_year INTEGER, day_of_month INTEGER
        );
        INSERT INTO t_membership VALUES (101, 1, 10);
        INSERT INTO t_property VALUES (1, 'Generation', 1, 1);
        INSERT INTO t_property VALUES (1, 'Units Generating', 2, 2);
        INSERT INTO t_unit VALUES (1, 'MW');
        INSERT INTO t_unit VALUES (2, '-');
        INSERT INTO t_key VALUES (1, 1001, 10, 0, 4, 1, 0);
        INSERT INTO t_key VALUES (1, 1002, 10, 0, 4, 2, 0);
        INSERT INTO t_period_0 VALUES (1, '05/04/2024 00:00:00', 2024, 4, 5);
        INSERT INTO t_period_0 VALUES (2, '06/04/2024 00:00:00', 2024, 4, 6);
        INSERT INTO t_data_values VALUES (1001, 0, 1, 76.5);
        INSERT INTO t_data_values VALUES (1001, 0, 2, 0.0);
        INSERT INTO t_data_values VALUES (1002, 0, 1, 1.0);
        INSERT INTO t_data_values VALUES (1002, 0, 2, 0.0);
        """
    )
    reader_module._NATIVE_PERIODS.clear()
    monkeypatch.setattr(reader_module, "_native_connection", lambda _: connection)
    yield source
    connection.close()
    reader_module._NATIVE_PERIODS.clear()


def test_native_solution_zip_generation(native_solution: Path):
    rows = read_dispatch(
        native_solution,
        SolutionSelection("ST", "Interval", datetime(2024, 4, 5), "Mean", None),
    )
    assert [(row.generator_name, row.generation_mw, row.source_unit) for row in rows] == [
        ("G1", 76.5, "MW")
    ]


def test_native_solution_zip_timestamp_listing_uses_calendar_metadata(native_solution: Path):
    assert list_solution_timestamps(native_solution) == [
        datetime(2024, 4, 5),
        datetime(2024, 4, 6),
    ]


def test_native_solution_zip_commitment(native_solution: Path):
    assert read_commitment(native_solution, datetime(2024, 4, 5)) == {"G1": 1.0}
    assert read_commitment(native_solution, datetime(2024, 4, 6)) == {"G1": 0.0}
