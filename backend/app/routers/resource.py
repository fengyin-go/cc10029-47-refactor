"""统一资源接口：查询、导出、登记和动作按模块 key 分派到领域服务。"""
from __future__ import annotations

import csv
import io
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from app.schemas import ActionPayload, ActionResult, EntryPayload, PageResult
from app.services import service_for
from app.services.relay import target_relay_info

router = APIRouter(prefix="/api", tags=["业务资源"])


def require_service(module: str):
    service = service_for(module)
    if service is None:
        raise HTTPException(status_code=404, detail=f"未知业务模块：{module}")
    return service


@router.get("/{module}", response_model=PageResult[dict])
def list_entries(
    module: str,
    request: Request,
    keyword: str | None = Query(default=None),
    status: str | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1, le=200),
) -> PageResult[dict]:
    service = require_service(module)
    filters = {
        key: value.strip()
        for key, value in request.query_params.items()
        if key in service.LIST_FIELDS and value.strip()
    }
    items, total = service.list_entries(
        filters=filters, keyword=keyword, status=status, page=page, size=size)
    items = [enrich_entry(service, item) for item in items]
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/{module}/export")
def export_entries(
    module: str,
    request: Request,
    keyword: str | None = Query(default=None),
    status: str | None = Query(default=None),
) -> StreamingResponse:
    service = require_service(module)
    filters = {
        key: value.strip()
        for key, value in request.query_params.items()
        if key in service.LIST_FIELDS and value.strip()
    }
    items, _ = service.list_entries(
        filters=filters, keyword=keyword, status=status, page=1, size=1_000_000)
    output = io.StringIO()
    output.write("\ufeff")
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(["id", *service.LIST_FIELDS, "status"])
    for item in items:
        values = [item.get("id"), *(item.get(field, "") for field in service.LIST_FIELDS),
                  item.get("status", "")]
        writer.writerow([
            "'" + str(value) if str(value or "").startswith(("=", "+", "-", "@")) else value
            for value in values
        ])
    return StreamingResponse(
        iter([output.getvalue()]), media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{module}.csv"'},
    )


@router.get("/{module}/{entry_id}", response_model=dict)
def get_entry(module: str, entry_id: int) -> dict[str, Any]:
    service = require_service(module)
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"{service.ENTITY} {entry_id} 不存在或已归档")
    return enrich_entry(service, entry)


def enrich_entry(service, entry: dict[str, Any]) -> dict[str, Any]:
    """在响应里附上派发回执和来源信息，供详情页和列表展示。"""
    # 接力内部占位标记不下发到前端。
    detail = {key: value for key, value in entry.items() if not key.startswith("_relay")}
    status = getattr(service, "relay_status", None)
    if callable(status):
        info = status(entry.get("id"))
        if info is not None:
            detail["dispatch"] = info
            # 列表里用扁平字段展示，保持通用表格的字段契约不变。
            if info["required"] or info["has_dispatch"]:
                detail["dispatch_state"] = info["state"]
                detail["dispatch_retryable"] = info["retryable"]
                detail["dispatch_message"] = info["message"]
    relay = target_relay_info(service.MODULE, entry)
    if relay["from_relay"]:
        detail["relay"] = relay
        detail["relay_source"] = relay["source"]
        detail["relay_complete"] = relay["complete"]
    return detail


@router.get("/{module}/{entry_id}/dispatch", response_model=dict)
def get_dispatch_status(module: str, entry_id: int) -> dict[str, Any]:
    """发起人查询来源单据的派发进度、回执以及是否还能重试。"""
    service = require_service(module)
    status = getattr(service, "relay_status", None)
    if not callable(status):
        raise HTTPException(status_code=404, detail=f"{service.ENTITY}不涉及派发")
    info = status(entry_id)
    if info is None:
        raise HTTPException(status_code=404, detail=f"{service.ENTITY} {entry_id} 不存在或已归档")
    return info


@router.post("/{module}/{entry_id}/dispatch/retry", response_model=ActionResult)
def retry_dispatch(module: str, entry_id: int) -> ActionResult:
    """派发中止后重新提交原动作，从中断处续办。"""
    service = require_service(module)
    retry = getattr(service, "retry_dispatch", None)
    if not callable(retry):
        raise HTTPException(status_code=404, detail=f"{service.ENTITY}不涉及派发")
    entry, message = retry(entry_id)
    return ActionResult(ok=entry is not None, message=message, entry=entry)


@router.post("/{module}", response_model=ActionResult)
def create_entry(module: str, payload: EntryPayload) -> ActionResult:
    service = require_service(module)
    entry, missing = service.create_entry(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message=f"{service.ENTITY}已登记", entry=entry)


@router.post("/{module}/{entry_id}/actions", response_model=ActionResult)
def run_action(module: str, entry_id: int, payload: ActionPayload) -> ActionResult:
    service = require_service(module)
    entry, message = service.run_action(entry_id, payload.action.strip())
    return ActionResult(ok=entry is not None, message=message, entry=entry)
