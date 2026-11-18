"""补充自检：仿真模式（新接口）+ 中断后继续 + 2 小时自动中断的线上表现。

用法：python tools/check_modes.py
会短暂占用音频设备，跑完自动清理测试记录。
"""

from __future__ import annotations

import json
import sqlite3
import sys
import time
from pathlib import Path

import httpx

BASE = "http://127.0.0.1:8765"
DB = Path(__file__).resolve().parent.parent / "data" / "interview.db"
c = httpx.Client(base_url=BASE, timeout=180)

passed: list[str] = []
failed: list[str] = []
created: list[str] = []


def ok(name: str, detail: str = "") -> None:
    passed.append(name)
    print(f"  PASS  {name}{('  ' + detail) if detail else ''}")


def bad(name: str, err: str) -> None:
    failed.append(f"{name}: {err}")
    print(f"  FAIL  {name}: {err}")


def check(name: str, fn) -> None:
    try:
        fn()
    except Exception as exc:  # noqa: BLE001
        bad(name, str(exc)[:300])


def ensure_idle() -> None:
    state = c.get("/api/session/state").json()
    if state.get("active"):
        c.post(f"/api/interviews/{state['session_id']}/stop")


def devices() -> tuple[str, str]:
    data = c.get("/api/devices").json()
    out = (data.get("outputs") or [{}])[0].get("name", "")
    ins = [i["name"] for i in data.get("inputs") or [] if "Loopback" not in i["name"]]
    return out, ins[0] if ins else ""


def payload(mode: str) -> dict:
    out_dev, in_dev = devices()
    return {
        "role": "自检岗位-模式",
        "resume": "三年 RAG 经验，召回率 62%→81%。",
        "position_id": None,
        "position_name": "自检岗位-模式",
        "level": "mid",
        "engine": "local",
        "answer_level": "standard",
        "output_device": out_dev,
        "input_device": in_dev,
        "mode": mode,
        "background": "",
    }


def wait(fn, timeout: float, interval: float = 0.8):
    deadline = time.time() + timeout
    result = None
    while time.time() < deadline:
        result = fn()
        if result:
            return result
        time.sleep(interval)
    return result


def test_simulate_mode() -> None:
    ensure_idle()
    r = c.post("/api/interviews", json=payload("real"))
    assert r.status_code == 200, r.text[:200]
    data = r.json()
    iid = data["session_id"]
    created.append(iid)
    r = c.post(
        "/api/session/sim",
        json={"enabled": True, "audible": False, "mode": "bank"},
    )
    assert r.status_code == 200, r.text[:200]
    assert c.get(f"/api/interviews/{iid}").json().get("mode") == "simulate"

    def has_card():
        doc = c.get(f"/api/interviews/{iid}").json()
        return (doc.get("questions") or [None])[0]

    assert wait(has_card, 40), "仿真 40 秒内没有出卡片"
    time.sleep(2)
    c.post(f"/api/interviews/{iid}/stop")
    doc = c.get(f"/api/interviews/{iid}").json()
    assert doc["status"] == "finished", doc["status"]
    ok("新接口仿真模式：出卡、可停止", f"id={iid}")


def test_interrupted_resume() -> None:
    ensure_idle()
    r = c.post("/api/interviews", json=payload("real"))
    iid = r.json()["session_id"]
    created.append(iid)
    time.sleep(2)
    c.post(f"/api/interviews/{iid}/stop")
    doc = c.get(f"/api/interviews/{iid}").json()
    elapsed_before = 0
    ended = doc.get("ended_at") or time.time()

    # 模拟“进程被杀”：DB 里改成 interrupted
    con = sqlite3.connect(DB)
    row = con.execute("SELECT doc FROM sessions WHERE id = ?", (iid,)).fetchone()
    stored = json.loads(row[0])
    stored.update(
        {
            "status": "interrupted",
            "active": False,
            "ended_at": ended,
            "end_reason": "服务重启/异常退出，未手动停止",
            "report": None,
        }
    )
    con.execute(
        "UPDATE sessions SET doc = ? WHERE id = ?",
        (json.dumps(stored, ensure_ascii=False), iid),
    )
    con.commit()
    con.close()

    running = c.get("/api/interviews/running").json().get("item")
    assert running and running["id"] == iid and running["status"] == "interrupted", running

    r = c.post(f"/api/interviews/{iid}/resume")
    assert r.status_code == 200, r.text[:200]
    state = c.get("/api/session/state").json()
    assert state["active"] is True and state["session_id"] == iid, state
    assert state["interview"]["status"] == "running", state["interview"]
    ok("中断的面试可以继续（reopen 后计时接续）", f"id={iid}")

    c.post(f"/api/interviews/{iid}/stop")
    doc = c.get(f"/api/interviews/{iid}").json()
    assert doc["status"] == "finished", doc["status"]
    ok("继续后的面试可以正常停止")


def test_expired_lists_as_interrupted() -> None:
    """线上表现：超过 2 小时的 running 记录，列表读取时会被判定为系统自动中断。"""
    ensure_idle()
    r = c.post("/api/interviews", json=payload("real"))
    iid = r.json()["session_id"]
    created.append(iid)
    time.sleep(2)
    c.post(f"/api/interviews/{iid}/stop")

    con = sqlite3.connect(DB)
    row = con.execute("SELECT doc FROM sessions WHERE id = ?", (iid,)).fetchone()
    stored = json.loads(row[0])
    stored.update(
        {
            "status": "running",
            "active": True,
            "started_at": time.time() - 3 * 3600,
            "ended_at": None,
            "end_reason": "",
        }
    )
    con.execute(
        "UPDATE sessions SET doc = ? WHERE id = ?",
        (json.dumps(stored, ensure_ascii=False), iid),
    )
    con.commit()
    con.close()

    items = c.get("/api/interviews?limit=200").json()["items"]
    item = next((i for i in items if i["id"] == iid), None)
    assert item and item["status"] == "auto_interrupted", item
    ok("超过 2 小时未结束 → 读取时标记「系统自动中断」")


def cleanup() -> None:
    try:
        c.post("/api/session/stop")
    except Exception:  # noqa: BLE001
        pass
    for iid in created:
        try:
            c.post(f"/api/interviews/{iid}/stop")
        except Exception:  # noqa: BLE001
            pass
        try:
            c.delete(f"/api/interviews/{iid}")
        except Exception:  # noqa: BLE001
            pass


def main() -> int:
    ensure_idle()
    check("仿真模式（新接口）", test_simulate_mode)
    check("中断后继续", test_interrupted_resume)
    check("超时自动中断", test_expired_lists_as_interrupted)
    cleanup()
    print("\n通过 %d / 失败 %d" % (len(passed), len(failed)))
    for item in failed:
        print("  ✗", item)
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
