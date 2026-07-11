"""Core validation logic for UMF schemas.

This module provides reusable validation functions used by:
- tablespec CLI
- External tooling

All validation logic is centralized here to ensure consistency across packages.
UMF loading is cached per-process-lifetime - file changes trigger process restart.
"""

from dataclasses import dataclass, field
import logging
from pathlib import Path
from typing import Any

import jsonschema
from pydantic import ValidationError

from tablespec.completeness_validator import (
    validate_baseline_expectations,
    validate_domain_types,
    validate_provenance_columns,
)
from tablespec.excel_converter import ExcelToUMFConverter, UMFToExcelConverter
from tablespec.expectation_utils import expectation_dicts_from_umf
from tablespec.models import UMF, save_umf_to_yaml
from tablespec.naming_validator import validate_naming_conventions
from tablespec.relationship_validator import RelationshipValidator
from tablespec.survivorship_display import SurvivorshipValidator
from tablespec.type_mappings import VALID_PYSPARK_TYPES
from tablespec.umf_loader import UMFFormat, UMFLoader

try:
    from tablespec.validation.gx_executor import GXSuiteExecutor
except ImportError:
    GXSuiteExecutor = None  # type: ignore[assignment]

# Cache the JSON schema at module level since it never changes at runtime
_UMF_JSON_SCHEMA: dict[str, Any] = UMF.model_json_schema()


def _get_expectation_dicts(umf: UMF) -> list[dict[str, Any]]:
    """Get expectation dicts from ExpectationSuite, falling back to legacy fields."""
    return expectation_dicts_from_umf(umf)


@dataclass
class ValidationContext:
    """Context for validation operations with process-lifetime UMF caching.

    The UMF cache is designed for CLI usage where each invocation is a new process.
    File changes will trigger a process restart, so we don't need sophisticated
    cache invalidation - the cache lives for the process lifetime.

    For long-running processes, the mtime-based cache key can be enhanced to detect
    file changes and invalidate automatically.
    """

    umf_cache: dict[Path, tuple[float, UMF]] = field(default_factory=dict)
    converter: UMFLoader = field(default_factory=UMFLoader)
    logger: logging.Logger = field(default_factory=lambda: logging.getLogger(__name__))

    def load_umf(self, path: Path) -> UMF:
        """Load UMF from file/directory with caching.

        For CLI tools (one-shot processes), this caches for the process lifetime.
        If the file changes, the process must be restarted to see changes.

        For long-running processes, the mtime tracking can detect changes and
        automatically invalidate the cache.

        Args:
            path: Path to UMF file or directory

        Returns:
            Loaded and validated UMF model

        """
        path = path.resolve()

        # Determine file to check for mtime
        check_file = path / "table.yaml" if path.is_dir() else path

        try:
            current_mtime = check_file.stat().st_mtime
        except (FileNotFoundError, OSError):
            current_mtime = 0

        # Check cache - return if mtime matches (file unchanged)
        if path in self.umf_cache:
            cached_mtime, cached_umf = self.umf_cache[path]
            if cached_mtime == current_mtime:
                return cached_umf

        # Load fresh UMF and cache it
        umf = self.converter.load(path)
        self.umf_cache[path] = (current_mtime, umf)
        return umf

    def clear_cache(self) -> None:
        """Clear the UMF cache. Useful for testing."""
        self.umf_cache.clear()


def validate_table(
    table_dir: Path,
    context: ValidationContext,
    verbose: bool = False,
    check_completeness: bool = True,
) -> tuple[bool, list[str]]:
    """Validate a single UMF table.

    Performs comprehensive validation including:
    - JSON schema validation against UMF specification
    - UMF schema structure (Pydantic models)
    - Filename pattern correctness
    - Column naming conventions (lowercase_snake_case)
    - Great Expectations validation rules (if present)
    - Expectation type compatibility with GX library
    - Column references in expectations
    - Table references in expectations (e.g., other_table_name)
    - Relationship integrity (when multiple tables present)
    - Survivorship rules for generated tables

    When check_completeness=True (default), also validates:
    - Provenance metadata columns are present
    - Domain type mappings are valid
    - Baseline validations exist for each column

    Args:
        table_dir: Path to table directory (split format) or UMF file
        context: ValidationContext with caching
        verbose: Include detailed error information
        check_completeness: If True, validate provenance columns, domain types,
            and baseline expectations (default True)

    Returns:
        Tuple of (success: bool, errors: list[str])
        success=True if validation passed, False otherwise
        errors contains human-readable error messages

    """
    errors: list[str] = []

    try:
        # Load UMF with caching
        umf = context.load_umf(table_dir)

        # Use cached JSON schema (generated once at module load)
        json_schema = _UMF_JSON_SCHEMA

        # 1. JSON schema validation
        try:
            umf_dict = umf.model_dump(mode="json", exclude_none=True)
            jsonschema.validate(instance=umf_dict, schema=json_schema)
        except jsonschema.ValidationError as e:
            errors.append(f"Schema validation failed: {e.message}")
        except jsonschema.SchemaError as e:
            errors.append(f"Schema error: {e.message}")

        # 2. Validate naming conventions
        naming_errors = validate_naming_conventions(umf)
        if naming_errors:
            for entity, err in naming_errors:
                errors.append(f"Naming error in {entity}: {err}")

        # 3. Validate filename pattern if present
        pattern_errors = context.converter.validate_filename_pattern(umf)
        if pattern_errors:
            errors.extend(pattern_errors)

        # 4. Build table lookup for cross-table validation
        # For single-table validation, load sibling tables from parent directory
        all_umf_tables = [umf]
        if table_dir.is_dir() and (table_dir / "table.yaml").exists():
            parent_dir = table_dir.parent
            for subdir in parent_dir.iterdir():
                if (
                    subdir.is_dir()
                    and (subdir / "table.yaml").exists()
                    and subdir != table_dir
                ):
                    try:
                        sibling = context.load_umf(subdir)
                        all_umf_tables.append(sibling)
                    except Exception:
                        context.logger.debug(
                            "Failed to load sibling UMF from %s", subdir
                        )

        # Build table name lookup (includes aliases)
        table_names = {t.table_name.lower() for t in all_umf_tables}
        for t in all_umf_tables:
            if t.aliases:
                table_names.update(a.lower() for a in t.aliases)

        # 5. Validate expectations if present
        exp_dicts = _get_expectation_dicts(umf)
        if exp_dicts:
            # Validate expectation configuration through the consolidated GX executor.
            gx_executor = None
            if GXSuiteExecutor is not None:
                try:
                    gx_executor = GXSuiteExecutor()
                except ImportError:
                    gx_executor = None

            column_names = {col.name for col in umf.columns}

            for i, exp in enumerate(exp_dicts):
                exp_type = exp.get("type")
                if not exp_type:
                    errors.append(f"Expectation {i}: Missing 'type' field")
                    continue

                # Skip pending implementation marker
                if exp_type == "expect_validation_rule_pending_implementation":
                    continue

                kwargs = exp.get("kwargs", {})
                meta = exp.get("meta", {})

                # Validate expectation with GX library using actual kwargs
                if gx_executor is not None:
                    is_valid, gx_error = gx_executor.validate_expectation(
                        exp_type, kwargs, meta
                    )
                    if not is_valid and gx_error:
                        errors.append(f"Expectation {i} ({exp_type}): {gx_error}")

                # Validate type_ parameter for type-checking expectations
                if exp_type == "expect_column_values_to_be_of_type":
                    type_val = kwargs.get("type_")
                    if type_val and type_val not in VALID_PYSPARK_TYPES:
                        valid_types = ", ".join(sorted(VALID_PYSPARK_TYPES))
                        errors.append(
                            f"Expectation {i} ({exp_type}): Invalid type_ '{type_val}'. "
                            + f"Must be one of: {valid_types}"
                        )

                # Validate column references in expectations
                referenced_cols = set()

                # Extract column names from various kwargs patterns
                if "column" in kwargs:
                    referenced_cols.add(kwargs["column"])
                if "column_list" in kwargs:
                    referenced_cols.update(kwargs["column_list"])
                if "column_A" in kwargs:
                    referenced_cols.add(kwargs["column_A"])
                if "column_B" in kwargs:
                    referenced_cols.add(kwargs["column_B"])
                if "column_set" in kwargs:
                    referenced_cols.update(kwargs["column_set"])

                # Check for non-existent columns (skip meta_* and source_* columns added during ingestion)
                missing_cols = {
                    col
                    for col in (referenced_cols - column_names)
                    if not col.startswith("meta_") and not col.startswith("source_")
                }
                if missing_cols:
                    missing_str = ", ".join(sorted(missing_cols))
                    errors.append(
                        f"Expectation {i} ({exp_type}): References non-existent column(s): {missing_str}"
                    )

                # Validate table references in expectations
                if exp_type == "expect_table_row_count_to_equal_other_table":
                    other_table_name = kwargs.get("other_table_name")
                    if not other_table_name:
                        errors.append(
                            f"Expectation {i} ({exp_type}): Missing required parameter 'other_table_name'"
                        )
                    elif other_table_name.lower() not in table_names:
                        errors.append(
                            f"Expectation {i} ({exp_type}): Referenced table '{other_table_name}' does not exist"
                        )

        # 6. Validate relationships when multiple tables present
        if len(all_umf_tables) > 1:
            rel_validator = RelationshipValidator()
            rel_errors = rel_validator.validate_all_relationships(all_umf_tables)
            for table_name, error_list in rel_errors.items():
                for error_type, message in error_list:
                    errors.append(
                        f"Relationship error in {table_name}: [{error_type}] {message}"
                    )

        # 7. Validate survivorship rules for generated tables
        all_tables_map = {
            t.table_name: [col.name for col in t.columns] for t in all_umf_tables
        }
        if hasattr(umf, "derivations") and umf.derivations:
            is_valid, surv_errors = SurvivorshipValidator.validate(
                umf.derivations, all_tables=all_tables_map
            )
            if not is_valid:
                errors.extend(surv_errors)

        # 8-10. Completeness checks (optional, enabled by default)
        if check_completeness:
            # 8. Validate provenance columns are present. Generated (gold)
            # tables are exempt: provenance is synthesized at INGEST time on
            # landing tables; a derived table's columns come from derivations.
            if umf.table_type != "generated":
                provenance_errors = validate_provenance_columns(umf)
                for _col_name, error_msg in provenance_errors:
                    errors.append(f"Provenance column error: {error_msg}")

            # 9. Validate domain types are valid
            domain_errors = validate_domain_types(umf)
            for col_name, error_msg in domain_errors:
                errors.append(f"Domain type error in {col_name}: {error_msg}")

            # 10. Validate baseline expectations exist
            baseline_errors = validate_baseline_expectations(umf)
            for col_name, error_msg in baseline_errors:
                errors.append(f"Baseline validation error in {col_name}: {error_msg}")

    except ValidationError as e:
        if verbose:
            for error in e.errors():
                errors.append(f"{error['loc']}: {error['msg']}")
        else:
            errors.append(f"{len(e.errors())} validation errors found")
    except FileNotFoundError as e:
        errors.append(f"File error: {e}")
    except Exception as e:
        errors.append(f"Validation error: {e}")

    return len(errors) == 0, errors


def validate_pipeline(
    pipeline_dir: Path,
    context: ValidationContext,
    verbose: bool = False,
    check_completeness: bool = True,
) -> dict[str, list[str]]:
    """Validate all tables in a pipeline.

    Loads all table UMFs and performs validation, with batch relationship checking.

    Args:
        pipeline_dir: Path to pipeline directory containing table subdirectories
        context: ValidationContext with caching
        verbose: Include detailed error information
        check_completeness: If True, validate provenance columns, domain types,
            and baseline expectations (default True)

    Returns:
        Dict mapping table names to error lists
        Empty list for each table means validation passed

    """
    results: dict[str, list[str]] = {}

    # Get all table directories
    table_dirs = sorted(
        [d for d in pipeline_dir.iterdir() if d.is_dir() and not d.name.startswith(".")]
    )

    if not table_dirs:
        return results

    # Load all UMFs and track their paths
    umf_paths: list[tuple[UMF, Path]] = []
    for table_dir in table_dirs:
        if not (table_dir / "table.yaml").exists():
            continue

        try:
            umf = context.load_umf(table_dir)
            umf_paths.append((umf, table_dir))
        except Exception as e:
            results[table_dir.name] = [f"Failed to load: {e}"]

    # Validate each table
    for umf, table_path in umf_paths:
        _success, errors = validate_table(
            table_path, context, verbose=verbose, check_completeness=check_completeness
        )
        results[umf.table_name] = errors

    return results


def show_table_info(
    table_dir: Path,
    context: ValidationContext,
) -> dict[str, Any]:
    """Get structured information about a table.

    Args:
        table_dir: Path to table directory or UMF file
        context: ValidationContext with caching

    Returns:
        Dictionary with table information

    """
    umf = context.load_umf(table_dir)

    return {
        "table_name": umf.table_name,
        "canonical_name": umf.canonical_name,
        "description": umf.description,
        "version": umf.version,
        "columns": {
            "total": len(umf.columns),
            "sample": [
                {
                    "name": col.name,
                    "data_type": col.data_type,
                    "nullable": col.is_nullable_for_all_contexts(),
                    "source": col.source,
                }
                for col in umf.columns[:10]
            ],
        },
        "validation": {
            "has_rules": bool(_get_expectation_dicts(umf)),
            "expectation_count": len(_get_expectation_dicts(umf)),
        },
        "relationships": {
            "foreign_keys": len(umf.relationships.foreign_keys or [])
            if umf.relationships
            else 0,
            "referenced_by": len(umf.relationships.referenced_by or [])
            if umf.relationships
            else 0,
        },
        "file_format": {
            "has_pattern": bool(umf.file_format and umf.file_format.filename_pattern),
        },
        "derivations": {
            "has_mappings": bool(hasattr(umf, "derivations") and umf.derivations),
            "mapping_count": len(umf.derivations.get("mappings", {}))
            if hasattr(umf, "derivations") and umf.derivations
            else 0,
        },
    }


def convert_table(
    source: Path,
    dest: Path,
    target_format: UMFFormat | None = None,
    context: ValidationContext | None = None,
) -> None:
    """Convert UMF between formats.

    Args:
        source: Source UMF file or directory
        dest: Destination path
        target_format: Target format (auto-detected if None)
        context: ValidationContext with caching (creates new if None)

    """
    if context is None:
        context = ValidationContext()

    # Detect source format and load
    umf = context.load_umf(source)

    # Determine target format
    if target_format is None:
        # Default to SPLIT for directories or unspecified, JSON for .json files
        target_format = UMFFormat.JSON if dest.suffix == ".json" else UMFFormat.SPLIT

    # Save in target format
    context.converter.save(umf, dest, target_format)


def export_table_to_excel(
    table_dir: Path,
    dest: Path,
    context: ValidationContext | None = None,
    force: bool = False,
) -> None:
    """Export UMF table to Excel workbook.

    Args:
        table_dir: Path to table directory or UMF file
        dest: Destination Excel file path
        context: ValidationContext with caching (creates new if None)
        force: If True, overwrite destination file if it exists

    Raises:
        ValueError: If destination file exists and force=False

    """
    if context is None:
        context = ValidationContext()

    # Check if destination exists
    if dest.exists() and not force:
        msg = f"File already exists: {dest}. Use force=True to overwrite."
        raise ValueError(msg)

    # Load UMF
    umf = context.load_umf(table_dir)

    # Convert to Excel
    converter = UMFToExcelConverter()
    workbook = converter.convert(umf)

    # Delete existing file if force is True (openpyxl doesn't fully overwrite)
    if dest.exists() and force:
        dest.unlink()

    # Save
    workbook.save(dest)


def import_table_from_excel(
    source: Path,
    dest: Path,
    context: ValidationContext | None = None,
    commit: bool = False,
) -> str | None:
    """Import Excel workbook to UMF with atomic per-change commits.

    Converts Excel workbook to UMF and saves to destination format.
    When commit=True, detects all changes in-memory and creates individual
    commits for each change, enabling clear git history of modifications.

    Args:
        source: Source Excel file path
        dest: Destination path (directory for split format, or .json file for JSON)
        context: ValidationContext with caching (creates new if None)
        commit: If True, create individual git commits for each detected change

    Returns:
        Last commit hash if commit=True and changes were committed, None otherwise

    """
    if context is None:
        context = ValidationContext()

    # Convert from Excel (returns UMF and review notes)
    converter = ExcelToUMFConverter()
    new_umf, review_notes = converter.convert(source)

    # Load old UMF BEFORE saving (if committing) - for in-memory comparison
    old_umf = None
    if commit and (dest.is_dir() or (not dest.exists() and not dest.suffix)):
        table_dir = dest
        try:
            old_umf = context.load_umf(table_dir)
        except Exception:
            # No old UMF exists yet - this is a new table
            old_umf = None

    # Determine output format from destination path
    is_split_format = dest.is_dir() or (not dest.exists() and not dest.suffix)
    if is_split_format:
        output_format = UMFFormat.SPLIT
    elif dest.suffix == ".json":
        output_format = UMFFormat.JSON
    else:
        # .yaml/.yml suffix - use SPLIT format (single file mode)
        output_format = UMFFormat.SPLIT

    # If not committing, just save and return
    if not commit:
        if is_split_format:
            context.converter.save(new_umf, dest, output_format)
        elif output_format == UMFFormat.JSON:
            context.converter.save_json(new_umf, dest)
        else:
            save_umf_to_yaml(new_umf, dest)
        return None

    # Commit mode: detect changes in-memory and apply/commit each one
    return _import_with_atomic_commits(
        old_umf=old_umf,
        new_umf=new_umf,
        dest=dest,
        output_format=output_format,
        context=context,
        review_notes=review_notes,
    )


def _import_with_atomic_commits(
    old_umf: UMF | None,
    new_umf: UMF,
    dest: Path,
    output_format: UMFFormat,
    context: ValidationContext,
    review_notes: dict[str, str | None],
) -> str | None:
    """Apply changes from new UMF with per-review-note commits.

    Detects all changes in-memory, groups them by review note, and creates
    individual commits for each unique review note. This creates clear, focused
    git history with business context.

    Args:
        old_umf: Previous UMF version (None if new table)
        new_umf: New UMF version from Excel import
        dest: Destination path
        output_format: Output format (SPLIT/JSON/YAML)
        context: ValidationContext
        review_notes: Map from change key to review note text

    Returns:
        Last commit hash if any commits were made, None otherwise

    """
    from tablespec.excel_import_git import ExcelImportCommitter
    from tablespec.umf_change_applier import (
        apply_column_change,
        apply_metadata_change,
        apply_validation_change,
    )
    from tablespec.umf_diff import UMFDiff

    # Detect all changes in-memory
    diff = UMFDiff(old_umf, new_umf)
    all_changes = (
        diff.get_column_changes()
        + diff.get_validation_changes()
        + diff.get_metadata_changes()
    )

    if not all_changes:
        # No changes detected - just save and return
        context.converter.save(new_umf, dest, output_format)
        return None

    # Initialize git committer
    try:
        committer = ExcelImportCommitter(dest if dest.is_dir() else dest.parent)
    except ValueError:
        # Not in a git repo - just save without committing
        context.converter.save(new_umf, dest, output_format)
        return None

    # Group changes by review note
    grouped = _group_changes_by_note(all_changes, review_notes)

    # Apply and commit each group sequentially
    current_umf = old_umf if old_umf else new_umf
    last_commit = None

    for review_note, changes in grouped.items():
        # Apply this group's changes (in-place modifications preserve order)
        from tablespec.umf_diff import (
            UMFColumnChange,
            UMFMetadataChange,
            UMFValidationChange,
        )

        for change in changes:
            if isinstance(change, UMFColumnChange):
                current_umf = apply_column_change(current_umf, change)

        for change in changes:
            if isinstance(change, UMFValidationChange):
                current_umf = apply_validation_change(current_umf, change)

        for change in changes:
            if isinstance(change, UMFMetadataChange):
                current_umf = apply_metadata_change(current_umf, change)

        # Save UMF to disk (preserves order due to _sort_recursive fix)
        context.converter.save(current_umf, dest, output_format)

        # Build detailed commit message with change descriptions
        table_name = current_umf.table_name
        change_details = _format_change_details(changes, table_name)
        commit_message = (
            f"{review_note}\n\n{change_details}" if change_details else review_note
        )

        # Commit with detailed message
        # Git will detect exactly which files changed in this step
        changed_files = list(dest.glob("**/*.yaml"))
        last_commit = committer.commit_changes(
            changed_files,
            {},
            commit_message,
        )

    return last_commit


def _group_changes_by_note(
    all_changes: list,
    review_notes_map: dict[str, str | None],
) -> dict[str, list]:
    """Group changes by their review note.

    Creates stable grouping where changes with the same review note
    are committed together.

    Args:
        all_changes: List of all change objects (columns, validation, metadata)
        review_notes_map: Map from change key to review note text

    Returns:
        Dict mapping review note text to list of changes (insertion order preserved)

    """
    from collections import OrderedDict

    groups = OrderedDict()
    no_note_changes = []

    for change in all_changes:
        key = change.get_key()
        note = review_notes_map.get(key)

        if note:
            if note not in groups:
                groups[note] = []
            groups[note].append(change)
        else:
            no_note_changes.append(change)

    # Changes without notes get a default group
    if no_note_changes:
        groups["Update from Excel import"] = no_note_changes

    return groups


def _get_rule_identifier(exp: dict, table_name: str) -> str:
    """Build rule identifier for commit messages.

    Creates: table.column.rule_type.index or table.rule_type.index

    Args:
        exp: Expectation dict with type, kwargs, and meta
        table_name: Name of the table

    Returns:
        Rule identifier string

    """
    rule_type = exp.get("type", "").replace("expect_", "")
    column = exp.get("kwargs", {}).get("column", "-")
    meta = exp.get("meta", {})
    rule_index = meta.get("rule_index", 0)

    if column and column != "-":
        return f"{table_name}.{column}.{rule_type}.{rule_index}"
    return f"{table_name}.{rule_type}.{rule_index}"


def _describe_rule_change(old_rule: dict, new_rule: dict, rule_id: str) -> str:
    """Generate a commit message describing what changed in a rule.

    Compares old vs new rule and generates a descriptive message.

    Args:
        old_rule: Original expectation dict
        new_rule: Updated expectation dict
        rule_id: Rule identifier string (e.g., table.column.type.index)

    Returns:
        Commit message describing the change

    """
    changes = []

    # Check severity change
    old_severity = old_rule.get("meta", {}).get("severity", "info")
    new_severity = new_rule.get("meta", {}).get("severity", "info")
    if old_severity != new_severity:
        changes.append(f"severity from {old_severity} to {new_severity}")

    # Check description change
    old_desc = old_rule.get("meta", {}).get("description", "")
    new_desc = new_rule.get("meta", {}).get("description", "")
    if old_desc != new_desc:
        changes.append("description")

    # Check type change (unlikely but possible)
    old_type = old_rule.get("type", "")
    new_type = new_rule.get("type", "")
    if old_type != new_type:
        changes.append(f"type from {old_type} to {new_type}")

    # Check kwargs change (rule configuration)
    old_kwargs = old_rule.get("kwargs", {})
    new_kwargs = new_rule.get("kwargs", {})
    if old_kwargs != new_kwargs:
        # Check specific fields that typically change
        for key in set(list(old_kwargs.keys()) + list(new_kwargs.keys())):
            if old_kwargs.get(key) != new_kwargs.get(key):
                if key != "column":  # Don't report column changes as that's implicit
                    changes.append(f"{key}")

    if changes:
        change_desc = ", ".join(changes)
        return f"Modified {rule_id}: {change_desc}"

    # Fallback if we can't detect specific changes
    return f"Modified {rule_id}"


def _get_saved_files(dest: Path, format_type: UMFFormat) -> list[Path]:
    """Get list of files saved in given format.

    Args:
        dest: Destination directory or file
        format_type: UMF format type

    Returns:
        List of file paths

    """
    if format_type == UMFFormat.SPLIT and dest.is_dir():
        # Return all YAML files in split directory
        return list(dest.glob("**/*.yaml")) + list(dest.glob("**/*.yml"))
    if format_type == UMFFormat.JSON:
        return [dest]
    return [dest]


def _format_change_details(changes: list, table_name: str) -> str:
    """Format detailed change descriptions for commit message.

    Args:
        changes: List of change objects with .description() method
        table_name: Name of the table being changed

    Returns:
        Formatted string with bullet list of changes

    """
    if not changes:
        return ""

    lines = [f"Changes in {table_name}:"]
    for change in changes:
        lines.append(f"- {change.description()}")

    return "\n".join(lines)
