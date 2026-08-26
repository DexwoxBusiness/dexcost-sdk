"""Generate or verify the Python vNext contract-freeze snapshots.

Only implementation-derived files are generated here.  Human-reviewed JSON
schemas, golden cases, capability decisions, and exclusion rationale remain
ordinary version-controlled artifacts and are covered by the manifest digest.
"""

from __future__ import annotations

import argparse
import hashlib
import inspect
import json
import re
import sqlite3
import sys
import tempfile
import types
import typing
from collections.abc import Mapping, Sequence
from enum import Enum
from pathlib import Path

PYTHON_ROOT = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = PYTHON_ROOT.parent
CONTRACT_ROOT = REPOSITORY_ROOT / "contracts" / "python-vnext" / "v1"
SOURCE_ROOT = PYTHON_ROOT / "src"

# Running this file directly must inspect the checkout, never an older globally
# installed wheel with the same package name.
sys.path.insert(0, str(SOURCE_ROOT))

_ADDRESS = re.compile(r"0x[0-9a-fA-F]+")
_GENERATED = frozenset({"public-api.json", "storage-migrations.json", "manifest.json"})


def _canonical_artifact_bytes(relative: str, data: bytes) -> bytes:
    """Normalize text checkout line endings before hashing frozen artifacts."""

    return data.replace(b"\r\n", b"\n") if relative.endswith((".json", ".md")) else data


def _json_bytes(value: object) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode()


def _stable_signature(value: object) -> str | None:
    if inspect.isclass(value) and issubclass(value, Enum):
        # EnumMeta's introspected constructor changes across CPython versions;
        # the stable public contract is lookup by one value.
        return "(value)"
    try:
        signature = str(inspect.signature(value))
    except (TypeError, ValueError):
        return None
    if _ADDRESS.search(signature):
        raise RuntimeError(f"unstable object address in public signature: {signature}")
    return signature


def _kind(value: object) -> str:
    if typing.get_origin(value) is not None:
        return "type_alias"
    if inspect.isclass(value):
        # Public aliases such as AttributionUsageMetricV3 = str are contract
        # types, not snapshots of CPython's version-specific built-in methods.
        return "type_alias" if value.__module__ == "builtins" else "class"
    if inspect.iscoroutinefunction(value):
        return "async_function"
    if inspect.isfunction(value) or inspect.ismethod(value) or inspect.isbuiltin(value):
        return "function"
    return "constant"


def _module(value: object, kind: str) -> str:
    """Return a stable module label for version-sensitive typing objects."""

    if kind == "type_alias" and typing.get_origin(value) in (
        types.UnionType,
        typing.Union,
    ):
        # CPython 3.10-3.13 expose ``X | Y`` through ``types.UnionType``;
        # CPython 3.14 normalizes the same public alias through ``typing.Union``.
        return "types"
    return getattr(value, "__module__", type(value).__module__)


def _public_members(cls: type[object]) -> list[dict[str, object]]:
    members: list[dict[str, object]] = []
    for name, raw in sorted(vars(cls).items()):
        if name.startswith("_"):
            continue
        member: object = raw
        member_kind = "attribute"
        if isinstance(raw, classmethod):
            member = raw.__func__
            member_kind = "class_method"
        elif isinstance(raw, staticmethod):
            member = raw.__func__
            member_kind = "static_method"
        elif isinstance(raw, property):
            member = raw.fget if raw.fget is not None else raw
            member_kind = "property"
        elif inspect.isfunction(raw):
            member_kind = "method"
        signature = _stable_signature(member)
        if signature is None and member_kind == "attribute":
            continue
        entry: dict[str, object] = {"name": name, "kind": member_kind}
        if signature is not None:
            entry["signature"] = signature
        members.append(entry)
    return members


def public_api_snapshot() -> dict[str, object]:
    import dexcost

    exports: list[dict[str, object]] = []
    for name in dexcost.__all__:
        value = getattr(dexcost, name)
        kind = _kind(value)
        entry: dict[str, object] = {
            "name": name,
            "kind": kind,
            "module": _module(value, kind),
        }
        signature = None if entry["kind"] == "type_alias" else _stable_signature(value)
        if signature is not None:
            entry["signature"] = signature
        if entry["kind"] == "class":
            entry["members"] = _public_members(value)
        exports.append(entry)
    return {
        "freeze_version": "python-vnext-v1",
        "package": "dexcost",
        "package_version": dexcost.__version__,
        "python_requires": ">=3.10",
        "exports": exports,
    }


def _normalize_sql(value: str | None) -> str | None:
    if value is None:
        return None
    return " ".join(value.split())


def _sqlite_schema() -> list[dict[str, object]]:
    from dexcost.storage.sqlite import SQLiteStorage

    with tempfile.TemporaryDirectory(prefix="dexcost-contract-") as directory:
        database = Path(directory) / "freeze.db"
        storage = SQLiteStorage(db_path=database)
        storage.close()
        connection = sqlite3.connect(database)
        try:
            rows = connection.execute(
                "SELECT type, name, tbl_name, sql FROM sqlite_master "
                "WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"
            ).fetchall()
            result: list[dict[str, object]] = []
            for object_type, name, table_name, sql in rows:
                entry: dict[str, object] = {
                    "type": object_type,
                    "name": name,
                    "table": table_name,
                    "sql": _normalize_sql(sql),
                }
                if object_type == "table":
                    columns = connection.execute(
                        f'PRAGMA table_info("{name}")'
                    ).fetchall()
                    entry["columns"] = [
                        {
                            "position": column[0],
                            "name": column[1],
                            "type": column[2],
                            "not_null": bool(column[3]),
                            "default": column[4],
                            "primary_key_position": column[5],
                        }
                        for column in columns
                    ]
                result.append(entry)
            return result
        finally:
            connection.close()


def storage_snapshot() -> dict[str, object]:
    from dexcost.storage import migrations

    registered: list[dict[str, object]] = []
    for (from_version, to_version), function in sorted(migrations._SQLITE_MIGRATIONS.items()):
        source = inspect.getsource(function).encode()
        registered.append(
            {
                "from": from_version,
                "to": to_version,
                "function": function.__name__,
                "summary": inspect.getdoc(function),
                "source_sha256": hashlib.sha256(source).hexdigest(),
            }
        )
    return {
        "freeze_version": "python-vnext-v1",
        "engine": "sqlite",
        "target_schema_version": migrations.TARGET_SCHEMA_VERSION,
        "migrations": registered,
        "schema": _sqlite_schema(),
    }


def _artifact_entries(overrides: Mapping[str, bytes]) -> list[dict[str, object]]:
    entries: list[dict[str, object]] = []
    relative_paths = {
        path.relative_to(CONTRACT_ROOT).as_posix()
        for path in CONTRACT_ROOT.rglob("*")
        if path.is_file()
    } | set(overrides)
    for relative in sorted(relative_paths):
        if relative == "manifest.json":
            continue
        data = overrides.get(relative)
        if data is None:
            data = (CONTRACT_ROOT / relative).read_bytes()
        data = _canonical_artifact_bytes(relative, data)
        entries.append(
            {
                "path": relative,
                "byte_size": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
            }
        )
    return entries


def manifest_snapshot(overrides: Mapping[str, bytes]) -> dict[str, object]:
    inventory_path = CONTRACT_ROOT / "wire-schema-inventory.json"
    referenced: list[dict[str, object]] = []
    if inventory_path.is_file():
        inventory = json.loads(inventory_path.read_text(encoding="utf-8"))
        paths = {
            item["repository_path"]
            for item in inventory["contracts"]
            if item.get("repository_path", "").startswith("fixtures/")
        }
        for relative in sorted(paths):
            path = (REPOSITORY_ROOT / relative).resolve()
            path.relative_to(REPOSITORY_ROOT.resolve())
            data = _canonical_artifact_bytes(relative, path.read_bytes())
            referenced.append(
                {
                    "path": relative,
                    "byte_size": len(data),
                    "sha256": hashlib.sha256(data).hexdigest(),
                }
            )
    return {
        "schema_version": "1",
        "freeze_id": "python-vnext-v1",
        "sequence_owner": "python",
        "catalog_sdk_contract": 1,
        "attribution_contract": "3.2.0",
        "business_attribution_contract": "1.1.0",
        "classification_states": ["implemented", "intentionally_excluded", "required"],
        "artifacts": _artifact_entries(overrides),
        "referenced_artifacts": referenced,
    }


def generated_payloads() -> dict[str, bytes]:
    payloads = {
        "public-api.json": _json_bytes(public_api_snapshot()),
        "storage-migrations.json": _json_bytes(storage_snapshot()),
    }
    payloads["manifest.json"] = _json_bytes(manifest_snapshot(payloads))
    return payloads


def write() -> None:
    CONTRACT_ROOT.mkdir(parents=True, exist_ok=True)
    payloads = generated_payloads()
    for relative, data in payloads.items():
        (CONTRACT_ROOT / relative).write_bytes(data)


def check() -> list[str]:
    problems: list[str] = []
    if not CONTRACT_ROOT.is_dir():
        return [f"contract root does not exist: {CONTRACT_ROOT}"]
    payloads = generated_payloads()
    for relative in sorted(_GENERATED):
        path = CONTRACT_ROOT / relative
        if not path.is_file():
            problems.append(f"missing generated artifact: {relative}")
        elif path.read_bytes() != payloads[relative]:
            problems.append(f"generated artifact drift: {relative}")
    return problems


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--write", action="store_true", help="refresh generated snapshots")
    action.add_argument("--check", action="store_true", help="fail on contract drift")
    arguments = parser.parse_args(argv)
    if arguments.write:
        write()
        return 0
    problems = check()
    if problems:
        for problem in problems:
            print(problem, file=sys.stderr)
        print(
            "run python scripts/freeze_contract.py --write after reviewed changes",
            file=sys.stderr,
        )
        return 1
    print(f"contract freeze verified: {CONTRACT_ROOT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
