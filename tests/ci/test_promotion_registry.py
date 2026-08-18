"""Promotion-channel registry: schema invariants and browser-level policy enforcement.

The registry decides which competitor channels the collectors may fetch. Its
rules were prose until the pydantic layer landed; these tests pin the two things
that actually matter — that the shipped YAML still satisfies the policy, and
that a channel marked T2 is refused by the browser rather than merely skipped by
a well-behaved script.
"""

import pytest
from bubus import EventBus
from pydantic import ValidationError

from browser_use.browser import BrowserProfile, BrowserSession
from browser_use.browser.watchdogs.security_watchdog import SecurityWatchdog
from scripts.site_collectors.registry.models import (
	AccessTier,
	Channel,
	OfficialStatus,
	PromotionRegistry,
	RobotsVerdict,
	load_registry,
)


@pytest.fixture(scope='module')
def registry() -> PromotionRegistry:
	"""The real shipped registry — validation failures here are the point."""
	return load_registry()


def _channel(**overrides) -> dict:
	base = {
		'url': 'https://example.com/',
		'company': 'acme',
		'channel_type': 'website',
		'access_tier': 'T0',
		'robots_verdict': 'allow',
		'official_status': 'claimed',
		'consumer_or_b2b': 'consumer',
		'collect': True,
	}
	return base | overrides


class TestRegistryFileIntegrity:
	def test_shipped_registry_loads(self, registry: PromotionRegistry):
		assert registry.companies and registry.brands and registry.channels

	def test_every_blocked_channel_is_uncollected(self, registry: PromotionRegistry):
		"""The core policy: T2 exists to be recorded, never fetched."""
		leaked = [c.url for c in registry.channels if c.access_tier is AccessTier.BLOCKED and c.collect]
		assert leaked == []

	def test_no_collected_channel_is_robots_disallowed(self, registry: PromotionRegistry):
		leaked = [c.url for c in registry.channels if c.collect and c.robots_verdict is RobotsVerdict.DISALLOW]
		assert leaked == []

	def test_pending_decisions_are_not_collected(self, registry: PromotionRegistry):
		"""Sources that allow `*` but name AI crawlers stay frozen until ruled on."""
		leaked = [c.url for c in registry.channels if c.decision_pending and c.collect]
		assert leaked == []

	def test_verified_channels_cite_evidence(self, registry: PromotionRegistry):
		unsourced = [c.url for c in registry.channels if c.official_status is OfficialStatus.VERIFIED and c.official_evidence is None]
		assert unsourced == []

	def test_app_ids_resolve_to_brands(self, registry: PromotionRegistry):
		"""appstore_watch keys its observations on these; duplicates would merge two brands."""
		app_ids = [b.app_ios for b in registry.brands if b.app_ios]
		assert len(app_ids) == len(set(app_ids))
		assert all(b.company in registry.by_company for b in registry.brands)


class TestPolicyInvariants:
	def test_blocked_tier_rejects_collect(self):
		with pytest.raises(ValidationError, match='T2 channels must have collect: false'):
			Channel.model_validate(_channel(access_tier='T2', robots_verdict='authwall', collect=True))

	def test_robots_disallow_rejects_collect(self):
		with pytest.raises(ValidationError, match='robots disallows this path'):
			Channel.model_validate(_channel(robots_verdict='disallow', collect=True))

	def test_decision_pending_rejects_collect(self):
		with pytest.raises(ValidationError, match='decision_pending'):
			Channel.model_validate(_channel(robots_verdict='named_ai_block', decision_pending=True, collect=True))

	def test_verified_requires_evidence(self):
		with pytest.raises(ValidationError, match='requires official_evidence'):
			Channel.model_validate(_channel(official_status='verified'))

	def test_channel_must_name_an_owner(self):
		with pytest.raises(ValidationError, match='must reference a brand or a company'):
			payload = _channel()
			del payload['company']
			Channel.model_validate(payload)

	def test_unknown_tier_is_rejected(self):
		"""A typo'd tier must fail loudly rather than default to something permissive."""
		with pytest.raises(ValidationError):
			Channel.model_validate(_channel(access_tier='T3'))

	def test_duplicate_channel_urls_rejected(self, registry: PromotionRegistry):
		payload = registry.model_dump(mode='json')
		payload['channels'].append(payload['channels'][0])
		with pytest.raises(ValidationError, match='channel urls must be unique'):
			PromotionRegistry.model_validate(payload)

	def test_dangling_brand_reference_rejected(self, registry: PromotionRegistry):
		payload = registry.model_dump(mode='json')
		payload['channels'][0]['brand'] = 'no_such_brand'
		with pytest.raises(ValidationError, match='unknown brand'):
			PromotionRegistry.model_validate(payload)


class TestBrowserLevelEnforcement:
	"""The registry is handed to BrowserProfile, so the block is the browser's, not the script's."""

	@pytest.fixture(scope='class')
	def watchdog(self) -> SecurityWatchdog:
		registry = load_registry()
		targets = registry.collectible(AccessTier.BROWSER_REQUIRED)
		profile = BrowserProfile(
			allowed_domains=registry.allowed_domains(targets),
			prohibited_domains=registry.prohibited_domains(),
			headless=True,
			user_data_dir=None,
		)
		return SecurityWatchdog(browser_session=BrowserSession(browser_profile=profile), event_bus=EventBus())

	def test_every_blocked_channel_url_is_refused(self, watchdog: SecurityWatchdog, registry: PromotionRegistry):
		"""Navigation to any T2 channel must fail even if a page links to it."""
		blocked = [str(c.url) for c in registry.channels if c.access_tier is AccessTier.BLOCKED]
		assert blocked, 'registry should contain blocked channels'
		allowed_anyway = [url for url in blocked if watchdog._is_url_allowed(url)]
		assert allowed_anyway == []

	def test_collectible_browser_targets_are_reachable(self, watchdog: SecurityWatchdog, registry: PromotionRegistry):
		"""The allowlist must not be so tight that the run cannot do its job."""
		for channel in registry.collectible(AccessTier.BROWSER_REQUIRED):
			assert watchdog._is_url_allowed(str(channel.url)), f'{channel.url} should be reachable'

	def test_offsite_navigation_is_refused(self, watchdog: SecurityWatchdog):
		"""Anything outside the run's target set is out of scope by construction."""
		assert watchdog._is_url_allowed('https://malicious.example.org/') is False

	def test_subdomain_of_blocked_host_is_refused(self, watchdog: SecurityWatchdog):
		"""Blocking linkedin.com must also block its regional and API subdomains."""
		assert watchdog._is_url_allowed('https://kr.linkedin.com/company/dramabox') is False
		assert watchdog._is_url_allowed('https://www.linkedin.com/company/dramabox') is False
