import os

# Redis 접속 정보 (환경 변수 또는 기본값)
REDIS_HOST = os.environ.get("REDIS_HOST", "localhost")
REDIS_PORT = int(os.environ.get("REDIS_PORT", "6379"))

# Redis 키 접두사
QUEUE = "demo:queue"
JOB_KEY_PREFIX = "demo:job:"
STATUS_KEY_PREFIX = "demo:status:"
RESULT_KEY_PREFIX = "demo:result:"
REPLY_KEY_PREFIX = "demo:reply:"

# TTL 설정 (초)
RESULT_TTL_S = 3600
REPLY_TTL_S = 600
STATUS_TTL_S = 3600

# 작업 타임아웃 여유 시간 (초) — BLPOP timeout = work_s + 이 값
JOB_TIMEOUT_GRACE_S = 30
