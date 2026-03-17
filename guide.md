# Task Dispatcher Study Guide

이 문서는 이 저장소를 처음 보는 사람이 `코드 흐름`, `Redis 사용 방식`, `Producer/Worker 구조`를 빠르게 이해하도록 돕기 위한 학습용 가이드다.

## 1. 이 프로젝트가 하는 일

이 프로젝트는 아주 전형적인 `Producer -> Queue -> Worker` 구조를 구현한 예제다.

구성은 단순하다.

- 클라이언트가 API에 작업을 요청한다.
- API는 작업 내용을 Redis에 저장하고 큐에 넣는다.
- Worker가 큐에서 작업을 꺼내 처리한다.
- 처리 결과를 다시 Redis에 기록한다.
- API는 그 결과를 기다렸다가 최종 응답을 반환한다.

즉, 겉으로는 HTTP API처럼 보이지만, 내부에서는 Redis가 작업 중개자 역할을 한다.

## 2. 큰 그림

```text
Client
  |
  | HTTP POST /jobs
  v
FastAPI app/api.py
  |
  | Redis에 job 저장 + queue에 job_id push
  v
Redis
  |
  | worker가 BLPOP으로 대기
  v
Worker app/worker_demo.py
  |
  | 작업 처리 후 result/reply 저장
  v
Redis
  |
  | API가 reply를 BLPOP으로 기다림
  v
FastAPI response
```

핵심 포인트는 `작업 본문 전체`를 큐에 넣지 않고 `job_id`만 큐에 넣는다는 점이다. 실제 payload는 별도 key에 저장한다.

## 3. 파일별 역할

- [`app/config.py`](app/config.py): Redis 접속 정보, key 이름, TTL, 타임아웃 여유 시간을 모아둔 설정 파일
- [`app/api.py`](app/api.py): FastAPI 서버. 작업 등록, 상태 조회, 결과 조회, health/stats 제공
- [`app/worker_demo.py`](app/worker_demo.py): Redis 큐를 소비하는 Worker 프로세스
- [`app/producer_demo.py`](app/producer_demo.py): 로컬에서 동작 확인할 때 쓰는 CLI Producer
- [`docker-compose.yml`](docker-compose.yml): Redis, API, Worker, Locust를 한 번에 띄우는 실행 구성

## 4. Redis 키 설계

[`app/config.py`](app/config.py)에서 아래 key들을 정의한다.

- `demo:queue`
- `demo:job:<job_id>`
- `demo:status:<job_id>`
- `demo:result:<job_id>`
- `demo:reply:<job_id>`

각 역할은 이렇다.

- `queue`: 처리 대기열
- `job`: 실제 작업 payload 저장
- `status`: 현재 상태 추적
- `result`: 최종 결과 저장
- `reply`: API가 즉시 응답받기 위한 1회성 응답 채널

이 구조가 좋은 이유는 역할이 분리돼 있기 때문이다.

- 큐는 순서 보장에 집중
- job key는 payload 보관에 집중
- status key는 모니터링에 집중
- result/reply key는 응답 전달에 집중

## 5. 왜 `job_id`만 큐에 넣을까

초보자가 제일 먼저 봐야 할 설계 포인트다.

큐에 전체 JSON payload를 넣는 방식도 가능하다. 그런데 이 프로젝트는 그렇게 하지 않는다.

대신 다음 순서를 쓴다.

1. `job_id` 생성
2. `demo:job:<job_id>`에 payload 저장
3. `demo:queue`에 `job_id`만 push

이 방식의 장점:

- payload와 queue를 분리할 수 있다
- 상태 조회 시 `job_id` 기준으로 모든 데이터를 찾기 쉽다
- 결과, 상태, 에러를 같은 `job_id` 축으로 관리할 수 있다
- 큐 메시지를 가볍게 유지할 수 있다

## 6. API가 하는 일

[`app/api.py`](app/api.py)에서 제일 중요한 엔드포인트는 `POST /jobs`다.

흐름은 거의 아래와 같다.

1. 요청값을 검증한다.
2. `job_id`를 만든다.
3. payload를 `demo:job:<id>`에 저장한다.
4. 상태를 `waiting`으로 기록한다.
5. 큐에 `job_id`를 넣는다.
6. `demo:reply:<id>`를 `BLPOP`으로 기다린다.
7. Worker 결과가 오면 성공/실패 응답으로 바꿔서 반환한다.

여기서 중요한 점은 API가 `비동기 작업을 등록만 하고 끝내는 구조`가 아니라는 점이다. 이 프로젝트는 작업 등록 후 곧바로 결과까지 기다린다.

즉:

- 내부 구조는 비동기 분산 처리
- 외부 사용자 경험은 동기 응답

이 패턴은 "작업 처리는 워커에게 맡기고 싶지만, 클라이언트는 최종 성공/실패를 바로 받고 싶다"는 요구에 맞는다.

## 7. `BLPOP`이 핵심인 이유

이 저장소를 이해하려면 `BLPOP`을 알아야 한다.

`BLPOP`은 Redis List에서 값을 꺼내되, 값이 없으면 기다리는 명령이다.

이 프로젝트는 두 곳에서 `BLPOP`을 쓴다.

- Worker: `demo:queue`를 기다림
- API: `demo:reply:<job_id>`를 기다림

즉 양쪽 다 폴링하지 않는다. Redis가 값이 들어올 때까지 block 상태로 대기한다.

장점:

- 불필요한 while polling이 줄어든다
- 구현이 단순하다
- 응답 대기 모델을 이해하기 쉽다

## 8. Worker가 하는 일

[`app/worker_demo.py`](app/worker_demo.py)의 핵심은 `main()`과 `_process_job()`이다.

Worker 루프는 아래처럼 생각하면 된다.

```python
while True:
    job_id = BLPOP(queue)
    process(job_id)
```

실제 처리 순서는 이렇다.

1. 큐에서 `job_id`를 받는다.
2. `demo:job:<id>`에서 payload를 읽는다.
3. 상태를 `running`으로 바꾼다.
4. `work_s`만큼 sleep 하며 작업을 흉내 낸다.
5. 성공 또는 실패 결과를 만든다.
6. `result` key와 `reply` key에 기록한다.
7. 상태를 `finish` 또는 `fail`로 바꾼다.

이 프로젝트의 Worker는 실제 비즈니스 로직 대신 `sleep`으로 작업 시간을 시뮬레이션한다. 그래서 아키텍처를 공부하기에 좋다. 복잡한 도메인 로직 없이 큐 흐름에만 집중할 수 있기 때문이다.

## 9. 상태 추적이 왜 필요한가

`status` key가 없으면 작업이 지금 어디까지 갔는지 알 수 없다.

이 프로젝트는 상태를 최소한 다음 값으로 나눈다.

- `waiting`
- `running`
- `finish`
- `fail`

이 덕분에 다음이 가능하다.

- API에서 `/jobs/{job_id}/status` 제공
- 디버깅이 쉬움
- Worker가 멈췄는지 추정 가능
- 운영 중 현재 병목이 queue인지 worker 처리인지 보기 쉬움

## 10. 결과를 왜 `result`와 `reply`로 나눌까

이것도 중요한 설계 포인트다.

겉보기에는 둘 다 결과 저장처럼 보이지만 목적이 다르다.

- `result`: 나중에 다시 조회하기 위한 저장소
- `reply`: 지금 기다리는 요청에게 즉시 전달하기 위한 채널

즉:

- `reply`는 실시간 응답용
- `result`는 조회/보관용

그래서 API는 먼저 `result`가 이미 있으면 즉시 반환하고, 없으면 `reply`를 기다린다.

## 11. 예외 처리 설계

[`app/worker_demo.py`](app/worker_demo.py)에서 좋은 부분은 "에러가 나도 API를 영원히 기다리게 두지 않으려는 의도"가 분명하다는 점이다.

Worker 처리 중 예외가 나면:

- 상태를 `fail`로 기록
- 실패 결과를 `result`에 저장
- 실패 결과를 `reply`에도 push

즉, 실패해도 반드시 응답 경로를 남긴다.

[`app/api.py`](app/api.py)도 타임아웃을 둔다.

- `timeout = work_s + JOB_TIMEOUT_GRACE_S`

의미는 간단하다.

- 정상 작업 시간보다 조금 더 기다린다
- 그래도 응답이 없으면 worker crash 또는 hang으로 보고 실패 처리한다

이 설계 덕분에 API 요청이 무한정 매달리지 않는다.

## 12. TTL이 있는 이유

[`app/config.py`](app/config.py)에는 TTL이 정의돼 있다.

- `RESULT_TTL_S = 3600`
- `REPLY_TTL_S = 600`
- `STATUS_TTL_S = 3600`

왜 필요할까.

- 결과가 영구히 쌓이면 Redis 메모리가 계속 증가한다
- reply key는 일회성이라 오래 남길 이유가 없다
- status/result는 디버깅을 위해 일정 시간만 남기면 충분하다

즉, 이 프로젝트는 "데모지만 운영 감각이 조금 들어간 설계"라고 보면 된다.

## 13. `producer_demo.py`는 왜 있나

[`app/producer_demo.py`](app/producer_demo.py)는 API 없이도 흐름을 확인해보는 로컬 CLI 도구다.

학습 관점에서는 오히려 이 파일이 Redis 흐름을 직접 보기 좋다.

볼만한 포인트:

- 작업 enqueue
- outstanding 개수 추적
- queue length 조회
- Redis 메모리 통계 출력
- reply key 대기 후 결과 출력

즉, 브라우저나 HTTP 없이도 "큐 시스템이 실제로 어떻게 흐르는지" 확인할 수 있다.

## 14. Compose 구조는 왜 이렇게 되어 있나

[`docker-compose.yml`](docker-compose.yml)을 보면 서비스는 5개다.

- `redis`
- `api`
- `worker-1`
- `worker-2`
- `locust`

핵심 학습 포인트:

- worker를 2개 띄워서 수평 확장 개념을 보여줌
- API와 Worker가 같은 이미지를 쓰고 `command`만 다르게 줄 수 있음
- Redis healthcheck가 통과된 뒤 다른 서비스가 뜨도록 함

이건 실무에서도 자주 보는 패턴이다. 같은 코드베이스에서 역할만 다르게 배치한다.

## 15. 이 프로젝트를 읽는 추천 순서

처음부터 문서 전체를 다 읽기보다 아래 순서가 낫다.

1. [`app/config.py`](app/config.py)에서 key 구조를 먼저 본다.
2. [`app/worker_demo.py`](app/worker_demo.py)에서 queue 소비 흐름을 본다.
3. [`app/api.py`](app/api.py)에서 요청이 어떻게 queue로 연결되는지 본다.
4. [`docker-compose.yml`](docker-compose.yml)에서 서비스 구성을 본다.
5. 마지막에 [`app/producer_demo.py`](app/producer_demo.py)를 보고 보조 도구 역할을 이해한다.

이 순서가 좋은 이유는 시스템의 중심이 `UI`가 아니라 `queue flow`이기 때문이다.

## 16. 공부할 때 직접 확인해볼 질문

아래 질문에 답해보면 구조가 빨리 잡힌다.

- 왜 queue에는 payload가 아니라 `job_id`만 넣었을까?
- 왜 `result`와 `reply`를 둘 다 만들었을까?
- Worker가 죽으면 API는 어떻게 실패를 판단할까?
- Worker 수를 1개에서 2개로 늘리면 어떤 점이 달라질까?
- `BLPOP` 대신 polling으로 구현하면 어떤 비효율이 생길까?
- TTL이 없다면 Redis에는 어떤 문제가 생길까?

## 17. 직접 실험해볼 것

학습은 코드를 읽는 것보다 조금 만져보는 쪽이 빠르다.

추천 실험:

1. Worker를 1개만 띄우고 여러 요청을 보내본다.
2. Worker를 2개 띄우고 같은 요청을 보내본다.
3. `work_s=1`, `work_s=5`, `fail=true`를 섞어서 요청해본다.
4. 작업 중 Worker 프로세스를 죽였을 때 API 응답이 어떻게 바뀌는지 본다.
5. Redis key를 직접 조회해보며 `queue`, `job`, `status`, `result`, `reply`의 차이를 본다.

## 18. 한 줄 요약

이 저장소는 "Redis를 중간에 둔 작업 분배 구조를 가장 단순한 형태로 보여주는 학습용 예제"다.

핵심만 남기면 이 문장이다.

`API는 작업을 넣고 기다리고, Worker는 작업을 꺼내 처리하고, Redis는 둘 사이의 상태와 결과를 연결한다.`

## 19. 처음 설치하고 셋업하는 방법

이 프로젝트를 처음 실행할 때는 `Podman의 모든 기능`을 한 번에 보려 하지 않는 편이 낫다.

추천 순서는 이렇다.

1. 먼저 `docker compose`로 구조를 이해한다.
2. 그다음 `podman compose`로 같은 스택을 실행해본다.
3. 마지막으로 `Quadlet + systemd`를 본다.

이 순서가 좋은 이유는 아키텍처 학습과 Podman 운영 기능 학습을 분리할 수 있기 때문이다.

## 20. 가장 쉬운 시작: Docker Compose

문서상 이 프로젝트는 [`docker-compose.yml`](docker-compose.yml)을 기준으로 바로 실행할 수 있다.

### 20.1 준비

- Git 설치
- Docker Desktop 또는 Docker Engine 설치

### 20.2 저장소 받기

```bash
git clone <your-repo-url>
cd task-dispatcher-demo
git switch develop
```

### 20.3 컨테이너 실행

```bash
docker compose up -d --build
```

### 20.4 상태 확인

```bash
docker compose ps
curl http://localhost:8000/health
curl http://localhost:8000/stats
```

### 20.5 작업 요청 테스트

```bash
curl -X POST http://localhost:8000/jobs \
  -H "Content-Type: application/json" \
  -d '{"work_s": 3, "fail": false}'
```

### 20.6 로그 보기

```bash
docker compose logs -f api
docker compose logs -f worker-1
docker compose logs -f worker-2
```

### 20.7 종료

```bash
docker compose down
```

처음에는 이 단계만 해도 충분하다. 이걸로 `API -> Redis -> Worker -> 응답` 흐름을 모두 볼 수 있다.

테스트 후 한 번에 정리하고 싶다면 아래 스크립트를 사용할 수 있다.

```bash
bash scripts/teardown_all.sh
```

## 21. Podman으로 실행하기

문서 기준으로는 `podman compose`도 바로 지원한다.

### 21.1 준비

- Podman 설치

### 21.2 실행

```bash
podman compose up -d --build
```

### 21.3 확인

```bash
podman compose ps
curl http://localhost:8000/health
curl http://localhost:8000/stats
```

### 21.4 종료

```bash
podman compose down
```

학습 목적이라면 `docker compose`와 `podman compose`의 차이를 크게 의식하지 않아도 된다. 둘 다 현재 저장소 구조를 이해하는 데는 충분하다.

Podman Compose로 띄운 뒤에도 동일하게 아래 스크립트로 정리할 수 있다.

```bash
bash scripts/teardown_all.sh
```

## 22. 문서가 권장하는 Podman 심화 방식: Podman Machine + Quadlet

[`README.md`](README.md)에서는 Podman의 운영형 관리 방식으로 `Quadlet + systemd`를 안내한다.

핵심은 `Docker 컨테이너 안에 Podman을 넣는 방식`이 아니라, `Podman Machine 안으로 들어가서 systemd로 관리`하는 방식이라는 점이다.

### 22.1 Podman Machine 접속

```bash
podman machine init
podman machine start
podman machine ssh
```

이후 명령은 `podman machine ssh` 안에서 실행한다.

### 22.2 systemd 디렉토리 준비

```bash
mkdir -p ~/.config/containers/systemd/
```

### 22.3 Quadlet 네트워크 및 서비스 파일 생성

문서에 있는 아래 파일들을 만든다.

- `dispatcher.network`
- `dispatcher-redis.container`
- `dispatcher-api.container`
- `dispatcher-worker-1.container`
- `dispatcher-worker-2.container`
- `dispatcher-locust.container`

이 파일 내용은 [`README.md`](README.md)에 그대로 나와 있다.

### 22.4 systemd 반영 및 시작

```bash
systemctl --user daemon-reload
systemctl --user start dispatcher-redis.service
systemctl --user start dispatcher-api.service
systemctl --user start dispatcher-worker-1.service
systemctl --user start dispatcher-worker-2.service
systemctl --user start dispatcher-locust.service
```

### 22.5 로그아웃 후에도 유지하려면

```bash
loginctl enable-linger $(whoami)
```

이 단계는 "그냥 실행해보기"보다 "Podman에서 자동 재시작과 systemd 관리까지 확인해보기"에 가깝다.

Quadlet로 올린 서비스까지 정리하려면 아래 스크립트를 실행하면 된다.

```bash
bash scripts/teardown_all.sh
```

## 23. 무엇부터 하면 좋나

처음 공부할 때 추천 경로는 아래와 같다.

1. `docker compose up -d --build`
2. `curl /health`, `curl /jobs`, `curl /stats`
3. `logs -f api`, `logs -f worker-1`, `logs -f worker-2`
4. Worker를 일부러 중지하거나 요청을 여러 개 보내보며 큐 동작 확인
5. 익숙해지면 `podman compose`
6. 마지막에 `podman machine + quadlet`

## 24. 중요한 주의점

[`README.md`](README.md)에서도 분명히 말하듯이 다음은 피하는 편이 좋다.

- Compose와 Quadlet을 동시에 사용하지 않기
- 같은 포트와 같은 컨테이너 이름을 중복으로 띄우지 않기
- 처음부터 Podman 운영 기능과 아키텍처 학습을 한 번에 하려고 하지 않기

처음엔 `docker compose`로 구조를 이해하고, 그 다음에 Podman 쪽으로 넘어가는 게 가장 덜 꼬인다.
