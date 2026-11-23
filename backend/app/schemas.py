"""通用接口契约；模块字段由领域服务持有，不再为每个模块复制一份模型类。"""
from __future__ import annotations

from typing import Any, Generic, TypeVar

from pydantic import BaseModel, Field

T = TypeVar("T")


class PageResult(BaseModel, Generic[T]):
    items: list[T]
    total: int
    page: int = 1
    size: int = 20


class ActionResult(BaseModel):
    ok: bool
    message: str
    entry: dict[str, Any] | None = None
    flow: dict[str, Any] | None = None


class EntryPayload(BaseModel):
    values: dict[str, Any] = Field(default_factory=dict)
    remark: str | None = None


class ActionPayload(BaseModel):
    action: str
    remark: str | None = None


class ClaimPayload(BaseModel):
    """分层领取：count 为空表示按剩余配额尽量整组派发。"""
    claimant: str | None = None
    count: int | None = Field(default=None, ge=0)
    mode: str = "整组"
    token: str | None = None
    user_ids: Any = None
    label_type: str | None = None
    label_spec: str | None = None


class VoidClaimPayload(BaseModel):
    claim: str


class TopUpPayload(BaseModel):
    """后补名额：total/increment 二选一，layer_* 用于分层配额。"""
    total: int | None = Field(default=None, ge=0)
    increment: int | None = None
    layer_totals: Any = None
    layer_increments: Any = None
