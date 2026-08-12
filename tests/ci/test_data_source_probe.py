"""Tests for structured external data source availability probes."""

import asyncio
from datetime import date

import httpx
from pydantic import HttpUrl

from scripts.check_data_sources import DataSourceProbeResult, probe_data_source, summarize_probe_results
from scripts.data_source_catalog import (
	DataSourceAccess,
	DataSourceCatalog,
	DataSourceCategory,
	DataSourceDefinition,
	DataSourceTestLevel,
)


def make_source(source_id: str, test_level: DataSourceTestLevel, url: str) -> DataSourceDefinition:
	"""Build a minimal validated source for probe unit tests."""
	return DataSourceDefinition(
		id=source_id,
		name=source_id,
		category=DataSourceCategory.SOCIAL_MEDIA,
		url=HttpUrl(url),
		access=DataSourceAccess.PUBLIC_STATIC,
		test_level=test_level,
		expected_http_statuses=[200],
		description='Probe test source.',
	)


async def test_probe_data_source_reports_expected_and_unexpected_statuses() -> None:
	"""Classify response codes against each source's declared contract."""
	response_status = 200

	def handle_request(request: httpx.Request) -> httpx.Response:
		return httpx.Response(response_status, request=request)

	source = make_source('behavioral_source', DataSourceTestLevel.BEHAVIORAL, 'https://example.com/')
	transport = httpx.MockTransport(handle_request)
	async with httpx.AsyncClient(transport=transport) as client:
		passed_result = await probe_data_source(source, client, asyncio.Semaphore(1))
		response_status = 503
		failed_result = await probe_data_source(source, client, asyncio.Semaphore(1))

	assert passed_result.ok is True
	assert passed_result.status_code == 200
	assert failed_result.ok is False
	assert failed_result.status_code == 503


def test_probe_summary_gates_behavioral_sources_by_default() -> None:
	"""Keep informational availability failures out of the default quality gate."""
	behavioral_source = make_source('behavioral_source', DataSourceTestLevel.BEHAVIORAL, 'https://behavioral.example.com/')
	availability_source = make_source(
		'availability_source', DataSourceTestLevel.AVAILABILITY, 'https://availability.example.com/'
	)
	catalog = DataSourceCatalog(
		version=1,
		last_reviewed=date(2026, 8, 12),
		sources=[behavioral_source, availability_source],
	)
	results = [
		DataSourceProbeResult(
			source_id=source.id,
			category=source.category,
			test_level=source.test_level,
			ok=False,
			status_code=503,
			elapsed_ms=1,
		)
		for source in catalog.sources
	]

	default_summary = summarize_probe_results(catalog, results, strict=False)
	strict_summary = summarize_probe_results(catalog, results, strict=True)

	assert default_summary.failed == 2
	assert default_summary.gate_failures == 1
	assert strict_summary.gate_failures == 2
