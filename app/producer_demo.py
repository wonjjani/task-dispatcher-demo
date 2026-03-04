import asyncio
import json
import os
import sys
import uuid
from datetime import datetime

try:
    import resource
except ImportError:
    resource = None  # Windows 환경에서는 사용 불가

import redis.asyncio as redis

from app.config import (
    REDIS_HOST, REDIS_PORT,
    QUEUE, JOB_KEY_PREFIX, STATUS_KEY_PREFIX,
    RESULT_KEY_PREFIX, REPLY_KEY_PREFIX,
)

PRINT_LOCK = asyncio.Lock()
OUTSTANDING = 0
OUT_LOCK = asyncio.Lock()
MAX_RSS_SEEN = 0


def ts():
    return datetime.now().strftime("%H:%M:%S.%f")[:-3]


async def safe_print(*args, **kwargs):
    async with PRINT_LOCK:
        print(*args, **kwargs)


def human_bytes(n: int | None) -> str:
    if n is None:
        return "n/a"
    units = ["B", "KB", "MB", "GB", "TB"]
    x = float(n)
    for u in units:
        if x < 1024.0 or u == units[-1]:
            return f"{int(x)}{u}" if u == "B" else f"{x:.2f}{u}"
        x /= 1024.0
    return f"{x:.2f}TB"


def _linux_rss_bytes_from_proc() -> int | None:
    try:
        with open("/proc/self/status", "r", encoding="utf-8") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    kb = int(line.split()[1])
                    return kb * 1024
    except Exception:
        pass

    try:
        with open("/proc/self/statm", "r", encoding="utf-8") as f:
            parts = f.read().strip().split()
            rss_pages = int(parts[1])
        page_size = os.sysconf("SC_PAGE_SIZE")
        return rss_pages * page_size
    except Exception:
        return None


def _ru_maxrss_bytes() -> int | None:
    if resource is None:
        return None
    try:
        ru = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        if sys.platform != "darwin":
            return int(ru) * 1024  
        return int(ru)  
    except Exception:
        return None


def process_mem_linux() -> dict:
    global MAX_RSS_SEEN
    cur = _linux_rss_bytes_from_proc()
    if cur is not None and cur > MAX_RSS_SEEN:
        MAX_RSS_SEEN = cur
    ru_max = _ru_maxrss_bytes()
    return {
        "cur_h": human_bytes(cur),
        "max_seen_h": human_bytes(MAX_RSS_SEEN if MAX_RSS_SEEN > 0 else None),
        "os_max_h": human_bytes(ru_max),
    }


async def inc_outstanding(delta: int) -> int:
    global OUTSTANDING
    async with OUT_LOCK:
        OUTSTANDING += delta
        return OUTSTANDING


async def get_outstanding() -> int:
    async with OUT_LOCK:
        return OUTSTANDING


async def set_status_waiting(r: redis.Redis, job_id: str):
    await r.hset(STATUS_KEY_PREFIX + job_id, mapping={
        "state": "waiting",
        "worker": "",
        "updated_at": ts(),
        "detail": "queued",
    })


async def enqueue_job(r: redis.Redis, work_s: float, fail: bool) -> str:
    job_id = str(uuid.uuid4())[:8]
    job = {"job_id": job_id, "work_s": work_s, "fail": fail}

    # payload 저장
    await r.set(JOB_KEY_PREFIX + job_id, json.dumps(job))

    # waiting 상태
    await set_status_waiting(r, job_id)

    # FIFO 큐: RPUSH + (워커) BLPOP
    await r.rpush(QUEUE, job_id)

    await inc_outstanding(+1)
    await safe_print(f"[{ts()}][producer][waiting] enqueued job_id={job_id} job={job}")
    return job_id


async def queue_len(r: redis.Redis) -> int:
    try:
        return int(await r.llen(QUEUE))
    except Exception:
        return -1


async def queue_ahead(r: redis.Redis, job_id: str) -> int | None:
    try:
        pos = await r.lpos(QUEUE, job_id)
        return int(pos) if pos is not None else None
    except Exception:
        return None


async def redis_mem(r: redis.Redis) -> dict:
    try:
        info = await r.info(section="memory")
        return {
            "used_h": info.get("used_memory_human"),
            "max_h": info.get("maxmemory_human"),
            "max": info.get("maxmemory"),
            "frag": info.get("mem_fragmentation_ratio"),
        }
    except Exception:
        return {"used_h": "?", "max_h": "?", "max": None, "frag": "?"}


async def print_stats(r: redis.Redis, last_job_id: str | None = None):
    qlen = await queue_len(r)
    out = await get_outstanding()
    mem = await redis_mem(r)
    pm = process_mem_linux()

    max_h = mem["max_h"]
    if mem["max"] in (0, "0", None):
        max_h = "unlimited"

    ahead_txt = ""
    if last_job_id:
        ahead = await queue_ahead(r, last_job_id)
        ahead_txt = f" | last_job={last_job_id} ahead={ahead if ahead is not None else 'n/a'}"

    await safe_print(
        f"[{ts()}][stats] outstanding={out} queue_len={qlen} | "
        f"redis={mem['used_h']}(max={max_h}, frag={mem['frag']}) | "
        f"proc_rss={pm['cur_h']}(max_seen={pm['max_seen_h']}, os_max={pm['os_max_h']})"
        f"{ahead_txt}"
    )


async def wait_reply_and_print(r: redis.Redis, job_id: str, timeout_s: int = 60):
    """
    Reply Queue 방식:
    - 워커가 demo:reply:<job_id> 에 result를 RPUSH
    - 여기서는 BLPOP으로 그 키만 기다렸다가 출력
    """
    reply_key = REPLY_KEY_PREFIX + job_id

    try:
        # BLPOP: (key, value) or None
        item = await r.blpop(reply_key, timeout=timeout_s)
        print(reply_key, item)
        if not item:
            await inc_outstanding(-1)
            await safe_print(f"\n[{ts()}][producer][timeout][{job_id}] (no reply)\n")
            return

        _, raw = item
        try:
            result = json.loads(raw)
        except Exception:
            result = {"raw": raw}

        state = result.get("state") or ("finish" if result.get("ok") else "fail")

        # (옵션) result 키가 있으면 그것도 확인 가능
        # result_raw = await r.get(RESULT_KEY_PREFIX + job_id)

        await inc_outstanding(-1)
        await safe_print(f"\n[{ts()}][producer][{state}][{job_id}] result={result}\n")

    except Exception as e:
        await inc_outstanding(-1)
        await safe_print(f"\n[{ts()}][producer][error][{job_id}] {e}\n")


def parse_cmd(line: str):
    line = line.strip()
    if not line:
        return ("noop", None)

    low = line.lower()
    if low in ("q", "quit", "exit"):
        return ("quit", None)
    if low in ("h", "help", "?"):
        return ("help", None)
    if low in ("stats", "s"):
        return ("stats", None)

    parts = line.split()
    try:
        work_s = float(parts[0])
    except ValueError:
        return ("error", "첫 토큰은 work_s(초) 숫자여야 함. 예) 5  / 10.5 fail")

    fail = False
    if len(parts) >= 2:
        flag = parts[1].lower()
        if flag in ("fail", "f", "1", "true", "y", "yes"):
            fail = True
        elif flag in ("ok", "o", "0", "false", "n", "no"):
            fail = False
        else:
            return ("error", "두번째 토큰은 fail/ok 중 하나. 예) 10.5 fail / 7 ok")

    return ("enqueue", {"work_s": work_s, "fail": fail})


async def ainput(prompt: str = "") -> str:
    # input()은 블로킹이라 executor로 돌림
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, lambda: input(prompt))


async def main():
    r = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, decode_responses=True)

    await safe_print("=== producer console (Reply Queue 1:1) ===")
    await safe_print("입력: <work_s> [fail|ok]   예) 5   / 10.5 fail   / 7 ok")
    await safe_print("명령: stats(s) | help | q")
    await safe_print("NOTE: 완료는 Pub/Sub이 아니라 demo:reply:<job_id> BLPOP로 받음.")
    await safe_print("----------------------------------------------------")

    last_job_id = None

    while True:
        await print_stats(r, last_job_id=last_job_id)

        try:
            line = await ainput("> ")
        except (EOFError, KeyboardInterrupt):
            await safe_print("\nbye")
            break

        cmd, payload = parse_cmd(line)

        if cmd == "noop":
            continue
        if cmd == "help":
            await safe_print("사용법:")
            await safe_print("  5            -> 5초 작업, 성공")
            await safe_print("  10.5 fail     -> 10.5초 작업, 실패")
            await safe_print("  7 ok          -> 7초 작업, 성공")
            await safe_print("  stats         -> 상태 출력(마지막 job ahead 포함)")
            await safe_print("  q             -> 종료")
            continue
        if cmd == "stats":
            await print_stats(r, last_job_id=last_job_id)
            continue
        if cmd == "error":
            await safe_print(f"[error] {payload}")
            continue
        if cmd == "quit":
            await safe_print("bye")
            break

        if cmd == "enqueue":
            last_job_id = await enqueue_job(r, payload["work_s"], payload["fail"])
            ahead = await queue_ahead(r, last_job_id)
            await safe_print(f"[{ts()}][position] job_id={last_job_id} ahead={ahead if ahead is not None else 'n/a'}")

            asyncio.create_task(wait_reply_and_print(r, last_job_id, timeout_s=300))


if __name__ == "__main__":
    asyncio.run(main())
