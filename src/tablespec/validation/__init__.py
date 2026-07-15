"""Great Expectations validation utilities."""

from .dbt_results import DbtResultsError, dbt_validation_report, parse_dbt_run_results
from .gx_processor import GXExpectationProcessor
from .html_report import render_validation_report_html, write_validation_report
from .report import ValidationReport

# Define __all__ at module level for type checkers
__all__ = [
    "VALIDATION_ERROR_SCHEMA",
    "VALIDATION_RESULT_SCHEMA",
    "DbtResultsError",
    "ExpectColumnValuesToCastToType",
    "ExpectColumnValuesToMatchDomainType",
    "GXExpectationProcessor",
    "GXTableValidator",
    "TableValidator",
    "ValidationBlockingError",
    "ValidationDeltaWriter",
    "ValidationReport",
    "ValidationResult",
    "build_validation_report_from_staged_execution",
    "dbt_validation_report",
    "parse_dbt_run_results",
    "render_validation_report_html",
    "write_validation_report",
    "write_validation_results",
]

# TableValidator and GXTableValidator require pyspark - only available with tablespec[spark]
try:
    from .table_validator import VALIDATION_ERROR_SCHEMA, TableValidator
except ImportError:
    # pyspark not available - symbols won't be accessible at runtime but type checkers can see __all__
    pass

# Optional modules that may not be ported yet
try:
    from .custom_gx_expectations import ExpectColumnValuesToCastToType
    from .custom_gx_expectations import ExpectColumnValuesToMatchDomainType
except (ImportError, ValueError):
    pass

try:
    from .delta_writer import (
        VALIDATION_RESULT_SCHEMA,
        ValidationDeltaWriter,
        write_validation_results,
    )
except (ImportError, ValueError):
    pass

try:
    from .gx_table_validator import (
        GXTableValidator,
        ValidationBlockingError,
        ValidationResult,
    )
except (ImportError, ValueError):
    pass

try:
    from .staged_report import build_validation_report_from_staged_execution
except (ImportError, ValueError):
    pass
