#!/usr/bin/env python3
"""Fail-closed release metadata and public-tree checks.

The release workflow must publish a prerelease with a reviewed Notes file and
must never silently accept an already-created release with different state.
This helper intentionally has no GitHub write operations.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
# ``sandbox`` is reserved for private workflow tests (for example
# ``v0.0.0-sandbox.2``); all public release tags remain experimental/preview
# channels and are still checked against their tag-bound Notes file.
TAG_RE = re.compile(r"^v\d+\.\d+\.\d+-(?:experimental|preview|alpha|beta|rc|sandbox)\.\d+$")
NOTES_RE = re.compile(r"^v\d+\.\d+\.\d+-(?:experimental|preview|alpha|beta|rc|sandbox)\.\d+\.md$")
EXCLUDED_DIGEST_FILES = {Path("PUBLIC_TREE_MANIFEST.json")}


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _git(*args: str) -> str:
    return subprocess.run(["git", *args], cwd=ROOT, check=True,
                          capture_output=True, text=True).stdout.strip()


def validate_tag(tag: str) -> None:
    if not TAG_RE.fullmatch(tag):
        raise ValueError(f"unsupported release tag: {tag}")


def notes_path(tag: str) -> Path:
    validate_tag(tag)
    name = f"{tag}.md"
    if not NOTES_RE.fullmatch(name):
        raise ValueError("release Notes filename is not tag-bound")
    path = ROOT / "docs" / "release" / name
    if not path.is_file():
        raise FileNotFoundError(path)
    return path


def tracked_tree_sha256() -> str:
    """Hash the public commit's tracked files in stable path order."""
    raw = subprocess.run(["git", "ls-files", "-z"], cwd=ROOT, check=True,
                         capture_output=True).stdout
    paths = [Path(item.decode()) for item in raw.split(b"\0") if item]
    digest = hashlib.sha256()
    for relative in sorted(paths):
        if relative in EXCLUDED_DIGEST_FILES:
            continue
        blob = ROOT / relative
        digest.update(relative.as_posix().encode())
        digest.update(b"\0")
        digest.update(hashlib.sha256(blob.read_bytes()).digest())
    return digest.hexdigest()


def _run_public_scan(notes: Path) -> None:
    scanner = ROOT / "tools" / "public_release_check.py"
    subprocess.run(["python3", str(scanner), "--root", str(ROOT),
                    "--notes-file", str(notes)], cwd=ROOT, check=True)


def verify(tag: str, expected_tree: str, expected_notes: str,
           expected_public_commit: str = "") -> dict[str, str]:
    validate_tag(tag)
    if not re.fullmatch(r"[0-9a-f]{64}", expected_tree or ""):
        raise ValueError("EXPECTED_PUBLIC_TREE_SHA256 must be a 64-char digest")
    if not re.fullmatch(r"[0-9a-f]{64}", expected_notes or ""):
        raise ValueError("EXPECTED_RELEASE_NOTES_SHA256 must be a 64-char digest")
    notes = notes_path(tag)
    _run_public_scan(notes)
    head = _git("rev-parse", "HEAD")
    if not re.fullmatch(r"[0-9a-f]{40}", expected_public_commit or ""):
        raise ValueError("EXPECTED_PUBLIC_COMMIT must be a full public commit SHA")
    if head != expected_public_commit:
        raise ValueError(f"tag target is not the frozen public commit: {head} != {expected_public_commit}")
    tree = tracked_tree_sha256()
    notes_digest = sha256(notes)
    if tree != expected_tree:
        raise ValueError(f"public tree digest mismatch: {tree} != {expected_tree}")
    if notes_digest != expected_notes:
        raise ValueError(f"release Notes digest mismatch: {notes_digest} != {expected_notes}")
    return {"tag": tag, "head": head, "tree_sha256": tree,
            "notes_sha256": notes_digest}


def verify_existing_release(tag: str, notes: Path, title: str, payload: str,
                            expected_public_commit: str = "") -> None:
    validate_tag(tag)
    expected_notes = notes_path(tag)
    if notes.resolve() != expected_notes.resolve():
        raise ValueError("release Notes path is not bound to the tag")
    expected_body = notes.read_text(encoding="utf-8")
    try:
        value = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise ValueError("gh release view returned invalid JSON") from exc
    if value.get("isPrerelease") is not True:
        raise ValueError("existing release is not marked prerelease")
    if value.get("name") != title:
        raise ValueError("existing release title mismatch")
    if value.get("body") != expected_body:
        raise ValueError("existing release body does not match frozen Notes")
    target = str(value.get("targetCommitish") or "")
    head = _git("rev-parse", "HEAD")
    if not re.fullmatch(r"[0-9a-f]{40}", expected_public_commit or ""):
        raise ValueError("EXPECTED_PUBLIC_COMMIT must be a full public commit SHA")
    if expected_public_commit != head:
        raise ValueError("existing release checkout is not the frozen public commit")
    if target:
        # gh may return a branch/ref name for targetCommitish.  Resolve it
        # locally (including the remote-tracking ref from a full CI fetch) and
        # compare the resulting commit, never the display name.
        candidates = [target]
        if not target.startswith("origin/"):
            candidates.extend([f"origin/{target}", f"refs/remotes/origin/{target}"])
        resolved = ""
        for candidate in candidates:
            try:
                resolved = _git("rev-parse", f"{candidate}^{{commit}}")
                break
            except subprocess.CalledProcessError:
                continue
        if not resolved:
            raise ValueError("existing release target commit is unresolved")
        if resolved != head:
            raise ValueError("existing release target commit mismatch")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    verify_parser = sub.add_parser("verify")
    verify_parser.add_argument("--tag", required=True)
    verify_parser.add_argument("--expected-tree-sha256", required=True)
    verify_parser.add_argument("--expected-notes-sha256", required=True)
    verify_parser.add_argument("--expected-public-commit", required=True)
    existing = sub.add_parser("verify-existing-release")
    existing.add_argument("--tag", required=True)
    existing.add_argument("--notes-file", type=Path, required=True)
    existing.add_argument("--title", required=True)
    existing.add_argument("--expected-public-commit", required=True)
    args = parser.parse_args(argv)
    if args.command == "verify":
        result = verify(args.tag, args.expected_tree_sha256, args.expected_notes_sha256,
                        args.expected_public_commit)
    else:
        notes = args.notes_file.resolve()
        payload = os.environ.get("RELEASE_JSON", "")
        verify_existing_release(args.tag, notes, args.title, payload,
                                args.expected_public_commit)
        result = {"tag": args.tag, "existing_release": "verified"}
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
