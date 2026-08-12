"""Tests for bounded external data source crawl experiments."""

import asyncio

import httpx
import pytest
from pydantic import HttpUrl

from scripts.crawl_data_sources import (
	CrawlClassification,
	CrawlOptions,
	CrawlPageResult,
	FetchFailureKind,
	HostRateLimiter,
	RobotsResult,
	UrlSafetyValidator,
	build_crawl_source_result,
	canonicalize_crawl_url,
	classify_page,
	crawl_source,
	evaluate_crawl_quality_gate,
	extract_html_metrics,
	fetch_bounded_url,
	fetch_crawl_page,
	normalize_crawl_url,
	rank_discovered_urls,
	select_crawl_sources,
	summarize_crawl_results,
)
from scripts.data_source_catalog import (
	DataSourceAccess,
	DataSourceCategory,
	DataSourceDefinition,
	DataSourceTestLevel,
)


def make_source() -> DataSourceDefinition:
	"""Build a validated crawl source for isolated HTTP tests."""
	return DataSourceDefinition(
		id='crawl_source',
		name='Crawl Source',
		category=DataSourceCategory.SOCIAL_MEDIA,
		url=HttpUrl('https://example.com/'),
		access=DataSourceAccess.PUBLIC_STATIC,
		test_level=DataSourceTestLevel.BEHAVIORAL,
		expected_http_statuses=[200],
		description='Bounded crawler test source.',
	)


def test_extract_html_metrics_filters_unsafe_and_external_links() -> None:
	"""Extract visible content while keeping only safe same-origin targets."""
	content = b"""
		<html><head><title> Crawl Page </title><script>hidden script text</script></head>
		<body><p>Visible text for the crawler.</p>
		<a href="/article">Article</a><a href="/delete">Delete</a>
		<a href="https://outside.example.net/page">External</a><form></form></body></html>
	"""
	metrics = extract_html_metrics(content, 'https://example.com/')

	assert metrics.title == 'Crawl Page'
	assert 'https://example.com/article' in metrics.same_origin_links
	assert all('/delete' not in url for url in metrics.same_origin_links)
	assert metrics.external_link_count == 1
	assert metrics.script_count == 1
	assert metrics.form_count == 1
	assert metrics.text_chars > 0


def test_normalize_crawl_url_rejects_mutating_and_non_http_targets() -> None:
	"""Reject fragments, non-web schemes, and potentially mutating routes."""
	assert normalize_crawl_url('https://Example.com/path#section') == 'https://example.com/path'
	assert normalize_crawl_url('https://example.com/logout') is None
	assert normalize_crawl_url('https://example.com/login') is None
	assert normalize_crawl_url('https://example.com/cdn-cgi/l/email-protection') is None
	assert normalize_crawl_url('https://example.com/delete-account') is None
	assert normalize_crawl_url('https://example.com/%6c%6f%67%6f%75%74') is None
	assert normalize_crawl_url('https://example.com/safe%2Flogout') is None
	assert normalize_crawl_url('https://example.com/safe/%252e%252e/private') is None
	assert normalize_crawl_url('https://example.com/path?action=delete') is None
	assert normalize_crawl_url('mailto:person@example.com') is None
	assert canonicalize_crawl_url('https://example.com\\@127.0.0.1/') is None
	assert canonicalize_crawl_url('HTTPS://Example.com:443/a/../page?utm_source=x&b=2&a=1#part') == (
		'https://example.com/page?a=1&b=2'
	)


def test_extract_html_metrics_honors_base_and_nofollow_directives() -> None:
	"""Do not enqueue links excluded by anchor or document crawling directives."""
	metrics = extract_html_metrics(
		b'<html><head><base href="/docs/"></head><body><a href="guide">Guide</a>'
		b'<a href="private" rel="nofollow">Private</a></body></html>',
		'https://example.com/start',
	)
	document_nofollow = extract_html_metrics(
		b'<html><head><meta name="robots" content="noindex,nofollow"></head><body><a href="/hidden">Hidden</a></body></html>',
		'https://example.com/',
	)

	assert metrics.same_origin_links == ['https://example.com/docs/guide']
	assert metrics.nofollow_link_count == 1
	assert document_nofollow.same_origin_links == []
	assert document_nofollow.document_nofollow is True


def test_rank_discovered_urls_prefers_current_content_scope() -> None:
	"""Prioritize scoped content links over global site navigation."""
	urls = [
		'https://github.com/features',
		'https://github.com/browser-use/browser-use/issues',
		'https://github.com/explore',
		'https://github.com/browser-use/browser-use/tree/main/docs',
	]

	ranked = rank_discovered_urls('https://github.com/browser-use/browser-use', urls)

	assert ranked[:2] == [
		'https://github.com/browser-use/browser-use/tree/main/docs',
		'https://github.com/browser-use/browser-use/issues',
	]


def test_classify_page_distinguishes_content_shell_login_and_blocking() -> None:
	"""Map response evidence to crawl-relevant behavior classes."""
	rich_metrics = extract_html_metrics(
		('<html><body>' + ('useful content ' * 100) + '<a href="/next">Next</a></body></html>').encode(),
		'https://example.com/',
	)
	shell_metrics = extract_html_metrics(
		b'<html><body><div id="app"></div><script></script><script></script><script></script></body></html>',
		'https://example.com/',
	)
	single_bundle_shell_metrics = extract_html_metrics(
		b'<html><body><div id="root"></div><script src="app.js"></script></body></html>',
		'https://example.com/',
	)

	assert (
		classify_page(status_code=200, content_type='text/html', final_url='https://example.com/', metrics=rich_metrics)
		== CrawlClassification.CONTENT_RICH
	)
	assert (
		classify_page(status_code=200, content_type='text/html', final_url='https://example.com/', metrics=shell_metrics)
		== CrawlClassification.JAVASCRIPT_SHELL
	)
	assert (
		classify_page(
			status_code=200,
			content_type='text/html',
			final_url='https://example.com/',
			metrics=single_bundle_shell_metrics,
		)
		== CrawlClassification.JAVASCRIPT_SHELL
	)
	assert (
		classify_page(
			status_code=200,
			content_type='text/html',
			final_url='https://example.com/login',
			metrics=rich_metrics,
		)
		== CrawlClassification.LOGIN_REDIRECT
	)
	assert (
		classify_page(status_code=403, content_type='text/html', final_url='https://example.com/', metrics=rich_metrics)
		== CrawlClassification.BLOCKED
	)


async def test_crawl_source_respects_robots_and_same_origin_page_limit() -> None:
	"""Follow safe internal links within depth and page-count limits."""

	def handle_request(request: httpx.Request) -> httpx.Response:
		if request.url.path == '/robots.txt':
			return httpx.Response(200, text='User-agent: *\nAllow: /\n', request=request)
		if request.url.path == '/':
			return httpx.Response(
				200,
				text=(
					'<html><head><title>Home</title></head><body>'
					+ ('home text ' * 100)
					+ '<a href="/article">Article</a><a href="/logout">Logout</a>'
					+ '<a href="https://outside.example.net/">Outside</a></body></html>'
				),
				headers={'content-type': 'text/html'},
				request=request,
			)
		return httpx.Response(
			200,
			text='<html><head><title>Article</title></head><body>' + ('article text ' * 100) + '</body></html>',
			headers={'content-type': 'text/html'},
			request=request,
		)

	options = CrawlOptions(
		max_pages_per_source=2,
		max_depth=1,
		per_host_delay_seconds=0,
		allow_private_networks=True,
	)
	transport = httpx.MockTransport(handle_request)
	async with httpx.AsyncClient(transport=transport, follow_redirects=True) as client:
		result = await crawl_source(
			make_source(),
			options,
			client,
			HostRateLimiter(0),
			asyncio.Semaphore(1),
		)

	assert result.robots.allowed is True
	assert [page.final_url for page in result.pages] == ['https://example.com/', 'https://example.com/article']
	assert all('/logout' not in url for page in result.pages for url in page.discovered_same_origin_urls)
	assert summarize_crawl_results([result]).total_pages == 2


async def test_crawl_source_skips_robots_disallowed_source() -> None:
	"""Do not fetch a page when robots.txt disallows the crawler."""
	requested_paths: list[str] = []

	def handle_request(request: httpx.Request) -> httpx.Response:
		requested_paths.append(request.url.path)
		return httpx.Response(200, text='User-agent: *\nDisallow: /\n', request=request)

	transport = httpx.MockTransport(handle_request)
	async with httpx.AsyncClient(transport=transport) as client:
		result = await crawl_source(
			make_source(),
			CrawlOptions(per_host_delay_seconds=0, allow_private_networks=True),
			client,
			HostRateLimiter(0),
			asyncio.Semaphore(1),
		)

	assert requested_paths == ['/robots.txt']
	assert result.pages[0].classification == CrawlClassification.ROBOTS_DENIED


async def test_crawl_source_stops_after_rate_limit_response() -> None:
	"""Stop consuming a source queue immediately after an HTTP 429 response."""
	requested_paths: list[str] = []

	def handle_request(request: httpx.Request) -> httpx.Response:
		requested_paths.append(request.url.path)
		if request.url.path == '/robots.txt':
			return httpx.Response(200, text='User-agent: *\nAllow: /\n', request=request)
		if request.url.path == '/':
			return httpx.Response(
				200,
				text='<html><body>' + ('content ' * 150) + '<a href="/a">A</a><a href="/b">B</a></body></html>',
				headers={'content-type': 'text/html'},
				request=request,
			)
		return httpx.Response(429, text='slow down', headers={'content-type': 'text/plain'}, request=request)

	transport = httpx.MockTransport(handle_request)
	async with httpx.AsyncClient(transport=transport) as client:
		result = await crawl_source(
			make_source(),
			CrawlOptions(
				max_pages_per_source=4,
				max_depth=1,
				per_host_delay_seconds=0,
				allow_private_networks=True,
			),
			client,
			HostRateLimiter(0),
			asyncio.Semaphore(1),
		)

	assert requested_paths == ['/robots.txt', '/', '/a']
	assert [page.status_code for page in result.pages] == [200, 429]


async def test_crawl_source_does_not_revisit_redirect_canonical_url() -> None:
	"""Treat a redirect destination as visited before queuing page links."""
	requested_paths: list[str] = []

	def handle_request(request: httpx.Request) -> httpx.Response:
		requested_paths.append(request.url.path)
		if request.url.path == '/robots.txt':
			return httpx.Response(200, text='User-agent: *\nAllow: /\n', request=request)
		if request.url.path == '/legacy':
			return httpx.Response(302, headers={'location': '/canonical'}, request=request)
		return httpx.Response(
			200,
			text=(
				'<html><body>' + ('content ' * 150) + '<a href="/canonical">Canonical</a><a href="/next">Next</a></body></html>'
			),
			headers={'content-type': 'text/html'},
			request=request,
		)

	source = make_source().model_copy(update={'url': 'https://example.com/legacy'})
	transport = httpx.MockTransport(handle_request)
	async with httpx.AsyncClient(transport=transport, follow_redirects=True) as client:
		result = await crawl_source(
			source,
			CrawlOptions(
				max_pages_per_source=2,
				max_depth=1,
				per_host_delay_seconds=0,
				allow_private_networks=True,
			),
			client,
			HostRateLimiter(0),
			asyncio.Semaphore(1),
		)

	assert [page.final_url for page in result.pages] == ['https://example.com/canonical', 'https://example.com/next']
	assert requested_paths.count('/canonical') == 1


async def test_url_safety_rejects_private_and_mixed_dns_answers() -> None:
	"""Any non-public DNS answer must fail closed before the HTTP client is used."""
	requests_sent = 0

	async def private_resolver(host: str, port: int) -> list[str]:
		return ['203.0.113.10', '127.0.0.1']

	def handle_request(request: httpx.Request) -> httpx.Response:
		nonlocal requests_sent
		requests_sent += 1
		return httpx.Response(200, request=request)

	validator = UrlSafetyValidator(resolver=private_resolver)
	decision = await validator.validate('https://example.test/page')
	async with httpx.AsyncClient(transport=httpx.MockTransport(handle_request)) as client:
		fetch = await fetch_bounded_url('https://example.test/page', client, HostRateLimiter(0), validator, 100, 1)

	assert decision.allowed is False
	assert '127.0.0.1' in (decision.reason or '')
	assert fetch.failure_kind == FetchFailureKind.UNSAFE_NETWORK
	assert requests_sent == 0


async def test_bounded_fetch_blocks_cross_origin_redirect_and_records_truncation() -> None:
	"""Redirect boundaries and prefix hashes must be explicit in fetch evidence."""
	requested_urls: list[str] = []

	def handle_request(request: httpx.Request) -> httpx.Response:
		requested_urls.append(str(request.url))
		if request.url.path == '/redirect':
			return httpx.Response(302, headers={'location': 'https://other.example/page'}, request=request)
		return httpx.Response(
			200,
			content=b'x' * 20,
			headers={'content-type': 'text/html', 'content-length': '20'},
			request=request,
		)

	transport = httpx.MockTransport(handle_request)
	validator = UrlSafetyValidator(allow_private_networks=True)
	async with httpx.AsyncClient(transport=transport) as client:
		redirect = await fetch_bounded_url('https://example.com/redirect', client, HostRateLimiter(0), validator, 100, 3)
		truncated = await fetch_bounded_url('https://example.com/content', client, HostRateLimiter(0), validator, 10, 3)

	assert redirect.failure_kind == FetchFailureKind.CROSS_ORIGIN_REDIRECT
	assert requested_urls == ['https://example.com/redirect', 'https://example.com/content']
	assert truncated.content == b'x' * 10
	assert truncated.content_truncated is True
	assert truncated.declared_content_length == 20


async def test_truncated_html_is_inconclusive_instead_of_content_or_shell() -> None:
	"""Do not make semantic page-quality decisions from a bounded HTML prefix."""
	content = b'<html><body>' + (b'useful content ' * 100) + b'</body></html>'

	def handle_request(request: httpx.Request) -> httpx.Response:
		return httpx.Response(200, content=content, headers={'content-type': 'text/html'}, request=request)

	async with httpx.AsyncClient(transport=httpx.MockTransport(handle_request)) as client:
		page = await fetch_crawl_page(
			'https://example.com/content',
			0,
			client,
			HostRateLimiter(0),
			100,
			safety_validator=UrlSafetyValidator(allow_private_networks=True),
		)

	assert page.content_truncated is True
	assert page.content_sha256_scope == 'prefix'
	assert page.classification == CrawlClassification.TRUNCATED_HTML


async def test_crawl_source_applies_robots_policy_to_every_queued_url() -> None:
	"""A path disallowed by robots.txt must never be requested as a discovered page."""
	requested_paths: list[str] = []

	def handle_request(request: httpx.Request) -> httpx.Response:
		requested_paths.append(request.url.path)
		if request.url.path == '/robots.txt':
			return httpx.Response(200, text='User-agent: *\nDisallow: /private\n', request=request)
		return httpx.Response(
			200,
			text='<html><body>' + ('content ' * 150) + '<a href="/private">Private</a></body></html>',
			headers={'content-type': 'text/html'},
			request=request,
		)

	async with httpx.AsyncClient(transport=httpx.MockTransport(handle_request)) as client:
		result = await crawl_source(
			make_source(),
			CrawlOptions(
				max_pages_per_source=2,
				max_depth=1,
				per_host_delay_seconds=0,
				allow_private_networks=True,
			),
			client,
			HostRateLimiter(0),
			asyncio.Semaphore(1),
		)

	assert requested_paths == ['/robots.txt', '/']
	assert result.pages[-1].classification == CrawlClassification.ROBOTS_DENIED
	assert result.pages[-1].requested_url == 'https://example.com/private'


async def test_crawl_source_checks_robots_before_following_redirect_target() -> None:
	"""A same-origin redirect must not bypass a path-specific robots rule."""
	requested_paths: list[str] = []

	def handle_request(request: httpx.Request) -> httpx.Response:
		requested_paths.append(request.url.path)
		if request.url.path == '/robots.txt':
			return httpx.Response(200, text='User-agent: *\nDisallow: /private\n', request=request)
		return httpx.Response(302, headers={'location': '/private'}, request=request)

	async with httpx.AsyncClient(transport=httpx.MockTransport(handle_request)) as client:
		result = await crawl_source(
			make_source(),
			CrawlOptions(per_host_delay_seconds=0, allow_private_networks=True),
			client,
			HostRateLimiter(0),
			asyncio.Semaphore(1),
		)

	assert requested_paths == ['/robots.txt', '/']
	assert result.pages[0].classification == CrawlClassification.ROBOTS_DENIED
	assert result.pages[0].robots_allowed is False


async def test_crawl_source_does_not_follow_cross_origin_redirect() -> None:
	"""A source redirect may be recorded but must not establish a new crawl origin."""
	requested_urls: list[str] = []

	def handle_request(request: httpx.Request) -> httpx.Response:
		requested_urls.append(str(request.url))
		if request.url.path == '/robots.txt':
			return httpx.Response(200, text='User-agent: *\nAllow: /\n', request=request)
		return httpx.Response(302, headers={'location': 'https://other.example/landing'}, request=request)

	async with httpx.AsyncClient(transport=httpx.MockTransport(handle_request)) as client:
		result = await crawl_source(
			make_source(),
			CrawlOptions(per_host_delay_seconds=0, allow_private_networks=True),
			client,
			HostRateLimiter(0),
			asyncio.Semaphore(1),
		)

	assert requested_urls == ['https://example.com/robots.txt', 'https://example.com/']
	assert result.pages[0].classification == CrawlClassification.CROSS_ORIGIN_REDIRECT
	assert result.pages[0].failure_kind == FetchFailureKind.CROSS_ORIGIN_REDIRECT


def test_source_selection_and_quality_gate_reject_silent_empty_success() -> None:
	"""Unknown filters and failed behavioral contracts must produce explicit gate failures."""
	source = make_source()
	with pytest.raises(ValueError, match='missing_source'):
		select_crawl_sources([source], CrawlOptions(source_ids={'missing_source'}))
	with pytest.raises(ValueError, match='selected no data sources'):
		select_crawl_sources([source], CrawlOptions(categories={DataSourceCategory.COMMERCE}))

	failed_source = build_crawl_source_result(
		source,
		str(source.url),
		RobotsResult(url='https://example.com/robots.txt', allowed=True),
		[
			CrawlPageResult(
				requested_url=str(source.url),
				depth=0,
				classification=CrawlClassification.FETCH_ERROR,
				failure_kind=FetchFailureKind.FETCH_ERROR,
				elapsed_ms=1,
				error='network failed',
			)
		],
	)
	gate = evaluate_crawl_quality_gate([failed_source], CrawlOptions())

	assert gate.passed is False
	assert gate.pass_rate == 0
	assert gate.fetch_error_rate == 1

	availability_failure = failed_source.model_copy(update={'test_level': DataSourceTestLevel.AVAILABILITY})
	assert evaluate_crawl_quality_gate([availability_failure], CrawlOptions()).passed is True
	assert evaluate_crawl_quality_gate([availability_failure], CrawlOptions(strict=True)).passed is False
