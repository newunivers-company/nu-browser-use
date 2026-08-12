"""Run a bounded, read-only crawl experiment across catalogued data sources."""

import argparse
import asyncio
import hashlib
import ipaddress
import posixpath
import re
import socket
import sys
import time
from collections import Counter, deque
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from enum import StrEnum
from html.parser import HTMLParser
from pathlib import Path
from typing import Literal
from urllib.parse import parse_qsl, unquote, urldefrag, urlencode, urljoin, urlsplit, urlunsplit
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
UNSAFE_QUERY_KEYS = {'action', 'command', 'delete', 'destroy', 'do', 'logout', 'operation', 'remove', 'unsubscribe'}
UNSAFE_QUERY_VALUES = {'delete', 'destroy', 'logout', 'remove', 'signout', 'unsubscribe'}
TRACKING_QUERY_KEYS = {'fbclid', 'gclid', 'mc_cid', 'mc_eid', 'ref_src'}
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
LOW_VALUE_PATH_PARTS = {'archive', 'author', 'authors', 'category', 'search', 'tag', 'tags'}
CONTENT_PATH_PARTS = {
	'article',
	'articles',
	'docs',
	'documentation',
	'guide',
	'guides',
	'issues',
	'page',
	'post',
	'posts',
	'questions',
	'story',
}
REDIRECT_STATUS_CODES = {301, 302, 303, 307, 308}
HostResolver = Callable[[str, int], Awaitable[list[str]]]
RedirectValidator = Callable[[str], Awaitable[str | None]]


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
	CROSS_ORIGIN_REDIRECT = 'cross_origin_redirect'
	UNSAFE_URL = 'unsafe_url'
	REDIRECT_ERROR = 'redirect_error'
	FETCH_ERROR = 'fetch_error'


class FetchFailureKind(StrEnum):
	"""Machine-readable reasons a bounded HTTP fetch did not complete."""

	INVALID_URL = 'invalid_url'
	DISALLOWED_ROUTE = 'disallowed_route'
	UNSAFE_NETWORK = 'unsafe_network'
	CROSS_ORIGIN_REDIRECT = 'cross_origin_redirect'
	INVALID_REDIRECT = 'invalid_redirect'
	TOO_MANY_REDIRECTS = 'too_many_redirects'
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
	max_redirects: int = Field(default=5, ge=0, le=10)
	respect_robots: bool = True
	allow_private_networks: bool = False
	strict: bool = False
	minimum_pass_rate: float = Field(default=1.0, ge=0.0, le=1.0)
	minimum_fetched_sources: int = Field(default=1, ge=1)
	maximum_fetch_error_rate: float = Field(default=0.0, ge=0.0, le=1.0)
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
	app_root_count: int = Field(default=0, ge=0)
	nofollow_link_count: int = Field(default=0, ge=0)
	document_nofollow: bool = False


class RobotsResult(BaseModel):
	"""robots.txt decision and retrieval evidence for one source."""

	url: str
	status_code: int | None = None
	allowed: bool
	error: str | None = None
	redirect_chain: list[str] = Field(default_factory=list)


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
	declared_content_length: int | None = Field(default=None, ge=0)
	content_truncated: bool = False
	content_sha256: str | None = None
	content_sha256_scope: Literal['full', 'prefix'] | None = None
	text_chars: int = Field(default=0, ge=0)
	total_links: int = Field(default=0, ge=0)
	same_origin_link_count: int = Field(default=0, ge=0)
	external_link_count: int = Field(default=0, ge=0)
	script_count: int = Field(default=0, ge=0)
	form_count: int = Field(default=0, ge=0)
	redirect_count: int = Field(default=0, ge=0)
	redirect_chain: list[str] = Field(default_factory=list)
	elapsed_ms: int = Field(ge=0)
	error: str | None = None
	failure_kind: FetchFailureKind | None = None
	robots_allowed: bool | None = None
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
	ok: bool = False
	failure_reasons: list[str] = Field(default_factory=list)


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
	passed_sources: int = Field(ge=0)
	failed_sources: int = Field(ge=0)
	fetch_error_rate: float = Field(ge=0.0, le=1.0)


class CrawlGateFailure(BaseModel):
	"""One source-level or aggregate crawl quality-gate failure."""

	source_id: str | None = None
	reason: str


class CrawlQualityGate(BaseModel):
	"""Quality gate calculated from selected crawl sources and configured thresholds."""

	eligible_sources: int = Field(ge=0)
	passed_sources: int = Field(ge=0)
	pass_rate: float = Field(ge=0.0, le=1.0)
	minimum_pass_rate: float = Field(ge=0.0, le=1.0)
	fetched_sources: int = Field(ge=0)
	minimum_fetched_sources: int = Field(ge=1)
	fetch_error_rate: float = Field(ge=0.0, le=1.0)
	maximum_fetch_error_rate: float = Field(ge=0.0, le=1.0)
	passed: bool
	failures: list[CrawlGateFailure]


class CrawlExperimentResult(BaseModel):
	"""Serializable output of a complete crawl experiment."""

	generated_at: datetime
	options: CrawlOptions
	summary: CrawlSummary
	gate: CrawlQualityGate
	sources: list[CrawlSourceResult]


class LimitedContent(BaseModel):
	"""A bounded response body and whether additional bytes were discarded."""

	content: bytes = Field(exclude=True)
	truncated: bool


class BoundedFetchResult(BaseModel):
	"""Internal evidence returned by a safety-checked, manually redirected GET."""

	requested_url: str
	final_url: str | None = None
	status_code: int | None = None
	content_type: str | None = None
	declared_content_length: int | None = Field(default=None, ge=0)
	content: bytes = Field(default=b'', exclude=True)
	content_truncated: bool = False
	redirect_chain: list[str] = Field(default_factory=list)
	failure_kind: FetchFailureKind | None = None
	error: str | None = None


class UrlSafetyDecision(BaseModel):
	"""Result of validating one URL against public-network crawl policy."""

	url: str
	allowed: bool
	addresses: list[str] = Field(default_factory=list)
	reason: str | None = None


class _HtmlMetricsParser(HTMLParser):
	"""Small dependency-free parser for crawl-relevant HTML metrics."""

	def __init__(self) -> None:
		super().__init__(convert_charrefs=True)
		self.title_parts: list[str] = []
		self.text_parts: list[str] = []
		self.links: list[tuple[str, bool]] = []
		self.script_count = 0
		self.form_count = 0
		self.app_root_count = 0
		self.base_href: str | None = None
		self.document_nofollow = False
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
		elif normalized_tag in {'div', 'main'}:
			attributes = dict(attrs)
			root_identifier = (attributes.get('id') or '').casefold()
			if root_identifier in {'app', 'root', '__next', '__nuxt', 'svelte'}:
				self.app_root_count += 1
		elif normalized_tag == 'base' and self.base_href is None:
			self.base_href = dict(attrs).get('href')
		elif normalized_tag == 'meta':
			attributes = dict(attrs)
			if (attributes.get('name') or '').casefold() in {'robots', 'googlebot'}:
				directives = {part.strip().casefold() for part in (attributes.get('content') or '').split(',')}
				self.document_nofollow = self.document_nofollow or 'nofollow' in directives or 'none' in directives
		elif normalized_tag == 'a':
			attributes = dict(attrs)
			href = attributes.get('href')
			if href:
				rel_values = {part.casefold() for part in (attributes.get('rel') or '').split()}
				self.links.append((href, 'nofollow' in rel_values))

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


async def resolve_host_addresses(host: str, port: int) -> list[str]:
	"""Resolve a host asynchronously for public-network policy validation."""
	loop = asyncio.get_running_loop()
	address_info = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
	return sorted({str(item[4][0]) for item in address_info})


class UrlSafetyValidator:
	"""Reject non-public crawl destinations immediately before each request."""

	def __init__(
		self,
		*,
		allow_private_networks: bool = False,
		resolver: HostResolver = resolve_host_addresses,
	) -> None:
		self.allow_private_networks = allow_private_networks
		self.resolver = resolver

	async def validate(self, url: str) -> UrlSafetyDecision:
		"""Validate one canonical URL before any network request is sent."""
		canonical_url = canonicalize_crawl_url(url)
		if canonical_url is None:
			return UrlSafetyDecision(url=url, allowed=False, reason='URL is not a canonical HTTP(S) target')
		parsed = urlsplit(canonical_url)
		host = parsed.hostname or ''
		port = parsed.port or (443 if parsed.scheme == 'https' else 80)
		if self.allow_private_networks:
			return UrlSafetyDecision(url=canonical_url, allowed=True)
		if host == 'localhost' or host.endswith('.localhost') or host.endswith('.local'):
			return UrlSafetyDecision(url=canonical_url, allowed=False, reason=f'local hostname is not crawlable: {host}')

		try:
			try:
				addresses = [str(ipaddress.ip_address(host))]
			except ValueError:
				addresses = await self.resolver(host, port)
			if not addresses:
				raise OSError(f'no addresses resolved for {host}')
		except Exception as error:
			decision = UrlSafetyDecision(
				url=canonical_url,
				allowed=False,
				reason=f'DNS resolution failed for {host}: {type(error).__name__}: {error}',
			)
			return decision

		non_public_addresses = [address for address in addresses if not ipaddress.ip_address(address).is_global]
		decision = UrlSafetyDecision(
			url=canonical_url,
			allowed=not non_public_addresses,
			addresses=addresses,
			reason=(
				f'host resolves to non-public address(es): {", ".join(non_public_addresses)}' if non_public_addresses else None
			),
		)
		return decision


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


def canonicalize_crawl_url(url: str) -> str | None:
	"""Return a deterministic HTTP(S) URL without applying traversal policy."""
	if '\\' in url or any(ord(character) < 32 for character in url):
		return None
	url_without_fragment = urldefrag(url).url
	try:
		parsed = urlsplit(url_without_fragment)
		port = parsed.port
	except ValueError:
		return None
	if parsed.scheme.casefold() not in {'http', 'https'} or not parsed.hostname or parsed.username or parsed.password:
		return None

	host = parsed.hostname.casefold()
	try:
		normalized_host = str(ipaddress.ip_address(host))
	except ValueError:
		try:
			normalized_host = host.encode('idna').decode('ascii')
		except UnicodeError:
			return None
	if ':' in normalized_host:
		normalized_host = f'[{normalized_host}]'
	default_port = 443 if parsed.scheme.casefold() == 'https' else 80
	netloc = normalized_host if port is None or port == default_port else f'{normalized_host}:{port}'

	raw_path = parsed.path or '/'
	normalized_path = posixpath.normpath('/' + raw_path.lstrip('/'))
	if raw_path.endswith('/') and normalized_path != '/':
		normalized_path += '/'
	query_pairs = [
		(key, value)
		for key, value in parse_qsl(parsed.query, keep_blank_values=True)
		if not key.casefold().startswith('utm_') and key.casefold() not in TRACKING_QUERY_KEYS
	]
	normalized_query = urlencode(sorted(query_pairs), doseq=True)
	return urlunsplit((parsed.scheme.casefold(), netloc, normalized_path, normalized_query, ''))


def _route_is_safe(url: str) -> bool:
	"""Reject paths and query actions that could trigger authentication or mutation."""
	parsed = urlsplit(url)
	decoded_path = parsed.path
	for _ in range(2):
		decoded_path = unquote(decoded_path)
	decoded_segments = [part.casefold() for part in decoded_path.split('/') if part]
	if any(segment in {'.', '..'} for segment in decoded_segments):
		return False
	for segment in decoded_segments:
		if segment in SKIPPED_PATH_PARTS:
			return False
		tokens = {token for token in re.split(r'[-_.]+', segment) if token}
		if tokens & {
			'auth',
			'delete',
			'destroy',
			'login',
			'logout',
			'register',
			'remove',
			'signin',
			'signout',
			'signup',
			'unsubscribe',
		}:
			return False
	for key, value in parse_qsl(parsed.query, keep_blank_values=True):
		normalized_key = unquote(key).casefold()
		normalized_value = unquote(value).casefold()
		if normalized_key in UNSAFE_QUERY_KEYS:
			if normalized_key not in {'action', 'command', 'do', 'operation'} or normalized_value in UNSAFE_QUERY_VALUES:
				return False
	return True


def normalize_crawl_url(url: str) -> str | None:
	"""Canonicalize an HTTP(S) URL and reject routes unsafe for read-only traversal."""
	canonical_url = canonicalize_crawl_url(url)
	if canonical_url is None or not _route_is_safe(canonical_url):
		return None
	return canonical_url


def is_same_origin(url: str, origin_url: str) -> bool:
	"""Return whether two URLs use the same scheme and network location."""
	canonical_url = canonicalize_crawl_url(url)
	canonical_origin = canonicalize_crawl_url(origin_url)
	if canonical_url is None or canonical_origin is None:
		return False
	parsed_url = urlsplit(canonical_url)
	parsed_origin = urlsplit(canonical_origin)
	return (parsed_url.scheme.lower(), parsed_url.netloc.lower()) == (
		parsed_origin.scheme.lower(),
		parsed_origin.netloc.lower(),
	)


def rank_discovered_urls(current_page_url: str, urls: list[str]) -> list[str]:
	"""Prefer likely content and pagination while penalizing archive-style link farms."""
	current_path = urlsplit(current_page_url).path.rstrip('/')

	def priority(url: str) -> tuple[int, int, int, int, str]:
		parsed = urlsplit(url)
		path_parts = [part.lower() for part in parsed.path.split('/') if part]
		in_current_scope = bool(current_path and parsed.path.startswith(f'{current_path}/'))
		global_navigation = bool(path_parts and path_parts[0] in GLOBAL_NAVIGATION_PATH_PARTS)
		low_value = bool(set(path_parts) & LOW_VALUE_PATH_PARTS)
		content_likely = bool(set(path_parts) & CONTENT_PATH_PARTS) or any(part.isdigit() for part in path_parts)
		return (
			1 if low_value else 0,
			0 if in_current_scope else 1 if not global_navigation else 2,
			0 if content_likely else 1,
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
	nofollow_link_count = 0
	seen_links: set[str] = set()
	base_url = urljoin(page_url, parser.base_href) if parser.base_href else page_url
	for href, nofollow in parser.links:
		if nofollow or parser.document_nofollow:
			nofollow_link_count += 1
			continue
		normalized_url = normalize_crawl_url(urljoin(base_url, href))
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
		app_root_count=parser.app_root_count,
		nofollow_link_count=nofollow_link_count,
		document_nofollow=parser.document_nofollow,
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
	if metrics.text_chars < 200 and (metrics.script_count >= 3 or (metrics.app_root_count > 0 and metrics.script_count > 0)):
		return CrawlClassification.JAVASCRIPT_SHELL
	if metrics.text_chars >= 1_000 or (metrics.text_chars >= 400 and metrics.total_links >= 5):
		return CrawlClassification.CONTENT_RICH
	return CrawlClassification.SPARSE_HTML


async def read_limited_response(response: httpx.Response, maximum_bytes: int) -> LimitedContent:
	"""Read at most ``maximum_bytes`` while recording whether bytes were discarded."""
	content = bytearray()
	async for chunk in response.aiter_bytes():
		remaining = maximum_bytes + 1 - len(content)
		if remaining <= 0:
			break
		content.extend(chunk[:remaining])
		if len(content) > maximum_bytes:
			break
	return LimitedContent(content=bytes(content[:maximum_bytes]), truncated=len(content) > maximum_bytes)


def _declared_content_length(response: httpx.Response) -> int | None:
	"""Parse a non-negative Content-Length header when it is valid."""
	value = response.headers.get('content-length')
	if value is None:
		return None
	try:
		length = int(value)
	except ValueError:
		return None
	return length if length >= 0 else None


async def fetch_bounded_url(
	url: str,
	client: httpx.AsyncClient,
	rate_limiter: HostRateLimiter,
	safety_validator: UrlSafetyValidator,
	maximum_bytes: int,
	max_redirects: int,
	*,
	redirect_validator: RedirectValidator | None = None,
) -> BoundedFetchResult:
	"""GET one URL with route, public-network, redirect, and body-size boundaries."""
	requested_url = canonicalize_crawl_url(url)
	if requested_url is None:
		return BoundedFetchResult(
			requested_url=url,
			failure_kind=FetchFailureKind.INVALID_URL,
			error='URL is not a valid canonical HTTP(S) target',
		)
	current_url = normalize_crawl_url(requested_url)
	if current_url is None:
		return BoundedFetchResult(
			requested_url=requested_url,
			failure_kind=FetchFailureKind.DISALLOWED_ROUTE,
			error='URL route is excluded from read-only crawling',
		)
	initial_origin = current_url
	redirect_chain = [current_url]

	for redirect_number in range(max_redirects + 1):
		safety = await safety_validator.validate(current_url)
		if not safety.allowed:
			return BoundedFetchResult(
				requested_url=requested_url,
				final_url=current_url,
				redirect_chain=redirect_chain,
				failure_kind=FetchFailureKind.UNSAFE_NETWORK,
				error=safety.reason,
			)
		try:
			async with rate_limiter.limit(current_url):
				async with client.stream('GET', current_url, follow_redirects=False) as response:
					status_code = response.status_code
					if status_code in REDIRECT_STATUS_CODES:
						location = response.headers.get('location')
						if not location:
							return BoundedFetchResult(
								requested_url=requested_url,
								final_url=current_url,
								status_code=status_code,
								redirect_chain=redirect_chain,
								failure_kind=FetchFailureKind.INVALID_REDIRECT,
								error='Redirect response did not provide a Location header',
							)
						if redirect_number >= max_redirects:
							return BoundedFetchResult(
								requested_url=requested_url,
								final_url=current_url,
								status_code=status_code,
								redirect_chain=redirect_chain,
								failure_kind=FetchFailureKind.TOO_MANY_REDIRECTS,
								error=f'Redirect limit of {max_redirects} was exceeded',
							)
						next_url = normalize_crawl_url(urljoin(current_url, location))
						if next_url is None:
							return BoundedFetchResult(
								requested_url=requested_url,
								final_url=current_url,
								status_code=status_code,
								redirect_chain=redirect_chain,
								failure_kind=FetchFailureKind.INVALID_REDIRECT,
								error='Redirect target is invalid or excluded from read-only crawling',
							)
						if not is_same_origin(next_url, initial_origin):
							return BoundedFetchResult(
								requested_url=requested_url,
								final_url=next_url,
								status_code=status_code,
								redirect_chain=[*redirect_chain, next_url],
								failure_kind=FetchFailureKind.CROSS_ORIGIN_REDIRECT,
								error=f'Cross-origin redirect blocked: {current_url} -> {next_url}',
							)
						if redirect_validator is not None:
							redirect_error = await redirect_validator(next_url)
							if redirect_error is not None:
								return BoundedFetchResult(
									requested_url=requested_url,
									final_url=next_url,
									status_code=status_code,
									redirect_chain=[*redirect_chain, next_url],
									failure_kind=FetchFailureKind.ROBOTS_DENIED,
									error=redirect_error,
								)
						current_url = next_url
						redirect_chain.append(current_url)
						continue
					limited_content = await read_limited_response(response, maximum_bytes)
					content_type = response.headers.get('content-type', '').split(';', maxsplit=1)[0].strip().casefold()
					declared_length = _declared_content_length(response)
					return BoundedFetchResult(
						requested_url=requested_url,
						final_url=current_url,
						status_code=status_code,
						content_type=content_type or None,
						declared_content_length=declared_length,
						content=limited_content.content,
						content_truncated=limited_content.truncated
						or (declared_length is not None and declared_length > len(limited_content.content)),
						redirect_chain=redirect_chain,
					)
		except Exception as error:
			return BoundedFetchResult(
				requested_url=requested_url,
				final_url=current_url,
				redirect_chain=redirect_chain,
				failure_kind=FetchFailureKind.FETCH_ERROR,
				error=f'{type(error).__name__}: {error}',
			)

	raise AssertionError('bounded redirect loop exhausted unexpectedly')


class RobotsPolicyCache:
	"""Cache one robots policy per origin and evaluate every queued URL."""

	def __init__(
		self,
		client: httpx.AsyncClient,
		rate_limiter: HostRateLimiter,
		safety_validator: UrlSafetyValidator,
		maximum_bytes: int,
		max_redirects: int,
		*,
		respect_robots: bool,
	) -> None:
		self.client = client
		self.rate_limiter = rate_limiter
		self.safety_validator = safety_validator
		self.maximum_bytes = maximum_bytes
		self.max_redirects = max_redirects
		self.respect_robots = respect_robots
		self._policies: dict[str, tuple[RobotsResult, RobotFileParser | None]] = {}

	async def check(self, url: str) -> RobotsResult:
		"""Return the cached origin policy evaluated for one concrete URL."""
		parsed_url = urlsplit(url)
		origin = urlunsplit((parsed_url.scheme, parsed_url.netloc, '', '', ''))
		robots_url = f'{origin}/robots.txt'
		if not self.respect_robots:
			return RobotsResult(url=robots_url, allowed=True)
		if origin not in self._policies:
			fetch = await fetch_bounded_url(
				robots_url,
				self.client,
				self.rate_limiter,
				self.safety_validator,
				min(self.maximum_bytes, 500_000),
				self.max_redirects,
			)
			parser: RobotFileParser | None = None
			if fetch.failure_kind is not None:
				result = RobotsResult(
					url=robots_url,
					status_code=fetch.status_code,
					allowed=False,
					error=fetch.error,
					redirect_chain=fetch.redirect_chain,
				)
			elif fetch.status_code == 200 and fetch.content_truncated:
				result = RobotsResult(
					url=robots_url,
					status_code=fetch.status_code,
					allowed=False,
					error='robots.txt exceeded the bounded response size; crawl skipped conservatively',
					redirect_chain=fetch.redirect_chain,
				)
			elif fetch.status_code == 200:
				parser = RobotFileParser()
				parser.set_url(robots_url)
				parser.parse(fetch.content.decode('utf-8', errors='replace').splitlines())
				result = RobotsResult(
					url=robots_url,
					status_code=fetch.status_code,
					allowed=True,
					redirect_chain=fetch.redirect_chain,
				)
			elif fetch.status_code == 429 or (fetch.status_code is not None and fetch.status_code >= 500):
				result = RobotsResult(
					url=robots_url,
					status_code=fetch.status_code,
					allowed=False,
					error='robots.txt was temporarily unavailable; crawl skipped conservatively',
					redirect_chain=fetch.redirect_chain,
				)
			else:
				result = RobotsResult(
					url=robots_url,
					status_code=fetch.status_code,
					allowed=True,
					redirect_chain=fetch.redirect_chain,
				)
			self._policies[origin] = (result, parser)

		result, parser = self._policies[origin]
		allowed = result.allowed and (parser is None or parser.can_fetch(CRAWLER_USER_AGENT, url))
		return result.model_copy(update={'allowed': allowed})


async def fetch_robots_policy(
	source_url: str,
	client: httpx.AsyncClient,
	rate_limiter: HostRateLimiter,
	maximum_bytes: int,
	*,
	safety_validator: UrlSafetyValidator | None = None,
	max_redirects: int = 5,
) -> RobotsResult:
	"""Fetch and evaluate robots.txt for one URL through the shared policy implementation."""
	validator = safety_validator or UrlSafetyValidator()
	cache = RobotsPolicyCache(
		client,
		rate_limiter,
		validator,
		maximum_bytes,
		max_redirects,
		respect_robots=True,
	)
	return await cache.check(source_url)


async def fetch_crawl_page(
	url: str,
	depth: int,
	client: httpx.AsyncClient,
	rate_limiter: HostRateLimiter,
	maximum_bytes: int,
	*,
	safety_validator: UrlSafetyValidator | None = None,
	max_redirects: int = 5,
	robots_allowed: bool | None = None,
	redirect_validator: RedirectValidator | None = None,
) -> CrawlPageResult:
	"""Fetch and analyze one crawl page."""
	started_at = time.monotonic()
	validator = safety_validator or UrlSafetyValidator()
	fetch = await fetch_bounded_url(
		url,
		client,
		rate_limiter,
		validator,
		maximum_bytes,
		max_redirects,
		redirect_validator=redirect_validator,
	)
	if fetch.failure_kind is not None:
		classification = {
			FetchFailureKind.CROSS_ORIGIN_REDIRECT: CrawlClassification.CROSS_ORIGIN_REDIRECT,
			FetchFailureKind.INVALID_REDIRECT: CrawlClassification.REDIRECT_ERROR,
			FetchFailureKind.TOO_MANY_REDIRECTS: CrawlClassification.REDIRECT_ERROR,
			FetchFailureKind.INVALID_URL: CrawlClassification.UNSAFE_URL,
			FetchFailureKind.DISALLOWED_ROUTE: CrawlClassification.UNSAFE_URL,
			FetchFailureKind.UNSAFE_NETWORK: CrawlClassification.UNSAFE_URL,
			FetchFailureKind.ROBOTS_DENIED: CrawlClassification.ROBOTS_DENIED,
		}.get(fetch.failure_kind, CrawlClassification.FETCH_ERROR)
		return CrawlPageResult(
			requested_url=url,
			final_url=fetch.final_url,
			depth=depth,
			status_code=fetch.status_code,
			classification=classification,
			redirect_count=max(0, len(fetch.redirect_chain) - 1),
			redirect_chain=fetch.redirect_chain,
			elapsed_ms=round((time.monotonic() - started_at) * 1000),
			error=fetch.error,
			failure_kind=fetch.failure_kind,
			robots_allowed=False if fetch.failure_kind == FetchFailureKind.ROBOTS_DENIED else robots_allowed,
		)

	assert fetch.status_code is not None and fetch.final_url is not None
	content_type = fetch.content_type or ''
	metrics = extract_html_metrics(fetch.content, fetch.final_url) if 'html' in content_type else None
	classification = classify_page(
		status_code=fetch.status_code,
		content_type=content_type,
		final_url=fetch.final_url,
		metrics=metrics,
	)
	return CrawlPageResult(
		requested_url=url,
		final_url=fetch.final_url,
		depth=depth,
		status_code=fetch.status_code,
		content_type=content_type or None,
		classification=classification,
		title=metrics.title if metrics else None,
		content_bytes=len(fetch.content),
		declared_content_length=fetch.declared_content_length,
		content_truncated=fetch.content_truncated,
		content_sha256=hashlib.sha256(fetch.content).hexdigest() if fetch.content else None,
		content_sha256_scope='prefix' if fetch.content_truncated else 'full' if fetch.content else None,
		text_chars=metrics.text_chars if metrics else 0,
		total_links=metrics.total_links if metrics else 0,
		same_origin_link_count=len(metrics.same_origin_links) if metrics else 0,
		external_link_count=metrics.external_link_count if metrics else 0,
		script_count=metrics.script_count if metrics else 0,
		form_count=metrics.form_count if metrics else 0,
		redirect_count=max(0, len(fetch.redirect_chain) - 1),
		redirect_chain=fetch.redirect_chain,
		elapsed_ms=round((time.monotonic() - started_at) * 1000),
		robots_allowed=robots_allowed,
		discovered_same_origin_urls=metrics.same_origin_links if metrics else [],
	)


async def crawl_source(
	source: DataSourceDefinition,
	options: CrawlOptions,
	client: httpx.AsyncClient,
	rate_limiter: HostRateLimiter,
	semaphore: asyncio.Semaphore,
	*,
	safety_validator: UrlSafetyValidator | None = None,
) -> CrawlSourceResult:
	"""Crawl a bounded same-origin page set for one data source."""
	async with semaphore:
		start_url = canonicalize_crawl_url(str(source.url)) or str(source.url)
		validator = safety_validator or UrlSafetyValidator(allow_private_networks=options.allow_private_networks)
		robots_cache = RobotsPolicyCache(
			client,
			rate_limiter,
			validator,
			options.max_content_bytes,
			options.max_redirects,
			respect_robots=options.respect_robots,
		)
		robots = await robots_cache.check(start_url)
		if not robots.allowed:
			denied_page = CrawlPageResult(
				requested_url=start_url,
				depth=0,
				classification=CrawlClassification.ROBOTS_DENIED,
				elapsed_ms=0,
				error=robots.error or 'robots.txt disallows this crawler',
				robots_allowed=False,
			)
			return build_crawl_source_result(
				source,
				start_url,
				robots,
				[denied_page],
			)

		start_normalized_url = normalize_crawl_url(start_url)
		if start_normalized_url is None:
			unsafe_page = CrawlPageResult(
				requested_url=start_url,
				depth=0,
				classification=CrawlClassification.UNSAFE_URL,
				elapsed_ms=0,
				error='Catalog start URL is excluded from read-only crawling',
				failure_kind=FetchFailureKind.DISALLOWED_ROUTE,
				robots_allowed=True,
			)
			return build_crawl_source_result(source, start_url, robots, [unsafe_page])

		queue: deque[tuple[str, int]] = deque([(start_normalized_url, 0)])
		queued_urls = {start_normalized_url}
		pages: list[CrawlPageResult] = []
		while queue and len(pages) < options.max_pages_per_source:
			url, depth = queue.popleft()
			url_robots = await robots_cache.check(url)
			if not url_robots.allowed:
				pages.append(
					CrawlPageResult(
						requested_url=url,
						depth=depth,
						classification=CrawlClassification.ROBOTS_DENIED,
						elapsed_ms=0,
						error=url_robots.error or 'robots.txt disallows this URL',
						robots_allowed=False,
					)
				)
				continue

			async def validate_redirect(target_url: str) -> str | None:
				redirect_robots = await robots_cache.check(target_url)
				if redirect_robots.allowed:
					return None
				return redirect_robots.error or f'robots.txt disallows redirect target {target_url}'

			page = await fetch_crawl_page(
				url,
				depth,
				client,
				rate_limiter,
				options.max_content_bytes,
				safety_validator=validator,
				max_redirects=options.max_redirects,
				robots_allowed=True,
				redirect_validator=validate_redirect,
			)
			pages.append(page)
			if page.status_code == 429:
				break
			if page.final_url:
				final_normalized_url = normalize_crawl_url(page.final_url)
				if final_normalized_url:
					queued_urls.add(final_normalized_url)
			if depth >= options.max_depth or page.classification not in {
				CrawlClassification.CONTENT_RICH,
				CrawlClassification.SPARSE_HTML,
			}:
				continue
			for discovered_url in rank_discovered_urls(page.final_url or url, page.discovered_same_origin_urls):
				if not is_same_origin(discovered_url, start_normalized_url):
					continue
				normalized_url = normalize_crawl_url(discovered_url)
				if normalized_url is None or normalized_url in queued_urls:
					continue
				queued_urls.add(normalized_url)
				queue.append((normalized_url, depth + 1))

		return build_crawl_source_result(source, start_url, robots, pages)


def build_crawl_source_result(
	source: DataSourceDefinition,
	start_url: str,
	robots: RobotsResult,
	pages: list[CrawlPageResult],
) -> CrawlSourceResult:
	"""Evaluate one source against its catalog contract and preserve failure reasons."""
	fetched_pages = [page for page in pages if page.status_code is not None and page.failure_kind is None]
	if source.test_level == DataSourceTestLevel.BEHAVIORAL:
		ok = any(
			page.status_code in source.expected_http_statuses
			and page.classification in {CrawlClassification.CONTENT_RICH, CrawlClassification.SPARSE_HTML}
			for page in fetched_pages
		)
	else:
		ok = any(page.status_code in source.expected_http_statuses for page in fetched_pages)
	failure_reasons: list[str] = []
	if not fetched_pages:
		failure_reasons.append('no HTTP page was fetched')
	if source.test_level == DataSourceTestLevel.BEHAVIORAL and not any(
		page.classification in {CrawlClassification.CONTENT_RICH, CrawlClassification.SPARSE_HTML} for page in fetched_pages
	):
		failure_reasons.append('no crawlable HTML content was observed')
	if fetched_pages and not any(page.status_code in source.expected_http_statuses for page in fetched_pages):
		observed_statuses = sorted({page.status_code for page in fetched_pages if page.status_code is not None})
		failure_reasons.append(f'observed HTTP statuses {observed_statuses} were outside the catalog contract')
	for page in pages:
		if page.failure_kind is not None or page.classification == CrawlClassification.ROBOTS_DENIED:
			failure_reasons.append(f'{page.requested_url}: {page.error or page.classification.value}')
	return CrawlSourceResult(
		source_id=source.id,
		name=source.name,
		category=source.category,
		access=source.access,
		test_level=source.test_level,
		start_url=start_url,
		robots=robots,
		pages=pages,
		ok=ok,
		failure_reasons=list(dict.fromkeys(failure_reasons)),
	)


def summarize_crawl_results(sources: list[CrawlSourceResult]) -> CrawlSummary:
	"""Aggregate page-level crawl measurements."""
	pages = [page for source in sources for page in source.pages]
	classification_counts = Counter(page.classification for page in pages)
	content_hashes = {page.content_sha256 for page in pages if page.content_sha256}
	fetch_error_pages = sum(page.classification == CrawlClassification.FETCH_ERROR for page in pages)
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
		passed_sources=sum(source.ok for source in sources),
		failed_sources=sum(not source.ok for source in sources),
		fetch_error_rate=fetch_error_pages / len(pages) if pages else 0.0,
	)


def select_crawl_sources(catalog_sources: list[DataSourceDefinition], options: CrawlOptions) -> list[DataSourceDefinition]:
	"""Validate filters and select crawl sources in stable catalog order."""
	known_source_ids = {source.id for source in catalog_sources}
	unknown_source_ids = options.source_ids - known_source_ids
	if unknown_source_ids:
		raise ValueError(f'Unknown data source IDs: {", ".join(sorted(unknown_source_ids))}')
	sources = [
		source
		for source in catalog_sources
		if (not options.source_ids or source.id in options.source_ids)
		and (not options.categories or source.category in options.categories)
		and (not options.test_levels or source.test_level in options.test_levels)
	]
	if not sources:
		raise ValueError('Crawl filters selected no data sources')
	return sources


def evaluate_crawl_quality_gate(
	sources: list[CrawlSourceResult],
	options: CrawlOptions,
) -> CrawlQualityGate:
	"""Evaluate behavioral contracts by default and all selected sources in strict mode."""
	eligible_sources = [source for source in sources if options.strict or source.test_level == DataSourceTestLevel.BEHAVIORAL]
	passed_sources = sum(source.ok for source in eligible_sources)
	pass_rate = passed_sources / len(eligible_sources) if eligible_sources else 1.0
	eligible_pages = [page for source in eligible_sources for page in source.pages]
	fetched_sources = sum(1 for source in eligible_sources if any(page.status_code is not None for page in source.pages))
	fetch_error_rate = (
		sum(page.classification == CrawlClassification.FETCH_ERROR for page in eligible_pages) / len(eligible_pages)
		if eligible_pages
		else 0.0
	)
	failures = [
		CrawlGateFailure(source_id=source.source_id, reason='; '.join(source.failure_reasons) or 'source contract failed')
		for source in eligible_sources
		if not source.ok
	]
	if pass_rate < options.minimum_pass_rate:
		failures.append(
			CrawlGateFailure(
				reason=f'pass rate {pass_rate:.3f} is below required {options.minimum_pass_rate:.3f}',
			)
		)
	if eligible_sources and fetched_sources < options.minimum_fetched_sources:
		failures.append(
			CrawlGateFailure(
				reason=f'fetched sources {fetched_sources} is below required {options.minimum_fetched_sources}',
			)
		)
	if fetch_error_rate > options.maximum_fetch_error_rate:
		failures.append(
			CrawlGateFailure(
				reason=f'fetch error rate {fetch_error_rate:.3f} exceeds allowed {options.maximum_fetch_error_rate:.3f}',
			)
		)
	return CrawlQualityGate(
		eligible_sources=len(eligible_sources),
		passed_sources=passed_sources,
		pass_rate=pass_rate,
		minimum_pass_rate=options.minimum_pass_rate,
		fetched_sources=fetched_sources,
		minimum_fetched_sources=options.minimum_fetched_sources,
		fetch_error_rate=fetch_error_rate,
		maximum_fetch_error_rate=options.maximum_fetch_error_rate,
		passed=not failures,
		failures=failures,
	)


async def run_crawl_experiment(options: CrawlOptions) -> CrawlExperimentResult:
	"""Run the bounded crawl across the selected catalog sources."""
	catalog = load_data_source_catalog(options.catalog_path)
	sources = select_crawl_sources(catalog.sources, options)
	timeout = httpx.Timeout(
		options.request_timeout_seconds,
		connect=min(options.request_timeout_seconds, 10.0),
	)
	limits = httpx.Limits(
		max_connections=options.concurrency,
		max_keepalive_connections=max(1, options.concurrency // 2),
	)
	rate_limiter = HostRateLimiter(options.per_host_delay_seconds)
	safety_validator = UrlSafetyValidator(allow_private_networks=options.allow_private_networks)
	semaphore = asyncio.Semaphore(options.concurrency)
	async with httpx.AsyncClient(
		follow_redirects=False,
		trust_env=False,
		timeout=timeout,
		limits=limits,
		headers={'User-Agent': BROWSER_USER_AGENT, 'Accept': 'text/html,application/xhtml+xml;q=0.9,*/*;q=0.5'},
	) as client:
		results = await asyncio.gather(
			*(
				crawl_source(
					source,
					options,
					client,
					rate_limiter,
					semaphore,
					safety_validator=safety_validator,
				)
				for source in sources
			)
		)

	summary = summarize_crawl_results(results)
	experiment = CrawlExperimentResult(
		generated_at=datetime.now(timezone.utc),
		options=options,
		summary=summary,
		gate=evaluate_crawl_quality_gate(results, options),
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
		f'unique_hashes={summary.unique_content_hashes} robots_denied={summary.robots_denied_sources} '
		f'fetch_error_rate={summary.fetch_error_rate:.3f}'
	)
	for source in experiment.sources:
		classifications = ','.join(page.classification.value for page in source.pages)
		status = 'PASS' if source.ok else 'FAIL'
		print(
			f'{status:4} {source.source_id:28} pages={len(source.pages):2} robots={source.robots.allowed!s:5} {classifications}'
		)
	gate = experiment.gate
	print(
		f'Quality gate: {"PASS" if gate.passed else "FAIL"} '
		f'({gate.passed_sources}/{gate.eligible_sources}, pass_rate={gate.pass_rate:.3f})'
	)
	for failure in gate.failures:
		prefix = f'{failure.source_id}: ' if failure.source_id else ''
		print(f'  - {prefix}{failure.reason}')


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
	parser.add_argument('--max-redirects', type=int, default=5)
	parser.add_argument('--ignore-robots', action='store_true')
	parser.add_argument(
		'--allow-private-networks',
		action='store_true',
		help='Allow loopback/private network crawling only for isolated local tests.',
	)
	parser.add_argument('--strict', action='store_true', help='Include availability-only sources in the crawl quality gate.')
	parser.add_argument('--minimum-pass-rate', type=float, default=1.0)
	parser.add_argument('--minimum-fetched-sources', type=int, default=1)
	parser.add_argument('--maximum-fetch-error-rate', type=float, default=0.0)
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
		max_redirects=arguments.max_redirects,
		respect_robots=not arguments.ignore_robots,
		allow_private_networks=arguments.allow_private_networks,
		strict=arguments.strict,
		minimum_pass_rate=arguments.minimum_pass_rate,
		minimum_fetched_sources=arguments.minimum_fetched_sources,
		maximum_fetch_error_rate=arguments.maximum_fetch_error_rate,
		output_path=arguments.output,
	)


def main() -> int:
	"""Run a crawl experiment and print its structured summary."""
	try:
		experiment = asyncio.run(run_crawl_experiment(parse_options()))
	except ValueError as error:
		print(f'Crawl configuration error: {error}', file=sys.stderr)
		return 2
	print_crawl_summary(experiment)
	return 0 if experiment.gate.passed else 1


if __name__ == '__main__':
	raise SystemExit(main())
