"""分层派发接口：候选明细、领取/失效、配额后补和标注待办。

这些路径必须注册在通用资源路由 `/api/{module}/{entry_id}` 之前，
否则 "candidates" 之类的固定段会被动态路径当成 entry_id 抢走。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException

from app.schemas import ActionResult, ClaimPayload, TopUpPayload, VoidClaimPayload
from app.services import service_for
from app.services.data_flow import flow_engine
from app.services.strata_dispatch import StrataDispatchService

router = APIRouter(prefix="/api/strata_dispatch", tags=["分层派发"])


def _service() -> StrataDispatchService:
    service = service_for("strata_dispatch")
    if not isinstance(service, StrataDispatchService):
        raise HTTPException(status_code=500, detail="分层派发服务未注册")
    return service


@router.get("/{dispatch_id}/candidates", response_model=dict)
def list_candidates(dispatch_id: int, status: str | None = None) -> dict[str, Any]:
    service = _service()
    entry = service.get_entry(dispatch_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"分层派发 {dispatch_id} 不存在或已归档")
    return {
        "dispatch": entry,
        "items": service.candidates(dispatch_id, status=status),
        "claims": service.claims(dispatch_id),
        "todos": service.todos(dispatch_id),
    }


@router.get("/{dispatch_id}/summary", response_model=dict)
def get_summary(dispatch_id: int) -> dict[str, Any]:
    summary = _service().summary(dispatch_id)
    if summary is None:
        raise HTTPException(status_code=404, detail=f"分层派发 {dispatch_id} 不存在或已归档")
    return summary


@router.post("/{dispatch_id}/claims", response_model=ActionResult)
def claim_samples(dispatch_id: int, payload: ClaimPayload) -> ActionResult:
    service = _service()
    entry, message = service.claim(
        dispatch_id,
        claimant=payload.claimant or service.DEFAULT_CLAIMANT,
        count=payload.count,
        mode=payload.mode,
        token=payload.token,
        user_ids=payload.user_ids,
        label_type=payload.label_type,
        label_spec=payload.label_spec,
    )
    duplicate = "已存在" in message
    flow = flow_engine.on_action("strata_dispatch", "批量派发", dispatch_id)
    return ActionResult(
        ok=entry is not None or duplicate,
        message=message,
        entry=entry,
        flow=flow if flow["hits"] > 0 else None,
    )


@router.post("/{dispatch_id}/void-claim", response_model=ActionResult)
def void_claim(dispatch_id: int, payload: VoidClaimPayload) -> ActionResult:
    service = _service()
    claim_row, message = service.void_claim(dispatch_id, payload.claim)
    return ActionResult(ok=claim_row is not None, message=message, entry=claim_row)


@router.post("/{dispatch_id}/top-up", response_model=ActionResult)
def top_up_quota(dispatch_id: int, payload: TopUpPayload) -> ActionResult:
    service = _service()
    entry, message = service.top_up(
        dispatch_id,
        total=payload.total,
        increment=payload.increment,
        layer_totals=payload.layer_totals,
        layer_increments=payload.layer_increments,
    )
    return ActionResult(ok=entry is not None, message=message, entry=entry)
