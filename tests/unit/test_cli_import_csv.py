"""Tests for the ``tablespec import-csv`` command (CSV(s) -> split-format specs)."""

from __future__ import annotations

from pathlib import Path

import yaml
from typer.testing import CliRunner

from tablespec.cli import app

runner = CliRunner(env={"NO_COLOR": "1", "TERM": "dumb"})


def test_import_csv_directory(tmp_path: Path) -> None:
    src = tmp_path / "data"
    src.mkdir()
    (src / "orders.csv").write_text("order_id,amount\n1,10.5\n")
    (src / "members.csv").write_text("member_id|name\n1|ann\n")
    dest = tmp_path / "specs"

    result = runner.invoke(app, ["import-csv", str(src), str(dest)])

    assert result.exit_code == 0, result.output
    assert "2 spec(s)" in result.output
    for table in ("orders", "members"):
        table_yaml = dest / table / "table.yaml"
        assert table_yaml.exists()
    spec = yaml.safe_load((dest / "members" / "table.yaml").read_text())
    assert spec["source"]["delimiter"] == "|"
    assert spec["source"]["path"].endswith("members.csv")


def test_import_csv_single_file(tmp_path: Path) -> None:
    csv = tmp_path / "orders.csv"
    csv.write_text("order_id,amount\n1,10.5\n")
    dest = tmp_path / "specs"

    result = runner.invoke(app, ["import-csv", str(csv), str(dest)])

    assert result.exit_code == 0, result.output
    assert (dest / "orders" / "table.yaml").exists()


def test_import_csv_existing_dest_requires_force(tmp_path: Path) -> None:
    csv = tmp_path / "orders.csv"
    csv.write_text("order_id\n1\n")
    dest = tmp_path / "specs"
    dest.mkdir()

    result = runner.invoke(app, ["import-csv", str(csv), str(dest)])
    assert result.exit_code == 1
    assert "--force" in result.output

    result = runner.invoke(app, ["import-csv", str(csv), str(dest), "--force"])
    assert result.exit_code == 0, result.output
    assert (dest / "orders" / "table.yaml").exists()


def test_import_csv_explicit_delimiter(tmp_path: Path) -> None:
    csv = tmp_path / "odd.csv"
    csv.write_text("a|b, c\n1|2\n")
    dest = tmp_path / "specs"

    result = runner.invoke(app, ["import-csv", str(csv), str(dest), "--delimiter", "|"])

    assert result.exit_code == 0, result.output
    spec = yaml.safe_load((dest / "odd" / "table.yaml").read_text())
    assert spec["source"]["delimiter"] == "|"


def test_import_csv_bad_header_fails(tmp_path: Path) -> None:
    csv = tmp_path / "bad.csv"
    csv.write_text("a,,c\n1,2,3\n")

    result = runner.invoke(app, ["import-csv", str(csv), str(tmp_path / "specs")])

    assert result.exit_code == 1
    assert "empty" in result.output
