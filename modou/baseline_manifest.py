"""Reproducible baseline manifests with a future-proof signing boundary.

The first implementation deliberately uses a plain SHA-256 attestation.  It
does not pretend that an unkeyed digest proves identity.  Callers may register
a real signer later without changing the manifest payload or verification API.
"""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Protocol


SCHEMA_VERSION = "baseline-manifest-v1"
ATTESTATION_VERSION = "baseline-attestation-v1"


class BaselineManifestError(ValueError):
    """Raised when a baseline cannot be built or verified safely."""


class ManifestSigner(Protocol):
    """Extension point for a managed signing service or existing key store."""

    algorithm: str
    key_id: str

    def sign(self, payload: bytes) -> str: ...

    def verify(self, payload: bytes, signature: str) -> bool: ...


def canonical_json(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _safe_relative(value: str) -> PurePosixPath:
    path = PurePosixPath(value)
    if path.is_absolute() or not path.parts or ".." in path.parts:
        raise BaselineManifestError(f"unsafe manifest path: {value}")
    return path


def _load_json(path: Path) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise BaselineManifestError(f"cannot read JSON evidence: {path.name}") from exc


def _lookup(value: object, dotted: str) -> object:
    current = value
    for part in dotted.split("."):
        if not isinstance(current, dict) or part not in current:
            raise BaselineManifestError(f"missing declared fact: {dotted}")
        current = current[part]
    return current


def build_manifest(*, root: Path, config: dict) -> dict:
    """Build a deterministic payload from an explicit, closed configuration."""
    root = root.resolve(strict=True)
    if config.get("schema_version") != "baseline-config-v1":
        raise BaselineManifestError("unsupported baseline configuration")
    allowed = {"schema_version", "baseline_id", "critical_paths", "evidence"}
    if set(config) - allowed:
        raise BaselineManifestError("baseline configuration has unsupported fields")
    baseline_id = str(config.get("baseline_id") or "").strip()
    if not baseline_id or len(baseline_id) > 100:
        raise BaselineManifestError("baseline_id is required")

    paths = config.get("critical_paths")
    if not isinstance(paths, list) or not paths or not all(isinstance(x, str) for x in paths):
        raise BaselineManifestError("critical_paths must be a non-empty string array")
    if len(paths) != len(set(paths)):
        raise BaselineManifestError("critical_paths contains duplicates")
    files: list[dict] = []
    for relative in sorted(paths):
        safe = _safe_relative(relative)
        path = (root / Path(*safe.parts)).resolve(strict=True)
        try:
            path.relative_to(root)
        except ValueError as exc:
            raise BaselineManifestError(f"critical path escapes root: {relative}") from exc
        if not path.is_file() or path.is_symlink():
            raise BaselineManifestError(f"critical path is not a regular file: {relative}")
        blob = path.read_bytes()
        files.append({"path": safe.as_posix(), "bytes": len(blob),
                      "sha256": sha256_bytes(blob)})

    evidence_config = config.get("evidence") or []
    if not isinstance(evidence_config, list):
        raise BaselineManifestError("evidence must be an array")
    evidence: list[dict] = []
    seen_ids: set[str] = set()
    for declaration in evidence_config:
        if not isinstance(declaration, dict) or set(declaration) != {"id", "path", "facts"}:
            raise BaselineManifestError("evidence declaration shape is invalid")
        evidence_id = str(declaration["id"])
        if not evidence_id or evidence_id in seen_ids:
            raise BaselineManifestError("evidence ids must be unique and non-empty")
        seen_ids.add(evidence_id)
        safe = _safe_relative(str(declaration["path"]))
        path = (root / Path(*safe.parts)).resolve(strict=True)
        try:
            path.relative_to(root)
        except ValueError as exc:
            raise BaselineManifestError(f"evidence path escapes root: {safe}") from exc
        if not path.is_file() or path.is_symlink():
            raise BaselineManifestError(f"evidence is not a regular file: {safe}")
        payload = _load_json(path)
        facts = declaration["facts"]
        if not isinstance(facts, list) or not facts or not all(isinstance(x, str) for x in facts):
            raise BaselineManifestError("evidence facts must be a non-empty string array")
        selected = {dotted: _lookup(payload, dotted) for dotted in sorted(set(facts))}
        evidence.append({
            "id": evidence_id,
            "path": safe.as_posix(),
            "sha256": sha256_bytes(path.read_bytes()),
            "facts": selected,
        })

    return {
        "schema_version": SCHEMA_VERSION,
        "baseline_id": baseline_id,
        "critical_files": files,
        "evidence": sorted(evidence, key=lambda item: item["id"]),
    }


def attest(manifest: dict, *, signer: ManifestSigner | None = None) -> dict:
    payload = canonical_json(manifest)
    digest = sha256_bytes(payload)
    if signer is None:
        return {
            "schema_version": ATTESTATION_VERSION,
            "manifest_schema_version": manifest.get("schema_version"),
            "algorithm": "sha256",
            "key_id": None,
            "payload_sha256": digest,
            "signature": digest,
            "assurance": "integrity-only",
        }
    return {
        "schema_version": ATTESTATION_VERSION,
        "manifest_schema_version": manifest.get("schema_version"),
        "algorithm": signer.algorithm,
        "key_id": signer.key_id,
        "payload_sha256": digest,
        "signature": signer.sign(payload),
        "assurance": "managed-signature",
    }


def verify(manifest: dict, attestation: dict, *, signer: ManifestSigner | None = None) -> dict:
    if manifest.get("schema_version") != SCHEMA_VERSION:
        raise BaselineManifestError("unsupported manifest schema")
    if attestation.get("schema_version") != ATTESTATION_VERSION:
        raise BaselineManifestError("unsupported attestation schema")
    payload = canonical_json(manifest)
    digest = sha256_bytes(payload)
    problems: list[str] = []
    if attestation.get("payload_sha256") != digest:
        problems.append("payload_sha256 mismatch")
    algorithm = attestation.get("algorithm")
    if algorithm == "sha256":
        if attestation.get("signature") != digest or attestation.get("key_id") is not None:
            problems.append("sha256 attestation mismatch")
    elif signer is None:
        problems.append("managed signer is required")
    elif algorithm != signer.algorithm or attestation.get("key_id") != signer.key_id:
        problems.append("signer identity mismatch")
    elif not signer.verify(payload, str(attestation.get("signature") or "")):
        problems.append("signature verification failed")
    return {"ok": not problems, "payload_sha256": digest,
            "assurance": attestation.get("assurance"), "problems": problems}


def write_json_atomic(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)

