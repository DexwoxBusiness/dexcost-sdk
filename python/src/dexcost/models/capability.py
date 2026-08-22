"""Provider-neutral identity for tools, skills, workflows, and extensions."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Literal, TypeAlias, cast

CapabilityKind: TypeAlias = Literal["tool", "skill", "workflow", "extension", "other"]
CapabilitySource: TypeAlias = Literal[
    "built_in", "project", "user", "plugin", "marketplace", "remote", "other"
]
CapabilityInvocation: TypeAlias = Literal[
    "explicit", "automatic", "nested", "scheduled", "remote", "other"
]

_CANONICAL_NAME = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")
_KINDS = frozenset({"tool", "skill", "workflow", "extension", "other"})
_SOURCES = frozenset(
    {"built_in", "project", "user", "plugin", "marketplace", "remote", "other"}
)
_INVOCATIONS = frozenset(
    {"explicit", "automatic", "nested", "scheduled", "remote", "other"}
)
_FIELDS = frozenset(
    {"name", "kind", "namespace", "version", "source", "source_id", "invocation"}
)


@dataclass(frozen=True)
class CapabilityIdentity:
    """Stable cross-provider identity for the reusable capability causing work."""

    name: str
    kind: CapabilityKind
    namespace: str | None = None
    version: str | None = None
    source: CapabilitySource | None = None
    source_id: str | None = None
    invocation: CapabilityInvocation | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or _CANONICAL_NAME.fullmatch(self.name) is None:
            raise ValueError("capability name must be a canonical lowercase identifier")
        if not isinstance(self.kind, str) or self.kind not in _KINDS:
            raise ValueError(f"unsupported capability kind {self.kind!r}")
        if self.namespace is not None and (
            not isinstance(self.namespace, str)
            or _CANONICAL_NAME.fullmatch(self.namespace) is None
        ):
            raise ValueError("capability namespace must be a canonical lowercase identifier")
        if self.version is not None and (
            not isinstance(self.version, str) or not 1 <= len(self.version) <= 128
        ):
            raise ValueError("capability version must contain 1 to 128 characters")
        if self.source is not None and (
            not isinstance(self.source, str) or self.source not in _SOURCES
        ):
            raise ValueError(f"unsupported capability source {self.source!r}")
        if self.source_id is not None:
            if not isinstance(self.source_id, str) or not 1 <= len(self.source_id) <= 256:
                raise ValueError("capability source_id must contain 1 to 256 characters")
            if self.source is None:
                raise ValueError("capability source_id requires source")
        if self.invocation is not None and (
            not isinstance(self.invocation, str) or self.invocation not in _INVOCATIONS
        ):
            raise ValueError(f"unsupported capability invocation {self.invocation!r}")

    def to_dict(self) -> dict[str, str]:
        """Serialize only contract fields, omitting absent optionals."""
        result = {"name": self.name, "kind": self.kind}
        for key in ("namespace", "version", "source", "source_id", "invocation"):
            value = getattr(self, key)
            if value is not None:
                result[key] = value
        return result

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> CapabilityIdentity:
        """Parse a strict durable or wire representation."""
        if not isinstance(value, dict):
            raise TypeError("capability identity must be a dictionary")
        unknown = set(value) - _FIELDS
        if unknown:
            raise ValueError(f"unknown capability identity fields: {sorted(unknown)}")
        try:
            name = value["name"]
            kind = value["kind"]
        except KeyError as exc:
            raise ValueError(f"capability identity requires {exc.args[0]}") from exc
        return cls(
            name=cast(str, name),
            kind=cast(CapabilityKind, kind),
            namespace=cast(str | None, value.get("namespace")),
            version=cast(str | None, value.get("version")),
            source=cast(CapabilitySource | None, value.get("source")),
            source_id=cast(str | None, value.get("source_id")),
            invocation=cast(CapabilityInvocation | None, value.get("invocation")),
        )


__all__ = [
    "CapabilityIdentity",
    "CapabilityInvocation",
    "CapabilityKind",
    "CapabilitySource",
]
