import json
import time
import socket
from datetime import datetime
import redis

from app.config import (
    REDIS_HOST, REDIS_PORT,
    QUEUE, JOB_KEY_PREFIX, STATUS_KEY_PREFIX,
    RESULT_KEY_PREFIX, REPLY_KEY_PREFIX,
    RESULT_TTL_S, REPLY_TTL_S, STATUS_TTL_S,
)

WORKER_NAME = socket.gethostname()


def new_redis():
    return redis.Redis(host=REDIS_HOST, port=REDIS_PORT, decode_responses=True)


def ts():
    return datetime.now().strftime("%H:%M:%S.%f")[:-3]


def set_status(r: redis.Redis, job_id: str, state: str, detail: str = ""):
    r.hset(STATUS_KEY_PREFIX + job_id, mapping={
        "state": state,
        "worker": WORKER_NAME,
        "updated_at": ts(),
        "detail": detail,
    })


def main():
    r = new_redis()
    print(f"[{ts()}][worker] start worker={WORKER_NAME} queue={QUEUE}")

    while True:
        item = r.blpop(QUEUE, timeout=0)
        if not item:
            continue

        _, job_id = item

        try:
            _process_job(r, job_id)
        except Exception as e:
            # 예외 발생 시에도 반드시 실패 결과를 기록하여 API가 응답할 수 있도록 보장
            print(f"[{ts()}][worker][crash] job_id={job_id} error={e}")
            try:
                result = {"ok": False, "state": "fail", "job_id": job_id, "worker": WORKER_NAME, "error": str(e)}
                set_status(r, job_id, "fail", f"worker crash: {e}")
                r.set(RESULT_KEY_PREFIX + job_id, json.dumps(result), ex=RESULT_TTL_S)
                reply_key = REPLY_KEY_PREFIX + job_id
                r.rpush(reply_key, json.dumps(result))
                r.expire(reply_key, REPLY_TTL_S)
                r.expire(STATUS_KEY_PREFIX + job_id, STATUS_TTL_S)
            except Exception as re:
                print(f"[{ts()}][worker][crash][recovery-fail] job_id={job_id} error={re}")


def _process_job(r: redis.Redis, job_id: str):
    """단일 작업 처리. 예외 발생 시 main 루프의 except로 전파된다."""
    job_raw = r.get(JOB_KEY_PREFIX + job_id)

    if not job_raw:
        result = {"ok": False, "state": "fail", "job_id": job_id, "error": "job payload missing"}
        set_status(r, job_id, "fail", "payload missing")
        _write_result(r, job_id, result)
        print(f"[{ts()}][worker][fail] job_id={job_id} reason=payload missing")
        return

    job = json.loads(job_raw)
    work_s = float(job.get("work_s", 0))
    fail = bool(job.get("fail", False))

    set_status(r, job_id, "running", f"sleep {work_s}s")
    print(f"[{ts()}][worker][running] job_id={job_id} work_s={work_s} fail={fail}")

    time.sleep(work_s)

    if fail:
        result = {"ok": False, "state": "fail", "job_id": job_id, "worker": WORKER_NAME, "reason": "forced fail"}
        set_status(r, job_id, "fail", "forced fail")
    else:
        result = {"ok": True, "state": "finish", "job_id": job_id, "worker": WORKER_NAME, "took_s": work_s}
        set_status(r, job_id, "finish", "done")

    _write_result(r, job_id, result)
    print(f"[{ts()}][worker][{result['state']}] job_id={job_id}")


def _write_result(r: redis.Redis, job_id: str, result: dict):
    """결과를 result 키와 reply 키에 기록한다."""
    r.set(RESULT_KEY_PREFIX + job_id, json.dumps(result), ex=RESULT_TTL_S)
    reply_key = REPLY_KEY_PREFIX + job_id
    r.rpush(reply_key, json.dumps(result))
    r.expire(reply_key, REPLY_TTL_S)
    r.expire(STATUS_KEY_PREFIX + job_id, STATUS_TTL_S)


if __name__ == "__main__":
    main()
