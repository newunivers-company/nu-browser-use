"""Run a bounded, read-only crawl experiment across catalogued data sources."""

import argparse
import asyncio
import hashlib
import re
import time
from collections import Counter, deque
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from enum import StrEnum
from html.parser import HTMLParser
from pathlib import Path
from typing import AsyncIterator
from urllib.parse import urldefrag, urljoin, urlsplit, urlunsplit
from urllib.robotparser import RobotFileParser

import httpx
from pydantic import BaseModel, ConfigDict, Field

from scripts.data_source_catalog import (
	DEFAULT_DATA_SOURCE_CATALOG_PATH,
	DataSourceAccess,
	DataSourceCategory,
	DataSourceDefinition,
	DataSourceTestLevel,
	load_data_source_catalog,
)

CRAWLER_USER_AGENT = 'nu-browser-use-crawl-experiment/1.0'
BROWSER_USER_AGENT = f'Mozilla/5.0 (compatible; {CRAWLER_USER_AGENT}; +https://github.com/browser-use/browser-use)'
SKIPPED_ELEMENT_NAMES = {'script', 'style', 'noscript', 'svg', 'template'}
SKIPPED_PATH_PARTS = {
	'auth',
	'cdn-cgi',
	'delete',
	'destroy',
	'login',
	'logout',
	'register',
	'remove',
	'sign-in',
	'signin',
	'signout',
	'signup',
	'unsubscribe',
}
GLOBAL_NAVIGATION_PATH_PARTS = {
	'about',
	'contact',
	'enterprise',
	'features',
	'legal',
	'pricing',
	'privacy',
	'settings',
	'solutions',
	'terms',
}


class CrawlClassification(StrEnum):
	"""Observed page behavior relevant to browser crawling."""

	CONTENT_RICH = 'content_rich'
	SPARSE_HTML = 'sparse_html'
	JAVASCRIPT_SHELL = 'javascript_shell'
	LOGIN_REDIRECT = 'login_redirect'
	BLOCKED = 'blocked'
	NON_HTML = 'non_html'
	HTTP_ERROR = 'http_error'
	ROBOTS_DENIED = 'robots_denied'
	FETCH_ERROR = 'fetch_error'


class CrawlOptions(BaseModel):
	"""Validated controls for a bounded crawl experiment."""

	model_config = ConfigDict(extra='forbid')

	catalog_path: Path = DEFAULT_DATA_SOURCE_CATALOG_PATH
	source_ids: set[str] = Field(default_factory=set)
	categories: set[DataSourceCategory] = Field(default_factory=set)
	test_levels: set[DataSourceTestLevel] = Field(default_factory=set)
	max_pages_per_source: int = Field(default=4, ge=1, le=20)
	max_depth: int = Field(default=1, ge=0, le=3)
	concurrency: int = Field(default=8, ge=1, le=30)
	request_timeout_seconds: float = Field(default=20.0, gt=0, le=120)
	per_host_delay_seconds: float = Field(default=0.35, ge=0, le=10)
	max_content_bytes: int = Field(default=2_000_000, ge=10_000, le=10_000_000)
	respect_robots: bool = True
	output_path: Path | None = None


class HtmlDocumentMetrics(BaseModel):
	"""Content and link measurements extracted from an HTML document."""

	title: str | None = None
	text_chars: int = Field(ge=0)
	total_links: int = Field(ge=0)
	same_origin_links: list[str]
	external_link_count: int = Field(ge=0)
	script_count: int = Field(ge=0)
	form_count: int = Field(ge=0)


class RobotsResult(BaseModel):
	"""robots.txt decision and retrieval evidence for one source."""

	url: str
	status_code: int | None = None
	allowed: bool
	error: str | None = None


class CrawlPageResult(BaseModel):
	"""Structured observation for one requested crawl page."""

	requested_url: str
	final_url: str | None = None
	depth: int = Field(ge=0)
	status_code: int | None = None
	content_type: str | None = None
	classification: CrawlClassification
	title: str | None = None
	content_bytes: int = Field(default=0, ge=0)
	content_sha256: str | None = None
	text_chars: int = Field(default=0, ge=0)
	total_links: int = Field(default=0, ge=0)
	same_origin_link_count: int = Field(default=0, ge=0)
	external_link_count: int = Field(default=0, ge=0)
	script_count: int = Field(default=0, ge=0)
	form_count: int = Field(default=0, ge=0)
	redirect_count: int = Field(default=0, ge=0)
	elapsed_ms: int = Field(ge=0)
	error: str | None = None
	discovered_same_origin_urls: list[str] = Field(default_factory=list)


class CrawlSourceResult(BaseModel):
	"""All crawl observations collected for one catalogued source."""

	source_id: str
	name: str
	category: DataSourceCategory
	access: DataSourceAccess
	test_level: DataSourceTestLevel
	start_url: str
	robots: RobotsResult
	pages: list[CrawlPageResult]


class CrawlSummary(BaseModel):
	"""Aggregate measurements for a complete crawl experiment."""

	total_sources: int = Field(ge=0)
	sources_with_fetched_pages: int = Field(ge=0)
	total_pages: int = Field(ge=0)
	classifications: dict[CrawlClassification, int]
	total_content_bytes: int = Field(ge=0)
	total_text_chars: int = Field(ge=0)
	unique_content_hashes: int = Field(ge=0)
	robots_denied_sources: int = Field(ge=0)


class CrawlExperimentResult(BaseModel):
	"""Serializable output of a complete crawl experiment."""

	generated_at: datetime
	options: CrawlOptions
	summary: CrawlSummary
	sources: list[CrawlSourceResult]


class _HtmlMetricsParser(HTMLParser):
	"""Small dependency-free parser for crawl-relevant HTML metrics."""

	def __init__(self) -> None:
		super().__init__(convert_charrefs=True)
		self.title_parts: list[str] = []
		self.text_parts: list[str] = []
		self.links: list[str] = []
		self.script_count = 0
		self.form_count = 0
		self._skip_depth = 0
		self._inside_title = False

	def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
		"""Track visible-text state, links, scripts, and forms."""
		normalized_tag = tag.lower()
		if normalized_tag in SKIPPED_ELEMENT_NAMES:
			self._skip_depth += 1
		if normalized_tag == 'title':
			self._inside_title = True
		if normalized_tag == 'script':
			self.script_count += 1
		elif normalized_tag == 'form':
			self.form_count += 1
		elif normalized_tag == 'a':
			attributes = dict(attrs)
			href = attributes.get('href')
			if href:
				self.links.append(href)

	def handle_endtag(self, tag: str) -> None:
		"""Restore visible-text state after skipped elements."""
		normalized_tag = tag.lower()
		if normalized_tag == 'title':
			self._inside_title = False
		if normalized_tag in SKIPPED_ELEMENT_NAMES and self._skip_depth:
			self._skip_depth -= 1

	def handle_data(self, data: str) -> None:
		"""Collect normalized visible text and document title text."""
		cleaned_data = ' '.join(data.split())
		if not cleaned_data:
			return
		if self._inside_title:
			self.title_parts.append(cleaned_data)
		if self._skip_depth == 0:
			self.text_parts.append(cleaned_data)


class HostRateLimiter:
	"""Serialize and delay requests to the same host."""

	def __init__(self, delay_seconds: float) -> None:
		self.delay_seconds = delay_seconds
		self._locks: dict[str, asyncio.Lock] = {}
		self._last_finished_at: dict[str, float] = {}

	@asynccontextmanager
	async def limit(self, url: str) -> AsyncIterator[None]:
		"""Hold a host-specific lock for one delayed request."""
		host = urlsplit(url).netloc.lower()
		lock = self._locks.setdefault(host, asyncio.Lock())
		async with lock:
			elapsed = time.monotonic() - self._last_finished_at.get(host, 0.0)
			if elapsed < self.delay_seconds:
				await asyncio.sleep(self.delay_seconds - elapsed)
			try:
				yield
			finally:
				self._last_finished_at[host] = time.monotonic()


def normalize_crawl_url(url: str) -> str | None:
	"""Normalize a safe HTTP(S) crawl URL and remove its fragment."""
	url_without_fragment = urldefrag(url).url
	parsed = urlsplit(url_without_fragment)
	if parsed.scheme not in {'http', 'https'} or not parsed.netloc:
		return None
	if any(part.lower() in SKIPPED_PATH_PARTS for part in parsed.path.split('/') if part):
		return None
	return urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), parsed.path or '/', parsed.query, ''))


def is_same_origin(url: str, origin_url: str) -> bool:
	"""Return whether two URLs use the same scheme and network location."""
	parsed_url = urlsplit(url)
	parsed_origin = urlsplit(origin_url)
	return (parsed_url.scheme.lower(), parsed_url.netloc.lower()) == (
		parsed_origin.scheme.lower(),
		parsed_origin.netloc.lower(),
	)


def rank_discovered_urls(current_page_url: str, urls: list[str]) -> list[str]:
	"""Prefer links near the current content path over global navigation."""
	current_path = urlsplit(current_page_url).path.rstrip('/')

	def priority(url: str) -> tuple[int, int, str]:
		parsed = urlsplit(url)
		path_parts = [part.lower() for part in parsed.path.split('/') if part]
		in_current_scope = bool(current_path and parsed.path.startswith(f'{current_path}/'))
		global_navigation = bool(path_parts and path_parts[0] in GLOBAL_NAVIGATION_PATH_PARTS)
		return (
			0 if in_current_scope else 1 if not global_navigation else 2,
			-len(path_parts),
			url,
		)

	return sorted(urls, key=priority)


def extract_html_metrics(content: bytes, page_url: str) -> HtmlDocumentMetrics:
	"""Extract visible text and safe links from an HTML response."""
	parser = _HtmlMetricsParser()
	parser.feed(content.decode('utf-8', errors='replace'))
	parser.close()

	normalized_links: list[str] = []
	external_link_count = 0
	seen_links: set[str] = set()
	for href in parser.links:
		normalized_url = normalize_crawl_url(urljoin(page_url, href))
		if normalized_url is None or normalized_url in seen_links:
			continue
		seen_links.add(normalized_url)
		if is_same_origin(normalized_url, page_url):
			normalized_links.append(normalized_url)
		else:
			external_link_count += 1

	title = ' '.join(parser.title_parts).strip() or None
	visible_text = ' '.join(parser.text_parts)
	return HtmlDocumentMetrics(
		title=title,
		text_chars=len(visible_text),
		total_links=len(seen_links),
		same_origin_links=normalized_links,
		external_link_count=external_link_count,
		script_count=parser.script_count,
		form_count=parser.form_count,
	)


def classify_page(
	*,
	status_code: int,
	content_type: str,
	final_url: str,
	metrics: HtmlDocumentMetrics | None,
) -> CrawlClassification:
	"""Classify a fetched page using response and extracted-content evidence."""
	if status_code in {401, 403, 429, 503}:
		return CrawlClassification.BLOCKED
	if status_code >= 400:
		return CrawlClassification.HTTP_ERROR
	final_path = urlsplit(final_url).path.lower()
	if re.search(r'/(login|signin|sign-in|auth)(/|$)', final_path):
		return CrawlClassification.LOGIN_REDIRECT
	if 'html' not in content_type.lower() or metrics is None:
		return CrawlClassification.NON_HTML
	if metrics.text_chars < 200 and metrics.script_count >= 3:
		return CrawlClassification.JAVASCRIPT_SHELL
	if metrics.text_chars >= 1_000 or (metrics.text_chars >= 400 and metrics.total_links >= 5):
		return CrawlClassification.CONTENT_RICH
	return CrawlClassification.SPARSE_HTML


async def read_limited_response(response: httpx.Response, maximum_bytes: int) -> bytes:
	"""Read a streaming response without exceeding the configured byte ceiling."""
	content = bytearray()
	async for chunk in response.aiter_bytes():
		remaining = maximum_bytes - len(content)
		if remaining <= 0:
			break
		content.extend(chunk[:remaining])
	return bytes(content)


async def fetch_robots_policy(
	source_url: str,
	client: httpx.AsyncClient,
	rate_limiter: HostRateLimiter,
	maximum_bytes: int,
) -> RobotsResult:
	"""Fetch and evaluate robots.txt for a source start URL."""
	parsed_url = urlsplit(source_url)
	robots_url = urlunsplit((parsed_url.scheme, parsed_url.netloc, '/robots.txt', '', ''))
	try:
		async with rate_limiter.limit(robots_url):
			async with client.stream('GET', robots_url) as response:
				content = await read_limited_response(response, min(maximum_bytes, 500_000))
		status_code = response.status_code
		if status_code == 200:
			parser = RobotFileParser()
			parser.set_url(robots_url)
			parser.parse(content.decode('utf-8', errors='replace').splitlines())
			return RobotsResult(
				url=robots_url,
				status_code=status_code,
				allowed=parser.can_fetch(CRAWLER_USER_AGENT, source_url),
			)
		if status_code == 429 or status_code >= 500:
			return RobotsResult(
				url=robots_url,
				status_code=status_code,
				allowed=False,
				error='robots.txt was temporarily unavailable; crawl skipped conservatively',
			)
		return RobotsResult(url=robots_url, status_code=status_code, allowed=True)
	except Exception as error:
		return RobotsResult(
			url=robots_url,
			allowed=False,
			error=f'{type(error).__name__}: {error}',
		)


async def fetch_crawl_page(
	url: str,
	depth: int,
	client: httpx.AsyncClient,
	rate_limiter: HostRateLimiter,
	maximum_bytes: int,
) -> CrawlPageResult:
	"""Fetch and analyze one crawl page."""
	started_at = time.monotonic()
	try:
		async with rate_limiter.limit(url):
			async with client.stream('GET', url) as response:
				content = await read_limited_response(response, maximum_bytes)
		status_code = response.status_code
		final_url = str(response.url)
		content_type = response.headers.get('content-type', '').split(';', maxsplit=1)[0].strip().lower()
		metrics = extract_html_metrics(content, final_url) if 'html' in content_type else None
		classification = classify_page(
			status_code=status_code,
			content_type=content_type,
			final_url=final_url,
			metrics=metrics,
		)
		return CrawlPageResult(
			requested_url=url,
			final_url=final_url,
			depth=depth,
			status_code=status_code,
			content_type=content_type or None,
			classification=classification,
			title=metrics.title if metrics else None,
			content_bytes=len(content),
			content_sha256=hashlib.sha256(content).hexdigest() if content else None,
			text_chars=metrics.text_chars if metrics else 0,
			total_links=metrics.total_links if metrics else 0,
			same_origin_link_count=len(metrics.same_origin_links) if metrics else 0,
			external_link_count=metrics.external_link_count if metrics else 0,
			script_count=metrics.script_count if metrics else 0,
			form_count=metrics.form_count if metrics else 0,
			redirect_count=len(response.history),
			elapsed_ms=round((time.monotonic() - started_at) * 1000),
			discovered_same_origin_urls=metrics.same_origin_links if metrics else [],
		)
	except Exception as error:
		return CrawlPageResult(
			requested_url=url,
			depth=depth,
			classification=CrawlClassification.FETCH_ERROR,
			elapsed_ms=round((time.monotonic() - started_at) * 1000),
			error=f'{type(error).__name__}: {error}',
		)


async def crawl_source(
	source: DataSourceDefinition,
	options: CrawlOptions,
	client: httpx.AsyncClient,
	rate_limiter: HostRateLimiter,
	semaphore: asyncio.Semaphore,
) -> CrawlSourceResult:
	"""Crawl a bounded same-origin page set for one data source."""
	async with semaphore:
		start_url = str(source.url)
		robots = (
			await fetch_robots_policy(start_url, client, rate_limiter, options.max_content_bytes)
			if options.respect_robots
			else RobotsResult(url='', allowed=True)
		)
		if not robots.allowed:
			denied_page = CrawlPageResult(
				requested_url=start_url,
				depth=0,
				classification=CrawlClassification.ROBOTS_DENIED,
				elapsed_ms=0,
				error=robots.error or 'robots.txt disallows this crawler',
			)
			return CrawlSourceResult(
				source_id=source.id,
				name=source.name,
				category=source.category,
				access=source.access,
				test_level=source.test_level,
				start_url=start_url,
				robots=robots,
				pages=[denied_page],
			)

		queue: deque[tuple[str, int]] = deque([(start_url, 0)])
		queued_urls = {normalize_crawl_url(start_url) or start_url}
		pages: list[CrawlPageResult] = []
		crawl_origin: str | None = None
		while queue and len(pages) < options.max_pages_per_source:
			url, depth = queue.popleft()
			page = await fetch_crawl_page(url, depth, client, rate_limiter, options.max_content_bytes)
			pages.append(page)
			if page.status_code == 429:
				break
			if page.final_url and crawl_origin is None:
				crawl_origin = page.final_url
			if page.final_url:
				final_normalized_url = normalize_crawl_url(page.final_url)
				if final_normalized_url:
					queued_urls.add(final_normalized_url)
			if depth >= options.max_depth or page.classification not in {
				CrawlClassification.CONTENT_RICH,
				CrawlClassification.SPARSE_HTML,
			}:
				continue
			origin_url = crawl_origin or page.final_url or start_url
			for discovered_url in rank_discovered_urls(page.final_url or url, page.discovered_same_origin_urls):
				if not is_same_origin(discovered_url, origin_url):
					continue
				normalized_url = normalize_crawl_url(discovered_url)
				if normalized_url is None or normalized_url in queued_urls:
					continue
				queued_urls.add(normalized_url)
				queue.append((normalized_url, depth + 1))

		return CrawlSourceResult(
			source_id=source.id,
			name=source.name,
			category=source.category,
			access=source.access,
			test_level=source.test_level,
			start_url=start_url,
			robots=robots,
			pages=pages,
		)


def summarize_crawl_results(sources: list[CrawlSourceResult]) -> CrawlSummary:
	"""Aggregate page-level crawl measurements."""
	pages = [page for source in sources for page in source.pages]
	classification_counts = Counter(page.classification for page in pages)
	content_hashes = {page.content_sha256 for page in pages if page.content_sha256}
	return CrawlSummary(
		total_sources=len(sources),
		sources_with_fetched_pages=sum(1 for source in sources if any(page.status_code is not None for page in source.pages)),
		total_pages=len(pages),
		classifications=dict(classification_counts),
		total_content_bytes=sum(page.content_bytes for page in pages),
		total_text_chars=sum(page.text_chars for page in pages),
		unique_content_hashes=len(content_hashes),
		robots_denied_sources=sum(
			1 for source in sources if any(page.classification == CrawlClassification.ROBOTS_DENIED for page in source.pages)
		),
	)


async def run_crawl_experiment(options: CrawlOptions) -> CrawlExperimentResult:
	"""Run the bounded crawl across the selected catalog sources."""
	catalog = load_data_source_catalog(options.catalog_path)
	sources = [
		source
		for source in catalog.sources
		if (not options.source_ids or source.id in options.source_ids)
		and (not options.categories or source.category in options.categories)
		and (not options.test_levels or source.test_level in options.test_levels)
	]
	timeout = httpx.Timeout(
		options.request_timeout_seconds,
		connect=min(options.request_timeout_seconds, 10.0),
	)
	limits = httpx.Limits(
		max_connections=options.concurrency,
		max_keepalive_connections=max(1, options.concurrency // 2),
	)
	rate_limiter = HostRateLimiter(options.per_host_delay_seconds)
	semaphore = asyncio.Semaphore(options.concurrency)
	async with httpx.AsyncClient(
		follow_redirects=True,
		timeout=timeout,
		limits=limits,
		headers={'User-Agent': BROWSER_USER_AGENT, 'Accept': 'text/html,application/xhtml+xml;q=0.9,*/*;q=0.5'},
	) as client:
		results = await asyncio.gather(*(crawl_source(source, options, client, rate_limiter, semaphore) for source in sources))

	experiment = CrawlExperimentResult(
		generated_at=datetime.now(timezone.utc),
		options=options,
		summary=summarize_crawl_results(results),
		sources=results,
	)
	if options.output_path is not None:
		options.output_path.parent.mkdir(parents=True, exist_ok=True)
		options.output_path.write_text(experiment.model_dump_json(indent=2), encoding='utf-8')
	return experiment


def print_crawl_summary(experiment: CrawlExperimentResult) -> None:
	"""Print aggregate and source-level crawl results."""
	summary = experiment.summary
	print(
		f'Crawled {summary.total_sources} sources and {summary.total_pages} pages; '
		f'{summary.sources_with_fetched_pages} sources returned fetched pages'
	)
	for classification, count in sorted(summary.classifications.items(), key=lambda item: item[0].value):
		print(f'{classification.value:18} {count:4}')
	print(
		f'content_bytes={summary.total_content_bytes} text_chars={summary.total_text_chars} '
		f'unique_hashes={summary.unique_content_hashes} robots_denied={summary.robots_denied_sources}'
	)
	for source in experiment.sources:
		classifications = ','.join(page.classification.value for page in source.pages)
		print(f'{source.source_id:28} pages={len(source.pages):2} robots={source.robots.allowed!s:5} {classifications}')


def parse_options() -> CrawlOptions:
	"""Parse CLI arguments into validated crawl options."""
	parser = argparse.ArgumentParser(description=__doc__)
	parser.add_argument('--catalog', type=Path, default=DEFAULT_DATA_SOURCE_CATALOG_PATH)
	parser.add_argument('--source', action='append', default=[])
	parser.add_argument(
		'--category',
		action='append',
		choices=[category.value for category in DataSourceCategory],
		default=[],
	)
	parser.add_argument(
		'--test-level',
		action='append',
		choices=[test_level.value for test_level in DataSourceTestLevel],
		default=[],
	)
	parser.add_argument('--max-pages-per-source', type=int, default=4)
	parser.add_argument('--max-depth', type=int, default=1)
	parser.add_argument('--concurrency', type=int, default=8)
	parser.add_argument('--request-timeout-seconds', type=float, default=20.0)
	parser.add_argument('--per-host-delay-seconds', type=float, default=0.35)
	parser.add_argument('--max-content-bytes', type=int, default=2_000_000)
	parser.add_argument('--ignore-robots', action='store_true')
	parser.add_argument('--output', type=Path)
	arguments = parser.parse_args()
	return CrawlOptions(
		catalog_path=arguments.catalog,
		source_ids=set(arguments.source),
		categories={DataSourceCategory(category) for category in arguments.category},
		test_levels={DataSourceTestLevel(test_level) for test_level in arguments.test_level},
		max_pages_per_source=arguments.max_pages_per_source,
		max_depth=arguments.max_depth,
		concurrency=arguments.concurrency,
		request_timeout_seconds=arguments.request_timeout_seconds,
		per_host_delay_seconds=arguments.per_host_delay_seconds,
		max_content_bytes=arguments.max_content_bytes,
		respect_robots=not arguments.ignore_robots,
		output_path=arguments.output,
	)


def main() -> int:
	"""Run a crawl experiment and print its structured summary."""
	experiment = asyncio.run(run_crawl_experiment(parse_options()))
	print_crawl_summary(experiment)
	return 0


if __name__ == '__main__':
	raise SystemExit(main())
