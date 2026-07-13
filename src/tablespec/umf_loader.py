"""Load and convert UMF between split and JSON formats.

Split format: Directory structure for git-friendly development (default)
JSON format: Single JSON file as canonical artifact standard
Legacy inline YAML: single-file whole-UMF documents, migration-only

Primary use: Loading UMF from supported authoring / artifact formats via
auto-detection
Secondary use: Converting between supported formats
"""

from enum import Enum
import json
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML

from tablespec.models import UMF


class UMFFormat(str, Enum):
    """UMF storage format."""

    SPLIT = "split"  # Directory structure (table.yaml + columns/) - default
    JSON = "json"  # Single JSON file (artifact standard)
    INLINE = "inline"  # Single .yaml/.yml/.umf file (legacy migration-only)


class UMFLoader:
    """Load and convert UMF between multiple formats.

    Supported formats:
    - Split: Directory structure with table.yaml + columns/*.yaml (default, git-friendly)
    - JSON: Single JSON file (artifact standard)
    - Inline YAML: legacy single-file whole-UMF documents, accessible only via
      explicit migration helpers

    Primary purpose: Load UMF from supported formats with automatic format detection.
    """

    def __init__(self) -> None:
        """Initialize loader with YAML handler."""
        self.yaml = YAML()
        self.yaml.default_flow_style = False
        self.yaml.preserve_quotes = True
        self.yaml.width = 100

        # Add custom constructor to preserve quoted numeric strings
        # This prevents YAML from coercing '009' -> 9 or '00597016818' -> 597016818
        original_construct_scalar = self.yaml.constructor.construct_scalar

        def preserve_quoted_strings(_constructor_self, node):
            """Override scalar construction to preserve quoted strings."""
            # If the node has explicit quotes, construct it as a plain string
            if hasattr(node, "style") and node.style in ("'", '"'):
                return str(node.value)
            # Otherwise, use the original constructor for type inference
            return original_construct_scalar(node)

        # Patch the constructor method (bind to the constructor instance)
        import types

        self.yaml.constructor.construct_scalar = types.MethodType(
            preserve_quoted_strings, self.yaml.constructor
        )

    @staticmethod
    def _convert_yaml_to_plain_strings(obj: Any) -> Any:
        """Recursively convert all YAML string types to plain Python str.

        This ensures YAML LiteralScalarString and other special types from ruamel.yaml
        are converted to plain str objects that can be safely serialized by Spark.

        Args:
            obj: Object to convert (dict, list, or scalar)

        Returns:
            Object with all string types converted to plain str

        """
        if isinstance(obj, dict):
            return {
                k: UMFLoader._convert_yaml_to_plain_strings(v) for k, v in obj.items()
            }
        if isinstance(obj, list):
            return [UMFLoader._convert_yaml_to_plain_strings(item) for item in obj]
        if isinstance(obj, str):
            # Convert any string-like object (including YAML types) to plain str
            return str(obj)
        return obj

    def load(self, path: str | Path) -> UMF:
        """Load UMF from any format (auto-detect).

        Supports all UMF formats:
        - JSON: Single JSON file (.json)
        - Split: Directory with table.yaml and columns/*.yaml
        - Legacy inline YAML files are intentionally not auto-detected; use the
          explicit migration helper for those.

        Args:
            path: Path to UMF source (file or directory)

        Returns:
            Loaded and validated UMF model

        Raises:
            ValueError: If format cannot be detected
            FileNotFoundError: If source files are missing
            ValidationError: If UMF data is invalid

        """
        path = Path(path)
        format_type = self.detect_format(path)
        return self._load(path, format_type)

    def validate_filename_pattern(self, umf: UMF) -> list[str]:
        """Validate that filename_pattern is properly configured.

        Checks that:
        1. filename_pattern.regex is a valid regex
        2. All capture group indices in captures are valid
        3. Capture group indices match regex groups
        4. Each captured column exists in UMF (for filename-sourced columns)

        Args:
            umf: UMF model to validate

        Returns:
            List of validation error messages (empty if valid)

        """
        import re

        errors = []

        # Skip if no filename pattern defined
        file_format = getattr(umf, "file_format", None)
        if not file_format:
            return errors
        filename_pattern = getattr(file_format, "filename_pattern", None)
        if not filename_pattern:
            return errors

        pattern = filename_pattern

        # 1. Validate regex is compilable
        try:
            compiled_regex = re.compile(pattern.regex)
        except re.error as e:
            errors.append(f"Invalid regex in filename_pattern: {e}")
            return errors

        # 2. Get actual capture groups from regex
        try:
            # Test against a minimal string to get group count
            num_groups = compiled_regex.groups
        except Exception as e:
            errors.append(f"Cannot determine capture groups: {e}")
            return errors

        # 3. Validate capture indices
        if num_groups > 0 and not pattern.captures:
            errors.append(
                "filename_pattern.captures is empty but regex has capture groups"
            )
            return errors

        # 4. Check each capture group exists in regex
        for group_idx, _column_name in pattern.captures.items():
            if group_idx < 1 or group_idx > num_groups:
                errors.append(
                    f"Capture group {group_idx} does not exist in regex "
                    + f"(regex has {num_groups} groups)"
                )

        # 5. Check each captured column exists in UMF
        column_names = {col.name for col in umf.columns}
        for group_idx, _column_name in pattern.captures.items():
            if _column_name not in column_names:
                errors.append(
                    f"Captured column '{_column_name}' (group {group_idx}) not found in table columns"
                )

        # 6. Validate filename-sourced columns exist in captures if applicable
        for col in umf.columns:
            if getattr(col, "source", None) == "filename":
                # Check if this column is in captures (it should be)
                if col.name not in pattern.captures.values():
                    errors.append(
                        f"Column '{col.name}' has source='filename' but is not in filename_pattern.captures"
                    )

        return errors

    def detect_format(self, path: Path) -> UMFFormat:
        """Auto-detect UMF format from path.

        Args:
            path: File or directory path

        Returns:
            Detected UMF format

        Raises:
            FileNotFoundError: If path doesn't exist
            ValueError: If format cannot be detected

        """
        # Check if path exists first
        if not path.exists():
            msg = f"Path does not exist: {path}"
            raise FileNotFoundError(msg)

        if path.is_file():
            if path.suffix == ".json":
                return UMFFormat.JSON
            if path.suffix in {".yaml", ".yml", ".umf"}:
                msg = (
                    f"Single-file YAML UMF documents are legacy-only and are not "
                    f"auto-detected: {path}. Use migrate_legacy_inline_yaml() to "
                    "convert it explicitly, or load a split-format directory / JSON file."
                )
                raise ValueError(msg)

        if path.is_dir():
            if not (path / "table.yaml").exists():
                raise FileNotFoundError(
                    f"Directory '{path}' exists but has no table.yaml. "
                    f"Expected split-format UMF directory with table.yaml and columns/ subdirectory."
                )
            if not (path / "columns").is_dir():
                raise FileNotFoundError(
                    f"Directory '{path}' has table.yaml but no columns/ subdirectory. "
                    f"Create a columns/ directory with one YAML file per column."
                )
            return UMFFormat.SPLIT

        msg = (
            f"Cannot detect format for {path}. "
            "Expected a .json file or a split-format directory with table.yaml+columns/"
        )
        raise ValueError(msg)

    def migrate_legacy_inline_yaml(
        self,
        source: Path,
        dest: Path,
        target_format: UMFFormat | None = None,
    ) -> None:
        """Migrate a legacy single-file YAML UMF into a supported format.

        This is the explicit compatibility path for `.yaml` / `.yml` / `.umf`
        whole-document UMFs. Normal `load()` calls do not auto-detect those files.
        """
        source = Path(source)
        dest = Path(dest)

        if target_format is None:
            target_format = (
                UMFFormat.JSON if dest.suffix == ".json" else UMFFormat.SPLIT
            )
        if target_format == UMFFormat.INLINE:
            msg = "Legacy inline YAML can only be migrated to split or JSON formats."
            raise ValueError(msg)

        umf = self._load_inline_yaml(source)
        self.save(umf, dest, target_format)

    def convert(
        self,
        source: Path,
        dest: Path,
        target_format: UMFFormat | None = None,
    ) -> None:
        """Convert UMF from source to dest.

        Args:
            source: Source UMF (file or directory)
            dest: Destination path
            target_format: Target format (auto-detected if None)

        Raises:
            ValueError: If formats cannot be determined
            FileNotFoundError: If source doesn't exist

        """
        # Detect source format
        source_format = self.detect_format(source)

        # Determine target format
        if target_format is None:
            # Infer from dest path: .json extension -> JSON, otherwise SPLIT (default)
            target_format = (
                UMFFormat.JSON if dest.suffix == ".json" else UMFFormat.SPLIT
            )

        # Load UMF
        umf = self._load(source, source_format)

        # Save in target format
        self.save(umf, dest, target_format)

    def _load(self, path: Path, format_type: UMFFormat) -> UMF:
        """Load UMF from path.

        Args:
            path: Source path
            format_type: Source format

        Returns:
            Loaded UMF model

        Raises:
            FileNotFoundError: If required files are missing
            ValueError: If UMF is invalid

        """
        if format_type == UMFFormat.JSON:
            return self._load_json(path)
        if format_type == UMFFormat.INLINE:
            return self._load_inline_yaml(path)
        # SPLIT format (table.yaml + columns/)
        return self._load_column_centric(path)

    def _load_inline_yaml(self, file_path: Path) -> UMF:
        """Load UMF from a single inline YAML document (.yaml/.yml/.umf).

        This is the legacy whole-document form used only by explicit migration
        helpers. Mirrors ``_load_json``'s legacy-field handling so YAML and JSON
        single-file sources behave identically during migration.
        """
        try:
            with file_path.open(encoding="utf-8") as f:
                data = self.yaml.load(f) or {}
        except FileNotFoundError:
            msg = f"UMF file not found: {file_path}"
            raise FileNotFoundError(msg) from None

        # Normalize string types for Spark/serialization consistency.
        data = self._convert_yaml_to_plain_strings(data)

        # Populate expectations from legacy fields if not already present (ADR-005).
        if "expectations" not in data and (
            "validation_rules" in data or "quality_checks" in data
        ):
            from tablespec.expectation_migration import ensure_expectation_suite_data

            data["expectations"] = ensure_expectation_suite_data(data)

        umf = UMF(**data)
        if hasattr(umf, "mtime"):
            umf.mtime = file_path.stat().st_mtime
        return umf

    def _load_json(self, file_path: Path) -> UMF:
        """Load UMF from JSON file.

        Args:
            file_path: Path to JSON file

        Returns:
            Loaded UMF model

        Raises:
            FileNotFoundError: If file doesn't exist
            ValueError: If JSON is invalid or doesn't match UMF schema

        """
        try:
            with file_path.open() as f:
                data = json.load(f)
        except FileNotFoundError:
            msg = f"JSON file not found: {file_path}"
            raise FileNotFoundError(msg) from None

        # Convert all string types to plain Python str for consistency and Spark compatibility
        data = self._convert_yaml_to_plain_strings(data)

        # Populate expectations from legacy fields if not already present (ADR-005)
        if "expectations" not in data and (
            "validation_rules" in data or "quality_checks" in data
        ):
            from tablespec.expectation_migration import ensure_expectation_suite_data

            data["expectations"] = ensure_expectation_suite_data(data)

        umf = UMF(**data)
        if hasattr(umf, "mtime"):
            umf.mtime = file_path.stat().st_mtime
        return umf

    def _load_column_centric(self, dir_path: Path) -> UMF:
        """Load UMF from split directory structure.

        Loads from split format:
        - table.yaml (table metadata, includes relationships)
        - columns/ directory (column definitions)
        - validation_rules.yaml (optional, formerly cross_column_validations.yaml)
        - pending_validations.yaml (optional)

        Args:
            dir_path: Directory containing split UMF

        Returns:
            Loaded UMF model

        Raises:
            FileNotFoundError: If required files are missing
            ValueError: If data is invalid

        """
        # Load table.yaml
        table_file = dir_path / "table.yaml"
        try:
            with table_file.open() as f:
                umf_data = self.yaml.load(f) or {}
        except FileNotFoundError:
            msg = f"Missing table.yaml in {dir_path}"
            raise FileNotFoundError(msg) from None

        # Load validation_rules.yaml (or cross_column_validations.yaml for backward compatibility)
        validation_rules_file = dir_path / "validation_rules.yaml"
        cross_val_file = dir_path / "cross_column_validations.yaml"

        # Try new filename first, then fall back to old filename
        cross_validations: dict = {}
        try:
            with validation_rules_file.open() as f:
                cross_validations = self.yaml.load(f) or {}
        except FileNotFoundError:
            try:
                with cross_val_file.open() as f:
                    cross_validations = self.yaml.load(f) or {}
                # Backward compatibility: Support old filename but emit warning
                import warnings

                warnings.warn(
                    f"cross_column_validations.yaml is deprecated. "
                    f"Please rename to validation_rules.yaml in {dir_path}",
                    DeprecationWarning,
                    stacklevel=2,
                )
            except FileNotFoundError:
                pass

        # Merge validation rules with table validations
        if cross_validations and "validation_rules" not in umf_data:
            umf_data["validation_rules"] = cross_validations
        elif cross_validations and "validation_rules" in umf_data:
            if "expectations" not in umf_data["validation_rules"]:
                umf_data["validation_rules"]["expectations"] = []
            if "expectations" in cross_validations:
                umf_data["validation_rules"]["expectations"].extend(
                    cross_validations["expectations"]
                )

        # Load pending_validations.yaml if it exists
        pending_val_file = dir_path / "pending_validations.yaml"
        try:
            with pending_val_file.open() as f:
                pending_validations = self.yaml.load(f) or {}
            # Merge pending expectations with table validations
            if pending_validations and "validation_rules" not in umf_data:
                umf_data["validation_rules"] = pending_validations
            elif pending_validations and "validation_rules" in umf_data:
                if "pending_expectations" in pending_validations:
                    umf_data["validation_rules"]["pending_expectations"] = (
                        pending_validations["pending_expectations"]
                    )
        except FileNotFoundError:
            pass

        # Load quality_checks.yaml if it exists (post-ingestion quality checks)
        quality_checks_file = dir_path / "quality_checks.yaml"
        try:
            with quality_checks_file.open() as f:
                quality_checks_data = self.yaml.load(f) or {}
            if quality_checks_data:
                umf_data["quality_checks"] = quality_checks_data
        except FileNotFoundError:
            pass

        # Load expectations.yaml if it exists (unified expectation suite, ADR-005)
        expectations_file = dir_path / "expectations.yaml"
        try:
            with expectations_file.open() as f:
                expectations_data = self.yaml.load(f) or {}
            if expectations_data:
                umf_data["expectations"] = expectations_data
        except FileNotFoundError:
            pass

        # Load and merge columns from columns/ directory
        columns_dir = dir_path / "columns"
        if not columns_dir.is_dir():
            msg = f"Missing columns/ directory in {dir_path}"
            raise FileNotFoundError(msg)

        columns = []
        derivations = None

        # Sort column files for consistent ordering
        column_files = sorted(columns_dir.glob("*.yaml"))
        for column_file in column_files:
            with column_file.open() as f:
                column_data = self.yaml.load(f) or {}

            # Extract base column metadata
            if "column" in column_data:
                col = column_data["column"]

                # Merge derivation into column (it's a sibling in on-disk format)
                if "derivation" in column_data:
                    col["derivation"] = column_data["derivation"]

                    # Also store in top-level derivations for backward compatibility
                    if derivations is None:
                        derivations = {}
                    if "mappings" not in derivations:
                        derivations["mappings"] = {}
                    # Key by canonical name if available, otherwise by column name
                    key = col.get("canonical_name", col.get("name"))
                    derivations["mappings"][key] = column_data["derivation"]

                columns.append(col)

                # Extract and merge column-specific validations
                if "validations" in column_data and isinstance(
                    column_data["validations"], list
                ):
                    # When expectations.yaml is loaded (new format), merge column
                    # validations directly into the expectations suite via migration.
                    if "expectations" in umf_data:
                        from tablespec.models.umf import Expectation

                        if "expectations" not in umf_data["expectations"]:
                            umf_data["expectations"]["expectations"] = []
                        for exp_dict in column_data["validations"]:
                            migrated = Expectation.from_gx_dict(exp_dict).model_dump(
                                exclude_none=True
                            )
                            umf_data["expectations"]["expectations"].append(migrated)
                    else:
                        # Legacy path: merge into validation_rules for later migration
                        if "validation_rules" not in umf_data:
                            umf_data["validation_rules"] = {}
                        if "expectations" not in umf_data["validation_rules"]:
                            umf_data["validation_rules"]["expectations"] = []
                        umf_data["validation_rules"]["expectations"].extend(
                            column_data["validations"]
                        )

        # Set columns
        if columns:
            umf_data["columns"] = columns

        # Set derivations if any
        if derivations:
            umf_data["derivations"] = derivations

        # Convert all YAML string types to plain Python str for Spark compatibility
        umf_data = self._convert_yaml_to_plain_strings(umf_data)

        if "expectations" not in umf_data and (
            "validation_rules" in umf_data or "quality_checks" in umf_data
        ):
            from tablespec.expectation_migration import ensure_expectation_suite_data

            umf_data["expectations"] = ensure_expectation_suite_data(umf_data)

        # Create UMF model
        umf = UMF(**umf_data)
        if hasattr(umf, "mtime"):
            umf.mtime = table_file.stat().st_mtime
        return umf

    def save(
        self, umf: UMF, path: str | Path, format: UMFFormat = UMFFormat.SPLIT
    ) -> None:
        """Save UMF to path in specified format.

        Args:
            umf: UMF model to save
            path: Destination path
            format: Target format (defaults to SPLIT)

        """
        path = Path(path)
        if format == UMFFormat.JSON:
            self.save_json(umf, path)
        elif format == UMFFormat.SPLIT:
            self._save_split(umf, path)
        else:
            msg = "UMFFormat.INLINE is legacy-only; migrate inline YAML explicitly instead."
            raise ValueError(msg)

    def save_json(self, umf: UMF, file_path: Path) -> None:
        """Save UMF as single JSON file (canonical artifact format).

        Args:
            umf: UMF model to save
            file_path: Output JSON file path

        """
        file_path.parent.mkdir(parents=True, exist_ok=True)

        # Convert to dict with sorted keys for deterministic output
        data = umf.model_dump(exclude_none=True)
        sorted_data = self._sort_recursive(data)

        # Write with indentation for readability
        with file_path.open("w") as f:
            json.dump(sorted_data, f, indent=2, default=str)

    def _save_split(self, umf: UMF, dir_path: Path) -> None:
        """Save UMF in split format (table.yaml + columns/).

        Structure:
        - table.yaml: Table metadata + relationships + table-level config
        - validation_rules.yaml: Multi-column validation rules (formerly cross_column_validations.yaml)
        - columns/{column_name}.yaml: Column metadata + column-specific validations/derivations

        Note: Relationships are now embedded in table.yaml (not separate relationships.yaml).
        Includes: foreign_keys, indexes, referenced_by, outgoing, incoming, summary.

        Args:
            umf: UMF model to save
            dir_path: Output directory path

        """
        dir_path.mkdir(parents=True, exist_ok=True)

        # 1. Save table.yaml
        table_data: dict[str, Any] = {
            "version": umf.version,
            "table_name": umf.table_name,
        }

        # Add canonical_name if present
        if hasattr(umf, "canonical_name") and getattr(umf, "canonical_name", None):
            table_data["canonical_name"] = umf.canonical_name

        # Add optional fields
        for key in [
            "aliases",
            "source_sheet_name",
            "source_file",
            "sheet_name",
            "table_type",
            "description",
            "primary_key",
            "unique_constraints",
            "config_data",
            "lookup_metadata",
        ]:
            val = getattr(umf, key, None)
            if val:
                table_data[key] = val

        # Add metadata
        if umf.metadata:
            table_data["metadata"] = (
                umf.metadata.model_dump(exclude_none=True)
                if hasattr(umf.metadata, "model_dump")
                else umf.metadata
            )

        # Add file_format if present
        file_format = getattr(umf, "file_format", None)
        if file_format:
            table_data["file_format"] = (
                file_format.model_dump(exclude_none=True)
                if hasattr(file_format, "model_dump")
                else file_format
            )

        # Add source if present (discriminated source shape, ADR-015)
        source = getattr(umf, "source", None)
        if source is not None:
            table_data["source"] = (
                source.model_dump(exclude_none=True)
                if hasattr(source, "model_dump")
                else source
            )

        # Add relationships (now embedded in table.yaml, not separate file)
        if umf.relationships:
            relationships_data = {}
            rel = umf.relationships
            if hasattr(rel, "foreign_keys") and rel.foreign_keys:
                relationships_data["foreign_keys"] = [
                    fk.model_dump(exclude_none=True)
                    if hasattr(fk, "model_dump")
                    else fk
                    for fk in rel.foreign_keys
                ]
            if hasattr(rel, "indexes") and rel.indexes:
                relationships_data["indexes"] = [
                    idx.model_dump(exclude_none=True)
                    if hasattr(idx, "model_dump")
                    else idx
                    for idx in rel.indexes
                ]
            if hasattr(rel, "referenced_by") and rel.referenced_by:
                relationships_data["referenced_by"] = [
                    ref.model_dump(exclude_none=True)
                    if hasattr(ref, "model_dump")
                    else ref
                    for ref in rel.referenced_by
                ]
            if hasattr(rel, "outgoing") and rel.outgoing:
                relationships_data["outgoing"] = [
                    out.model_dump(exclude_none=True)
                    if hasattr(out, "model_dump")
                    else out
                    for out in rel.outgoing
                ]
            if hasattr(rel, "incoming") and rel.incoming:
                relationships_data["incoming"] = [
                    inc.model_dump(exclude_none=True)
                    if hasattr(inc, "model_dump")
                    else inc
                    for inc in rel.incoming
                ]
            if hasattr(rel, "summary") and rel.summary:
                relationships_data["summary"] = (
                    rel.summary.model_dump(exclude_none=True)
                    if hasattr(rel.summary, "model_dump")
                    else rel.summary
                )

            if relationships_data:
                table_data["relationships"] = relationships_data

        # Note: Validation rules are NOT saved in table.yaml
        # They are split between:
        # - expectations.yaml (cross-column expectations, ADR-005 unified format)
        # - columns/{column_name}.yaml (column-specific expectations)
        # Legacy: validation_rules.yaml + quality_checks.yaml (when no unified suite)

        self._write_yaml(dir_path / "table.yaml", table_data)

        # Build column-validation index for saving column-specific expectations
        # alongside column definitions in columns/{name}.yaml.
        col_validations_map: dict[str, list[dict[str, Any]]] = {}

        if umf.expectations and umf.expectations.expectations:
            # --- ADR-005 unified format: write expectations.yaml ---
            all_exps: list[dict[str, Any]] = [
                exp.model_dump(exclude_none=True)
                for exp in umf.expectations.expectations
            ]

            def _is_cross_column(exp: dict) -> bool:
                column = exp.get("kwargs", {}).get("column")
                return column is None or column in {"", "-"}

            cross_exps = [e for e in all_exps if _is_cross_column(e)]
            for e in all_exps:
                if not _is_cross_column(e):
                    col = e["kwargs"]["column"]
                    col_validations_map.setdefault(col, []).append(e)

            expectations_data: dict[str, Any] = {}
            if cross_exps:
                expectations_data["expectations"] = cross_exps
            if umf.expectations.pending:
                expectations_data["pending"] = [
                    exp.model_dump(exclude_none=True)
                    for exp in umf.expectations.pending
                ]
            if umf.expectations.thresholds:
                expectations_data["thresholds"] = umf.expectations.thresholds
            if umf.expectations.alert_config:
                expectations_data["alert_config"] = umf.expectations.alert_config

            # Always write expectations.yaml when the unified suite exists,
            # even if empty — it serves as a format marker for the loader.
            self._write_yaml(
                dir_path / "expectations.yaml",
                expectations_data or {"expectations": []},
            )
        else:
            # --- Legacy format: validation_rules.yaml + quality_checks.yaml ---
            expectations_list: list[dict[str, Any]] = []
            if umf.validation_rules:

                def is_cross_column_validation(exp: dict) -> bool:
                    column = exp.get("kwargs", {}).get("column")
                    return column is None or column in {"", "-"}

                if (
                    hasattr(umf.validation_rules, "expectations")
                    and umf.validation_rules.expectations
                ):
                    expectations_list = umf.validation_rules.expectations
                elif isinstance(umf.validation_rules, dict):
                    expectations_list = umf.validation_rules.get("expectations", [])

                cross_validations = {
                    "expectations": [
                        exp
                        for exp in (expectations_list or [])
                        if isinstance(exp, dict) and is_cross_column_validation(exp)
                    ]
                }
                if cross_validations["expectations"]:
                    self._write_yaml(
                        dir_path / "validation_rules.yaml", cross_validations
                    )

                # Build column validation map from legacy expectations
                for exp in expectations_list or []:
                    if isinstance(exp, dict) and not is_cross_column_validation(exp):
                        col = exp.get("kwargs", {}).get("column")
                        if col:
                            col_validations_map.setdefault(col, []).append(exp)

                # Save pending_expectations if present
                pending_expectations = []
                if (
                    hasattr(umf.validation_rules, "pending_expectations")
                    and umf.validation_rules.pending_expectations
                ):
                    pending_expectations = umf.validation_rules.pending_expectations
                elif isinstance(umf.validation_rules, dict):
                    pending_expectations = umf.validation_rules.get(
                        "pending_expectations", []
                    )

                if pending_expectations:
                    pending_validations = {"pending_expectations": pending_expectations}
                    self._write_yaml(
                        dir_path / "pending_validations.yaml", pending_validations
                    )

            quality_checks = getattr(umf, "quality_checks", None)
            if quality_checks:
                quality_checks_data: dict[str, Any] = {}
                if hasattr(quality_checks, "checks") and quality_checks.checks:
                    quality_checks_data["checks"] = [
                        check.model_dump(exclude_none=True)
                        if hasattr(check, "model_dump")
                        else check
                        for check in quality_checks.checks
                    ]
                elif isinstance(quality_checks, dict) and quality_checks.get("checks"):
                    quality_checks_data["checks"] = quality_checks["checks"]

                thresholds = (
                    getattr(quality_checks, "thresholds", None)
                    if hasattr(quality_checks, "thresholds")
                    else None
                )
                if thresholds:
                    quality_checks_data["thresholds"] = (
                        thresholds.model_dump(exclude_none=True)
                        if hasattr(thresholds, "model_dump")
                        else thresholds
                    )

                alert_config = (
                    getattr(quality_checks, "alert_config", None)
                    if hasattr(quality_checks, "alert_config")
                    else None
                )
                if alert_config:
                    quality_checks_data["alert_config"] = (
                        alert_config.model_dump(exclude_none=True)
                        if hasattr(alert_config, "model_dump")
                        else alert_config
                    )

                if quality_checks_data.get("checks"):
                    self._write_yaml(
                        dir_path / "quality_checks.yaml", quality_checks_data
                    )

        # 4. Save columns/ directory
        if umf.columns:
            columns_dir = dir_path / "columns"
            columns_dir.mkdir(exist_ok=True)

            for col in umf.columns:
                col_name = (
                    col.name
                    if hasattr(col, "name")
                    else col.get("name")
                    if isinstance(col, dict)
                    else ""
                )
                col_dict = (
                    col.model_dump(exclude_none=True)
                    if hasattr(col, "model_dump")
                    else dict(col)
                    if isinstance(col, dict)
                    else {}
                )

                # Extract derivation from column dict (if present) to make it a sibling
                derivation = (
                    col_dict.pop("derivation", None)
                    if isinstance(col_dict, dict)
                    else None
                )

                col_data: dict[str, Any] = {"column": col_dict}

                # Add derivation as sibling if present
                if derivation:
                    col_data["derivation"] = derivation
                # Fallback: check top-level derivations for backward compatibility
                elif hasattr(umf, "derivations") and umf.derivations:
                    derivations_val = umf.derivations
                    mappings = (
                        derivations_val.get("mappings", {})
                        if isinstance(derivations_val, dict)
                        else getattr(derivations_val, "mappings", {})
                    )
                    canonical = (
                        col.canonical_name
                        if hasattr(col, "canonical_name")
                        else col.get("canonical_name", col_name)
                        if isinstance(col, dict)
                        else col_name
                    )
                    if canonical in mappings:
                        col_data["derivation"] = mappings[canonical]
                    elif col_name in mappings:
                        col_data["derivation"] = mappings[col_name]

                # Add column-specific validations
                if col_name in col_validations_map:
                    col_data["validations"] = col_validations_map[col_name]

                self._write_yaml(columns_dir / f"{col_name}.yaml", col_data)

    def _write_yaml(self, path: Path, data: dict) -> None:
        """Write YAML with deterministic formatting.

        Args:
            path: Output file path
            data: Data to write

        Note:
            Uses tablespec.formatting to apply consistent formatting if available.
            Falls back to ruamel.yaml output if formatting module is not installed.

        """
        # Sort recursively for deterministic output
        sorted_data = self._sort_recursive(data)
        # Strip trailing whitespace from all strings to avoid spurious git changes
        cleaned_data = self._strip_trailing_whitespace(sorted_data)

        # Try to use formatting module if available, otherwise fall back to ruamel.yaml
        try:
            from tablespec.formatting import format_yaml_dict

            formatted_yaml = format_yaml_dict(cleaned_data)
        except ImportError:
            # Fallback to unformatted YAML if formatting module is not available
            from io import StringIO

            output = StringIO()
            self.yaml.dump(cleaned_data, output)
            formatted_yaml = output.getvalue()
        except Exception as e:
            # Fallback to unformatted YAML if formatting fails
            from io import StringIO
            import warnings

            warnings.warn(
                f"YAML formatting failed for {path}: {e}. Writing unformatted YAML.",
                stacklevel=2,
            )
            output = StringIO()
            self.yaml.dump(cleaned_data, output)
            formatted_yaml = output.getvalue()

        # Write once to disk
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(formatted_yaml, encoding="utf-8")

    def _sort_recursive(self, obj: Any) -> Any:
        """Sort object recursively for deterministic YAML output.

        IMPORTANT: Never sorts lists - order matters for columns and expectations!
        Only sorts dictionary keys for deterministic output.

        Args:
            obj: Object to sort

        Returns:
            Sorted dict keys (recursively) but preserved list order

        """
        if isinstance(obj, dict):
            # Filter out None values, sort keys
            filtered = {k: v for k, v in obj.items() if v is not None}
            return {
                k: self._sort_recursive(filtered[k]) for k in sorted(filtered.keys())
            }

        if isinstance(obj, list):
            # NEVER sort lists - order matters!
            # Columns and expectations must stay in original order for in-place edits
            # to produce minimal diffs without spurious reordering
            return [self._sort_recursive(item) for item in obj]

        return obj

    def _strip_trailing_whitespace(self, obj: Any) -> Any:
        """Recursively strip trailing whitespace from all string values.

        Args:
            obj: Object to clean

        Returns:
            Cleaned version of object

        """
        if isinstance(obj, dict):
            return {k: self._strip_trailing_whitespace(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [self._strip_trailing_whitespace(item) for item in obj]
        if isinstance(obj, str):
            return obj.rstrip()
        return obj
