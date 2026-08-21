from datetime import datetime
from pathlib import Path

import pytest

from plexos_output2odms.crosswalk.load_snapshot import LoadMapping
from plexos_output2odms.plexos_solution.load_series import read_normalized_load_series
from plexos_output2odms.plexos_solution.regional_load import allocate_rts_nodal_load


def mapping(*, approved=True):
    return LoadMapping("101", "1", 10.0, 2.0, "101_1", "load-101", approved)


def write(path: Path, rows: list[str]):
    path.write_text(
        "timestamp,source_load_id,p_mw,q_mvar,p_provenance,q_provenance,q_policy\n"
        + "\n".join(rows) + "\n",
        encoding="utf-8",
    )


def test_generic_load_preserves_p_q_and_provenance(tmp_path: Path):
    path = tmp_path / "load.csv"
    write(path, ["2020-07-05T00:00:00,101,12.5,2.5,PLEXOS_OUTPUT,DERIVED,preserve_pf"])
    rows = read_normalized_load_series(path, datetime(2020, 7, 5), [mapping()])
    assert (rows[0]["load_p_mw"], rows[0]["load_q_mvar"]) == (12.5, 2.5)
    assert (rows[0]["p_provenance"], rows[0]["q_provenance"], rows[0]["q_policy"]) == (
        "PLEXOS_OUTPUT", "DERIVED", "preserve_pf"
    )


@pytest.mark.parametrize("bad", [
    ["2020-07-05T01:00:00,101,12.5,2.5,P,Q,policy"],
    ["2020-07-05T00:00:00,101,12.5,2.5,P,Q,policy", "2020-07-05T00:00:00,101,12.5,2.5,P,Q,policy"],
])
def test_generic_load_rejects_missing_exact_timestamp_and_duplicates(tmp_path: Path, bad):
    path = tmp_path / "load.csv"
    write(path, bad)
    with pytest.raises(ValueError):
        read_normalized_load_series(path, datetime(2020, 7, 5), [mapping()])


def test_generic_load_rejects_unapproved_mapping(tmp_path: Path):
    path = tmp_path / "load.csv"
    write(path, ["2020-07-05T00:00:00,101,12.5,2.5,P,Q,policy"])
    with pytest.raises(ValueError, match="approved"):
        read_normalized_load_series(path, datetime(2020, 7, 5), [mapping(approved=False)])


def test_generic_load_rejects_missing_and_unmapped_identities(tmp_path: Path):
    path = tmp_path / "load.csv"
    write(path, ["2020-07-05T00:00:00,101,12.5,2.5,P,Q,policy"])
    second = LoadMapping("102", "1", 1.0, 0.0, "102_1", "load-102", True)
    with pytest.raises(ValueError, match="missing mapped identities"):
        read_normalized_load_series(path, datetime(2020, 7, 5), [mapping(), second])
    write(path, ["2020-07-05T00:00:00,UNKNOWN,1,0,P,Q,policy"])
    with pytest.raises(ValueError, match="Unapproved/unmapped"):
        read_normalized_load_series(path, datetime(2020, 7, 5), [mapping()])


def test_generic_and_rts_profiles_have_same_operating_shape(tmp_path: Path):
    path = tmp_path / "load.csv"
    write(path, ["2020-07-05T00:00:00,101,12,2.4,P,Q,preserve_base_pf"])
    generic = read_normalized_load_series(path, datetime(2020, 7, 5), [mapping()])[0]
    rts = allocate_rts_nodal_load({"1": 12.0}, [mapping()])[0]
    required = {
        "resource_type", "source_load_id", "source_bus_id", "source_region",
        "base_p_mw", "base_q_mvar", "load_p_mw", "load_q_mvar",
        "p_provenance", "q_provenance", "q_policy", "target_load_name", "target_load_mrid",
    }
    assert required <= generic.keys()
    assert required <= rts.keys()
