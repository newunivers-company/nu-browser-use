"""Validated models and loader for browser evaluation data sources."""

from datetime import date
from enum import StrEnum
from pathlib import Path
from typing import Self

import yaml
from pydantic import BaseModel, ConfigDict, Field, HttpUrl, model_validator

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATA_SOURCE_CATALOG_PATH = PROJECT_ROOT / 'tests' / 'data_sources.yaml'


class DataSourceCategory(StrEnum):
	"""Supported high-level data source categories."""

	COMMERCE = 'commerce'
	DOCUMENTATION = 'documentation'
	PROMPT_LIBRARY = 'prompt_library'
	SOCIAL_MEDIA = 'social_media'
	TEST_SITE = 'test_site'


class DataSourceAccess(StrEnum):
	"""Browser access characteristics relevant to test reliability."""

	PUBLIC_STATIC = 'public_static'
	PUBLIC_JAVASCRIPT = 'public_javascript'
	LOGIN_REQUIRED = 'login_required'
	ANTI_BOT_PROTECTED = 'anti_bot_protected'


class DataSourceTestLevel(StrEnum):
	"""Depth of automated coverage assigned to a source."""

	BEHAVIORAL = 'behavioral'
	AVAILABILITY = 'availability'


class BrowserDataSourceContract(BaseModel):
	"""Semantic browser evidence required to accept a rendered source."""

	model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)

	require_same_origin: bool = True
	allowed_final_path_prefixes: list[str] = Field(default_factory=list)
	expected_title_markers: list[str] = Field(default_factory=list)
	required_content_markers: list[str] = Field(default_factory=list)
	minimum_visible_text_chars: int = Field(default=20, ge=0, le=100_000)
	minimum_meaningful_elements: int = Field(default=1, ge=0, le=10_000)
	minimum_interactive_elements: int = Field(default=1, ge=0, le=10_000)

	@model_validator(mode='after')
	def validate_browser_contract(self) -> Self:
		"""Reject ambiguous paths and duplicate case-insensitive evidence markers."""
		if any(not path.startswith('/') for path in self.allowed_final_path_prefixes):
			raise ValueError('allowed_final_path_prefixes entries must start with /')
		for field_name in ('allowed_final_path_prefixes', 'expected_title_markers', 'required_content_markers'):
			values = getattr(self, field_name)
			casefolded_values = [value.casefold() for value in values]
			if any(not value for value in values):
				raise ValueError(f'{field_name} entries must not be empty')
			if len(casefolded_values) != len(set(casefolded_values)):
				raise ValueError(f'{field_name} entries must be unique')
		return self


class DataSourceDefinition(BaseModel):
	"""One externally hosted source used by browser evaluation tests."""

	model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)

	id: str = Field(pattern=r'^[a-z][a-z0-9_]*$')
	name: str = Field(min_length=1)
	category: DataSourceCategory
	url: HttpUrl
	access: DataSourceAccess
	test_level: DataSourceTestLevel
	expected_http_statuses: list[int] = Field(min_length=1)
	description: str = Field(min_length=1)
	browser_contract: BrowserDataSourceContract = Field(default_factory=BrowserDataSourceContract)

	@model_validator(mode='after')
	def validate_expected_http_statuses(self) -> Self:
		"""Reject duplicate or invalid expected response codes."""
		if len(self.expected_http_statuses) != len(set(self.expected_http_statuses)):
			raise ValueError('expected_http_statuses must be unique')
		if any(status < 100 or status > 599 for status in self.expected_http_statuses):
			raise ValueError('expected_http_statuses must contain valid HTTP status codes')
		return self


class DataSourceCatalog(BaseModel):
	"""Versioned collection of unique browser evaluation data sources."""

	model_config = ConfigDict(extra='forbid')

	version: int = Field(ge=1)
	last_reviewed: date
	sources: list[DataSourceDefinition] = Field(min_length=1)

	@model_validator(mode='after')
	def validate_unique_sources(self) -> Self:
		"""Reject duplicate source identifiers and URLs."""
		source_ids = [source.id for source in self.sources]
		if len(source_ids) != len(set(source_ids)):
			raise ValueError('data source IDs must be unique')

		source_urls = [str(source.url) for source in self.sources]
		if len(source_urls) != len(set(source_urls)):
			raise ValueError('data source URLs must be unique')
		return self

	@property
	def by_id(self) -> dict[str, DataSourceDefinition]:
		"""Return definitions keyed by their stable source identifiers."""
		return {source.id: source for source in self.sources}


def load_data_source_catalog(path: Path = DEFAULT_DATA_SOURCE_CATALOG_PATH) -> DataSourceCatalog:
	"""Load and validate the YAML data source catalog at ``path``."""
	return DataSourceCatalog.model_validate(yaml.safe_load(path.read_text(encoding='utf-8')))
