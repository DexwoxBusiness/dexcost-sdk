"""Exercise a signed online or air-gapped Catalog Release with the Python SDK."""

from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path

from dexcost.catalog_releases import (
    CATALOG_KINDS,
    CatalogReleaseClient,
    CatalogReleaseStore,
    CatalogValidationError,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--store",
        type=Path,
        required=True,
        help="catalog store; corruption probes must use a disposable copy",
    )
    parser.add_argument("--key-id", required=True)
    parser.add_argument("--public-key", required=True)
    parser.add_argument("--expect-release", required=True)
    parser.add_argument("--endpoint")
    parser.add_argument("--channel", choices=("stable", "canary"), default="stable")
    parser.add_argument("--import-bundle", type=Path)
    parser.add_argument("--reject-corrupt-bundle", type=Path)
    parser.add_argument(
        "--reject-expired-bundle",
        type=Path,
        help="reject an expired signed bundle and prove that the LKG is preserved",
    )
    parser.add_argument(
        "--corrupt-active-store",
        action="store_true",
        help="DESTRUCTIVE: corrupt only the active slot in a disposable store copy",
    )
    parser.add_argument("--export-bundle", type=Path)
    return parser


def _corrupt_active_manifest(path: Path, channel: str) -> None:
    """Corrupt only the active release manifest in a disposable probe database."""
    connection = sqlite3.connect(path)
    try:
        row = connection.execute(
            "SELECT active_release_sequence, previous_release_sequence "
            "FROM sdk_catalog_state WHERE channel=?",
            (channel,),
        ).fetchone()
        if row is None or row[0] is None or row[1] is None:
            raise RuntimeError("corruption probe requires both active and previous releases")
        cursor = connection.execute(
            "UPDATE sdk_catalog_releases SET manifest_json=? "
            "WHERE release_sequence=?",
            (b"{", row[0]),
        )
        if cursor.rowcount != 1:
            raise RuntimeError("corruption probe did not target exactly one active release")
        connection.commit()
    finally:
        connection.close()


def main() -> None:
    args = _parser().parse_args()
    operations = [
        args.endpoint,
        args.import_bundle,
        args.reject_corrupt_bundle,
        args.reject_expired_bundle,
        True if args.corrupt_active_store else None,
    ]
    if sum(operation is not None for operation in operations) != 1:
        raise SystemExit(
            "specify exactly one of --endpoint, --import-bundle, or "
            "--reject-corrupt-bundle, --reject-expired-bundle, or "
            "--corrupt-active-store"
        )
    if args.corrupt_active_store:
        _corrupt_active_manifest(args.store, args.channel)
    store = CatalogReleaseStore(
        args.store,
        trusted_keys={args.key_id: args.public_key},
        require_signature=True,
    )
    try:
        if args.corrupt_active_store:
            snapshot = store.best_available(args.channel)
            if snapshot is None or snapshot.source != "previous":
                raise RuntimeError(
                    "corrupt active release did not fall back to the previous release"
                )
            status = "active_corrupt_previous_preserved"
        elif args.reject_corrupt_bundle is not None:
            value = json.loads(args.reject_corrupt_bundle.read_bytes())
            encoded = value["artifacts_base64url"]["gpu_prices"]
            value["artifacts_base64url"]["gpu_prices"] = (
                ("A" if encoded[0] != "A" else "B") + encoded[1:]
            )
            try:
                store.import_bundle(
                    json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
                )
            except CatalogValidationError as exc:
                snapshot = store.best_available(args.channel)
                if snapshot is None:
                    raise RuntimeError(
                        "corrupt import discarded the last-known-good release"
                    ) from exc
                status = "corrupt_rejected_lkg_preserved"
            else:
                raise RuntimeError("corrupt catalog bundle was accepted")
        elif args.reject_expired_bundle is not None:
            try:
                store.import_bundle(args.reject_expired_bundle.read_bytes())
            except CatalogValidationError as exc:
                if "expired" not in str(exc).lower():
                    raise RuntimeError(
                        "expired catalog bundle failed for an unexpected reason"
                    ) from exc
                snapshot = store.best_available(args.channel)
                if snapshot is None:
                    raise RuntimeError(
                        "expired import discarded the last-known-good release"
                    ) from exc
                status = "expired_rejected_lkg_preserved"
            else:
                raise RuntimeError("expired catalog bundle was accepted")
        elif args.import_bundle is not None:
            snapshot = store.import_bundle(args.import_bundle.read_bytes())
            status = "imported"
        else:
            result = CatalogReleaseClient(
                args.endpoint,
                store,
                channel=args.channel,
            ).refresh()
            if result.snapshot is None:
                raise RuntimeError(f"catalog refresh returned no snapshot: {result.error}")
            snapshot = result.snapshot
            status = result.status
        if snapshot.manifest.release_id != args.expect_release:
            raise RuntimeError(
                f"expected {args.expect_release}, received {snapshot.manifest.release_id}"
            )
        if set(snapshot.artifacts) != set(CATALOG_KINDS):
            raise RuntimeError("catalog probe did not activate exactly seven families")
        if args.export_bundle is not None:
            args.export_bundle.write_bytes(
                store.export_bundle(channel=args.channel, source="active")
            )
        print(
            json.dumps(
                {
                    "implementation": "python",
                    "status": status,
                    "release_id": snapshot.manifest.release_id,
                    "release_sequence": snapshot.manifest.release_sequence,
                    "channel": snapshot.manifest.channel,
                    "source": snapshot.source,
                    "stale": snapshot.stale,
                    "signature_key_ids": [
                        signature["key_id"] for signature in snapshot.manifest.signatures
                    ],
                    "artifact_sha256": {
                        kind: snapshot.manifest.artifacts[kind].sha256
                        for kind in CATALOG_KINDS
                    },
                },
                sort_keys=True,
            )
        )
    finally:
        store.close()


if __name__ == "__main__":
    main()
