"""
FastAPI 기반 작업 디스패처 API
- 작업 등록 시 결과가 나올 때까지 대기 후 성공/실패만 반환
- Worker 크래시 시 타임아웃으로 실패 처리
"""

import json
import logging
import os
import uuid
from contextlib import asynccontextmanager
from datetime import datetime

import redis.asyncio as aioredis
import jpype
if not jpype.isJVMStarted():
    jpype.startJVM()
from asposecells.api import License
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

from app.config import (
    REDIS_HOST, REDIS_PORT,
    QUEUE, JOB_KEY_PREFIX, STATUS_KEY_PREFIX,
    RESULT_KEY_PREFIX, REPLY_KEY_PREFIX,
    JOB_TIMEOUT_GRACE_S, RESULT_TTL_S,
)


# ---------------------------------------------------------------------------
# Pydantic 스키마
# ---------------------------------------------------------------------------

class JobRequest(BaseModel):
    work_s: float = Field(..., gt=0, le=300, description="작업 소요 시간 (초)")
    fail: bool = Field(False, description="강제 실패 여부")


class JobResponse(BaseModel):
    success: bool
    job_id: str
    message: str


class JobStatusResponse(BaseModel):
    job_id: str
    state: str
    worker: str
    updated_at: str
    detail: str


class StatsResponse(BaseModel):
    queue_length: int
    redis_memory_used: str
    redis_memory_max: str
    redis_fragmentation_ratio: str | float


class HealthResponse(BaseModel):
    status: str
    redis: str


# ---------------------------------------------------------------------------
# 앱 생명주기 (Redis 연결 관리)
# ---------------------------------------------------------------------------

redis_pool: aioredis.Redis | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global redis_pool
    redis_pool = aioredis.Redis(
        host=REDIS_HOST, port=REDIS_PORT, decode_responses=True,
    )
    yield
    if redis_pool:
        await redis_pool.aclose()


app = FastAPI(
    title="Task Dispatcher API",
    description="Redis 기반 Producer-Worker 작업 디스패처 데모 API",
    version="1.0.0",
    lifespan=lifespan,
)

# ---------------------------------------------------------------------------
# Aspose License
# ---------------------------------------------------------------------------
license_path = os.getenv("ASPOSE_LICENSE_PATH")
if license_path:
    lic = License()
    try:
        lic.setLicense(license_path)
        logger.info("Aspose license loaded successfully")
    except Exception as e:
        logger.warning(f"Aspose license not loaded (evaluation mode): {e}")
else:
    logger.info("Aspose license path not configured; running in evaluation mode")


# ---------------------------------------------------------------------------
# 유틸리티
# ---------------------------------------------------------------------------

def ts() -> str:
    return datetime.now().strftime("%H:%M:%S.%f")[:-3]


def get_redis() -> aioredis.Redis:
    if redis_pool is None:
        raise HTTPException(status_code=503, detail="Redis 연결 없음")
    return redis_pool


async def _wait_for_result(r: aioredis.Redis, job_id: str, timeout_s: int) -> JobResponse:
    """reply key를 BLPOP으로 대기하여 성공/실패 응답을 생성한다."""
    # 이미 결과가 저장되어 있으면 즉시 반환
    result_raw = await r.get(RESULT_KEY_PREFIX + job_id)
    if result_raw:
        result = json.loads(result_raw)
        return _to_job_response(job_id, result)

    # BLPOP으로 대기
    reply_key = REPLY_KEY_PREFIX + job_id
    try:
        item = await r.blpop(reply_key, timeout=timeout_s)
    except Exception as e:
        await _mark_fail(r, job_id, f"결과 대기 중 오류: {e}")
        return JobResponse(success=False, job_id=job_id, message=f"결과 대기 중 오류: {e}")

    if not item:
        # 타임아웃 — Worker 크래시 또는 무응답
        await _mark_fail(r, job_id, "Worker 응답 없음 (타임아웃)")
        return JobResponse(
            success=False,
            job_id=job_id,
            message="작업 시간 초과 (Worker 응답 없음)",
        )

    _, raw = item
    try:
        result = json.loads(raw)
    except Exception:
        result = {"ok": False}

    return _to_job_response(job_id, result)


def _to_job_response(job_id: str, result: dict) -> JobResponse:
    """Worker 결과 dict를 성공/실패 JobResponse로 변환한다."""
    if result.get("ok"):
        return JobResponse(success=True, job_id=job_id, message="작업 완료")

    # 실패 사유 결정
    reason = result.get("reason") or result.get("error") or "알 수 없는 오류"
    return JobResponse(success=False, job_id=job_id, message=f"작업 실패: {reason}")


async def _mark_fail(r: aioredis.Redis, job_id: str, detail: str):
    """타임아웃 등으로 실패 시 Redis 상태를 fail로 마킹한다."""
    await r.hset(STATUS_KEY_PREFIX + job_id, mapping={
        "state": "fail",
        "updated_at": ts(),
        "detail": detail,
    })


# ---------------------------------------------------------------------------
# 엔드포인트
# ---------------------------------------------------------------------------

@app.get("/health", response_model=HealthResponse, tags=["시스템"])
async def health_check():
    """Redis 연결 상태를 포함한 헬스 체크"""
    r = get_redis()
    try:
        await r.ping()
        return HealthResponse(status="ok", redis="connected")
    except Exception:
        raise HTTPException(status_code=503, detail="Redis 연결 실패")


@app.post("/jobs", response_model=JobResponse, tags=["작업"])
async def create_job(req: JobRequest):
    """
    작업 등록 후 결과가 나올 때까지 대기하여 성공/실패만 반환한다.
    Worker 크래시 시 타임아웃으로 실패 처리된다.
    """
    r = get_redis()
    job_id = str(uuid.uuid4())[:8]
    job = {"job_id": job_id, "work_s": req.work_s, "fail": req.fail}

    # payload 저장
    await r.set(JOB_KEY_PREFIX + job_id, json.dumps(job), ex=RESULT_TTL_S)

    # waiting 상태 설정
    await r.hset(STATUS_KEY_PREFIX + job_id, mapping={
        "state": "waiting",
        "worker": "",
        "updated_at": ts(),
        "detail": "queued via API",
    })

    # FIFO 큐에 등록
    await r.rpush(QUEUE, job_id)

    # 결과 대기 (work_s + 여유 시간)
    timeout_s = int(req.work_s) + JOB_TIMEOUT_GRACE_S
    return await _wait_for_result(r, job_id, timeout_s)


@app.get("/jobs/{job_id}/status", response_model=JobStatusResponse, tags=["작업"])
async def get_job_status(job_id: str):
    """작업 상태 조회 (waiting / running / finish / fail)"""
    r = get_redis()
    data = await r.hgetall(STATUS_KEY_PREFIX + job_id)
    if not data:
        raise HTTPException(status_code=404, detail=f"작업 {job_id}을(를) 찾을 수 없음")
    return JobStatusResponse(
        job_id=job_id,
        state=data.get("state", "unknown"),
        worker=data.get("worker", ""),
        updated_at=data.get("updated_at", ""),
        detail=data.get("detail", ""),
    )


@app.get("/jobs/{job_id}/result", response_model=JobResponse, tags=["작업"])
async def get_job_result(job_id: str):
    """
    작업 결과 조회. 결과가 아직 없으면 대기 후 성공/실패만 반환한다.
    이미 완료된 작업은 즉시 반환한다.
    """
    r = get_redis()

    # 작업 존재 여부 확인
    status = await r.hgetall(STATUS_KEY_PREFIX + job_id)
    if not status:
        raise HTTPException(status_code=404, detail=f"작업 {job_id}을(를) 찾을 수 없음")

    # 이미 결과가 있으면 즉시 반환
    result_raw = await r.get(RESULT_KEY_PREFIX + job_id)
    if result_raw:
        result = json.loads(result_raw)
        return _to_job_response(job_id, result)

    # 원래 work_s를 조회하여 타임아웃 계산
    job_raw = await r.get(JOB_KEY_PREFIX + job_id)
    if job_raw:
        job = json.loads(job_raw)
        timeout_s = int(job.get("work_s", 0)) + JOB_TIMEOUT_GRACE_S
    else:
        timeout_s = JOB_TIMEOUT_GRACE_S

    return await _wait_for_result(r, job_id, timeout_s)


@app.get("/stats", response_model=StatsResponse, tags=["시스템"])
async def get_stats():
    """시스템 통계 (큐 길이, Redis 메모리 등)"""
    r = get_redis()
    qlen = await r.llen(QUEUE)

    try:
        info = await r.info(section="memory")
        used_h = info.get("used_memory_human", "?")
        max_mem = info.get("maxmemory", 0)
        max_h = info.get("maxmemory_human", "?")
        if max_mem in (0, "0", None):
            max_h = "unlimited"
        frag = info.get("mem_fragmentation_ratio", "?")
    except Exception:
        used_h, max_h, frag = "?", "?", "?"

    return StatsResponse(
        queue_length=qlen,
        redis_memory_used=used_h,
        redis_memory_max=max_h,
        redis_fragmentation_ratio=frag,
    )
