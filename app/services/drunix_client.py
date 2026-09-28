"""HTTP client for the Drunix bridge (drunix/bridge).

The bridge owns the Fabric Gateway connection to the Drunix Lite Peer; this
client only speaks its small JSON API:

    POST /v1/submit    {"function", "args"} -> committed-VALID transaction or error
    POST /v1/evaluate  {"function", "args"} -> query result
    GET  /health       -> bridge + gateway + chaincode readiness

A transaction is reported as successful only when the bridge says it was
committed with validation code VALID. Everything else becomes one of the
typed ``Drunix*Error`` exceptions, so callers can tell a *rejection by the
ledger* apart from *unavailable infrastructure*.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import httpx

from app.config import settings
from app.exceptions import (
    DrunixConflictError,
    DrunixError,
    DrunixInvalidCommitError,
    DrunixRejectedError,
    DrunixTimeoutError,
    DrunixUnavailableError,
)

log = logging.getLogger("agentguard.drunix")

_BY_CATEGORY = {
    "CHAINCODE_REJECTED": DrunixRejectedError,
    "MVCC_READ_CONFLICT": DrunixConflictError,
    "DRUNIX_INVALID_COMMIT": DrunixInvalidCommitError,
    "DRUNIX_UNAVAILABLE": DrunixUnavailableError,
    "DRUNIX_TIMEOUT": DrunixTimeoutError,
}


@dataclass(frozen=True)
class DrunixTx:
    """A transaction that Drunix committed as VALID."""

    function: str
    tx_id: str
    block_number: Optional[int]  # None only for a RECOVERED commit read back from state
    validation_code: str
    result: Any
    latency_ms: int
    endorse_ms: int = 0
    commit_ms: int = 0
    channel: str = ""
    chaincode: str = ""

    def as_dict(self) -> Dict[str, Any]:
        return {"function": self.function, "tx_id": self.tx_id, "block_number": self.block_number,
                "status": self.validation_code, "latency_ms": self.latency_ms,
                "channel": self.channel, "chaincode": self.chaincode}


@dataclass
class DrunixClient:
    base_url: str
    token: str = ""
    timeout: float = 45.0
    transport: Optional[httpx.BaseTransport] = field(default=None, repr=False)

    def _client(self, timeout: Optional[float] = None) -> httpx.Client:
        if not self.base_url:
            raise httpx.ConnectError("DRUNIX_BRIDGE_URL is not configured")
        headers = {"Authorization": f"Bearer {self.token}"} if self.token else {}
        # trust_env=False: the bridge is a local service; never route it through
        # a host HTTP proxy.
        return httpx.Client(base_url=self.base_url, headers=headers, transport=self.transport,
                            timeout=timeout or self.timeout, trust_env=False)

    def _post(self, path: str, function: str, args: List[str]) -> Dict[str, Any]:
        try:
            with self._client() as c:
                resp = c.post(path, json={"function": function, "args": [str(a) for a in args]})
        except httpx.TimeoutException as exc:
            raise DrunixTimeoutError(f"Drunix bridge did not answer {function} within {self.timeout:.0f}s",
                                     category="DRUNIX_TIMEOUT", stage="bridge", function=function) from exc
        except httpx.HTTPError as exc:
            raise DrunixUnavailableError(f"Drunix bridge unreachable at {self.base_url}: {type(exc).__name__}",
                                         category="DRUNIX_UNAVAILABLE", stage="bridge", function=function) from exc
        try:
            body = resp.json()
        except ValueError as exc:
            raise DrunixUnavailableError(f"malformed response from Drunix bridge (HTTP {resp.status_code})",
                                         category="BRIDGE_ERROR", stage="bridge", function=function) from exc
        if not isinstance(body, dict):
            raise DrunixUnavailableError("malformed response from Drunix bridge", category="BRIDGE_ERROR",
                                         stage="bridge", function=function)
        if resp.status_code == 200 and body.get("ok") is True:
            return body
        err = body.get("error") if isinstance(body.get("error"), dict) else {}
        category = str(err.get("category") or ("BRIDGE_ERROR" if resp.status_code < 500 else "DRUNIX_UNAVAILABLE"))
        cls = _BY_CATEGORY.get(category, DrunixError)
        raise cls(str(err.get("message") or f"Drunix bridge returned HTTP {resp.status_code}"),
                  category=category, code=str(err.get("code") or ""), stage=str(err.get("stage") or ""),
                  function=function, tx_id=str(err.get("tx_id") or ""), block_number=err.get("block_number"))

    def submit(self, function: str, args: List[Any]) -> DrunixTx:
        body = self._post("/v1/submit", function, args)
        # Defence in depth: never accept a "success" that is not a VALID commit
        # with a transaction id.
        if body.get("status") != "VALID" or body.get("validation_code") != "VALID" or not body.get("tx_id"):
            raise DrunixInvalidCommitError(
                f"bridge reported {function} without a VALID commit (status={body.get('status')!r})",
                category="DRUNIX_INVALID_COMMIT", stage="commit", function=function,
                tx_id=str(body.get("tx_id") or ""))
        return DrunixTx(
            function=function, tx_id=body["tx_id"], block_number=int(body["block_number"]) if body.get("block_number") is not None else None,
            validation_code="VALID", result=body.get("result"), latency_ms=int(body.get("latency_ms") or 0),
            endorse_ms=int(body.get("endorse_ms") or 0), commit_ms=int(body.get("commit_ms") or 0),
            channel=str(body.get("channel") or ""), chaincode=str(body.get("chaincode") or ""))

    def evaluate(self, function: str, args: List[Any]) -> Any:
        return self._post("/v1/evaluate", function, args).get("result")

    def health(self) -> Dict[str, Any]:
        try:
            with self._client(timeout=6.0) as c:
                resp = c.get("/health")
            body = resp.json()
            body["http_status"] = resp.status_code
            return body
        except (httpx.HTTPError, ValueError) as exc:
            return {"status": "down", "bridge": "unreachable", "drunix": "unknown", "chaincode_status": "unknown",
                    "error": {"category": "DRUNIX_UNAVAILABLE", "message": f"{type(exc).__name__}: bridge unreachable"}}

    def recent(self) -> List[Dict[str, Any]]:
        try:
            with self._client(timeout=5.0) as c:
                return c.get("/v1/transactions").json().get("transactions", [])
        except (httpx.HTTPError, ValueError):
            return []


_client: Optional[DrunixClient] = None


def get_client() -> DrunixClient:
    global _client
    if _client is None:
        _client = DrunixClient(settings.drunix_bridge_url.rstrip("/"), settings.drunix_bridge_token,
                               settings.drunix_timeout_seconds)
    return _client


def set_client(client: Optional[DrunixClient]) -> None:
    """Tests inject a client backed by a fake bridge transport."""
    global _client
    _client = client
