# @covers US-034-AC2
"""Tests for UMF format loading and conversion (split ↔ JSON)."""

import json

import pytest
from ruamel.yaml import YAML

from tablespec import save_umf_to_yaml
from tablespec.umf_loader import UMFFormat, UMFLoader
from tablespec.models import UMF, UMFColumn

pytestmark = [pytest.mark.no_spark, pytest.mark.fast]


class TestFormatDetection:
    """Test UMF format auto-detection."""

    def test_detect_json_file(self, tmp_path):
        """Test detection of JSON format from .json file."""
        json_file = tmp_path / "test.umf.json"
        json_file.write_text('{"version": "1.0"}')

        converter = UMFLoader()
        format_type = converter.detect_format(json_file)

        assert format_type == UMFFormat.JSON

    def test_detect_split_directory(self, tmp_path):
        """Test detection of SPLIT format from directory structure."""
        split_dir = tmp_path / "test_table"
        split_dir.mkdir()
        (split_dir / "table.yaml").write_text("version: '1.0'\n")
        (split_dir / "columns").mkdir()

        converter = UMFLoader()
        format_type = converter.detect_format(split_dir)

        assert format_type == UMFFormat.SPLIT

    def test_detect_format_invalid_path(self, tmp_path):
        """Test error handling for invalid paths."""
        converter = UMFLoader()

        with pytest.raises(FileNotFoundError, match="Path does not exist"):
            converter.detect_format(tmp_path / "nonexistent")

    def test_detect_format_directory_without_schema(self, tmp_path):
        """Test error handling for directory without table.yaml."""
        converter = UMFLoader()

        with pytest.raises(FileNotFoundError, match="no table.yaml"):
            converter.detect_format(tmp_path)

    @pytest.mark.parametrize("suffix", [".yaml", ".yml", ".umf"])
    def test_detect_inline_yaml_file(self, tmp_path, suffix):
        """A single .yaml/.yml/.umf file is rejected from normal auto-detection."""
        inline_file = tmp_path / f"table{suffix}"
        inline_file.write_text("version: '1.0'\ntable_name: t\ncolumns: []\n")

        with pytest.raises(ValueError, match="legacy-only"):
            UMFLoader().detect_format(inline_file)


class TestLegacyInlineYamlMigration:
    """Legacy single-file YAML/.umf UMF migration helpers."""

    @pytest.mark.parametrize("suffix", [".yaml", ".yml", ".umf"])
    def test_load_single_inline_yaml_file_rejected_by_default_load(
        self, tmp_path, suffix
    ):
        """UMFLoader.load() rejects a whole-UMF YAML document from one file."""
        umf = UMF(
            version="1.0",
            table_name="metrics",
            columns=[UMFColumn(name="id", data_type="INTEGER")],
        )
        inline_file = tmp_path / f"metrics.umf{suffix}"
        save_umf_to_yaml(umf, inline_file)

        with pytest.raises(ValueError, match="legacy-only"):
            UMFLoader().load(inline_file)

    def test_migrate_legacy_inline_yaml_populates_expectations_from_legacy(
        self, tmp_path
    ):
        """Explicit migration preserves the legacy validation_rules -> expectations behavior."""
        inline_file = tmp_path / "legacy.umf.yaml"
        inline_file.write_text(
            "version: '1.0'\n"
            "table_name: legacy\n"
            "columns:\n"
            "  - name: id\n"
            "    data_type: INTEGER\n"
            "validation_rules:\n"
            "  expectations:\n"
            "    - type: expect_column_to_exist\n"
            "      kwargs:\n"
            "        column: id\n"
        )

        out_dir = tmp_path / "migrated"
        UMFLoader().migrate_legacy_inline_yaml(inline_file, out_dir)

        loaded = UMFLoader().load(out_dir)
        assert loaded.expectations is not None


class TestRoundtripConversion:
    """Test roundtrip conversion: SPLIT ↔ JSON."""

    @pytest.fixture
    def complete_umf_dir(self, tmp_path):
        """Create a complete UMF in SPLIT format."""
        umf = UMF(
            version="1.0",
            table_name="complete_table",
            canonical_name="CompleteTable",
            aliases=["complete_tbl", "CompleteTbl"],
            description="Complete table with all fields",
            columns=[
                UMFColumn(
                    name="id",
                    data_type="INTEGER",
                    nullable={"MD": False, "MP": False, "ME": False},
                    description="Primary key",
                ),
                UMFColumn(
                    name="name",
                    data_type="VARCHAR",
                    length=100,
                    nullable={"MD": True, "MP": True, "ME": True},
                    sample_values=["Alice", "Bob"],
                ),
                UMFColumn(
                    name="created_at",
                    data_type="DATETIME",
                    nullable={"MD": False, "MP": False, "ME": False},
                ),
            ],
        )

        umf_dir = tmp_path / "complete"
        loader = UMFLoader()
        loader.save(umf, umf_dir)
        return umf_dir

    def test_roundtrip_split_to_json_to_split(self, complete_umf_dir, tmp_path):
        """Test complete roundtrip: SPLIT → JSON → SPLIT."""
        converter = UMFLoader()

        # Load original
        original = UMFLoader().load(complete_umf_dir)

        # Convert to JSON
        json_file = tmp_path / "converted.json"
        converter.convert(complete_umf_dir, json_file, target_format=UMFFormat.JSON)

        # Convert back to SPLIT
        final_split = tmp_path / "final_split"
        converter.convert(json_file, final_split, target_format=UMFFormat.SPLIT)

        # Load result
        result = UMFLoader().load(final_split)

        # Compare key fields
        assert result.table_name == original.table_name
        assert result.canonical_name == original.canonical_name
        assert result.description == original.description
        assert len(result.columns) == len(original.columns)
        # Aliases are sorted, so compare as sets
        assert set(result.aliases or []) == set(original.aliases or [])

    def test_roundtrip_preserves_column_details(self, complete_umf_dir, tmp_path):
        """Test that column details are preserved through roundtrip."""
        converter = UMFLoader()

        original = UMFLoader().load(complete_umf_dir)

        # Roundtrip through JSON
        json_file = tmp_path / "intermediate.json"
        converter.convert(complete_umf_dir, json_file, target_format=UMFFormat.JSON)

        final_split = tmp_path / "final"
        converter.convert(json_file, final_split, target_format=UMFFormat.SPLIT)

        result = UMFLoader().load(final_split)

        # Check columns match (by name, since they may be reordered)
        orig_by_name = {col.name: col for col in original.columns}
        result_by_name = {col.name: col for col in result.columns}

        assert set(orig_by_name.keys()) == set(result_by_name.keys())

        for col_name, orig_col in orig_by_name.items():
            result_col = result_by_name[col_name]
            assert result_col.data_type == orig_col.data_type
            assert result_col.length == orig_col.length
            assert result_col.nullable == orig_col.nullable


class TestExpectationSuitePersistence:
    """Test expectations.yaml persistence in split format (ADR-005)."""

    def test_save_split_writes_expectations_yaml(self, tmp_path):
        """When umf.expectations is populated, saver writes expectations.yaml."""
        from tablespec.models.umf import Expectation, ExpectationMeta, ExpectationSuite

        loader = UMFLoader()
        umf = UMF(
            version="1.0",
            table_name="test_table",
            canonical_name="TestTable",
            columns=[UMFColumn(name="id", data_type="VARCHAR")],
            expectations=ExpectationSuite(
                expectations=[
                    Expectation(
                        type="expect_column_values_to_not_be_null",
                        kwargs={"column": "id"},
                        meta=ExpectationMeta(stage="raw", severity="critical"),
                    ),
                ],
            ),
        )

        target = tmp_path / "out"
        loader.save(umf, target)

        assert (target / "expectations.yaml").exists()
        # Legacy files should NOT be written
        assert not (target / "validation_rules.yaml").exists()
        assert not (target / "quality_checks.yaml").exists()

    def test_roundtrip_preserves_ingestion(self, tmp_path):
        """ingestion (mode/order_by) must survive save->load in split format.

        Regression: _save_split previously omitted ingestion, so a spec with
        incremental MERGE reloaded as ingestion=None and downstream fell back
        to a full-rebuild ``table`` materialization.
        """
        from tablespec.models.umf import IngestionConfig

        loader = UMFLoader()
        umf = UMF(
            version="1.0",
            table_name="test_table",
            canonical_name="TestTable",
            primary_key=["id"],
            columns=[UMFColumn(name="id", data_type="VARCHAR")],
            ingestion=IngestionConfig(mode="incremental", order_by=["_load_ts"]),
        )

        target = tmp_path / "out"
        loader.save(umf, target)
        reloaded = loader.load(target)

        assert reloaded.ingestion is not None
        assert reloaded.ingestion.mode == "incremental"
        assert reloaded.ingestion.order_by == ["_load_ts"]
        assert reloaded.primary_key == ["id"]

    def test_save_split_expectations_splits_by_column(self, tmp_path):
        """Column-specific expectations go to column files, cross-column to expectations.yaml."""
        from tablespec.models.umf import Expectation, ExpectationMeta, ExpectationSuite

        loader = UMFLoader()
        umf = UMF(
            version="1.0",
            table_name="test_table",
            canonical_name="TestTable",
            columns=[
                UMFColumn(name="id", data_type="VARCHAR"),
                UMFColumn(name="name", data_type="VARCHAR"),
            ],
            expectations=ExpectationSuite(
                expectations=[
                    # Cross-column expectation
                    Expectation(
                        type="expect_table_columns_to_match_set",
                        kwargs={"column_set": ["id", "name"]},
                        meta=ExpectationMeta(stage="raw"),
                    ),
                    # Column-specific expectation
                    Expectation(
                        type="expect_column_values_to_not_be_null",
                        kwargs={"column": "id"},
                        meta=ExpectationMeta(stage="raw", severity="critical"),
                    ),
                ],
            ),
        )

        target = tmp_path / "out"
        loader.save(umf, target)

        # Cross-column in expectations.yaml
        exp_data = YAML().load((target / "expectations.yaml").read_text())
        assert len(exp_data["expectations"]) == 1
        assert (
            exp_data["expectations"][0]["type"] == "expect_table_columns_to_match_set"
        )

        # Column-specific in column file
        col_data = YAML().load((target / "columns" / "id.yaml").read_text())
        assert "validations" in col_data
        assert len(col_data["validations"]) == 1
        assert (
            col_data["validations"][0]["type"] == "expect_column_values_to_not_be_null"
        )

    def test_save_split_expectations_thresholds_and_pending(self, tmp_path):
        """Thresholds, alert_config, and pending are persisted in expectations.yaml."""
        from tablespec.models.umf import Expectation, ExpectationMeta, ExpectationSuite

        loader = UMFLoader()
        umf = UMF(
            version="1.0",
            table_name="test_table",
            canonical_name="TestTable",
            columns=[UMFColumn(name="id", data_type="VARCHAR")],
            expectations=ExpectationSuite(
                expectations=[
                    Expectation(
                        type="expect_table_columns_to_match_set",
                        kwargs={"column_set": ["id"]},
                        meta=ExpectationMeta(stage="raw"),
                    ),
                ],
                pending=[
                    Expectation(
                        type="expect_column_values_to_be_unique",
                        kwargs={"column": "id"},
                        meta=ExpectationMeta(stage="raw"),
                    ),
                ],
                thresholds={"max_critical_failure_percent": 5.0},
                alert_config={"channel": "ops"},
            ),
        )

        target = tmp_path / "out"
        loader.save(umf, target)

        exp_data = YAML().load((target / "expectations.yaml").read_text())
        assert exp_data["thresholds"] == {"max_critical_failure_percent": 5.0}
        assert exp_data["alert_config"] == {"channel": "ops"}
        assert len(exp_data["pending"]) == 1

    def test_roundtrip_expectations_via_split(self, tmp_path):
        """ExpectationSuite survives save→load roundtrip in split format."""
        from tablespec.models.umf import Expectation, ExpectationMeta, ExpectationSuite

        loader = UMFLoader()
        umf = UMF(
            version="1.0",
            table_name="test_table",
            canonical_name="TestTable",
            columns=[
                UMFColumn(name="id", data_type="VARCHAR"),
                UMFColumn(name="value", data_type="INTEGER"),
            ],
            expectations=ExpectationSuite(
                expectations=[
                    Expectation(
                        type="expect_table_columns_to_match_set",
                        kwargs={"column_set": ["id", "value"]},
                        meta=ExpectationMeta(stage="raw"),
                    ),
                    Expectation(
                        type="expect_column_values_to_not_be_null",
                        kwargs={"column": "id"},
                        meta=ExpectationMeta(stage="raw", severity="critical"),
                    ),
                    Expectation(
                        type="expect_column_values_to_be_between",
                        kwargs={"column": "value", "min_value": 0, "max_value": 100},
                        meta=ExpectationMeta(
                            stage="ingested", severity="warning", blocking=True
                        ),
                    ),
                ],
                thresholds={"max_critical_failure_percent": 5.0},
            ),
        )

        target = tmp_path / "out"
        loader.save(umf, target)
        loaded = loader.load(target)

        assert loaded.expectations is not None
        assert len(loaded.expectations.expectations) == 3
        assert len(loaded.expectations.raw) == 2
        assert len(loaded.expectations.ingested) == 1
        assert loaded.expectations.thresholds == {"max_critical_failure_percent": 5.0}

    def test_load_expectations_yaml_directly(self, tmp_path):
        """Loading from a dir with expectations.yaml populates umf.expectations."""
        (tmp_path / "table.yaml").write_text(
            "version: '1.0'\ntable_name: test_table\ncanonical_name: TestTable\n"
        )
        (tmp_path / "expectations.yaml").write_text(
            "expectations:\n"
            "  - type: expect_column_values_to_not_be_null\n"
            "    kwargs:\n"
            "      column: id\n"
            "    meta:\n"
            "      stage: raw\n"
            "      severity: critical\n"
        )
        (tmp_path / "columns").mkdir()
        (tmp_path / "columns" / "id.yaml").write_text(
            "column:\n  name: id\n  data_type: VARCHAR\n"
        )

        loader = UMFLoader()
        umf = loader.load(tmp_path)

        assert umf.expectations is not None
        assert len(umf.expectations.expectations) == 1
        assert umf.expectations.expectations[0].meta.stage == "raw"
        assert umf.expectations.expectations[0].meta.severity == "critical"

    def test_load_json_populates_expectations_from_legacy(self, tmp_path):
        """JSON loader populates expectations from legacy validation_rules."""
        json_data = {
            "version": "1.0",
            "table_name": "test_table",
            "canonical_name": "TestTable",
            "columns": [{"name": "id", "data_type": "VARCHAR"}],
            "validation_rules": {
                "expectations": [
                    {
                        "type": "expect_column_values_to_not_be_null",
                        "kwargs": {"column": "id"},
                    }
                ]
            },
        }
        json_file = tmp_path / "test.json"
        json_file.write_text(json.dumps(json_data))

        loader = UMFLoader()
        umf = loader.load(json_file)

        assert umf.expectations is not None
        assert len(umf.expectations.expectations) == 1

    def test_load_column_validations_merge_into_expectations(self, tmp_path):
        """Column-level validations merge into expectations suite when expectations.yaml exists."""
        (tmp_path / "table.yaml").write_text(
            "version: '1.0'\ntable_name: test_table\ncanonical_name: TestTable\n"
        )
        (tmp_path / "expectations.yaml").write_text(
            "expectations:\n"
            "  - type: expect_table_columns_to_match_set\n"
            "    kwargs:\n"
            "      column_set: [id]\n"
            "    meta:\n"
            "      stage: raw\n"
        )
        (tmp_path / "columns").mkdir()
        (tmp_path / "columns" / "id.yaml").write_text(
            "column:\n  name: id\n  data_type: VARCHAR\n"
            "validations:\n"
            "  - type: expect_column_values_to_not_be_null\n"
            "    kwargs:\n"
            "      column: id\n"
        )

        loader = UMFLoader()
        umf = loader.load(tmp_path)

        assert umf.expectations is not None
        # 1 from expectations.yaml + 1 from column file
        assert len(umf.expectations.expectations) == 2


class TestQualityChecksPersistence:
    """Test quality_checks persistence in split format."""

    def test_save_split_persists_quality_thresholds_and_alerts(self, tmp_path):
        """quality_checks thresholds/alert_config should be written to quality_checks.yaml."""
        loader = UMFLoader()
        quality_thresholds = {
            "max_critical_failure_percent": 5.0,
            "min_success_rate": 95.0,
        }
        alert_config = {"channel": "ops", "threshold_breach_only": True}

        umf = UMF(
            version="1.0",
            table_name="quality_table",
            canonical_name="QualityTable",
            columns=[
                UMFColumn(
                    name="id",
                    data_type="VARCHAR",
                )
            ],
            quality_checks={
                "checks": [
                    {
                        "expectation": {
                            "expectation_type": "expect_column_values_to_not_be_null",
                            "kwargs": {"column": "id"},
                        },
                        "severity": "critical",
                        "blocking": True,
                    }
                ],
                "thresholds": quality_thresholds,
                "alert_config": alert_config,
            },
        )

        target_dir = tmp_path / "pipeline" / "tables" / "quality_table"
        loader.save(umf, target_dir)

        qc_path = target_dir / "quality_checks.yaml"
        assert qc_path.exists()

        qc_data = YAML().load(qc_path.read_text())
        assert qc_data["thresholds"] == quality_thresholds
        assert qc_data["alert_config"] == alert_config


class TestFormatInference:
    """Test automatic format inference."""

    def test_infer_target_format_from_json_file(self, tmp_path):
        """Test inferring JSON format from .json extension."""
        umf = UMF(
            version="1.0",
            table_name="test",
            canonical_name="Test",
            columns=[UMFColumn(name="id", data_type="INTEGER")],
        )

        # Save as SPLIT
        split_dir = tmp_path / "test_table"
        loader = UMFLoader()
        loader.save(umf, split_dir)

        # Convert to JSON without specifying format - should infer from .json extension
        json_file = tmp_path / "output.json"
        converter = UMFLoader()
        converter.convert(split_dir, json_file)

        assert json_file.exists()
        result = UMFLoader().load(json_file)
        assert result.table_name == "test"

    def test_infer_target_format_from_directory(self, tmp_path):
        """Test inferring SPLIT format from directory path."""
        umf = UMF(
            version="1.0",
            table_name="test",
            canonical_name="Test",
            columns=[UMFColumn(name="id", data_type="INTEGER")],
        )

        # Save as JSON
        json_file = tmp_path / "test.json"
        loader = UMFLoader()
        loader.save_json(umf, json_file)

        # Convert to directory without specifying format - should infer SPLIT
        split_dir = tmp_path / "test_split"
        converter = UMFLoader()
        converter.convert(json_file, split_dir)

        assert split_dir.exists()
        assert (split_dir / "table.yaml").exists()
        result = UMFLoader().load(split_dir)
        assert result.table_name == "test"


class TestDataPreservation:
    """Test that all data is preserved through conversion."""

    def test_preserves_nullable_per_lob(self, tmp_path):
        """Test that nullable LOB-specific settings are preserved."""
        umf = UMF(
            version="1.0",
            table_name="test",
            canonical_name="Test",
            columns=[
                UMFColumn(
                    name="gov_id",
                    data_type="VARCHAR",
                    nullable={"MD": True, "MP": False, "ME": False},
                ),
            ],
        )

        # Save as SPLIT
        split_dir = tmp_path / "test_table"
        loader = UMFLoader()
        loader.save(umf, split_dir)

        # Roundtrip through JSON
        converter = UMFLoader()
        json_file = tmp_path / "intermediate.json"
        converter.convert(split_dir, json_file)

        final_split = tmp_path / "final"
        converter.convert(json_file, final_split)

        result = UMFLoader().load(final_split)
        gov_id_col = next(c for c in result.columns if c.name == "gov_id")

        # Nullable can be a dict or Nullable model; access appropriately
        if isinstance(gov_id_col.nullable, dict):
            assert gov_id_col.nullable["MD"] is True
            assert gov_id_col.nullable["MP"] is False
            assert gov_id_col.nullable["ME"] is False
        else:
            assert gov_id_col.nullable.MD is True
            assert gov_id_col.nullable.MP is False
            assert gov_id_col.nullable.ME is False

    def test_preserves_sample_values(self, tmp_path):
        """Test that sample values are preserved."""
        umf = UMF(
            version="1.0",
            table_name="test",
            canonical_name="Test",
            columns=[
                UMFColumn(
                    name="status",
                    data_type="VARCHAR",
                    sample_values=["ACTIVE", "INACTIVE", "PENDING"],
                ),
            ],
        )

        # Save as SPLIT
        split_dir = tmp_path / "test_table"
        loader = UMFLoader()
        loader.save(umf, split_dir)

        # Roundtrip through JSON
        converter = UMFLoader()
        json_file = tmp_path / "intermediate.json"
        converter.convert(split_dir, json_file)

        final_split = tmp_path / "final"
        converter.convert(json_file, final_split)

        result = UMFLoader().load(final_split)
        status_col = next(c for c in result.columns if c.name == "status")

        assert status_col.sample_values == ["ACTIVE", "INACTIVE", "PENDING"]


class TestJsonConversion:
    """Test JSON format conversion (artifact standard)."""

    @pytest.fixture
    def complete_umf(self, tmp_path):
        """Create a complete UMF for JSON testing."""
        return UMF(
            version="1.0",
            table_name="json_test",
            canonical_name="JsonTest",
            description="Test table for JSON conversion",
            columns=[
                UMFColumn(name="id", data_type="INTEGER"),
                UMFColumn(name="name", data_type="VARCHAR", length=100),
            ],
        )

    def test_split_to_json_conversion(self, complete_umf, tmp_path):
        """Test conversion from SPLIT to JSON."""
        # Save as SPLIT
        split_dir = tmp_path / "test_split"
        loader = UMFLoader()
        loader.save(complete_umf, split_dir)

        converter = UMFLoader()
        json_file = tmp_path / "output.umf.json"

        converter.convert(split_dir, json_file, target_format=UMFFormat.JSON)

        assert json_file.exists()
        result = converter._load_json(json_file)
        assert result.table_name == "json_test"
        assert len(result.columns) == 2

    def test_json_to_split_conversion(self, complete_umf, tmp_path):
        """Test conversion from JSON to SPLIT."""
        converter = UMFLoader()

        # First save as JSON
        json_file = tmp_path / "test.umf.json"
        converter.save_json(complete_umf, json_file)

        # Convert to SPLIT
        split_dir = tmp_path / "output_split"
        converter.convert(json_file, split_dir, target_format=UMFFormat.SPLIT)

        assert split_dir.exists()
        assert (split_dir / "table.yaml").exists()
        result = UMFLoader().load(split_dir)
        assert result.table_name == "json_test"

    def test_roundtrip_split_to_json_to_split(self, complete_umf, tmp_path):
        """Test roundtrip: SPLIT → JSON → SPLIT."""
        converter = UMFLoader()

        # Save original as SPLIT
        original_split = tmp_path / "original"
        converter.save(complete_umf, original_split)

        # Convert to JSON
        json_file = tmp_path / "intermediate.umf.json"
        converter.convert(original_split, json_file)

        # Convert back to SPLIT
        final_split = tmp_path / "final"
        converter.convert(json_file, final_split)

        # Verify
        original = UMFLoader().load(original_split)
        result = UMFLoader().load(final_split)

        assert result.table_name == original.table_name
        assert result.canonical_name == original.canonical_name
        assert len(result.columns) == len(original.columns)

    def test_json_is_valid_json(self, complete_umf, tmp_path):
        """Test that JSON output is valid JSON."""
        converter = UMFLoader()
        json_file = tmp_path / "test.umf.json"
        converter.save_json(complete_umf, json_file)

        # Verify it's valid JSON
        with json_file.open() as f:
            data = json.load(f)

        assert data["table_name"] == "json_test"
        assert data["version"] == "1.0"

    def test_json_is_deterministic(self, complete_umf, tmp_path):
        """Test that JSON output is deterministic."""
        converter = UMFLoader()

        # Save twice
        json_file1 = tmp_path / "output1.umf.json"
        json_file2 = tmp_path / "output2.umf.json"

        converter.save_json(complete_umf, json_file1)
        converter.save_json(complete_umf, json_file2)

        # Compare files
        assert json_file1.read_text() == json_file2.read_text()

    def test_json_formatted_readable(self, complete_umf, tmp_path):
        """Test that JSON output is indented for readability."""
        converter = UMFLoader()
        json_file = tmp_path / "test.umf.json"
        converter.save_json(complete_umf, json_file)

        content = json_file.read_text()

        # Should have indentation (2 spaces)
        assert "  " in content
        # Should be multi-line
        assert "\n" in content


class TestWriteYaml:
    """Test the _write_yaml method."""

    def test_write_yaml_creates_file(self, tmp_path):
        """Test that _write_yaml creates a YAML file."""
        loader = UMFLoader()
        output_file = tmp_path / "test.yaml"
        data = {"key": "value", "nested": {"inner": "data"}}
        loader._write_yaml(output_file, data)
        assert output_file.exists()
        content = output_file.read_text()
        assert "key" in content
        assert "value" in content

    def test_write_yaml_creates_parent_dirs(self, tmp_path):
        """Test that _write_yaml creates parent directories."""
        loader = UMFLoader()
        output_file = tmp_path / "subdir" / "deep" / "test.yaml"
        loader._write_yaml(output_file, {"key": "value"})
        assert output_file.exists()

    def test_write_yaml_sorts_keys(self, tmp_path):
        """Test that _write_yaml sorts dictionary keys."""
        loader = UMFLoader()
        output_file = tmp_path / "sorted.yaml"
        data = {"zebra": 1, "apple": 2, "mango": 3}
        loader._write_yaml(output_file, data)
        content = output_file.read_text()
        # apple should appear before mango, which appears before zebra
        assert content.index("apple") < content.index("mango")
        assert content.index("mango") < content.index("zebra")

    def test_write_yaml_strips_trailing_whitespace(self, tmp_path):
        """Test that trailing whitespace in strings is stripped."""
        loader = UMFLoader()
        output_file = tmp_path / "stripped.yaml"
        data = {"description": "hello   "}
        loader._write_yaml(output_file, data)
        content = output_file.read_text()
        # The actual value should not have trailing whitespace
        assert "hello   " not in content

    def test_write_yaml_fallback_when_formatting_fails(self, tmp_path, monkeypatch):
        """Test fallback path when format_yaml_dict raises an exception."""
        import warnings

        loader = UMFLoader()
        output_file = tmp_path / "fallback.yaml"

        # Monkey-patch the formatting module to raise an error
        import tablespec.formatting as fmt_module

        def broken_format(*args, **kwargs):
            raise RuntimeError("Formatting broken")

        monkeypatch.setattr(fmt_module, "format_yaml_dict", broken_format)

        data = {"key": "value"}
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            loader._write_yaml(output_file, data)
            # Should have emitted a warning about formatting failure
            assert any(
                "YAML formatting failed" in str(warning.message) for warning in w
            )

        assert output_file.exists()
        content = output_file.read_text()
        assert "key" in content


class TestWriteJson:
    """Test save_json method."""

    def test_save_json_creates_file(self, tmp_path):
        """Test that save_json creates a JSON file."""
        loader = UMFLoader()
        umf = UMF(
            version="1.0",
            table_name="test",
            canonical_name="Test",
            columns=[UMFColumn(name="id", data_type="INTEGER")],
        )
        output_file = tmp_path / "test.json"
        loader.save_json(umf, output_file)
        assert output_file.exists()
        data = json.loads(output_file.read_text())
        assert data["table_name"] == "test"

    def test_save_json_creates_parent_dirs(self, tmp_path):
        """Test that save_json creates parent directories."""
        loader = UMFLoader()
        umf = UMF(
            version="1.0",
            table_name="test",
            canonical_name="Test",
            columns=[UMFColumn(name="id", data_type="INTEGER")],
        )
        output_file = tmp_path / "subdir" / "deep" / "test.json"
        loader.save_json(umf, output_file)
        assert output_file.exists()

    def test_save_json_sorted_keys(self, tmp_path):
        """Test that JSON output has sorted keys."""
        loader = UMFLoader()
        umf = UMF(
            version="1.0",
            table_name="test",
            canonical_name="Test",
            columns=[UMFColumn(name="id", data_type="INTEGER")],
        )
        output_file = tmp_path / "test.json"
        loader.save_json(umf, output_file)
        content = output_file.read_text()
        # Keys should be sorted, so 'columns' comes before 'table_name'
        assert content.index("columns") < content.index("table_name")


class TestConvertFormat:
    """Test convert method with various configurations."""

    def test_convert_split_to_json(self, tmp_path):
        """Test converting from split format to JSON."""
        loader = UMFLoader()
        umf = UMF(
            version="1.0",
            table_name="convert_test",
            canonical_name="ConvertTest",
            columns=[UMFColumn(name="id", data_type="INTEGER")],
        )
        split_dir = tmp_path / "split"
        loader.save(umf, split_dir)

        json_file = tmp_path / "output.json"
        loader.convert(split_dir, json_file, target_format=UMFFormat.JSON)
        assert json_file.exists()
        result = loader.load(json_file)
        assert result.table_name == "convert_test"

    def test_convert_json_to_split(self, tmp_path):
        """Test converting from JSON format to split."""
        loader = UMFLoader()
        umf = UMF(
            version="1.0",
            table_name="convert_test",
            canonical_name="ConvertTest",
            columns=[UMFColumn(name="id", data_type="INTEGER")],
        )
        json_file = tmp_path / "input.json"
        loader.save_json(umf, json_file)

        split_dir = tmp_path / "output_split"
        loader.convert(json_file, split_dir, target_format=UMFFormat.SPLIT)
        assert (split_dir / "table.yaml").exists()
        assert (split_dir / "columns").is_dir()

    def test_convert_infers_json_from_extension(self, tmp_path):
        """Test that convert infers JSON format from .json extension."""
        loader = UMFLoader()
        umf = UMF(
            version="1.0",
            table_name="test",
            canonical_name="Test",
            columns=[UMFColumn(name="id", data_type="INTEGER")],
        )
        split_dir = tmp_path / "split"
        loader.save(umf, split_dir)

        json_file = tmp_path / "inferred.json"
        loader.convert(split_dir, json_file)  # No target_format specified
        assert json_file.exists()

    def test_convert_infers_split_from_directory(self, tmp_path):
        """Test that convert infers SPLIT format for non-.json paths."""
        loader = UMFLoader()
        umf = UMF(
            version="1.0",
            table_name="test",
            canonical_name="Test",
            columns=[UMFColumn(name="id", data_type="INTEGER")],
        )
        json_file = tmp_path / "input.json"
        loader.save_json(umf, json_file)

        split_dir = tmp_path / "output_dir"
        loader.convert(json_file, split_dir)  # No target_format specified
        assert (split_dir / "table.yaml").exists()


class TestLoadColumnCentricEdgeCases:
    """Test edge cases in _load_column_centric."""

    def test_load_with_pending_validations(self, tmp_path):
        """Test loading with pending_validations.yaml."""
        (tmp_path / "table.yaml").write_text(
            "version: '1.0'\ntable_name: test_table\ncanonical_name: TestTable\n"
        )
        (tmp_path / "pending_validations.yaml").write_text(
            "pending_expectations:\n"
            "  - type: expect_column_values_to_not_be_null\n"
            "    kwargs:\n"
            "      column: id\n"
            "    meta:\n"
            "      reason: Needs review\n"
        )
        (tmp_path / "columns").mkdir()
        (tmp_path / "columns" / "id.yaml").write_text(
            "column:\n  name: id\n  data_type: VARCHAR\n"
        )

        loader = UMFLoader()
        umf = loader.load(tmp_path)
        assert umf.validation_rules is not None
        assert umf.validation_rules.pending_expectations is not None
        assert len(umf.validation_rules.pending_expectations) == 1

    def test_load_with_quality_checks(self, tmp_path):
        """Test loading with quality_checks.yaml."""
        (tmp_path / "table.yaml").write_text(
            "version: '1.0'\ntable_name: test_table\ncanonical_name: TestTable\n"
        )
        (tmp_path / "quality_checks.yaml").write_text(
            "checks:\n"
            "  - expectation:\n"
            "      expectation_type: expect_column_values_to_not_be_null\n"
            "      kwargs:\n"
            "        column: id\n"
            "    severity: critical\n"
            "    blocking: true\n"
        )
        (tmp_path / "columns").mkdir()
        (tmp_path / "columns" / "id.yaml").write_text(
            "column:\n  name: id\n  data_type: VARCHAR\n"
        )

        loader = UMFLoader()
        umf = loader.load(tmp_path)
        assert umf.quality_checks is not None
        assert umf.expectations is not None
        assert len(umf.expectations.expectations) == 1
        assert umf.expectations.expectations[0].meta.severity == "critical"

    def test_load_with_cross_column_validations_deprecated(self, tmp_path):
        """Test loading with deprecated cross_column_validations.yaml emits warning."""
        import warnings

        (tmp_path / "table.yaml").write_text(
            "version: '1.0'\ntable_name: test_table\ncanonical_name: TestTable\n"
        )
        (tmp_path / "cross_column_validations.yaml").write_text(
            "expectations:\n"
            "  - type: expect_compound_columns_to_be_unique\n"
            "    kwargs:\n"
            "      column_list: [id, name]\n"
        )
        (tmp_path / "columns").mkdir()
        (tmp_path / "columns" / "id.yaml").write_text(
            "column:\n  name: id\n  data_type: VARCHAR\n"
        )

        loader = UMFLoader()
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            umf = loader.load(tmp_path)
            assert any(
                "cross_column_validations.yaml is deprecated" in str(warning.message)
                for warning in w
            )

        assert umf.validation_rules is not None
        assert len(umf.validation_rules.expectations) == 1

    def test_load_merges_cross_validations_with_existing(self, tmp_path):
        """Test that cross-column validations merge with table-level validations."""
        (tmp_path / "table.yaml").write_text(
            "version: '1.0'\ntable_name: test_table\ncanonical_name: TestTable\n"
            "validation_rules:\n"
            "  expectations:\n"
            "    - type: existing_expectation\n"
            "      kwargs: {}\n"
        )
        (tmp_path / "validation_rules.yaml").write_text(
            "expectations:\n  - type: cross_column_expectation\n    kwargs: {}\n"
        )
        (tmp_path / "columns").mkdir()
        (tmp_path / "columns" / "id.yaml").write_text(
            "column:\n  name: id\n  data_type: VARCHAR\n"
        )

        loader = UMFLoader()
        umf = loader.load(tmp_path)
        assert umf.validation_rules is not None
        # Should have both expectations merged
        assert len(umf.validation_rules.expectations) == 2

    def test_load_missing_table_yaml_raises(self, tmp_path):
        """Test that missing table.yaml raises FileNotFoundError."""
        (tmp_path / "columns").mkdir()
        (tmp_path / "columns" / "id.yaml").write_text(
            "column:\n  name: id\n  data_type: VARCHAR\n"
        )

        loader = UMFLoader()
        # Force split format detection by having the right structure
        # but then remove table.yaml
        with pytest.raises((FileNotFoundError, ValueError)):
            loader._load_column_centric(tmp_path)

    def test_load_missing_columns_dir_raises(self, tmp_path):
        """Test that missing columns/ directory raises FileNotFoundError."""
        (tmp_path / "table.yaml").write_text("version: '1.0'\ntable_name: test_table\n")

        loader = UMFLoader()
        with pytest.raises(FileNotFoundError, match="Missing columns/ directory"):
            loader._load_column_centric(tmp_path)

    @pytest.mark.parametrize("suffix", [".yaml", ".yml"])
    def test_detect_legacy_inline_yaml_file_is_rejected(self, tmp_path, suffix):
        """A single legacy YAML file is rejected from normal auto-detection."""
        yaml_file = tmp_path / f"test{suffix}"
        yaml_file.write_text("version: '1.0'")
        loader = UMFLoader()
        with pytest.raises(ValueError, match="legacy-only"):
            loader.detect_format(yaml_file)


class TestSortRecursive:
    """Test _sort_recursive helper."""

    def test_sorts_dict_keys(self):
        loader = UMFLoader()
        result = loader._sort_recursive({"b": 1, "a": 2, "c": 3})
        assert list(result.keys()) == ["a", "b", "c"]

    def test_preserves_list_order(self):
        loader = UMFLoader()
        result = loader._sort_recursive([3, 1, 2])
        assert result == [3, 1, 2]

    def test_filters_none_values(self):
        loader = UMFLoader()
        result = loader._sort_recursive({"a": 1, "b": None, "c": 3})
        assert "b" not in result

    def test_recursive_nested(self):
        loader = UMFLoader()
        result = loader._sort_recursive(
            {"z": {"b": 1, "a": 2}, "a": [{"c": 3, "b": 4}]}
        )
        assert list(result.keys()) == ["a", "z"]
        assert list(result["z"].keys()) == ["a", "b"]
        assert list(result["a"][0].keys()) == ["b", "c"]


class TestStripTrailingWhitespace:
    """Test _strip_trailing_whitespace helper."""

    def test_strips_string(self):
        loader = UMFLoader()
        assert loader._strip_trailing_whitespace("hello  ") == "hello"

    def test_strips_in_dict(self):
        loader = UMFLoader()
        result = loader._strip_trailing_whitespace({"k": "val  "})
        assert result["k"] == "val"

    def test_strips_in_list(self):
        loader = UMFLoader()
        result = loader._strip_trailing_whitespace(["a  ", "b  "])
        assert result == ["a", "b"]

    def test_passes_through_non_string(self):
        loader = UMFLoader()
        assert loader._strip_trailing_whitespace(42) == 42


class TestConvertYamlToPlainStrings:
    """Test _convert_yaml_to_plain_strings helper."""

    def test_converts_dict(self):
        result = UMFLoader._convert_yaml_to_plain_strings({"k": "val"})
        assert result == {"k": "val"}
        assert type(result["k"]) is str

    def test_converts_list(self):
        result = UMFLoader._convert_yaml_to_plain_strings(["a", "b"])
        assert result == ["a", "b"]

    def test_passes_through_non_string(self):
        assert UMFLoader._convert_yaml_to_plain_strings(42) == 42
        assert UMFLoader._convert_yaml_to_plain_strings(True) is True


class TestValidateFilenamePattern:
    """Test validate_filename_pattern method."""

    def test_no_file_format(self):
        """Test validation with no file_format returns empty errors."""
        loader = UMFLoader()
        umf = UMF(
            version="1.0",
            table_name="test",
            canonical_name="Test",
            columns=[UMFColumn(name="id", data_type="INTEGER")],
        )
        errors = loader.validate_filename_pattern(umf)
        assert errors == []

    def test_valid_pattern(self):
        """Test validation with a valid filename pattern."""
        loader = UMFLoader()
        umf = UMF(
            version="1.0",
            table_name="test",
            canonical_name="Test",
            columns=[
                UMFColumn(name="id", data_type="INTEGER"),
                UMFColumn(name="rundate", data_type="VARCHAR", source="filename"),
            ],
            file_format={
                "filename_pattern": {
                    "regex": r"data_(\d{8})\.txt",
                    "captures": {1: "rundate"},
                },
            },
        )
        errors = loader.validate_filename_pattern(umf)
        assert errors == []


class TestSaveUmfToYaml:
    """Test the save_umf_to_yaml() convenience function."""

    def test_saves_as_yaml_file(self, tmp_path):
        """Test that save_umf_to_yaml saves a YAML file."""
        umf = UMF(
            version="1.0",
            table_name="test",
            canonical_name="Test",
            columns=[UMFColumn(name="id", data_type="INTEGER")],
        )

        output_path = tmp_path / "test.umf.yaml"
        save_umf_to_yaml(umf, output_path)

        # Should have created the YAML file
        assert output_path.exists()
        assert output_path.is_file()

    def test_loads_back_correctly(self, tmp_path):
        """Test that saved UMF can be loaded back."""
        umf = UMF(
            version="1.0",
            table_name="test",
            canonical_name="Test",
            columns=[UMFColumn(name="id", data_type="INTEGER")],
        )

        output_path = tmp_path / "test.umf.yaml"
        save_umf_to_yaml(umf, output_path)

        # Load back from the YAML file
        from tablespec.models.umf import load_umf_from_yaml

        loaded = load_umf_from_yaml(output_path)

        assert loaded.table_name == "test"
        assert loaded.canonical_name == "Test"
