"""Pure Python utilities for working with table schemas in UMF format."""

from tablespec.gx_baseline import BaselineExpectationGenerator, UmfToGxMapper
from tablespec.gx_constraint_extractor import GXConstraintExtractor
from tablespec.models import (
    UMF,
    Cardinality,
    Expectation,
    ExpectationMeta,
    ExpectationSuite,
    DerivationCandidate,
    FileFormat,
    FileFormatSpec,
    FilenamePattern,
    ForeignKey,
    IncomingRelationship,
    Index,
    IngestionConfig,
    IngestionExclusionRule,
    JoinViaSpec,
    LookupJoin,
    MergeCondition,
    Nullable,
    OutgoingRelationship,
    OutputConfig,
    ScdConfig,
    PostUpsertRule,
    QualityCheck,
    QualityChecks,
    ReferencedBy,
    RelationshipSummary,
    Relationships,
    Survivorship,
    TableReference,
    UMFColumn,
    UMFColumnDerivation,
    UMFMetadata,
    ValidationRule,
    ValidationRules,
    classify_validation_type,
    load_umf_from_yaml,
    save_umf_to_yaml,
)
from tablespec.profiling import ColumnProfile, DataFrameProfile, ProfileToGxMapper
from tablespec.prompts import (
    generate_column_validation_prompt,
    generate_documentation_prompt,
    generate_filename_pattern_prompt,
    generate_relationship_prompt,
    generate_survivorship_prompt,
    generate_survivorship_prompt_per_column,
    generate_validation_prompt,
    generate_validation_prompt_per_column,
    has_validation_rules,
    should_generate_column_prompt,
)
from tablespec.dbt import generate_dbt_dag_project, generate_dbt_project
from tablespec.schemas import (
    SQLPlanGenerator,
    build_ingest_select,
    generate_ingest_sql,
    generate_json_schema,
    generate_pyspark_schema,
    generate_sql_ddl,
    generate_sql_plan,
)
from tablespec.type_mappings import (
    VALID_PYSPARK_TYPES,
    map_pyspark_to_sql_type,
    map_to_gx_spark_type,
    map_to_json_type,
    map_to_pyspark_type,
)
from tablespec.validation import GXExpectationProcessor

from tablespec.changelog_generator import ChangelogGenerator
from tablespec.compatibility import (
    CompatibilityIssue,
    CompatibilityReport,
    check_compatibility,
)
from tablespec.bootstrap import bootstrap_from_specs, bootstrap_from_tables
from tablespec.guidebook import generate as generate_guidebook
from tablespec.excel_converter import ExcelToUMFConverter, UMFToExcelConverter
from tablespec.inference.domain_types import DomainTypeInference, DomainTypeRegistry
from tablespec.sample_data import GenerationConfig, SampleDataGenerator
from tablespec.umf_diff import UMFDiff
from tablespec.umf_loader import UMFFormat, UMFLoader

from importlib.metadata import version as _get_version

__version__ = _get_version("tablespec")

__all__ = [
    # -- Core UMF I/O & Models --
    "UMF",
    "UMFColumn",
    "UMFMetadata",
    "classify_validation_type",
    "load_umf_from_yaml",
    "save_umf_to_yaml",
    # -- Schema Generation --
    "SQLPlanGenerator",
    "build_ingest_select",
    "generate_dbt_dag_project",
    "generate_dbt_project",
    "generate_guidebook",
    "generate_ingest_sql",
    "generate_json_schema",
    "generate_pyspark_schema",
    "generate_sql_ddl",
    "generate_sql_plan",
    # -- Type Mappings --
    "VALID_PYSPARK_TYPES",
    "map_pyspark_to_sql_type",
    "map_to_gx_spark_type",
    "map_to_json_type",
    "map_to_pyspark_type",
    # -- Great Expectations Integration --
    "BaselineExpectationGenerator",
    "GXConstraintExtractor",
    "GXExpectationProcessor",
    "UmfToGxMapper",
    # -- Profiling --
    "ColumnProfile",
    "DataFrameProfile",
    "ProfileToGxMapper",
    # -- LLM Prompt Generation --
    "generate_column_validation_prompt",
    "generate_documentation_prompt",
    "generate_filename_pattern_prompt",
    "generate_relationship_prompt",
    "generate_survivorship_prompt",
    "generate_survivorship_prompt_per_column",
    "generate_validation_prompt",
    "generate_validation_prompt_per_column",
    "has_validation_rules",
    "should_generate_column_prompt",
    # -- Supporting Model Classes --
    "Cardinality",
    "Expectation",
    "ExpectationMeta",
    "ExpectationSuite",
    "DerivationCandidate",
    "FileFormat",
    "FileFormatSpec",
    "FilenamePattern",
    "ForeignKey",
    "IncomingRelationship",
    "Index",
    "IngestionConfig",
    "IngestionExclusionRule",
    "JoinViaSpec",
    "LookupJoin",
    "MergeCondition",
    "Nullable",
    "OutgoingRelationship",
    "OutputConfig",
    "ScdConfig",
    "PostUpsertRule",
    "QualityCheck",
    "QualityChecks",
    "ReferencedBy",
    "RelationshipSummary",
    "Relationships",
    "Survivorship",
    "TableReference",
    "UMFColumnDerivation",
    "ValidationRule",
    "ValidationRules",
    # -- Excel Conversion --
    "UMFToExcelConverter",
    "ExcelToUMFConverter",
    # -- Split-Format UMF --
    "UMFLoader",
    "UMFFormat",
    # -- Sample Data --
    "SampleDataGenerator",
    "GenerationConfig",
    # -- Domain Inference --
    "DomainTypeInference",
    "DomainTypeRegistry",
    # -- Change Management --
    "UMFDiff",
    "ChangelogGenerator",
    # -- Compatibility Checking --
    "check_compatibility",
    "CompatibilityReport",
    "CompatibilityIssue",
    "bootstrap_from_tables",
    "bootstrap_from_specs",
]

# SparkToUmfMapper and TableValidator are available only if pyspark is installed (via tablespec[spark])
try:
    from tablespec.profiling import JdbcToUmfMapper, SparkToUmfMapper  # noqa: F401
    from tablespec.type_mappings import map_to_pyspark_type_obj  # noqa: F401
    from tablespec.validation import VALIDATION_ERROR_SCHEMA, TableValidator  # noqa: F401

    __all__.extend(
        [
            "VALIDATION_ERROR_SCHEMA",
            "JdbcToUmfMapper",
            "SparkToUmfMapper",
            "TableValidator",
            "map_to_pyspark_type_obj",
        ]
    )
except ImportError:
    # pyspark not available - Spark-dependent classes won't be exported
    pass

try:
    from tablespec.spark_factory import SparkSessionFactory, create_delta_spark_session  # noqa: F401

    __all__.extend(["SparkSessionFactory", "create_delta_spark_session"])
except ImportError:
    # pyspark not available - Spark factory won't be exported
    pass
