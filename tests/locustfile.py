"""
Task Dispatcher 부하 테스트

실행 방법:
  # Compose로 (Web UI: http://localhost:8089)
  podman compose up --build -d

  # 또는 로컬에서 직접
  locust -f tests/locustfile.py --host http://localhost:8000 --users 5 --spawn-rate 5
"""

import random

from locust import HttpUser, between, task


class TaskDispatcherUser(HttpUser):
    """API에 작업을 등록하고 성공/실패 응답을 검증하는 가상 유저"""

    wait_time = between(1, 3)

    @task(5)
    def create_success_job(self):
        """성공 작업 등록 (비중 높음)"""
        work_s = round(random.uniform(1, 3), 1)
        with self.client.post(
            "/jobs",
            json={"work_s": work_s, "fail": False},
            name="POST /jobs (success)",
            catch_response=True,
        ) as resp:
            if resp.status_code == 200:
                data = resp.json()
                if data.get("success") is True:
                    resp.success()
                else:
                    resp.failure(f"expected success=true, got: {data}")
            else:
                resp.failure(f"status={resp.status_code}")

    @task(2)
    def create_fail_job(self):
        """실패 작업 등록 (fail 플래그)"""
        with self.client.post(
            "/jobs",
            json={"work_s": 1, "fail": True},
            name="POST /jobs (fail)",
            catch_response=True,
        ) as resp:
            if resp.status_code == 200:
                data = resp.json()
                if data.get("success") is False:
                    resp.success()
                else:
                    resp.failure(f"expected success=false, got: {data}")
            else:
                resp.failure(f"status={resp.status_code}")

    @task(2)
    def check_stats(self):
        """시스템 통계 조회"""
        self.client.get("/stats", name="GET /stats")

    @task(1)
    def health_check(self):
        """헬스 체크"""
        self.client.get("/health", name="GET /health")
