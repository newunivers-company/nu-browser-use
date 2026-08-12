"""Probe every catalogued browser evaluation source over HTTP."""

import argparse
import asyncio
import json
import time
from pathlib import Path

import httpx
from pydantic import BaseModel, ConfigDict, Field

from scripts.data_source_catalog import (
	DEFAULT_DATA_SOURCE_CATALOG_PATH,
	DataSourceCatalog,
	DataSourceCategory,
	DataSourceDefinition,
	DataSourceTestLevel,
	load_data_source_catalog,
)


class ProbeOptions(BaseModel):
	"""Validated command inputs for a catalog probe run."""

	model_config = ConfigDict(extra='forbid')

	catalog_path: Path
	categories: set[DataSourceCategory] = Field(default_factory=set)
	timeout_seconds: float = Field(default=15.0, gt=0, le=120)
	concurrency: int = Field(default=10, ge=1, le=50)
	strict: bool = False
	json_output: bool = False


class DataSourceProbeResult(BaseModel):
	"""Structured HTTP availability result for one data source."""

	source_id: str
	category: DataSourceCategory
	test_level: DataSourceTestLevel
	ok: bool
	status_code: int | None = None
	final_url: str | None = None
	elapsed_ms: int = Field(ge=0)
	error: str | None = None


class DataSourceProbeSummary(BaseModel):
	"""Aggregate result and gate counts for a complete probe run."""

	total: int = Field(ge=0)
	passed: int = Field(ge=0)
	failed: int = Field(ge=0)
	gate_failures: int = Field(ge=0)
	results: list[DataSourceProbeResult]


def summarize_probe_results(
	catalog: DataSourceCatalog,
	results: list[DataSourceProbeResult],
	*,
	strict: bool,
) -> DataSourceProbeSummary:
	"""Calculate pass and quality-gate counts for probe results."""
	failed_results = [result for result in results if not result.ok]
	gate_failures = [
		result
		for result in failed_results
		if strict or catalog.by_id[result.source_id].test_level == DataSourceTestLevel.BEHAVIORAL
	]
	return DataSourceProbeSummary(
		total=len(results),
		passed=len(results) - len(failed_results),
		failed=len(failed_results),
		gate_failures=len(gate_failures),
		results=results,
	)


async def probe_data_source(
	source: DataSourceDefinition,
	client: httpx.AsyncClient,
	semaphore: asyncio.Semaphore,
) -> DataSourceProbeResult:
	"""Fetch one source through a bounded shared HTTP client."""
	started_at = time.monotonic()
	async with semaphore:
		try:
			async with client.stream('GET', str(source.url)) as response:
				status_code = response.status_code
				final_url = str(response.url)
			return DataSourceProbeResult(
				source_id=source.id,
				category=source.category,
				test_level=source.test_level,
				ok=status_code in source.expected_http_statuses,
				status_code=status_code,
				final_url=final_url,
				elapsed_ms=round((time.monotonic() - started_at) * 1000),
			)
		except Exception as error:
			return DataSourceProbeResult(
				source_id=source.id,
				category=source.category,
				test_level=source.test_level,
				ok=False,
				elapsed_ms=round((time.monotonic() - started_at) * 1000),
				error=f'{type(error).__name__}: {error}',
			)


async def probe_catalog(options: ProbeOptions) -> DataSourceProbeSummary:
	"""Load, filter, and probe the configured catalog."""
	catalog = load_data_source_catalog(options.catalog_path)
	sources = [source for source in catalog.sources if not options.categories or source.category in options.categories]
	timeout = httpx.Timeout(options.timeout_seconds, connect=min(options.timeout_seconds, 10.0))
	limits = httpx.Limits(
		max_connections=options.concurrency,
		max_keepalive_connections=max(1, options.concurrency // 2),
	)
	headers = {'User-Agent': 'Mozilla/5.0 (compatible; nu-browser-use-data-source-check/1.0)'}
	semaphore = asyncio.Semaphore(options.concurrency)
	async with httpx.AsyncClient(
		follow_redirects=True,
		timeout=timeout,
		limits=limits,
		headers=headers,
	) as client:
		results = await asyncio.gather(*(probe_data_source(source, client, semaphore) for source in sources))

	return summarize_probe_results(catalog, results, strict=options.strict)


def print_probe_summary(summary: DataSourceProbeSummary) -> None:
	"""Print a compact human-readable probe table."""
	print(f'Checked {summary.total} data sources: {summary.passed} passed, {summary.failed} failed')
	for result in summary.results:
		status = 'PASS' if result.ok else 'FAIL'
		response = str(result.status_code) if result.status_code is not None else result.error or 'unknown error'
		print(
			f'{status:4}  {result.category.value:14}  {result.test_level.value:12}  '
			f'{result.source_id:28}  {response}  {result.elapsed_ms}ms'
		)
	if summary.gate_failures:
		print(f'Quality gate failures: {summary.gate_failures}')


def parse_options() -> ProbeOptions:
	"""Parse CLI arguments and return a validated options model."""
	parser = argparse.ArgumentParser(description=__doc__)
	parser.add_argument('--catalog', type=Path, default=DEFAULT_DATA_SOURCE_CATALOG_PATH)
	parser.add_argument(
		'--category',
		action='append',
		choices=[category.value for category in DataSourceCategory],
		default=[],
		help='Limit probes to one or more categories.',
	)
	parser.add_argument('--timeout-seconds', type=float, default=15.0)
	parser.add_argument('--concurrency', type=int, default=10)
	parser.add_argument('--strict', action='store_true', help='Gate on availability-only sources too.')
	parser.add_argument('--json', action='store_true', dest='json_output')
	arguments = parser.parse_args()
	return ProbeOptions(
		catalog_path=arguments.catalog,
		categories={DataSourceCategory(category) for category in arguments.category},
		timeout_seconds=arguments.timeout_seconds,
		concurrency=arguments.concurrency,
		strict=arguments.strict,
		json_output=arguments.json_output,
	)


def main() -> int:
	"""Run the HTTP probe command and return its quality-gate exit code."""
	options = parse_options()
	summary = asyncio.run(probe_catalog(options))
	if options.json_output:
		print(json.dumps(summary.model_dump(mode='json'), ensure_ascii=False, indent=2))
	else:
		print_probe_summary(summary)
	return 1 if summary.gate_failures else 0


if __name__ == '__main__':
	raise SystemExit(main())
