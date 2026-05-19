"""Adapters for automatic cost tracking of HTTP, browser, and compute services.

This package provides adapters that intercept or wrap outgoing
requests and automatically record cost events.
"""

from dexcost.adapters.aws_lambda import get_supported_regions, lambda_cost
from dexcost.adapters.browser import track_browser
from dexcost.adapters.http import register_domain_rate, track_http, untrack_http

__all__ = [
    "get_supported_regions",
    "lambda_cost",
    "register_domain_rate",
    "track_browser",
    "track_http",
    "untrack_http",
]
