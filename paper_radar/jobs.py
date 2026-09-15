"""后台任务管理：检索 / 生成画像这类耗时操作放到线程里，UI 轮询进度。

为什么不直接在请求里同步做完：一次检索要打 5 个数据源、还可能要调十几次 LLM，
同步 HTTP 很容易被浏览器或反向代理掐断。异步 + 轮询是最稳的做法。
"""

from __future__ import annotations

import threading
import traceback
import uuid
from datetime import datetime
from typing import Any, Callable

MAX_LOG_LINES = 300


class Job:
    def __init__(self, kind: str, payload: dict | None = None):
        self.id = uuid.uuid4().hex[:12]
        self.kind = kind
        self.payload = payload or {}
        self.status = "queued"          # queued | running | ok | error
        self.logs: list[str] = []
        self.result: Any = None
        self.error = ""
        self.created_at = datetime.now().isoformat(timespec="seconds")
        self.finished_at = ""

    def log(self, message: str) -> None:
        stamp = datetime.now().strftime("%H:%M:%S")
        self.logs.append(f"{stamp} {message}")
        if len(self.logs) > MAX_LOG_LINES:
            del self.logs[: len(self.logs) - MAX_LOG_LINES]

    def to_dict(self, with_result: bool = True) -> dict:
        data = {
            "id": self.id,
            "kind": self.kind,
            "status": self.status,
            "logs": self.logs[-80:],
            "created_at": self.created_at,
            "finished_at": self.finished_at,
            "error": self.error,
            "payload": self.payload,
        }
        if with_result:
            data["result"] = self.result
        return data


class JobManager:
    def __init__(self) -> None:
        self._jobs: dict[str, Job] = {}
        self._order: list[str] = []
        self._lock = threading.Lock()

    def submit(self, kind: str, fn: Callable[[Job], Any], payload: dict | None = None) -> Job:
        job = Job(kind, payload)
        with self._lock:
            self._jobs[job.id] = job
            self._order.append(job.id)
            if len(self._order) > 100:
                stale = self._order.pop(0)
                self._jobs.pop(stale, None)
        thread = threading.Thread(target=self._run, args=(job, fn), name=f"job-{job.id}", daemon=True)
        thread.start()
        return job

    def _run(self, job: Job, fn: Callable[[Job], Any]) -> None:
        job.status = "running"
        job.log(f"任务 {job.kind} 开始")
        try:
            job.result = fn(job)
            job.status = "ok"
        except Exception as exc:  # noqa: BLE001
            job.status = "error"
            job.error = f"{type(exc).__name__}: {exc}"
            job.log("异常：" + job.error)
            job.log(traceback.format_exc(limit=6))
        finally:
            job.finished_at = datetime.now().isoformat(timespec="seconds")
            job.log(f"任务结束：{job.status}")

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def list(self, limit: int = 20) -> list[dict]:
        with self._lock:
            ids = list(reversed(self._order))[:limit]
            return [self._jobs[i].to_dict(with_result=False) for i in ids if i in self._jobs]

    def has_running(self) -> bool:
        with self._lock:
            return any(j.status in ("queued", "running") for j in self._jobs.values())
