# Container & API 시연 가이드

> **문서 버전**: 1.0
> **최종 작성일**: 2026-02-22

---

## 목차

1. [프로젝트 파일 구조](#1-프로젝트-파일-구조)
2. [파일 설명](#2-파일-설명)
3. [API 엔드포인트](#3-api-엔드포인트)
4. [빌드 및 실행](#4-빌드-및-실행)
5. [부하 테스트 (Locust)](#5-부하-테스트-locust)
6. [로그 확인 방법](#6-로그-확인-방법)

---

## 1. 프로젝트 파일 구조

```
trescal-task-dispatcher/
├── app/
│   ├── __init__.py          # 패키지 임포트 활성화
│   ├── config.py            # 공유 설정 (환경변수 기반 Redis 접속, 키 접두사, TTL)
│   ├── api.py               # FastAPI 애플리케이션 (동기 대기 → 성공/실패 반환)
│   ├── producer_demo.py     # 대화형 콘솔 (CLI 로컬 개발용)
│   └── worker_demo.py       # Worker 프로세스 (크래시 보호 포함)
├── tests/
│   └── locustfile.py        # Locust 부하 테스트 시나리오
├── docs/
│   ├── producer_worker_demo.md   # 기술 명세서
│   └── container_api_guide.md    # 본 문서
├── .containerignore         # 컨테이너 빌드 제외 목록
├── .gitignore               # Git 추적 제외 목록
├── Containerfile            # 컨테이너 이미지 정의 (python:3.13-slim)
├── docker-compose.yml       # 서비스 오케스트레이션
└── requirements.txt         # Python 의존성
```

---

## 2. 파일 설명

| 파일                   | 역할                                                                                                                                                                                                |
| ---------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `app/__init__.py`      | 빈 파일. `app` 디렉토리를 Python 패키지로 인식시켜 `from app.config import ...` 임포트를 가능하게 합니다.                                                                                           |
| `app/config.py`        | Redis 접속 정보(`REDIS_HOST`, `REDIS_PORT`)를 환경변수에서 읽되, 기본값 `localhost:6379`를 유지합니다. 키 접두사, TTL 상수, 타임아웃 여유 시간(`JOB_TIMEOUT_GRACE_S`)을 한 곳에서 관리합니다.       |
| `app/api.py`           | FastAPI 기반 REST API. 작업 등록 시 결과가 나올 때까지 대기(BLPOP)하여 **성공/실패만** 반환합니다. Worker 크래시 시 타임아웃으로 실패 처리됩니다. Swagger UI는 `/docs`에서 자동 생성됩니다.         |
| `app/worker_demo.py`   | 동기 Worker 프로세스. 작업 처리를 `_process_job()` 함수로 분리하고 try/except로 감싸, 예외 시에도 실패 결과를 reply key에 기록합니다. 결과 기록은 `_write_result()`로 통합되어 있습니다.            |
| `app/producer_demo.py` | 비동기 대화형 콘솔(CLI). 로컬 개발/디버깅용이며, 컨테이너 환경에서는 `api.py`가 이 역할을 대체합니다. `import resource`에 try/except 가드가 적용되어 Windows에서도 임포트 가능합니다.               |
| `tests/locustfile.py`  | Locust 부하 테스트 시나리오. 성공/실패 작업 등록, stats 조회, health 체크 4종 태스크를 비중별로 실행합니다. 응답 본문의 `success` 필드까지 검증합니다.                                              |
| `Containerfile`        | `python:3.13-slim` 기반 컨테이너 이미지. `requirements.txt`를 먼저 COPY하여 레이어 캐싱을 활용합니다. 기본 CMD는 API 서버(`uvicorn`)이며, Worker와 Locust는 compose에서 command를 오버라이드합니다. |
| `docker-compose.yml`   | Redis, API, Worker 2개, Locust를 정의합니다. Redis healthcheck 통과 후 다른 서비스가 시작됩니다. 모든 서비스에 `restart: unless-stopped`가 적용되어 크래시 시 자동 재시작됩니다.                    |
| `requirements.txt`     | `redis`, `fastapi`, `uvicorn[standard]`, `locust` 의존성을 버전 범위로 지정합니다.                                                                                                                  |

---

## 3. API 엔드포인트

### 3.1 엔드포인트 요약

| Method | Path                    | 설명                                    | 주요 응답 코드 |
| ------ | ----------------------- | --------------------------------------- | -------------- |
| `GET`  | `/health`               | Redis 연결 포함 헬스 체크               | 200, 503       |
| `POST` | `/jobs`                 | 작업 등록 → 대기 → 성공/실패 반환       | 200, 422       |
| `GET`  | `/jobs/{job_id}/status` | 작업 상태 조회 (디버깅용)               | 200, 404       |
| `GET`  | `/jobs/{job_id}/result` | 작업 결과 조회 (대기 후 성공/실패 반환) | 200, 404       |
| `GET`  | `/stats`                | 큐 길이, Redis 메모리 통계              | 200            |
| `GET`  | `/docs`                 | Swagger UI (FastAPI 자동 생성)          | 200            |

### 3.2 엔드포인트 상세

#### `GET /health`

Redis 연결 상태를 포함한 헬스 체크.

```bash
curl http://localhost:8000/health
```

```json
{
  "status": "ok",
  "redis": "connected"
}
```

---

#### `POST /jobs`

작업을 등록하고 **결과가 나올 때까지 대기**하여 성공/실패만 반환합니다.

```bash
curl -X POST http://localhost:8000/jobs \
  -H "Content-Type: application/json" \
  -d '{"work_s": 5, "fail": false}'
```

**요청 본문:**

| 필드     | 타입  | 필수 | 설명                                 |
| -------- | ----- | ---- | ------------------------------------ |
| `work_s` | float | O    | 작업 소요 시간 (초, 0 초과 300 이하) |
| `fail`   | bool  | X    | 강제 실패 여부 (기본값: false)       |

**응답 — 성공 (200):**

```json
{
  "success": true,
  "job_id": "a1b2c3d4",
  "message": "작업 완료"
}
```

**응답 — 실패 (200):**

```json
{
  "success": false,
  "job_id": "a1b2c3d4",
  "message": "작업 실패: forced fail"
}
```

**응답 — Worker 타임아웃 (200):**

```json
{
  "success": false,
  "job_id": "a1b2c3d4",
  "message": "작업 시간 초과 (Worker 응답 없음)"
}
```

> **참고**: 응답 대기 시간은 `work_s + 30초`입니다. 5초 작업이면 최대 35초간 대기합니다.

---

#### `GET /jobs/{job_id}/status`

작업의 현재 상태를 조회합니다. 디버깅 및 모니터링 용도입니다.

```bash
curl http://localhost:8000/jobs/a1b2c3d4/status
```

```json
{
  "job_id": "a1b2c3d4",
  "state": "running",
  "worker": "f7c8e2a1b3d9",
  "updated_at": "14:30:05.123",
  "detail": "sleep 5.0s"
}
```

| state 값  | 의미             |
| --------- | ---------------- |
| `waiting` | 큐에 대기 중     |
| `running` | Worker가 처리 중 |
| `finish`  | 정상 완료        |
| `fail`    | 실패             |

---

#### `GET /jobs/{job_id}/result`

작업 결과를 조회합니다. 결과가 아직 없으면 **대기 후** 성공/실패를 반환합니다.

```bash
curl http://localhost:8000/jobs/a1b2c3d4/result
```

```json
{
  "success": true,
  "job_id": "a1b2c3d4",
  "message": "작업 완료"
}
```

> 이미 완료된 작업은 `demo:result:<id>` 키에서 즉시 조회되어 대기 없이 반환됩니다.

---

#### `GET /stats`

시스템 통계를 조회합니다.

```bash
curl http://localhost:8000/stats
```

```json
{
  "queue_length": 3,
  "redis_memory_used": "1.20M",
  "redis_memory_max": "unlimited",
  "redis_fragmentation_ratio": 1.23
}
```

---

#### `GET /docs`

Swagger UI를 브라우저에서 확인합니다.

```
http://localhost:8000/docs
```

---

## 4. 빌드 및 실행

### 4.1 사전 요구 사항

- **Podman** (5.x 이상)
- 포트 `8000`(API), `6379`(Redis), `8089`(Locust) 사용 가능

### 4.2 빌드 및 시작

```bash
빌드
podman compose build

실행
podman compose up -d

빌드 + 실행
podman compose up --build -d
```

### 4.3 컨테이너 상태 확인

```bash
podman compose ps
```

정상 시 출력 예시:

```
NAME               COMMAND                  STATUS
trescal-redis      redis-server             Up (healthy)
trescal-api        uvicorn app.api:app ...  Up (healthy)
trescal-worker-1   python -m app.worker_d.. Up
trescal-worker-2   python -m app.worker_d.. Up
trescal-locust     locust -f /app/tests/..  Up
```

### 4.4 시연 시나리오

```bash
# 1. 헬스 체크
curl http://localhost:8000/health

# 2. 작업 등록 (성공, 3초 대기 후 응답)
curl -X POST http://localhost:8000/jobs \
  -H "Content-Type: application/json" \
  -d '{"work_s": 3, "fail": false}'
# → {"success": true, "job_id": "...", "message": "작업 완료"}

# 3. 실패 작업 등록
curl -X POST http://localhost:8000/jobs \
  -H "Content-Type: application/json" \
  -d '{"work_s": 1, "fail": true}'
# → {"success": false, "job_id": "...", "message": "작업 실패: forced fail"}

# 4. 수평 확장 확인 (2개 동시 등록)
curl -X POST http://localhost:8000/jobs -H "Content-Type: application/json" -d '{"work_s":5}' &
curl -X POST http://localhost:8000/jobs -H "Content-Type: application/json" -d '{"work_s":5}' &
wait
# → 두 Worker가 병렬 처리하여 ~5초면 완료 (10초가 아님)

# 5. 상태 조회 (디버깅용)
curl http://localhost:8000/jobs/{job_id}/status

# 6. 시스템 통계
curl http://localhost:8000/stats
```

### 4.5 서비스 중지 및 정리

```bash
# 중지 (볼륨 유지)
podman compose down

# 중지 + 볼륨 삭제 (Redis 데이터 포함)
podman compose down -v
```

### 4.6 개별 서비스 재시작

```bash
podman compose restart api
podman compose restart worker-1 worker-2
```

---

## 5. 부하 테스트 (Locust)

### 5.1 접속

Compose 시작 후 브라우저에서 접속합니다:

```
http://localhost:8089
```

### 5.2 설정

| 항목                | 값                                        |
| ------------------- | ----------------------------------------- |
| **Number of users** | 5                                         |
| **Spawn rate**      | 5                                         |
| **Host**            | `http://api:8000` (Compose 내부 네트워크) |

> Host는 docker-compose.yml에 이미 `http://api:8000`으로 설정되어 있으므로 자동 입력됩니다.

### 5.3 테스트 시나리오

| 태스크               | 비중 | 동작                                    | Locust 검증           |
| -------------------- | ---- | --------------------------------------- | --------------------- |
| `create_success_job` | 5    | `POST /jobs` (work_s=1~3초, fail=false) | `success: true` 확인  |
| `create_fail_job`    | 2    | `POST /jobs` (work_s=1초, fail=true)    | `success: false` 확인 |
| `check_stats`        | 2    | `GET /stats`                            | HTTP 200 확인         |
| `health_check`       | 1    | `GET /health`                           | HTTP 200 확인         |

### 5.4 예상 결과

| Name                   | 예상 Median RT            | Failures |
| ---------------------- | ------------------------- | -------- |
| `POST /jobs (success)` | 1~3초 (work_s만큼 블로킹) | 0%       |
| `POST /jobs (fail)`    | ~1초 (work_s=1 고정)      | 0%       |
| `GET /stats`           | <50ms                     | 0%       |
| `GET /health`          | <10ms                     | 0%       |

- `POST /jobs (fail)`의 Locust Failure가 **0%인 이유**: Worker가 `{"success": false}`를 정상 반환하고, 테스트가 `success is False`를 기대하므로 검증 통과 = Locust success입니다.
- Worker 2개이므로 동시에 최대 2개 작업만 병렬 처리됩니다. 5명이 동시에 `POST /jobs`를 보내면 3명은 큐에서 대기합니다.

### 5.5 로컬 실행 (Compose 없이)

```bash
pip install locust
locust -f tests/locustfile.py --host http://localhost:8000 --users 5 --spawn-rate 5
```

---

## 6. 로그 확인 방법

### 6.1 전체 서비스 로그

```bash
# 전체 로그 (실시간 스트림)
podman compose logs -f

# 최근 50줄 + 실시간 스트림
podman compose logs -f --tail 50
```

### 6.2 서비스별 로그

```bash
podman compose logs -f api
podman compose logs -f worker-1
podman compose logs -f worker-2
podman compose logs -f redis
podman compose logs -f locust
```

### 6.3 Worker 로그 예시

```
trescal-worker-1  | [14:30:02.100][worker] start worker=f7c8e2a1b3d9 queue=demo:queue
trescal-worker-1  | [14:30:05.123][worker][running] job_id=a1b2c3d4 work_s=5.0 fail=False
trescal-worker-1  | [14:30:10.125][worker][finish] job_id=a1b2c3d4
trescal-worker-2  | [14:30:02.200][worker] start worker=c3d9e5f7a2b1 queue=demo:queue
trescal-worker-2  | [14:30:05.130][worker][running] job_id=e5f6g7h8 work_s=1.0 fail=True
trescal-worker-2  | [14:30:06.132][worker][fail] job_id=e5f6g7h8
```

두 Worker의 호스트명(컨테이너 ID)이 다르게 표시되어 **수평 확장을 시각적으로 확인**할 수 있습니다.

### 6.4 Worker 크래시 로그 예시

```
trescal-worker-1  | [14:35:00.500][worker][crash] job_id=x1y2z3w4 error=SomeException
trescal-worker-1  | [14:35:00.502][worker] start worker=f7c8e2a1b3d9 queue=demo:queue
```

크래시 후에도 Worker가 자동 복구되어 다음 작업을 계속 처리합니다.

### 6.5 API 서버 로그 예시

```
trescal-api  | INFO:     Uvicorn running on http://0.0.0.0:8000
trescal-api  | INFO:     192.168.1.1:54321 - "POST /jobs HTTP/1.1" 200
trescal-api  | INFO:     192.168.1.1:54322 - "GET /jobs/a1b2c3d4/status HTTP/1.1" 200
trescal-api  | INFO:     192.168.1.1:54323 - "GET /health HTTP/1.1" 200
```

### 6.6 키워드 필터링

```bash
# "fail"이 포함된 로그만 필터링
podman compose logs worker-1 worker-2 | grep fail

# 특정 job_id 추적
podman compose logs -f | grep "a1b2c3d4"

# 타임스탬프 포함 출력
podman compose logs -f -t
```

### 6.7 개별 컨테이너 직접 조회

```bash
podman logs -f trescal-api
podman logs -f trescal-worker-1
podman logs --since 5m trescal-worker-2
```
