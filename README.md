# Task Dispatcher Architecture

Redis 기반 태스크 디스패처 시스템으로, FastAPI Producer와 다수의 Worker로 구성됩니다.

---

## Containerfile

모든 서비스(API, Worker, Locust)가 공유하는 단일 컨테이너 이미지를 정의합니다.

| 항목 | 내용 |
| --- | --- |
| 베이스 이미지 | `python:3.13.0-slim` |
| 추가 런타임 | OpenJDK 17 (headless) |
| 비루트 사용자 | `appuser` (보안 강화) |
| 의존성 설치 | `requirements.txt` 레이어 캐싱 활용 |
| 소스 복사 | `app/`, `tests/` |
| 기본 CMD | `uvicorn app.api:app` (포트 8000) |

Worker와 Locust는 `docker-compose.yml`에서 `command`를 오버라이드하여 동일 이미지로 다른 역할을 수행합니다.

---

## docker-compose.yml

전체 서비스 스택을 정의하며, `podman compose` 또는 `docker compose`로 실행할 수 있습니다.

| 서비스 | 이미지/빌드 | 포트 | 역할 |
| --- | --- | --- | --- |
| `redis` | `redis:7-alpine` | 6379 | 메시지 브로커 (태스크 큐) |
| `api` | Containerfile 빌드 | 8000 | FastAPI Producer – 태스크 생성 API |
| `worker-1` | Containerfile 빌드 | – | Consumer – 태스크 처리 워커 |
| `worker-2` | Containerfile 빌드 | – | Consumer – 태스크 처리 워커 |
| `locust` | Containerfile 빌드 | 8089 | 부하 테스트 (Locust Web UI) |

주요 특징:
- **헬스체크**: Redis(`redis-cli ping`)와 API(`/health` 엔드포인트`) 헬스체크 설정
- **의존성 순서**: Redis → API → Locust, Redis → Worker 순으로 기동
- **자동 재시작**: 모든 서비스에 `restart: unless-stopped` 적용
- **볼륨**: `redis-data` Named Volume으로 Redis 데이터 영속화

```bash
# 전체 스택 빌드 및 실행
podman compose up -d --build

# 중지
podman compose down
```

---

## Podman Quadlet 설정 가이드

Windows 환경의 Podman Machine에서 systemd(Quadlet)를 이용한 컨테이너 자동 재시작 설정 가이드입니다.

### 왜 Quadlet인가?

Podman은 Docker와 달리 데몬이 없어 `restart: unless-stopped` 같은 정책이 자동으로 동작하지 않습니다. Quadlet은 systemd를 통해 컨테이너를 관리하며, 비정상 종료 시 자동 재시작을 안정적으로 지원합니다.

| systemd 설정         | 동작                    | Docker 정책 대응   |
| -------------------- | ----------------------- | ------------------ |
| `Restart=on-failure` | 비정상 종료 시만 재시작 | ≈ `unless-stopped` |
| `Restart=always`     | 어떤 종료든 재시작      | ≈ `always`         |
| `Restart=no`         | 재시작 안 함            | ≈ `no`             |

### 사전 준비

```bash
# Podman Machine에 SSH 접속
podman machine ssh
```

이하 모든 명령은 `podman machine ssh` 내부에서 실행합니다.

---

### 1. Quadlet 디렉토리 생성

```bash
mkdir -p ~/.config/containers/systemd/
```

---

### 2. 네트워크 설정

Compose와 달리 Quadlet은 공유 네트워크를 자동 생성하지 않으므로 직접 설정해야 합니다.

```bash
cat > ~/.config/containers/systemd/dispatcher.network << 'EOF'
[Network]
NetworkName=dispatcher-net

[Install]
WantedBy=default.target
EOF
```

---

### 3. Quadlet 파일 생성

#### Redis

```bash
cat > ~/.config/containers/systemd/dispatcher-redis.container << 'EOF'
[Unit]
Description=Task Dispatcher Redis
After=local-fs.target

[Container]
ContainerName=dispatcher-redis
Image=docker.io/library/redis:7-alpine
PublishPort=6379:6379
Volume=redis-data:/data
Network=dispatcher.network

[Service]
Restart=on-failure
RestartSec=5s

[Install]
WantedBy=default.target
EOF
```

#### API

```bash
cat > ~/.config/containers/systemd/dispatcher-api.container << 'EOF'
[Unit]
Description=Task Dispatcher API
After=dispatcher-redis.service

[Container]
ContainerName=dispatcher-api
Image=docker.io/library/task-dispatcher-api:latest
PublishPort=8000:8000
Network=dispatcher.network
Environment=REDIS_HOST=dispatcher-redis
Environment=REDIS_PORT=6379

[Service]
Restart=on-failure
RestartSec=5s

[Install]
WantedBy=default.target
EOF
```

#### Worker-1

```bash
cat > ~/.config/containers/systemd/dispatcher-worker-1.container << 'EOF'
[Unit]
Description=Task Dispatcher Worker 1
After=dispatcher-redis.service

[Container]
ContainerName=dispatcher-worker-1
Image=docker.io/library/task-dispatcher-worker-1:latest
Network=dispatcher.network
Environment=REDIS_HOST=dispatcher-redis
Environment=REDIS_PORT=6379
Exec=python -m app.worker_demo

[Service]
Restart=on-failure
RestartSec=5s

[Install]
WantedBy=default.target
EOF
```

#### Worker-2

```bash
cat > ~/.config/containers/systemd/dispatcher-worker-2.container << 'EOF'
[Unit]
Description=Task Dispatcher Worker 2
After=dispatcher-redis.service

[Container]
ContainerName=dispatcher-worker-2
Image=docker.io/library/task-dispatcher-worker-2:latest
Network=dispatcher.network
Environment=REDIS_HOST=dispatcher-redis
Environment=REDIS_PORT=6379
Exec=python -m app.worker_demo

[Service]
Restart=on-failure
RestartSec=5s

[Install]
WantedBy=default.target
EOF
```

#### Locust

```bash
cat > ~/.config/containers/systemd/dispatcher-locust.container << 'EOF'
[Unit]
Description=Task Dispatcher Locust
After=dispatcher-api.service

[Container]
ContainerName=dispatcher-locust
Image=docker.io/library/task-dispatcher-locust:latest
PublishPort=8089:8089
Network=dispatcher.network
Environment=LOCUST_HOST=http://dispatcher-api:8000
Exec=locust -f /app/tests/locustfile.py --host http://dispatcher-api:8000 --web-host 0.0.0.0

[Service]
Restart=on-failure
RestartSec=5s

[Install]
WantedBy=default.target
EOF
```

---

### 4. systemd 반영 및 시작

```bash
systemctl --user daemon-reload

systemctl --user start dispatcher-redis.service
systemctl --user start dispatcher-api.service
systemctl --user start dispatcher-worker-1.service
systemctl --user start dispatcher-worker-2.service
systemctl --user start dispatcher-locust.service
```

#### linger 활성화 (로그아웃 후에도 서비스 유지)

```bash
loginctl enable-linger $(whoami)
```

---

### 5. 관리 스크립트

#### 시작 스크립트

```bash
cat > ~/dispatcher-start.sh << 'EOF'
#!/bin/bash
systemctl --user start dispatcher-redis.service
systemctl --user start dispatcher-api.service
systemctl --user start dispatcher-worker-1.service
systemctl --user start dispatcher-worker-2.service
echo "All services started."
podman ps
EOF
chmod +x ~/dispatcher-start.sh
```

#### 중지 스크립트

```bash
cat > ~/dispatcher-stop.sh << 'EOF'
#!/bin/bash
systemctl --user stop dispatcher-worker-2.service
systemctl --user stop dispatcher-worker-1.service
systemctl --user stop dispatcher-api.service
systemctl --user stop dispatcher-redis.service
echo "All services stopped."
podman ps
EOF
chmod +x ~/dispatcher-stop.sh
```

#### 재시작 스크립트

```bash
cat > ~/dispatcher-restart.sh << 'EOF'
#!/bin/bash
systemctl --user restart dispatcher-redis.service
systemctl --user restart dispatcher-api.service
systemctl --user restart dispatcher-worker-1.service
systemctl --user restart dispatcher-worker-2.service
echo "All services restarted."
podman ps
EOF
chmod +x ~/dispatcher-restart.sh
```

#### Locust 스크립트

```bash
cat > ~/dispatcher-locust-start.sh << 'EOF'
#!/bin/bash
systemctl --user start dispatcher-locust.service
echo "Locust started."
podman ps
EOF
chmod +x ~/dispatcher-locust-start.sh
```

```bash
cat > ~/dispatcher-locust-stop.sh << 'EOF'
#!/bin/bash
systemctl --user stop dispatcher-locust.service
echo "Locust stopped."
podman ps
EOF
chmod +x ~/dispatcher-locust-stop.sh
```

---

### 사용법

#### podman machine ssh 안에서

```bash
~/dispatcher-start.sh
~/dispatcher-stop.sh
~/dispatcher-restart.sh
~/dispatcher-locust-start.sh
~/dispatcher-locust-stop.sh
```

#### Windows 터미널에서 직접 실행

```bash
podman machine ssh "~/dispatcher-start.sh"
podman machine ssh "~/dispatcher-stop.sh"
podman machine ssh "~/dispatcher-restart.sh"
podman machine ssh "~/dispatcher-locust-start.sh"
podman machine ssh "~/dispatcher-locust-stop.sh"
```

---

### 코드 변경 시 재배포

```bash
# 1. Windows 터미널에서 이미지 재빌드
podman compose build

# 2. 서비스 재시작
podman machine ssh "~/dispatcher-restart.sh"
```

---

### 상태 확인 및 디버깅

```bash
# 서비스 상태 확인
systemctl --user status dispatcher-api.service

# 로그 확인
journalctl --user -u dispatcher-api.service --no-pager -n 30

# Quadlet 파일 확인
cat ~/.config/containers/systemd/dispatcher-api.container

# Quadlet 생성 결과 확인
/usr/libexec/podman/quadlet -dryrun -user

# 실행 중인 컨테이너 확인
podman ps
```

---

### kill 테스트 (자동 재시작 확인)

```bash
podman kill dispatcher-api
sleep 5
podman ps   # dispatcher-api가 다시 Running 상태로 복구되어야 함
```

---

### Quadlet 정리 (삭제 시)

```bash
systemctl --user stop dispatcher-locust.service dispatcher-worker-2.service dispatcher-worker-1.service dispatcher-api.service dispatcher-redis.service
rm -f ~/.config/containers/systemd/dispatcher-*.container
rm -f ~/.config/containers/systemd/dispatcher.network
systemctl --user daemon-reload
rm -f ~/dispatcher-start.sh ~/dispatcher-stop.sh ~/dispatcher-restart.sh
rm -f ~/dispatcher-locust-start.sh ~/dispatcher-locust-stop.sh
```

---

### 주의사항

- **Compose와 Quadlet을 동시에 사용하지 마세요.** 같은 컨테이너 이름/포트로 충돌합니다.
- `docker-compose.yml`의 `restart: unless-stopped`는 Docker 이식성을 위해 그대로 유지하되, Podman에서는 Quadlet으로 관리합니다.
- Quadlet 파일 수정 후 반드시 `systemctl --user daemon-reload`를 실행하세요.
