"""Tests for the service catalog cost extraction engine."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from dexcost.service_catalog import CostExtractionResult, ServiceCatalog, ServiceEntry


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def catalog() -> ServiceCatalog:
    """Load the bundled service catalog."""
    return ServiceCatalog()


# ---------------------------------------------------------------------------
# Loading tests
# ---------------------------------------------------------------------------


class TestCatalogLoading:
    """Catalog loads bundled JSON correctly."""

    def test_loads_bundled_json(self, catalog: ServiceCatalog) -> None:
        entries = catalog.entries
        assert len(entries) > 0
        assert "tavily_search" in entries
        assert "pinecone_query" in entries

    def test_entries_have_required_fields(self, catalog: ServiceCatalog) -> None:
        for key, entry in catalog.entries.items():
            assert entry.key == key
            assert entry.display_name
            assert entry.domains
            assert entry.category
            assert entry.pricing_model
            assert entry.cost_extraction


# ---------------------------------------------------------------------------
# Domain matching tests
# ---------------------------------------------------------------------------


class TestDomainMatching:
    """Catalog matches URLs by domain and endpoint."""

    def test_exact_domain_match(self, catalog: ServiceCatalog) -> None:
        entry = catalog.lookup("https://api.tavily.com/search")
        assert entry is not None
        assert entry.key == "tavily_search"

    def test_wildcard_domain_match(self, catalog: ServiceCatalog) -> None:
        entry = catalog.lookup(
            "https://my-index-abc123.svc.us-east1-gcp.pinecone.io/query"
        )
        assert entry is not None
        assert entry.key == "pinecone_query"

    def test_wildcard_exact_suffix(self, catalog: ServiceCatalog) -> None:
        """*.pinecone.io should match just 'index.pinecone.io'."""
        entry = catalog.lookup("https://index.pinecone.io/query")
        assert entry is not None
        assert entry.key == "pinecone_query"

    def test_endpoint_matching_geocode(self, catalog: ServiceCatalog) -> None:
        entry = catalog.lookup(
            "https://maps.googleapis.com/maps/api/geocode/json?address=foo"
        )
        assert entry is not None
        assert entry.key == "google_maps_geocode"

    def test_endpoint_matching_directions(self, catalog: ServiceCatalog) -> None:
        entry = catalog.lookup(
            "https://maps.googleapis.com/maps/api/directions/json?origin=a&dest=b"
        )
        assert entry is not None
        assert entry.key == "google_maps_directions"

    def test_endpoint_matching_places(self, catalog: ServiceCatalog) -> None:
        entry = catalog.lookup(
            "https://maps.googleapis.com/maps/api/place/nearbysearch/json"
        )
        assert entry is not None
        assert entry.key == "google_maps_places"

    def test_unknown_domain_returns_none(self, catalog: ServiceCatalog) -> None:
        entry = catalog.lookup("https://unknown-service.example.com/v1/api")
        assert entry is None

    def test_multiple_domains_for_service(self, catalog: ServiceCatalog) -> None:
        """Services with multiple exact domains should match any of them."""
        entry_person = catalog.lookup("https://person.clearbit.com/v2/people")
        entry_company = catalog.lookup("https://company.clearbit.com/v2/companies")
        assert entry_person is not None
        assert entry_company is not None
        assert entry_person.key == "clearbit_enrichment"
        assert entry_company.key == "clearbit_enrichment"


# ---------------------------------------------------------------------------
# Cost extraction tests
# ---------------------------------------------------------------------------


class TestCostExtractionResponseBody:
    """Extraction type: response_body."""

    def test_tavily_credits_from_body(self, catalog: ServiceCatalog) -> None:
        entry = catalog.lookup("https://api.tavily.com/search")
        assert entry is not None

        result = catalog.extract_cost(
            entry,
            response_headers={},
            response_body={"usage": {"credits": 3}, "results": []},
        )
        assert result is not None
        # 3 credits * $0.008/credit = $0.024
        assert result.amount == Decimal("3") * Decimal("0.008")
        assert result.confidence == "computed"
        assert result.service_name == "Tavily Search"
        assert result.pricing_source == "service_catalog"

    def test_nested_body_path(self, catalog: ServiceCatalog) -> None:
        """Dotted paths like 'data.stats.computeUnits' are resolved."""
        entry = catalog.lookup("https://api.apify.com/v2/acts/run")
        assert entry is not None

        result = catalog.extract_cost(
            entry,
            response_headers={},
            response_body={"data": {"stats": {"computeUnits": 2.5}}},
        )
        assert result is not None
        # 2.5 * $0.25 = $0.625
        assert result.amount == Decimal("2.5") * Decimal("0.25")

    def test_fallback_credits_when_body_missing(self, catalog: ServiceCatalog) -> None:
        """When response body is None, fallback_credits is used."""
        entry = catalog.lookup("https://api.tavily.com/search")
        assert entry is not None

        result = catalog.extract_cost(
            entry,
            response_headers={},
            response_body=None,
        )
        assert result is not None
        # fallback 1 credit * $0.008 = $0.008
        assert result.amount == Decimal("1") * Decimal("0.008")
        assert result.confidence == "estimated"

    def test_fallback_credits_when_path_missing(self, catalog: ServiceCatalog) -> None:
        """When the path doesn't exist in body, fallback_credits is used."""
        entry = catalog.lookup("https://api.tavily.com/search")
        assert entry is not None

        result = catalog.extract_cost(
            entry,
            response_headers={},
            response_body={"unrelated_field": 42},
        )
        assert result is not None
        assert result.amount == Decimal("1") * Decimal("0.008")
        assert result.confidence == "estimated"


class TestCostExtractionResponseHeader:
    """Extraction type: response_header."""

    def test_pinecone_read_units_from_body(self, catalog: ServiceCatalog) -> None:
        entry = catalog.lookup(
            "https://my-index.svc.us-east1-gcp.pinecone.io/query"
        )
        assert entry is not None

        result = catalog.extract_cost(
            entry,
            response_headers={},
            response_body={"usage": {"readUnits": 5}},
        )
        assert result is not None
        # 5 * $0.000016 = $0.000080
        assert result.amount == Decimal("5") * Decimal("0.000016")
        assert result.confidence == "computed"

    def test_body_missing_returns_none(self, catalog: ServiceCatalog) -> None:
        """Pinecone uses response_body extraction; None body returns None."""
        entry = catalog.lookup(
            "https://my-index.svc.us-east1-gcp.pinecone.io/query"
        )
        assert entry is not None

        result = catalog.extract_cost(
            entry,
            response_headers={},
            response_body=None,
        )
        assert result is None

    def test_case_insensitive_header(self, catalog: ServiceCatalog) -> None:
        """Header lookup should be case-insensitive."""
        entry = catalog.lookup("https://app.scrapingbee.com/api/v1")
        assert entry is not None

        result = catalog.extract_cost(
            entry,
            response_headers={"spb-cost": "10"},
            response_body=None,
        )
        assert result is not None
        # 10 credits * $0.000327/credit = $0.003270
        assert result.amount == Decimal("10") * Decimal("0.000327")


class TestCostExtractionEndpointMatch:
    """Extraction type: endpoint_match."""

    def test_google_maps_geocode_fixed_cost(self, catalog: ServiceCatalog) -> None:
        entry = catalog.lookup(
            "https://maps.googleapis.com/maps/api/geocode/json"
        )
        assert entry is not None

        result = catalog.extract_cost(
            entry,
            response_headers={},
            response_body=None,
        )
        assert result is not None
        assert result.amount == Decimal("0.005")
        assert result.confidence == "exact"

    def test_sendgrid_endpoint_match(self, catalog: ServiceCatalog) -> None:
        entry = catalog.lookup("https://api.sendgrid.com/v3/mail/send")
        assert entry is not None

        result = catalog.extract_cost(
            entry,
            response_headers={},
            response_body=None,
        )
        assert result is not None
        assert result.amount == Decimal("0.00035")


class TestCostExtractionFixed:
    """Extraction type: fixed."""

    def test_exa_fixed_cost(self, catalog: ServiceCatalog) -> None:
        entry = catalog.lookup("https://api.exa.ai/search")
        assert entry is not None

        result = catalog.extract_cost(
            entry,
            response_headers={},
            response_body=None,
        )
        assert result is not None
        assert result.amount == Decimal("0.007")
        assert result.confidence == "exact"

    def test_serpapi_fixed_cost(self, catalog: ServiceCatalog) -> None:
        entry = catalog.lookup("https://serpapi.com/search")
        assert entry is not None

        result = catalog.extract_cost(
            entry,
            response_headers={},
            response_body=None,
        )
        assert result is not None
        assert result.amount == Decimal("0.01")


# ---------------------------------------------------------------------------
# Transform tests
# ---------------------------------------------------------------------------


class TestTransforms:
    """Named transforms are applied correctly."""

    def test_ms_to_seconds(self, catalog: ServiceCatalog) -> None:
        """E2B: duration_ms is converted to seconds and multiplied by rate."""
        entry = catalog.lookup("https://api.e2b.dev/sandbox")
        assert entry is not None

        result = catalog.extract_cost(
            entry,
            response_headers={},
            response_body={"duration_ms": 5000},
        )
        assert result is not None
        # 5000ms -> 5s * $0.000014/s = $0.000070
        assert result.amount == Decimal("5000") / Decimal("1000") * Decimal("0.000014")

    def test_ms_to_minutes(self, catalog: ServiceCatalog) -> None:
        """Browserbase: duration_ms is converted to minutes."""
        entry = catalog.lookup("https://api.browserbase.com/sessions")
        assert entry is not None

        result = catalog.extract_cost(
            entry,
            response_headers={},
            response_body={"duration_ms": 120000},
        )
        assert result is not None
        # 120000ms -> 2min * $0.002/min = $0.004
        assert result.amount == Decimal("120000") / Decimal("60000") * Decimal("0.002")

    def test_stripe_fee(self, catalog: ServiceCatalog) -> None:
        """Stripe: amount (cents) -> 2.9% + $0.30."""
        entry = catalog.lookup("https://api.stripe.com/v1/charges")
        assert entry is not None

        result = catalog.extract_cost(
            entry,
            response_headers={},
            response_body={"amount": 10000},  # $100.00
        )
        assert result is not None
        # $100 * 2.9% + $0.30 = $3.20
        expected = Decimal("10000") / Decimal("100") * Decimal("0.029") + Decimal("0.30")
        assert result.amount == expected


# ---------------------------------------------------------------------------
# User override tests
# ---------------------------------------------------------------------------


class TestOverrides:
    """User overrides take precedence over catalog rates."""

    def test_override_takes_precedence(self, catalog: ServiceCatalog) -> None:
        entry = catalog.lookup("https://api.tavily.com/search")
        assert entry is not None

        catalog.register_override("tavily_search", Decimal("0.05"), per="request")

        result = catalog.extract_cost(
            entry,
            response_headers={},
            response_body={"api_credits_used": 3},
        )
        assert result is not None
        assert result.amount == Decimal("0.05")
        assert result.pricing_source == "user_override"

    def test_catalog_version_changes_with_override(self, catalog: ServiceCatalog) -> None:
        version_before = catalog.catalog_version
        catalog.register_override("tavily_search", Decimal("0.05"))
        version_after = catalog.catalog_version
        assert version_before != version_after


# ---------------------------------------------------------------------------
# Catalog version tests
# ---------------------------------------------------------------------------


class TestCatalogVersion:
    """Catalog version is a stable hash."""

    def test_version_is_string(self, catalog: ServiceCatalog) -> None:
        assert isinstance(catalog.catalog_version, str)
        assert len(catalog.catalog_version) == 16

    def test_version_is_deterministic(self) -> None:
        cat1 = ServiceCatalog()
        cat2 = ServiceCatalog()
        assert cat1.catalog_version == cat2.catalog_version
