"""Tablespec CLI - UMF schema management and conversion.

Examples:
    # Convert JSON to split
    tablespec convert outreach_list.json tables/outreach_list/

    # Convert split to JSON
    tablespec convert tables/outreach_list/ outreach_list.json

    # Validate UMF
    tablespec validate tables/outreach_list/

    # Show UMF info
    tablespec info tables/outreach_list/

    # Batch convert
    tablespec batch-convert tables/ output/ --format json

"""

from pathlib import Path

import os

import click
from pydantic import ValidationError
from rich.console import Console
from rich.table import Table as RichTable
import typer

from tablespec.excel_converter import ExcelToUMFConverter, UMFToExcelConverter
from tablespec.inference.domain_types import DomainTypeInference, DomainTypeRegistry
from tablespec.dialects import CAST_DIALECTS, PROFILE_TARGETS, resolve_emit_defaults
from tablespec.umf_loader import UMFFormat, UMFLoader

# validator module is not yet ported; commands that depend on it will be
# registered only when the module is available.
try:
    from tablespec.validator import (  # type: ignore[import-not-found]
        ValidationContext,
        convert_table,
        show_table_info,
        validate_pipeline,
        validate_table,
    )

    _HAS_VALIDATOR = True
except ImportError:
    _HAS_VALIDATOR = False

app = typer.Typer(
    name="tablespec",
    help="Work with UMF (Universal Metadata Format) table schemas",
)
console = Console(no_color=bool(os.environ.get("NO_COLOR")))
_EMIT_DIALECT_HELP = (
    "Cast dialect for emitted models (duckdb, spark, databricks); "
    "databricks is the Databricks-facing alias for Spark-family cast SQL. "
    "Default: duckdb, or databricks when running on a Databricks runtime"
)
_EMIT_TARGET_HELP = (
    "profiles.yml adapter target. Default: mirrors --dialect, or the runnable "
    "databricks_notebook session target when running on a Databricks runtime"
)

# Module-level validation context (process lifetime caching) - only when validator is available
_validation_context = ValidationContext() if _HAS_VALIDATOR else None


if _HAS_VALIDATOR:

    @app.command()
    def convert(
        source: Path = typer.Argument(
            ...,
            help="Source UMF file or directory",
            exists=True,
        ),
        dest: Path = typer.Argument(
            ...,
            help="Destination path",
        ),
        format: str | None = typer.Option(
            None,
            "--format",
            "-f",
            help="Target format: 'split' or 'json' (auto-detected if not specified)",
        ),
        force: bool = typer.Option(
            False,
            "--force",
            help="Overwrite existing destination",
        ),
    ) -> None:
        """Convert UMF between split and JSON formats.

        Examples:
          # Split to JSON (for artifacts)
          tablespec convert tables/outreach_list/ outreach_list.json

          # JSON to split (for editing)
          tablespec convert outreach_list.json tables/outreach_list/

          # Explicit format
          tablespec convert tables/ output/ --format json

        """
        assert _validation_context is not None
        try:
            # Auto-detect source format
            source_format = _validation_context.converter.detect_format(source)

            # Determine target format
            if format:
                if format.lower() in ("split", "s"):
                    target_format = UMFFormat.SPLIT
                elif format.lower() in ("json", "j"):
                    target_format = UMFFormat.JSON
                else:
                    console.print(
                        f"[red]Error:[/red] Unknown format '{format}'. Use 'split' or 'json'."
                    )
                    raise typer.Exit(1)
            # Infer from source
            elif source_format == UMFFormat.SPLIT:
                target_format = UMFFormat.JSON  # Default: split -> JSON (artifact)
            else:  # JSON
                target_format = UMFFormat.SPLIT  # Default: JSON -> split (for editing)

            # Check if target exists
            if dest.exists() and not force:
                console.print(
                    f"[red]Error:[/red] {dest} already exists. Use --force to overwrite."
                )
                raise typer.Exit(1)

            # Convert
            console.print(f"[cyan]Converting[/cyan] {source} -> {dest}")
            console.print(
                f"[dim]Format: {source_format.value} -> {target_format.value}[/dim]"
            )

            convert_table(
                source, dest, target_format=target_format, context=_validation_context
            )

            console.print("[green]Done.[/green] Conversion complete!")

        except Exception as e:
            console.print(f"[red]Error:[/red] {e}")
            raise typer.Exit(1)

    @app.command()
    def validate(
        path: Path = typer.Argument(
            ...,
            help="UMF split directory or JSON file to validate",
            exists=True,
        ),
        verbose: bool = typer.Option(
            False,
            "--verbose",
            "-v",
            help="Show detailed validation errors",
        ),
    ) -> None:
        """Validate UMF schema for correctness.

        Performs comprehensive validation including:
        - JSON schema validation against UMF specification
        - UMF schema structure (Pydantic models)
        - Filename pattern correctness
        - Column naming conventions (lowercase_snake_case)
        - Great Expectations validation rules (if present)
        - Expectation type compatibility with GX library
        - Relationship integrity (automatic when multiple tables present)

        Examples:
          tablespec validate tables/outreach_list/
          tablespec validate outreach_list.json -v
          tablespec validate tables/

        """
        assert _validation_context is not None
        try:
            console.print(f"[cyan]Validating[/cyan] {path}...")

            # Determine if path is single table or pipeline
            if path.is_dir() and (path / "table.yaml").exists():
                # Single table validation
                success, errors = validate_table(
                    path, _validation_context, verbose=verbose
                )

                if success:
                    # Show summary
                    umf = _validation_context.load_umf(path)
                    console.print("[green]Valid[/green] UMF schema")
                    console.print(
                        f"  [cyan]Table:[/cyan] {umf.table_name} ({umf.canonical_name})"
                    )
                    console.print(f"    [cyan]Columns:[/cyan] {len(umf.columns)}")
                    if umf.file_format and umf.file_format.filename_pattern:
                        console.print("    [cyan]Filename pattern:[/cyan] Valid")
                    if umf.expectations and umf.expectations.expectations:
                        console.print(
                            f"    [cyan]Expectations:[/cyan] "
                            f"{len(umf.expectations.expectations)}"
                        )
                    if umf.relationships and umf.relationships.foreign_keys:
                        console.print(
                            f"    [cyan]Foreign keys:[/cyan] "
                            f"{len(umf.relationships.foreign_keys)}"
                        )
                    if hasattr(umf, "derivations") and umf.derivations:
                        surv_mappings = len(umf.derivations.get("mappings", {}))
                        console.print(
                            f"    [cyan]Survivorship mappings:[/cyan] {surv_mappings}"
                        )
                else:
                    console.print("[red]FAIL[/red] Validation failed")
                    for error in errors:
                        console.print(f"  {error}")
                    raise typer.Exit(1)
            elif path.is_dir():
                # Pipeline validation
                results = validate_pipeline(path, _validation_context, verbose=verbose)

                failed_tables = {name: errs for name, errs in results.items() if errs}

                if failed_tables:
                    console.print("[red]FAIL[/red] Validation failed")
                    for table_name, errs in failed_tables.items():
                        console.print(f"\n[red]{table_name}:[/red]")
                        for error in errs:
                            console.print(f"  {error}")
                    raise typer.Exit(1)
                console.print(
                    f"[green]Valid[/green] All {len(results)} tables passed validation"
                )
            else:
                # Single file validation
                success, errors = validate_table(
                    path, _validation_context, verbose=verbose
                )

                if success:
                    umf = _validation_context.load_umf(path)
                    console.print("[green]Valid[/green] UMF schema")
                    console.print(
                        f"  [cyan]Table:[/cyan] {umf.table_name} ({umf.canonical_name})"
                    )
                    console.print(f"    [cyan]Columns:[/cyan] {len(umf.columns)}")
                else:
                    console.print("[red]FAIL[/red] Validation failed")
                    for error in errors:
                        console.print(f"  {error}")
                    raise typer.Exit(1)

        except Exception as e:
            console.print(f"[red]Error:[/red] {e}")
            raise typer.Exit(1)

    @app.command()
    def info(
        path: Path = typer.Argument(
            ...,
            help="UMF split directory or JSON file",
            exists=True,
        ),
    ) -> None:
        """Display UMF schema information.

        Examples:
          tablespec info tables/outreach_list/
          tablespec info outreach_list.json

        """
        assert _validation_context is not None
        try:
            show_table_info(path, _validation_context)
            umf = _validation_context.load_umf(path)

            # Title
            console.print(f"\n[bold cyan]{umf.canonical_name}[/bold cyan]")
            console.print(f"  [dim]Table Name:[/dim] {umf.table_name}")

            if umf.description:
                console.print(f"  [dim]Description:[/dim] {umf.description}")

            console.print(f"  [dim]Version:[/dim] {umf.version}")

            # Columns summary
            console.print(f"\n[bold cyan]Columns ({len(umf.columns)}):[/bold cyan]")
            table = RichTable(show_header=True, show_lines=False)
            table.add_column("Name", style="cyan")
            table.add_column("Type", style="magenta")
            table.add_column("Nullable", justify="center")
            table.add_column("Source")

            for col in umf.columns[:10]:  # First 10
                nullable = "Y" if col.is_nullable_for_all_contexts() else "N"
                table.add_row(col.name, col.data_type, nullable, col.source or "data")

            console.print(table)
            if len(umf.columns) > 10:
                console.print(f"  [dim]... and {len(umf.columns) - 10} more[/dim]")

            # Validation summary
            if umf.expectations and umf.expectations.expectations:
                exp_count = len(umf.expectations.expectations)
                console.print(
                    f"\n[bold cyan]Validation:[/bold cyan] {exp_count} expectations"
                )

            # Relationships
            if umf.relationships:
                fk_count = len(umf.relationships.foreign_keys or [])
                ref_count = len(umf.relationships.referenced_by or [])
                if fk_count or ref_count:
                    console.print(
                        "\n[bold cyan]Relationships:[/bold cyan] "
                        + f"{fk_count} outgoing, {ref_count} incoming"
                    )

            console.print()

        except Exception as e:
            console.print(f"[red]Error:[/red] {e}")
            raise typer.Exit(1)


@app.command()
def batch_convert(
    source_dir: Path = typer.Argument(
        ...,
        help="Directory containing UMF files/dirs",
        exists=True,
    ),
    dest_dir: Path = typer.Argument(
        ...,
        help="Output directory",
    ),
    format: str = typer.Option(
        ...,
        "--format",
        "-f",
        help="Target format: 'split' or 'json'",
    ),
    pattern: str = typer.Option(
        "*.umf.yaml",
        "--pattern",
        "-p",
        help="File pattern to match",
    ),
    force: bool = typer.Option(
        False,
        "--force",
        help="Overwrite existing files",
    ),
) -> None:
    """Batch convert multiple UMF files.

    Examples:
      # Convert all JSON files to split
      tablespec batch-convert tables/ split-tables/ --format split

      # Convert all split dirs to JSON
      tablespec batch-convert split-tables/ tables/ --format json

    """
    converter = UMFLoader()

    # Parse format
    if format.lower() in ("split", "s"):
        target_format = UMFFormat.SPLIT
    elif format.lower() in ("json", "j"):
        target_format = UMFFormat.JSON
    else:
        console.print(
            f"[red]Error:[/red] Unknown format '{format}'. Use 'split' or 'json'."
        )
        raise typer.Exit(1)

    # Find files to convert
    if target_format == UMFFormat.SPLIT:
        # Converting TO split: find JSON files
        files = sorted(source_dir.rglob(pattern))
        # Also add .json files if pattern doesn't match them
        if "json" not in pattern:
            json_files = list(source_dir.rglob("*.umf.json"))
            files = sorted(set(files) | set(json_files))
    else:
        # Converting TO JSON: find split directories (have table.yaml)
        files = sorted({d.parent for d in source_dir.rglob("table.yaml")})

    if not files:
        console.print("[yellow]Warning:[/yellow] No files found matching pattern")
        return

    console.print(f"[cyan]Found {len(files)} files to convert[/cyan]")

    dest_dir.mkdir(parents=True, exist_ok=True)
    success_count = 0
    error_count = 0

    for file in files:
        try:
            # Determine output path
            if target_format == UMFFormat.SPLIT:
                # file.stem = "outreach_list.umf" or "outreach_list" -> "outreach_list"
                table_name = file.stem.replace(".umf", "").replace(".json", "")
                dest = dest_dir / table_name
            else:  # JSON
                # Convert .yaml/.json to .json
                table_name = file.stem.replace(".umf", "").replace(".json", "")
                dest = dest_dir / f"{table_name}.json"

            # Skip if exists and not force
            if dest.exists() and not force:
                console.print(f"  [yellow]SKIP[/yellow] {file} (exists, use --force)")
                continue

            console.print(f"  [dim]->[/dim] {file}")
            converter.convert(file, dest, target_format=target_format)
            success_count += 1

        except Exception as e:
            console.print(f"  [red]FAIL[/red] {file}: {e}")
            error_count += 1

    console.print()
    console.print(f"[green]Done.[/green] Complete: {success_count} converted", end="")
    if error_count:
        console.print(f", {error_count} errors", style="red")
    else:
        console.print()


@app.command()
def generate(
    source: Path = typer.Argument(
        ...,
        help="UMF split directory or JSON file to generate schema from",
        exists=True,
    ),
    format: str = typer.Option(
        ...,
        "--format",
        "-f",
        help="Output format: sql, pyspark, json, or ingest",
    ),
) -> None:
    """Generate SQL DDL, PySpark schema, JSON Schema, or an ingest plan from a UMF file.

    Writes raw output to stdout so it can be piped to files or other tools.
    The ``ingest`` format emits a committed raw->ingest SQL artifact (Databricks/Delta).

    Examples:
      tablespec generate tables/claims/ -f pyspark > schema.py
      tablespec generate tables/claims/ -f json > schema.json
      tablespec generate tables/claims/ -f ingest > claims.ingest.sql

    """
    import json as json_mod

    from tablespec.schemas.generators import (
        generate_json_schema,
        generate_pyspark_schema,
        generate_sql_ddl,
    )
    from tablespec.schemas.ingest_generator import generate_ingest_sql

    format_lower = format.strip().lower()
    if format_lower not in ("sql", "pyspark", "json", "ingest"):
        console.print(
            f"[red]Error:[/red] Unknown format '{format}'. Choose from: sql, pyspark, json, ingest"
        )
        raise typer.Exit(1)

    try:
        loader = UMFLoader()
        umf = loader.load(source)
        umf_data = umf.model_dump(exclude_none=True)

        if format_lower == "sql":
            result = generate_sql_ddl(umf_data)
            print(result)
        elif format_lower == "pyspark":
            result = generate_pyspark_schema(umf_data)
            print(result)
        elif format_lower == "json":
            result = generate_json_schema(umf_data)
            print(json_mod.dumps(result, indent=2))
        elif format_lower == "ingest":
            result = generate_ingest_sql(umf_data)
            print(result)

    except FileNotFoundError as e:
        console.print(f"[red]Error:[/red] {e}")
        raise typer.Exit(1)
    except ValidationError as e:
        console.print(f"[red]Validation Error:[/red]\n{e}")
        raise typer.Exit(1)
    except Exception as e:
        console.print(f"[red]Error:[/red] {e}")
        raise typer.Exit(1)


@app.command()
def emit(
    source: Path = typer.Argument(
        ...,
        help="UMF split directory or JSON file to emit a runnable project from",
        exists=True,
    ),
    out_dir: Path = typer.Argument(
        ...,
        help="Output directory for the emitted project",
    ),
    backend: str = typer.Option(
        "dbt",
        "--backend",
        "-b",
        help="Emission backend (currently: dbt)",
    ),
    project_name: str | None = typer.Option(
        None,
        "--project-name",
        help="dbt project + profile name (default depends on single/multi table)",
    ),
    dialect: str | None = typer.Option(
        None,
        "--dialect",
        help=_EMIT_DIALECT_HELP,
        click_type=click.Choice(CAST_DIALECTS),
    ),
    target: str | None = typer.Option(
        None,
        "--target",
        help=_EMIT_TARGET_HELP,
        click_type=click.Choice(PROFILE_TARGETS),
    ),
    run: bool = typer.Option(
        False,
        "--run",
        help="After emitting, run the project with dbt (dbt build via dbt-duckdb)",
    ),
) -> None:
    """Emit a runnable project for a UMF (or UMF set) via a backend.

    The ``dbt`` backend materializes a complete dbt project (model SQL,
    contracts/tests, sources, profiles, scaffolding) into OUT_DIR. With ``--run``
    it also invokes ``dbt build`` against the emitted project (requires dbt-duckdb).

    Examples:
      tablespec emit tables/ out/ --backend dbt --run

    """
    from tablespec.dbt import EmitterError, get_emitter

    try:
        loader = UMFLoader()
        if source.is_dir() and not (source / "schema.yaml").exists():
            # A directory of tables -> emit a multi-table project.
            umfs = [loader.load(p.parent) for p in sorted(source.rglob("table.yaml"))]
            if not umfs:
                # Fall back to treating it as a single split-format table dir.
                umfs = [loader.load(source)]
        else:
            umfs = [loader.load(source)]

        emitter = get_emitter(backend)
        # Resolve here purely for display; the generators apply the SAME
        # resolution, so what we print is what gets emitted.
        resolved_dialect, resolved_target = resolve_emit_defaults(dialect, target)
        console.print(
            f"[cyan]Emitting[/cyan] {backend} project for "
            f"{len(umfs)} table(s) -> {out_dir} "
            f"(dialect={resolved_dialect}, target={resolved_target})"
        )
        project = emitter.emit(
            umfs, out_dir, project_name=project_name, dialect=dialect, target=target
        )
        console.print(
            f"[green]Emitted[/green] {len(project.files)} files "
            f"(project '{project.project_name}') to {project.project_dir}"
        )

        if run:
            from tablespec.dbt import DbtRunner

            console.print("[cyan]Running[/cyan] dbt build...")
            runner = DbtRunner()
            result = runner.build(project)
            if result.success:
                console.print("[green]dbt build succeeded.[/green]")
            else:
                console.print(f"[red]dbt build failed[/red] (exit {result.returncode})")
                console.print(result.stdout)
                console.print(result.stderr)
                raise typer.Exit(1)

    except EmitterError as e:
        console.print(f"[red]Error:[/red] {e}")
        raise typer.Exit(1)
    except FileNotFoundError as e:
        console.print(f"[red]Error:[/red] {e}")
        raise typer.Exit(1)
    except ValidationError as e:
        console.print(f"[red]Validation Error:[/red]\n{e}")
        raise typer.Exit(1)


@app.command()
def export_excel(
    source: Path = typer.Argument(
        ...,
        help="Source UMF split directory or JSON file",
        exists=True,
    ),
    dest: Path = typer.Argument(
        ...,
        help="Destination Excel file (.xlsx)",
    ),
    force: bool = typer.Option(
        False,
        "--force",
        help="Overwrite destination file if it already exists",
    ),
) -> None:
    """Export UMF to Excel workbook for human-friendly editing.

    Creates a structured Excel workbook with:
    - Data validation (dropdowns, constraints)
    - Helper columns (validation status, suggestions)
    - Instructions and examples
    - Support for all UMF features

    Examples:
      tablespec export-excel tables/medical_claims/ claims.xlsx --force

    """
    try:
        # Check if destination exists
        if dest.exists() and not force:
            console.print(f"[red]Error:[/red] File already exists: {dest}")
            console.print("[yellow]Use --force to overwrite[/yellow]")
            raise typer.Exit(1)

        # Load UMF
        console.print(f"[cyan]Loading UMF[/cyan] from {source}...")
        loader = UMFLoader()
        umf = loader.load(source)

        # Convert to Excel
        console.print("[cyan]Creating Excel workbook[/cyan]...")
        excel_converter = UMFToExcelConverter()
        workbook = excel_converter.convert(umf)

        # Delete existing file if force is True (openpyxl doesn't fully overwrite)
        if dest.exists() and force:
            dest.unlink()

        # Save
        workbook.save(dest)
        console.print(f"[green]Done.[/green] Excel workbook created: {dest}")
        console.print("[dim]Now you can open this file in Excel and make edits![/dim]")

    except FileNotFoundError as e:
        console.print(f"[red]Error:[/red] {e}")
        raise typer.Exit(1)
    except ValidationError as e:
        console.print("[red]Error:[/red] Invalid UMF")
        for error in e.errors()[:5]:
            console.print(f"  {error['loc']}: {error['msg']}")
        raise typer.Exit(1)
    except Exception as e:
        console.print(f"[red]Error:[/red] {e}")
        raise typer.Exit(1)


@app.command()
def import_excel(
    source: Path = typer.Argument(
        ...,
        help="Source Excel file (.xlsx)",
        exists=True,
    ),
    dest: Path = typer.Argument(
        ...,
        help="Destination directory (split format)",
    ),
    force: bool = typer.Option(
        False,
        "--force",
        help="Overwrite existing destination",
    ),
) -> None:
    """Import Excel workbook to UMF split format.

    Validates the Excel workbook and converts to UMF in split format
    (git-friendly directory structure):
    - Full validation with helpful error messages
    - Identifies incompatibilities before conversion
    - Supports round-trip conversion

    Examples:
      tablespec import-excel table.xlsx tables/outreach_list/

    """
    try:
        # Convert from Excel
        console.print(f"[cyan]Loading Excel workbook[/cyan] from {source}...")
        converter = ExcelToUMFConverter()
        umf, _metadata = converter.convert(source)  # Unpack tuple

        # Check if destination exists
        if dest.exists() and not force:
            console.print(
                f"[red]Error:[/red] {dest} already exists. Use --force to overwrite."
            )
            raise typer.Exit(1)

        # Handle --force by removing existing destination
        if dest.exists() and force:
            import shutil

            if dest.is_dir():
                shutil.rmtree(dest)
            else:
                dest.unlink()

        # Save in split format
        console.print(f"[cyan]Saving to[/cyan] {dest}...")
        loader = UMFLoader()
        loader.save(umf, dest, UMFFormat.SPLIT)

        console.print(f"[green]Done.[/green] UMF saved successfully: {dest}")

    except FileNotFoundError as e:
        console.print(f"[red]Error:[/red] {e}")
        raise typer.Exit(1)
    except ValidationError as e:
        console.print("[red]Invalid UMF:[/red]")
        for error in e.errors()[:5]:
            console.print(f"  {error['loc']}: {error['msg']}")
        raise typer.Exit(1)
    except ValueError as e:
        console.print(f"[red]Validation Error:[/red] {e}")
        raise typer.Exit(1)
    except Exception as e:
        console.print(f"[red]Error:[/red] {e}")
        raise typer.Exit(1)


@app.command()
def domains_list(
    format: str = typer.Option(
        "text",
        "--format",
        "-f",
        help="Output format: 'text' or 'json'",
    ),
) -> None:
    """List all registered domain types.

    Shows a table of all available domain types with their descriptions.

    Examples:
      tablespec domains-list
      tablespec domains-list --format json

    """
    try:
        registry = DomainTypeRegistry()
        domain_types = registry.list_domain_types()

        if format.lower() == "json":
            # JSON output
            import json

            data = []
            for name in domain_types:
                dt = registry.get_domain_type(name)
                if dt:
                    data.append(
                        {
                            "name": name,
                            "title": dt.get("name", ""),
                            "description": dt.get("description", ""),
                        }
                    )
            console.print(json.dumps(data, indent=2))
        else:
            # Text output with table
            if not domain_types:
                console.print("[yellow]No domain types found[/yellow]")
                return

            table = RichTable(show_header=True, show_lines=False, title="Domain Types")
            table.add_column("Name", style="cyan")
            table.add_column("Title", style="green")
            table.add_column("Description", style="white")

            for name in sorted(domain_types):
                dt = registry.get_domain_type(name)
                if dt:
                    title = dt.get("name", "")
                    description = dt.get("description", "")
                    # Truncate long descriptions
                    if len(description) > 60:
                        description = description[:57] + "..."
                    table.add_row(name, title, description)

            console.print(table)
            console.print(f"\n[dim]Total: {len(domain_types)} domain types[/dim]")

    except FileNotFoundError as e:
        console.print(f"[red]Error:[/red] {e}")
        raise typer.Exit(1)
    except Exception as e:
        console.print(f"[red]Error:[/red] {e}")
        raise typer.Exit(1)


@app.command()
def domains_show(
    name: str = typer.Argument(
        ...,
        help="Domain type name (e.g., 'us_state_code')",
    ),
    format: str = typer.Option(
        "yaml",
        "--format",
        "-f",
        help="Output format: 'yaml' or 'json'",
    ),
) -> None:
    """Show full definition of a domain type.

    Displays the complete YAML or JSON definition including detection rules,
    validation specifications, and sample generation methods.

    Examples:
      tablespec domains-show us_state_code
      tablespec domains-show email --format json
      tablespec domains-show npi

    """
    try:
        registry = DomainTypeRegistry()
        domain_type = registry.get_domain_type(name)

        if not domain_type:
            console.print(f"[red]Error:[/red] Domain type '{name}' not found")
            raise typer.Exit(1)

        if format.lower() == "json":
            import json

            console.print(json.dumps({name: domain_type}, indent=2))
        else:
            # YAML output
            import yaml

            output = yaml.dump(
                {name: domain_type}, default_flow_style=False, sort_keys=False
            )
            console.print(output)

    except FileNotFoundError as e:
        console.print(f"[red]Error:[/red] {e}")
        raise typer.Exit(1)
    except Exception as e:
        console.print(f"[red]Error:[/red] {e}")
        raise typer.Exit(1)


@app.command()
def domains_infer(
    column_name: str = typer.Option(
        ...,
        "--column",
        "-c",
        help="Column name to analyze",
    ),
    description: str = typer.Option(
        None,
        "--description",
        "-d",
        help="Column description (optional)",
    ),
    samples: str = typer.Option(
        None,
        "--samples",
        "-s",
        help="Sample values (comma-separated, optional)",
    ),
) -> None:
    """Infer domain type for a column.

    Uses column name, description, and sample values to detect the most likely
    domain type and confidence score.

    Examples:
      tablespec domains-infer --column member_id
      tablespec domains-infer --column state --description "State code abbreviation"
      tablespec domains-infer --column phone --samples "5551234567,555-123-4567"

    """
    try:
        inference = DomainTypeInference()

        # Parse sample values
        sample_values = None
        if samples:
            sample_values = [s.strip() for s in samples.split(",")]

        # Infer
        domain_type, confidence = inference.infer_domain_type(
            column_name,
            description=description,
            sample_values=sample_values,
        )

        if domain_type:
            console.print(
                f"[green]Found[/green] Inferred domain type: [cyan]{domain_type}[/cyan]"
            )
            console.print(f"  [dim]Confidence:[/dim] {confidence:.1%}")

            # Show details
            registry = DomainTypeRegistry()
            dt = registry.get_domain_type(domain_type)
            if dt:
                console.print(f"  [dim]Title:[/dim] {dt.get('name', '')}")
                console.print(f"  [dim]Description:[/dim] {dt.get('description', '')}")
        else:
            console.print("[yellow]No domain type found with high confidence[/yellow]")
            console.print("  Try providing more context (description, sample values)")

    except FileNotFoundError as e:
        console.print(f"[red]Error:[/red] {e}")
        raise typer.Exit(1)
    except Exception as e:
        console.print(f"[red]Error:[/red] {e}")
        raise typer.Exit(1)


@app.command()
def domains_set(
    table_path: str = typer.Argument(..., help="Path to UMF table (file or directory)"),
    column: str = typer.Option(..., "--column", "-c", help="Column name"),
    domain_type: str = typer.Option(..., "--type", "-t", help="Domain type to assign"),
) -> None:
    """Set the domain type for a column.

    Validates that the domain type exists in the registry before setting.

    Examples:
      tablespec domains-set tables/claims/ --column gender_cd --type gender_code
      tablespec domains-set table.yaml --column state_cd --type us_state_code

    """
    from tablespec.authoring.mutations import modify_column

    try:
        # Validate domain type exists
        registry = DomainTypeRegistry()
        if not registry.get_domain_type(domain_type):
            available = registry.list_domain_types()
            console.print(f"[red]Error:[/red] Unknown domain type '{domain_type}'")
            console.print(
                f"[dim]Available: {', '.join(sorted(available)[:10])}... ({len(available)} total)[/dim]"
            )
            raise typer.Exit(1)

        loader = UMFLoader()
        umf = loader.load(Path(table_path))
        updated = modify_column(umf, column, domain_type=domain_type)
        dest = Path(table_path)
        fmt = UMFFormat.JSON if dest.suffix == ".json" else UMFFormat.SPLIT
        loader.save(updated, dest, format=fmt)
        console.print(
            f"[green]Set[/green] domain type '{domain_type}' on column '{column}'"
        )

    except (ValueError, FileNotFoundError) as e:
        console.print(f"[red]Error:[/red] {e}")
        raise typer.Exit(1)


@app.command()
def column_add(
    table_path: str = typer.Argument(..., help="Path to UMF table (file or directory)"),
    name: str = typer.Option(..., "--name", "-n", help="Column name"),
    data_type: str = typer.Option(
        ..., "--type", "-t", help="Data type (e.g., VARCHAR, INTEGER, DATE)"
    ),
    description: str = typer.Option(
        None, "--description", "-d", help="Column description"
    ),
    nullable: bool = typer.Option(
        True, "--nullable/--not-nullable", help="Whether column is nullable"
    ),
    length: int = typer.Option(None, "--length", help="Max length for VARCHAR/CHAR"),
) -> None:
    """Add a column to a UMF table.

    Examples:
      tablespec column-add tables/claims/ --name status_cd --type VARCHAR --length 10
      tablespec column-add table.yaml --name amount --type DECIMAL --not-nullable

    """
    from tablespec.authoring.mutations import add_column

    try:
        loader = UMFLoader()
        umf = loader.load(Path(table_path))

        kwargs: dict = {}
        if description is not None:
            kwargs["description"] = description
        if not nullable:
            kwargs["nullable"] = False
        if length is not None:
            kwargs["max_length"] = length

        updated = add_column(umf, name, data_type, **kwargs)
        dest = Path(table_path)
        fmt = UMFFormat.JSON if dest.suffix == ".json" else UMFFormat.SPLIT
        loader.save(updated, dest, format=fmt)
        console.print(
            f"[green]Added[/green] column '{name}' ({data_type}) to {umf.table_name}"
        )

    except (ValueError, FileNotFoundError) as e:
        console.print(f"[red]Error:[/red] {e}")
        raise typer.Exit(1)


@app.command()
def column_remove(
    table_path: str = typer.Argument(..., help="Path to UMF table (file or directory)"),
    name: str = typer.Option(..., "--name", "-n", help="Column name to remove"),
) -> None:
    """Remove a column from a UMF table.

    Examples:
      tablespec column-remove tables/claims/ --name legacy_field

    """
    from tablespec.authoring.mutations import remove_column

    try:
        loader = UMFLoader()
        umf = loader.load(Path(table_path))
        updated = remove_column(umf, name)
        dest = Path(table_path)
        fmt = UMFFormat.JSON if dest.suffix == ".json" else UMFFormat.SPLIT
        loader.save(updated, dest, format=fmt)
        console.print(f"[green]Removed[/green] column '{name}' from {umf.table_name}")

    except (ValueError, FileNotFoundError) as e:
        console.print(f"[red]Error:[/red] {e}")
        raise typer.Exit(1)


@app.command()
def column_modify(
    table_path: str = typer.Argument(..., help="Path to UMF table (file or directory)"),
    name: str = typer.Option(..., "--name", "-n", help="Column name to modify"),
    data_type: str = typer.Option(None, "--type", "-t", help="New data type"),
    description: str = typer.Option(
        None, "--description", "-d", help="New description"
    ),
    length: int = typer.Option(None, "--length", help="New max length"),
) -> None:
    """Modify a column's attributes in a UMF table.

    Examples:
      tablespec column-modify tables/claims/ --name status_cd --type VARCHAR --length 20
      tablespec column-modify table.yaml --name amount --description "Total claim amount"

    """
    from tablespec.authoring.mutations import modify_column

    try:
        loader = UMFLoader()
        umf = loader.load(Path(table_path))

        changes: dict = {}
        if data_type is not None:
            changes["data_type"] = data_type
        if description is not None:
            changes["description"] = description
        if length is not None:
            changes["max_length"] = length

        if not changes:
            console.print("[yellow]No changes specified.[/yellow]")
            raise typer.Exit(0)

        updated = modify_column(umf, name, **changes)
        dest = Path(table_path)
        fmt = UMFFormat.JSON if dest.suffix == ".json" else UMFFormat.SPLIT
        loader.save(updated, dest, format=fmt)
        console.print(
            f"[green]Modified[/green] column '{name}' in {umf.table_name}: {', '.join(changes)}"
        )

    except (ValueError, FileNotFoundError) as e:
        console.print(f"[red]Error:[/red] {e}")
        raise typer.Exit(1)


@app.command()
def column_rename(
    table_path: str = typer.Argument(..., help="Path to UMF table (file or directory)"),
    old_name: str = typer.Option(..., "--from", help="Current column name"),
    new_name: str = typer.Option(..., "--to", help="New column name"),
    keep_alias: bool = typer.Option(
        False, "--keep-alias", help="Add old name to aliases list"
    ),
) -> None:
    """Rename a column in a UMF table.

    Examples:
      tablespec column-rename tables/claims/ --from mbr_id --to member_id --keep-alias

    """
    from tablespec.authoring.mutations import rename_column

    try:
        loader = UMFLoader()
        umf = loader.load(Path(table_path))
        updated = rename_column(umf, old_name, new_name, keep_alias=keep_alias)
        dest = Path(table_path)
        fmt = UMFFormat.JSON if dest.suffix == ".json" else UMFFormat.SPLIT
        loader.save(updated, dest, format=fmt)
        msg = f"[green]Renamed[/green] '{old_name}' → '{new_name}' in {umf.table_name}"
        if keep_alias:
            msg += f" (alias '{old_name}' preserved)"
        console.print(msg)

    except (ValueError, FileNotFoundError) as e:
        console.print(f"[red]Error:[/red] {e}")
        raise typer.Exit(1)


@app.command()
def validation_sync(
    table_path: str = typer.Argument(
        ..., help="Path to UMF table directory (split format)"
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Show what would change without modifying files"
    ),
    clean_outdated: bool = typer.Option(
        False, "--clean-outdated", help="Remove outdated baseline expectations"
    ),
    aggressive: bool = typer.Option(
        False, "--aggressive", help="Upgrade unmarked expectations to baseline"
    ),
) -> None:
    """Sync baseline expectations from UMF metadata.

    Regenerates deterministic expectations from the current UMF definition
    and reconciles them with existing expectations, preserving user customizations.

    Examples:
      tablespec validation-sync tables/claims/
      tablespec validation-sync tables/claims/ --dry-run
      tablespec validation-sync tables/claims/ --clean-outdated

    """
    from tablespec.sync_baseline import BaselineSyncer

    try:
        syncer = BaselineSyncer()
        result = syncer.sync_table(
            Path(table_path),
            dry_run=dry_run,
            aggressive=aggressive,
            clean_outdated=clean_outdated,
        )

        if result.validations_added:
            console.print(
                f"[green]Added:[/green] {result.validations_added} expectations"
            )
        if result.validations_upgraded:
            console.print(
                f"[blue]Upgraded:[/blue] {result.validations_upgraded} expectations"
            )
        if result.validations_conflicts:
            console.print(
                f"[yellow]Conflicts:[/yellow] {result.validations_conflicts} (preserved existing)"
            )
        if result.validations_severity_preserved:
            console.print(
                f"[dim]Severity preserved:[/dim] {result.validations_severity_preserved}"
            )
        if not any(
            [
                result.validations_added,
                result.validations_upgraded,
                result.validations_conflicts,
            ]
        ):
            console.print("[dim]Already in sync.[/dim]")
        if dry_run:
            console.print("\n[dim]Dry run — no changes written.[/dim]")

    except (FileNotFoundError, ValueError) as e:
        console.print(f"[red]Error:[/red] {e}")
        raise typer.Exit(1)


@app.command()
def validation_remove(
    table_path: str = typer.Argument(..., help="Path to UMF table (file or directory)"),
    expectation_type: str = typer.Option(
        ..., "--type", "-t", help="Expectation type to remove"
    ),
    column: str = typer.Option(
        None, "--column", "-c", help="Column name (removes all matching if omitted)"
    ),
) -> None:
    """Remove expectations matching a type and optional column.

    Searches the unified expectations suite.

    Examples:
      tablespec validation-remove tables/claims/ --type expect_column_values_to_match_regex --column ssn
      tablespec validation-remove table.yaml --type expect_column_values_to_be_unique

    """
    from tablespec.authoring.mutations import remove_expectation

    try:
        loader = UMFLoader()
        umf = loader.load(Path(table_path))
        updated, count = remove_expectation(umf, expectation_type, column)

        if count == 0:
            console.print("[yellow]No matching expectations found.[/yellow]")
            return

        dest = Path(table_path)
        fmt = UMFFormat.JSON if dest.suffix == ".json" else UMFFormat.SPLIT
        loader.save(updated, dest, format=fmt)
        target = f" on column '{column}'" if column else ""
        console.print(
            f"[green]Removed[/green] {count} expectation(s) of type '{expectation_type}'{target}"
        )

    except (ValueError, FileNotFoundError) as e:
        console.print(f"[red]Error:[/red] {e}")
        raise typer.Exit(1)


@app.command()
def explore(
    path: Path = typer.Argument(
        ...,
        help="Path to a UMF directory",
        exists=True,
    ),
) -> None:
    """Launch interactive TUI for UMF exploration and editing.

    Browse tables and columns in a tree view, search across all loaded UMFs,
    and edit column descriptions and data types inline.

    Requires the tui extra: pip install tablespec[tui]

    Examples:
      tablespec explore tables/

    """
    try:
        from tablespec.tui import UMFExplorer
    except ImportError:
        console.print(
            "[red]Error:[/red] TUI requires textual. "
            "Install with: [cyan]pip install tablespec[tui][/cyan]"
        )
        raise typer.Exit(1)

    explorer = UMFExplorer(path)
    explorer.run()


@app.command()
def preview(
    table_path: str = typer.Argument(..., help="Path to UMF table (directory or file)"),
    against: str = typer.Option(None, "--against", help="CSV file to validate against"),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Preview validation expectations classified by stage."""
    from tablespec.authoring.preview import generate_preview
    from tablespec.gx_baseline import BaselineExpectationGenerator

    loader = UMFLoader()
    umf = loader.load(Path(table_path))
    umf_data = umf.model_dump()

    # Also generate baseline expectations
    gen = BaselineExpectationGenerator()
    baseline = gen.generate_baseline_expectations(umf_data)

    # Merge baseline into a temporary copy for preview
    suite_data = dict(umf_data.get("expectations") or {})
    all_exps = list(suite_data.get("expectations", []))
    all_exps.extend(baseline)
    suite_data["expectations"] = all_exps
    preview_data = {**umf_data, "expectations": suite_data}

    result = generate_preview(preview_data)

    # Display with Rich
    table = RichTable(title=f"Validation Preview: {umf.table_name}")
    table.add_column("Stage", style="bold")
    table.add_column("Type")
    table.add_column("Column")
    table.add_column("Severity")
    table.add_column("Source")

    for exp in result.raw:
        table.add_row(
            "[green]RAW[/green]",
            exp.type,
            exp.column or "-",
            exp.severity,
            exp.generated_from or "-",
        )
    for exp in result.ingested:
        table.add_row(
            "[blue]INGESTED[/blue]",
            exp.type,
            exp.column or "-",
            exp.severity,
            exp.generated_from or "-",
        )
    for exp in result.redundant:
        table.add_row(
            "[dim]REDUNDANT[/dim]",
            exp.type,
            exp.column or "-",
            exp.severity,
            exp.generated_from or "-",
        )
    for exp in result.unknown:
        table.add_row(
            "[yellow]UNKNOWN[/yellow]",
            exp.type,
            exp.column or "-",
            exp.severity,
            exp.generated_from or "-",
        )

    console.print(table)
    console.print(
        f"\nTotal: {result.total} ({len(result.raw)} raw, {len(result.ingested)} ingested, "
        f"{len(result.redundant)} redundant, {len(result.unknown)} unknown)"
    )


@app.command()
def apply_response(
    table_path: str = typer.Argument(..., help="Path to UMF table (file or directory)"),
    response_path: str = typer.Argument(..., help="Path to LLM response JSON file"),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Show what would be applied without modifying the UMF"
    ),
) -> None:
    """Apply LLM-generated validation expectations to a UMF table."""
    import json

    from tablespec.authoring.apply_response import apply_validation_response

    try:
        source = Path(table_path)
        resp_file = Path(response_path)

        if not source.exists():
            console.print(f"[red]Error:[/red] UMF path not found: {source}")
            raise typer.Exit(1)
        if not resp_file.exists():
            console.print(f"[red]Error:[/red] Response file not found: {resp_file}")
            raise typer.Exit(1)

        loader = UMFLoader()
        umf = loader.load(source)

        with resp_file.open(encoding="utf-8") as f:
            raw = json.load(f)

        if isinstance(raw, list):
            response = raw
        elif isinstance(raw, dict) and "expectations" in raw:
            response = raw["expectations"]
        else:
            console.print(
                "[red]Error:[/red] Response JSON must be a list or {expectations: [...]}."
            )
            raise typer.Exit(1)

        result = apply_validation_response(umf, response)

        if result.updated_umf is not None and not dry_run:
            fmt = UMFFormat.JSON if source.suffix == ".json" else UMFFormat.SPLIT
            loader.save(result.updated_umf, source, format=fmt)

        if result.added:
            console.print(f"[green]Added:[/green] {len(result.added)} expectations")
            for exp in result.added:
                stage = exp.get("meta", {}).get("validation_stage", "?")
                col = exp.get("kwargs", {}).get("column", "(table-level)")
                console.print(f"  [{stage}] {exp.get('type', '?')} on {col}")
        if result.deduplicated:
            console.print(
                f"[yellow]Deduplicated:[/yellow] {len(result.deduplicated)} (already exist)"
            )
        if result.invalid:
            console.print(f"[red]Invalid:[/red] {len(result.invalid)} rejected")
            for exp, reason in result.invalid:
                console.print(f"  {reason}")
        if not result.added and not result.deduplicated and not result.invalid:
            console.print("[dim]No expectations in response.[/dim]")
        if dry_run:
            console.print("\n[dim]Dry run - no changes written.[/dim]")

    except json.JSONDecodeError as e:
        console.print(f"[red]Error:[/red] Invalid JSON: {e}")
        raise typer.Exit(1)
    except Exception as e:
        console.print(f"[red]Error:[/red] {e}")
        raise typer.Exit(1)


@app.command()
def guidebook(
    root: Path = typer.Argument(
        ...,
        help="Directory of UMFs to render (split table.yaml dirs and/or *.umf.json).",
    ),
    output: Path = typer.Option(
        Path("guidebook"),
        "--output",
        "-o",
        help="Output directory for generated HTML.",
    ),
    group: str | None = typer.Option(
        None,
        "--group",
        "-g",
        help="Only render UMFs in this group (subfolder). Default: all groups.",
    ),
) -> None:
    """Render a directory of UMFs into a navigable HTML guidebook."""
    from tablespec.guidebook import generate as _generate

    if not root.exists():
        console.print(f"[red]Error:[/red] Root path not found: {root}")
        raise typer.Exit(1)

    written = _generate(root=root, output_dir=output, group=group)
    if not written:
        console.print("[yellow]Warning:[/yellow] No UMFs found to render.")
        return

    console.print(f"[green]Wrote {len(written)} file(s) to {output}[/green]")
    for path in written[:10]:
        console.print(f"  {path}")
    if len(written) > 10:
        console.print(f"  ... and {len(written) - 10} more")


@app.callback(invoke_without_command=True)
def version_callback(ctx: typer.Context) -> None:
    """Show version info or help."""
    if ctx.invoked_subcommand is None:
        if ctx.params.get("version"):
            from tablespec import __version__

            console.print(f"tablespec version {__version__}")
        else:
            console.print(ctx.get_help())
        raise typer.Exit


if __name__ == "__main__":
    app()
