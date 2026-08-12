"""Tests for bounded external data source crawl experiments."""

import asyncio

import httpx
from pydantic import HttpUrl

from scripts.crawl_data_sources import (
	CrawlClassification,
	CrawlOptions,
	HostRateLimiter,
	classify_page,
	crawl_source,
	extract_html_metrics,
	normalize_crawl_url,
	rank_discovered_urls,
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
	assert normalize_crawl_url('mailto:person@example.com') is None


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
			CrawlOptions(per_host_delay_seconds=0),
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
			CrawlOptions(max_pages_per_source=4, max_depth=1, per_host_delay_seconds=0),
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
			CrawlOptions(max_pages_per_source=2, max_depth=1, per_host_delay_seconds=0),
			client,
			HostRateLimiter(0),
			asyncio.Semaphore(1),
		)

	assert [page.final_url for page in result.pages] == ['https://example.com/canonical', 'https://example.com/next']
	assert requested_paths.count('/canonical') == 1
