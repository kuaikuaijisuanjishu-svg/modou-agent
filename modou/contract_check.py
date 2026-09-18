"""Interface contract checking (T08 / direction 3).

A frozen contract (per endpoint: expected status code and the response field
set with primitive types) is checked against an actual response.  The point
is the integration failure mode: each side can pass its own tests while the
wired-together response violates the contract.  Pure functions — the caller
supplies the actual response; this module never performs I/O.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

SCHEMA_VERSION = "contract-check-v1"

_TYPE_NAMES = {"object": dict, "array": list, "string": str,
               "integer": int, "number": (int, float), "boolean": bool}


class ContractError(ValueError):
    def __init__(self, code: str, detail: str):
        super().__init__(detail)
        self.code, self.detail = code, detail


def freeze_contract(endpoints: dict[str, Any]) -> dict[str, Any]:
    """Freeze the agreed shape: {endpoint: {method, status, response_fields,
    error_format?}}.  Stored verbatim; the freeze digest detects tampering."""
    if not isinstance(endpoints, dict) or not endpoints:
        raise ContractError("CONTRACT_INVALID", "endpoints must be a non-empty object")
    for endpoint, entry in endpoints.items():
        if not isinstance(endpoint, str) or not endpoint.startswith("/"):
            raise ContractError("CONTRACT_INVALID", endpoint)
        if not isinstance(entry, dict) or "status" not in entry:
            raise ContractError("CONTRACT_INVALID", f"{endpoint}: status required")
        fields = entry.get("response_fields", {})
        if not isinstance(fields, dict) or any(
                kind not in _TYPE_NAMES for kind in fields.values()):
            raise ContractError("CONTRACT_INVALID", f"{endpoint}: response_fields types")
    contract = {"schema_version": SCHEMA_VERSION, "endpoints": endpoints}
    contract["contract_sha256"] = hashlib.sha256(json.dumps(
        endpoints, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    return contract


def _type_of(value: Any) -> str:
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return "unknown"


def _diff_fields(expected: dict[str, str], actual: dict[str, Any], prefix="") -> list[dict]:
    rows = []
    for name, kind in sorted(expected.items()):
        path = prefix + name
        if name not in actual:
            rows.append({"kind": "missing_field", "path": path,
                         "detail": f"contract requires {kind}"})
        elif kind in ("object", "array") and kind == _type_of(actual[name]) and kind == "object":
            rows.extend(_diff_fields({}, actual[name], path + "."))  # nested contracts stay loose in v1
        else:
            actual_kind = _type_of(actual[name])
            # integer is acceptable where number is promised; not vice versa.
            compatible = (kind == actual_kind
                          or (kind == "number" and actual_kind == "integer"))
            if not compatible:
                rows.append({"kind": "type_mismatch", "path": path,
                             "detail": f"expected {kind}, got {actual_kind}"})
    extras = sorted(set(actual) - set(expected))
    rows.extend({"kind": "extra_field", "path": prefix + name,
                 "detail": "not in the frozen contract"} for name in extras)
    return rows


def check_response(contract: dict[str, Any], endpoint: str,
                   actual: dict[str, Any]) -> dict[str, Any]:
    """Check one actual response {status, body} against the frozen contract."""
    entry = contract.get("endpoints", {}).get(endpoint)
    if entry is None:
        raise ContractError("ENDPOINT_NOT_IN_CONTRACT", endpoint)
    differences: list[dict] = []
    expected_status = entry["status"]
    actual_status = actual.get("status")
    if actual_status != expected_status:
        differences.append({"kind": "status_mismatch", "path": "$status",
                            "detail": f"expected {expected_status}, got {actual_status}"})
    body = actual.get("body")
    if isinstance(body, dict):
        differences.extend(_diff_fields(entry.get("response_fields", {}), body))
    elif body is not None:
        differences.append({"kind": "type_mismatch", "path": "$body",
                            "detail": "response body must be an object"})
    error_format = entry.get("error_format")
    if error_format and isinstance(body, dict) and actual_status != expected_status:
        for field in error_format.get("fields", []):
            if field not in body:
                differences.append({"kind": "error_format", "path": field,
                                    "detail": "unified error field missing"})
    verdict = "contract_violation" if differences else "match"
    return {"schema_version": SCHEMA_VERSION, "endpoint": endpoint, "verdict": verdict,
            "differences": differences,
            "note": ("契约失败是集成失败：两侧各自的测试都可以通过。" if differences else "")}


def sides_pass_but_contract_fails(*, left_response, right_response, contract, endpoint) -> dict:
    """Freeze the direction-3 scenario: each side is green against its own
    local shape (its own tests), the wired response violates the shared
    frozen contract.  Returns the pairwise evidence for the demo record."""
    def local(response):
        body = response.get("body") or {}
        return freeze_contract({endpoint: {"status": response.get("status"),
            "response_fields": {k: _type_of(v) for k, v in body.items()}}})
    left = check_response(local(left_response), endpoint, left_response)
    right = check_response(local(right_response), endpoint, right_response)
    integration = check_response(contract, endpoint, _combined(left_response, right_response))
    return {"left_side": left, "right_side": right, "integration": integration,
            "scenario_holds": (left["verdict"] == "match" and right["verdict"] == "match"
                               and integration["verdict"] == "contract_violation")}


def _combined(left, right):
    """The wired response is what the new implementation actually returns:
    a renamed field means the old key is gone, not merged alongside it."""
    return {"status": right.get("status", left.get("status")), "body": right.get("body")}
