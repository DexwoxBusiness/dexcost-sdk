"""Client-scoped caller assertions, never provider-verified invoice evidence."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Literal
from urllib.parse import urlsplit
from weakref import WeakKeyDictionary, ref

ProviderBillingProvider = Literal["cohere", "google"]
ProviderBillingTier = Literal["paid", "free", "unknown"]
_bindings: WeakKeyDictionary[Any, tuple[str, str, str, object]] = WeakKeyDictionary()


def _owner(client: Any, provider: str) -> Any:
    # Native resources share the owning client's transport, not a global key.
    if provider == "google":
        return getattr(client, "_api_client", client)
    if provider == "cohere":
        return getattr(client, "_client_wrapper", client)
    return client


def _direct_endpoint(endpoint: str, provider: str) -> str | None:
    try:
        url = urlsplit(endpoint)
        hosts = {
            "cohere": {"api.cohere.com", "api.cohere.ai"},
            "google": {"generativelanguage.googleapis.com"},
        }
        if (
            url.scheme != "https"
            or url.hostname not in hosts.get(provider, set())
            or url.port not in (None, 443)
            or url.username
            or url.password
            or url.query
            or url.fragment
            or url.path not in ("", "/")
        ):
            return None
        return f"https://{url.hostname}"
    except (TypeError, ValueError):
        return None


def bind_provider_billing(
    client: Any,
    *,
    provider: ProviderBillingProvider,
    tier: ProviderBillingTier,
    endpoint: str,
) -> Callable[[], None]:
    """Attest a native client's direct account tier for public-gross estimates.

    The caller must know the client's account is paid PAYG (not trial/free,
    private deployment or negotiated billing). No credentials are inspected.
    Bindings are weak and last until unbound, replaced, or the client is freed.
    Return an idempotent unbind callback; an old callback cannot erase a newer
    binding. Rebind after changing credentials/account. In-flight streams can
    retain their admission snapshot; unbind before starting further requests.
    HTTP-only instrumentation cannot inherit this assertion.
    """
    route = _direct_endpoint(endpoint, provider)
    if route is None or tier not in ("paid", "free", "unknown"):
        raise ValueError("A supported direct endpoint and explicit billing tier are required")
    owner = _owner(client, provider)
    identity = object()
    owner_ref = ref(owner)
    _bindings[owner] = (provider, tier, route, identity)

    def unbind() -> None:
        current = owner_ref()
        binding = _bindings.get(current) if current is not None else None
        if current is not None and binding is not None and binding[3] is identity:
            del _bindings[current]

    return unbind


def _has_paid_billing(client: Any, provider: str, endpoint: str) -> bool:
    try:
        binding = _bindings.get(_owner(client, provider))
        return bool(
            binding and binding[:3] == (provider, "paid", _direct_endpoint(endpoint, provider))
        )
    except (TypeError, ValueError):
        return False
