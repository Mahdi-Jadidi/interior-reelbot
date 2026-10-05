"""Credit-safe boundary around the subscription-backed Higgsfield MCP.

The MCP server is OAuth-based; ChatGPT's connection cannot be assumed reusable
by a background service. No undocumented tool names or OAuth token flow are
invented here. A verified client/tool mapping can be plugged in after a live
account proof. The default behavior is an explicit, safe unavailable state.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from hashlib import sha256
import json
from typing import Any, Callable, Mapping


MCP_URL = "https://mcp.higgsfield.ai/mcp"


class GenerationUnavailable(RuntimeError):
    pass


class GenerationDenied(RuntimeError):
    pass


@dataclass(frozen=True)
class GenerationPlan:
    reel_id: str
    model: str
    operation: str
    parameters: Mapping[str, Any]
    estimated_credits: Decimal

    @property
    def plan_hash(self) -> str:
        payload = {
            "reel_id": self.reel_id,
            "model": self.model,
            "operation": self.operation,
            "parameters": self.parameters,
            "estimated_credits": str(self.estimated_credits),
        }
        return sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


@dataclass(frozen=True)
class GenerationAuthorization:
    approved_plan_hash: str
    reservation_id: str
    reserved_credits: Decimal


class HiggsfieldMCP:
    """Adapter accepting only an account-verified MCP invocation callback.

    `invoke` is installed only after OAuth and tool schema have been verified
    against the actual account. The callback must be idempotency-aware and its
    caller must persist the reservation before invoking this method.
    """

    def __init__(self, invoke: Callable[[str, Mapping[str, Any], str], Any] | None = None,
                 *, verified_operations: frozenset[str] = frozenset(),
                 verify_reservation: Callable[[GenerationPlan, GenerationAuthorization], bool] | None = None) -> None:
        self._invoke = invoke
        self._verified_operations = verified_operations
        self._verify_reservation = verify_reservation

    def generate(self, plan: GenerationPlan, authorization: GenerationAuthorization) -> Any:
        if not plan.reel_id or not plan.model or not plan.operation:
            raise GenerationDenied("Generation plan is incomplete")
        if plan.estimated_credits <= 0:
            raise GenerationDenied("A positive credit estimate is required")
        if authorization.approved_plan_hash != plan.plan_hash:
            raise GenerationDenied("The exact generation plan was not approved")
        if not authorization.reservation_id or authorization.reserved_credits < plan.estimated_credits:
            raise GenerationDenied("Insufficient persisted credit reservation")
        if self._invoke is None or plan.operation not in self._verified_operations or self._verify_reservation is None:
            raise GenerationUnavailable("Higgsfield MCP OAuth/tool access is not verified; use approved manual production")
        if not self._verify_reservation(plan, authorization):
            raise GenerationDenied("Persisted approval and reservation could not be verified")
        return self._invoke(plan.operation, plan.parameters, authorization.reservation_id)
