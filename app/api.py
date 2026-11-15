"""HTTP 接口层。"""

from __future__ import annotations

import datetime as dt
import logging

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from .models import (
    BatchRequest,
    BatchResult,
    ConflictResponse,
    DailyReport,
    OperationConflictResponse,
    TransferConflictResponse,
    TransferRequest,
    TransferResult,
)
from .service import (
    OperationIdConflictError,
    ReadingService,
    RevisionConflictError,
    TransferPreconditionError,
)

logger = logging.getLogger(__name__)

router = APIRouter()


def get_service(request: Request) -> ReadingService:
    return request.app.state.service


@router.get("/healthz", summary="健康检查")
def healthz() -> dict:
    return {"status": "ok"}


@router.post(
    "/api/v1/readings/batch",
    response_model=BatchResult,
    status_code=200,
    summary="批量补报 / 修订 / 撤回 PM2.5 小时值",
    responses={
        409: {
            "model": ConflictResponse,
            "description": "同一站点日期小时修订号已存在但内容不同；整批已回滚。",
        },
        422: {"description": "请求体校验失败。"},
    },
)
def submit_batch(
    payload: BatchRequest,
    service: ReadingService = Depends(get_service),
) -> BatchResult:
    """原子写入一批记录。

    * 每小时以最大 revision 为准；新 revision 追加，不覆盖历史。
    * 同 revision 同内容重复提交无副作用（幂等）。
    * 同 revision 不同内容 → 409，整批回滚，不留下部分写入。
    """
    try:
        outcome = service.apply_batch(payload.records)
    except RevisionConflictError as exc:
        body = ConflictResponse(
            message="one or more revisions already exist with different content; batch rolled back",
            conflicts=exc.conflicts,
        )
        raise HTTPException(status_code=409, detail=body.model_dump()) from exc

    return BatchResult(
        received=outcome.received,
        created=outcome.created,
        unchanged=outcome.unchanged,
        stations=outcome.stations,
        dates=outcome.dates,
    )


@router.post(
    "/api/v1/readings/transfer",
    response_model=TransferResult,
    status_code=200,
    summary="把源站小时读数转移到目标站（源站追加撤回，目标站写入原值）",
    responses={
        409: {
            "description": (
                "操作号异内容冲突，或转移前置条件不满足"
                "（源修订已变化 / 源读数非当前有效 / 目标小时已入库）；整次已回滚。"
            ),
        },
        422: {"description": "请求体校验失败。"},
    },
)
def transfer_reading(
    payload: TransferRequest,
    service: ReadingService = Depends(get_service),
) -> TransferResult:
    """原子地纠正记错站点的小时读数。

    * 源站每个待转移小时追加一条更高修订号的撤回记录，原浓度写入目标站 revision=1；
      操作登记、两站记录在同一事务提交，任一失败整次不生效。
    * 仅允许转移源站当前有效、且目标站该小时从未入库的读数；
      ``source_revision`` 是查询时看到的源站当前修订号，作为乐观锁。
    * 相同 ``operation_id`` 同内容重试返回首次结果（``replayed=true``，无副作用）；
      相同 ``operation_id`` 异内容返回 409 冲突。
    """
    try:
        result, replayed = service.apply_transfer(payload)
    except TransferPreconditionError as exc:
        body = TransferConflictResponse(
            message="one or more transfer preconditions failed; operation rolled back",
            conflicts=exc.conflicts,
        )
        raise HTTPException(status_code=409, detail=body.model_dump()) from exc
    except OperationIdConflictError as exc:
        body = OperationConflictResponse(
            message="operation_id was already used with a different request; operation rolled back",
            operation_id=payload.operation_id,
            existing_request=exc.existing_request,
            submitted_request=exc.submitted_request,
        )
        raise HTTPException(status_code=409, detail=body.model_dump()) from exc

    result = dict(result)
    result["replayed"] = replayed
    return TransferResult(**result)


@router.get(
    "/api/v1/stations/{station_id}/daily",
    response_model=DailyReport,
    summary="查询站点某北京日期的 PM2.5 日报",
)
def get_daily_report(
    station_id: str,
    date: dt.date = Query(..., description="北京时间观测日期 YYYY-MM-DD"),
    service: ReadingService = Depends(get_service),
) -> DailyReport:
    station_id = station_id.strip()
    if not station_id:
        raise HTTPException(status_code=422, detail="station_id must not be blank")
    return service.daily_report(station_id, date)
