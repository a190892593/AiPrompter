"""清掉自检脚本产生的测试面试记录（按岗位名里的关键字匹配），避免污染真实列表。

用法：python tools/cleanup_selftest_records.py [--dry-run]
"""

from __future__ import annotations

import sys

import httpx

BASE = "http://127.0.0.1:8765"
KEYWORDS = ("自检", "测试", "浸泡", "打断", "环回")


def main() -> int:
    dry = "--dry-run" in sys.argv
    c = httpx.Client(base_url=BASE, timeout=60)
    items = c.get("/api/interviews?limit=500").json()["items"]
    targets = [
        item
        for item in items
        if any(keyword in (item.get("role") or "") for keyword in KEYWORDS)
    ]
    if not targets:
        print("没有自检记录")
        return 0
    for item in targets:
        print(f"{item['id']}  {item['status']:<12} {item['role']}")
        if dry:
            continue
        if item["status"] in ("running", "interrupted"):
            c.post(f"/api/interviews/{item['id']}/stop")
        r = c.delete(f"/api/interviews/{item['id']}")
        if r.status_code >= 400:
            print("  删除失败:", r.status_code, r.text[:120])
    print(f"{'将删除' if dry else '已删除'} {len(targets)} 条自检记录")
    return 0


if __name__ == "__main__":
    sys.exit(main())
