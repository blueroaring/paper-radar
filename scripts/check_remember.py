"""验证高分记忆在真实检索/简报链路里的行为（直接打本地控制台 API）。

用法：python scripts/check_remember.py
"""

from __future__ import annotations

import json
import sys
import time
import urllib.request

BASE = "http://127.0.0.1:8848"


def call(path: str, payload: dict | None = None) -> dict:
    if payload is None:
        req = urllib.request.Request(BASE + path)
    else:
        req = urllib.request.Request(
            BASE + path,
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


def wait_job(job_id: str, timeout: float = 420) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = call(f"/api/jobs/{job_id}")["job"]
        if job["status"] in ("ok", "error"):
            for line in job["logs"][-6:]:
                print("    " + line)
            return job
        time.sleep(4)
    raise TimeoutError("任务超时")


def main() -> int:
    before = call("/api/remembered")
    print(f"检索前记忆：{before['total']} 条（阈值 {before['threshold']}）")

    topics = call("/api/topics")["topics"]
    if not topics:
        print("没有研究方向，先在控制台建一个。")
        return 1
    topic_id = topics[0]["id"]

    print(f"\n开始检索（topic={topic_id}, limit=10）…")
    job = call("/api/search", {"topic_id": topic_id, "limit": 10, "summarize": True})
    result = wait_job(job["job"]["id"])
    if result["status"] != "ok":
        print("检索失败：" + result.get("error", ""))
        return 1

    papers = result["result"]["papers"]
    marked = [p for p in papers if p.get("remembered")]
    newly = result["result"].get("remembered", [])
    print(f"\n返回 {len(papers)} 篇，其中带 ★ 标记 {len(marked)} 篇")
    print(f"本次新记住 {len(newly)} 篇：")
    for item in newly:
        print(f"  + [{round(item['score']*100)}] {item['title'][:64]}")

    print("\n逐条查看标记是否合理（阈值 {}）:".format(result["result"].get("remember_threshold")))
    for p in papers:
        flag = "★" if p.get("remembered") else " "
        print(f"  {flag} {round(p['score']*100):>3}  {p['title'][:66]}")

    after = call("/api/remembered")
    print(f"\n检索后记忆：{after['total']} 条（净增 {after['total'] - before['total']}）")

    wrong = [p for p in papers if p.get("remembered") and p["score"] < after["threshold"]]
    print("标记与阈值不一致的条目：", len(wrong))
    return 0


if __name__ == "__main__":
    sys.exit(main())
