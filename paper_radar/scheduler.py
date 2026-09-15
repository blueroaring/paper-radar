"""每日定时：内置守护线程（也可以在 Windows 计划任务 / cron 里调 `python -m paper_radar digest`）。

到点判断：每 20 秒比对一次 `HH:MM`，并按 `日期+时间` 记账，保证：
  * 改 config 里的 digest.time 立刻生效，不用重启；
  * 电脑休眠/关机错过后会补跑（但只在 `catch_up_window_minutes` 窗口内，
    避免晚上十点打开控制台却立刻收到一封早报）；
  * 同一天同一时刻只触发一次。
"""

from __future__ import annotations

import threading
from datetime import datetime, timedelta
from typing import Callable

from .digest import run_digest

WEEKDAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


class DailyScheduler(threading.Thread):
    def __init__(self, ctx, *, on_run: Callable[[dict], None] | None = None):
        super().__init__(name="paper-radar-scheduler", daemon=True)
        self.ctx = ctx
        self.on_run = on_run
        self._stop = threading.Event()
        self._fired: list[str] = []       # 已处理的 "日期T时间" 账目
        self._last_fired_date = ""
        self._running = False
        self.last_result: dict | None = None
        self.last_error = ""

    # ------------------------------------------------------------------ #
    def stop(self) -> None:
        self._stop.set()

    def status(self) -> dict:
        cfg = self.ctx.cfg
        target = str(cfg.get("digest.time", "08:30"))
        return {
            "enabled": bool(cfg.get("digest.enabled", True)),
            "time": target,
            "days": cfg.get("digest.days", list(WEEKDAYS)),
            "next_run": self._next_run_hint(),
            "catch_up_window_minutes": int(cfg.get("digest.catch_up_window_minutes", 180) or 0),
            "last_fired_date": self._last_fired_date,
            "running_now": self._running,
            "last_error": self.last_error,
        }

    def _next_run_hint(self) -> str:
        cfg = self.ctx.cfg
        days = [str(d).lower()[:3] for d in (cfg.get("digest.days") or WEEKDAYS)]
        try:
            hour, minute = [int(x) for x in str(cfg.get("digest.time", "08:30")).split(":")[:2]]
        except ValueError:
            return "时间格式错误（应为 HH:MM）"
        now = datetime.now()
        for offset in range(0, 8):
            candidate = (now + timedelta(days=offset)).replace(
                hour=hour, minute=minute, second=0, microsecond=0
            )
            if candidate <= now:
                continue
            if WEEKDAYS[candidate.weekday()] in days:
                return candidate.strftime("%Y-%m-%d %H:%M")
        return "（未来 7 天没有匹配的日期）"

    # ------------------------------------------------------------------ #
    def run(self) -> None:  # pragma: no cover - 线程体，由 daemon/serve 启动
        while not self._stop.is_set():
            try:
                self._tick()
            except Exception as exc:  # noqa: BLE001
                self.last_error = f"{type(exc).__name__}: {exc}"
            self._stop.wait(20)

    def _tick(self) -> None:
        cfg = self.ctx.cfg
        if not cfg.get("digest.enabled", True):
            return
        now = datetime.now()
        today = now.strftime("%Y-%m-%d")
        time_str = str(cfg.get("digest.time", "08:30"))
        key = f"{today}T{time_str}"
        if key in self._fired:
            return
        days = [str(d).lower()[:3] for d in (cfg.get("digest.days") or WEEKDAYS)]
        if WEEKDAYS[now.weekday()] not in days:
            return
        try:
            hour, minute = [int(x) for x in time_str.split(":")[:2]]
        except ValueError:
            return
        target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if now < target:
            return

        window = int(cfg.get("digest.catch_up_window_minutes", 180) or 0)
        if window and (now - target) > timedelta(minutes=window):
            # 错过太久（例如晚上才开机），今天不再补跑，等下一个触发点
            self._remember(key)
            return

        self._remember(key)
        self._running = True
        self._last_fired_date = today
        try:
            result = run_digest(self.ctx, progress=lambda msg: None)
            self.last_result = result
            if self.on_run:
                self.on_run(result)
        finally:
            self._running = False

    def _remember(self, key: str) -> None:
        self._fired.append(key)
        if len(self._fired) > 60:
            del self._fired[: len(self._fired) - 60]


def run_once(ctx, **kwargs) -> dict:
    """给计划任务 / 命令行用的一次性入口。"""
    return run_digest(ctx, **kwargs)
