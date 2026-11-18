"""全链路自测：逐个接口打一遍，报 PASS/FAIL，最后汇总。

用法：python tools/full_test.py [--fast] [--slow]
--fast 只跑接口面，跳过需要大模型的会话链路（几秒）
--slow 额外跑仿真面试（TTS + ASR，约 20 秒）
"""

from __future__ import annotations

import argparse
import sys
import time

import httpx

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

BASE = "http://127.0.0.1:8765"
c = httpx.Client(base_url=BASE, timeout=180)

passed: list[str] = []
failed: list[tuple[str, str]] = []


def check(name: str, fn) -> None:
    try:
        fn()
        passed.append(name)
        print(f"  PASS  {name}")
    except Exception as exc:  # noqa: BLE001
        failed.append((name, str(exc)[:200]))
        print(f"  FAIL  {name}: {exc}")


def get(path: str, **kw) -> httpx.Response:
    r = c.get(path, **kw)
    if r.status_code >= 400:
        raise AssertionError(f"GET {path} -> {r.status_code} {r.text[:120]}")
    return r


def post(path: str, **kw) -> httpx.Response:
    r = c.post(path, **kw)
    if r.status_code >= 400:
        raise AssertionError(f"POST {path} -> {r.status_code} {r.text[:120]}")
    return r


def put(path: str, **kw) -> httpx.Response:
    r = c.put(path, **kw)
    if r.status_code >= 400:
        raise AssertionError(f"PUT {path} -> {r.status_code} {r.text[:120]}")
    return r


def delete(path: str) -> httpx.Response:
    r = c.delete(path)
    if r.status_code >= 400:
        raise AssertionError(f"DELETE {path} -> {r.status_code} {r.text[:120]}")
    return r


# ---------------------------------------------------------------- 基础
def t_health() -> None:
    data = get("/api/health").json()
    assert "llm_live" in data or "ok" in data or data, data


def t_page() -> None:
    r = c.get("/")
    assert r.status_code == 200
    assert "assets/index-" in r.text
    for asset in __import__("re").findall(r"/assets/index-[^\"]+", r.text):
        assert c.get(asset).status_code == 200, asset


def t_devices() -> None:
    data = get("/api/devices").json()
    assert data.get("outputs") is not None, data
    assert data.get("inputs") is not None, data


# ---------------------------------------------------------------- 岗位
def t_positions() -> None:
    items = get("/api/positions").json()["items"]
    assert isinstance(items, list)
    created = post("/api/positions", json={"name": "自测岗位X", "jd": "测试JD", "skills": "A、B"}).json()
    pid = created["id"]
    ctx = get(f"/api/positions/{pid}/context").json()
    assert ctx["name"] == "自测岗位X" and "测试JD" in ctx["background"], ctx
    put(f"/api/positions/{pid}", json={"name": "自测岗位Y", "jd": "", "skills": ""})
    assert any(i["name"] == "自测岗位Y" for i in get("/api/positions").json()["items"])
    delete(f"/api/positions/{pid}")


# ---------------------------------------------------------------- 题库
def t_bank() -> None:
    before = len(get("/api/bank").json()["items"])
    created = post(
        "/api/bank",
        json={"category": "自测", "question": "自测题目：缓存一致性的方案？", "answer": "延迟双删 + binlog 补偿"},
    ).json()
    bid = created["id"]
    assert len(get("/api/bank").json()["items"]) == before + 1
    put(f"/api/bank/{bid}", json={"category": "自测", "question": "自测题目改", "answer": "答案改"})
    cats = get("/api/bank").json()["categories"]
    assert "自测" in cats, cats
    delete(f"/api/bank/{bid}")
    assert len(get("/api/bank").json()["items"]) == before


# ---------------------------------------------------------------- 提示词
def t_prompts() -> None:
    items = get("/api/prompts").json()["items"]
    assert len(items) == 8, len(items)
    key = "judge.user"
    original = next(i for i in items if i["key"] == key)["content"]
    put(f"/api/prompts/{key}", json={"content": original + "\n测试标记"})
    assert "测试标记" in next(i for i in get("/api/prompts").json()["items"] if i["key"] == key)["content"]
    post(f"/api/prompts/{key}/reset")
    assert "测试标记" not in next(i for i in get("/api/prompts").json()["items"] if i["key"] == key)["content"]
    try:
        put("/api/prompts/judge.user", json={"content": ""})
        raise AssertionError("空内容应当被拒绝")
    except AssertionError as exc:
        if "空内容应当被拒绝" in str(exc):
            raise
    # 上面 put 会抛 HTTP 400 → put() 内部 AssertionError，这里已覆盖


# ---------------------------------------------------------------- 设置
def t_settings() -> None:
    items = get("/api/settings").json()["items"]
    keys = {item["key"] for item in items}
    # 关键项必须存在（不硬编码总数，避免加配置后测试误报）
    for key in ("llm_base_url", "llm_model", "llm_thinking", "asr_provider", "vad_silence_ms", "judge_confidence"):
        assert key in keys, f"缺少设置项 {key}"
    secrets = [i for i in items if i["secret"]]
    assert secrets and all(i["value"] in ("已配置", "未配置") for i in secrets), secrets
    # 敏感项不回传明文
    assert not any(len(i["value"]) > 20 for i in secrets), "疑似泄露明文 Key"
    put("/api/settings", json={"values": {"judge_confidence": "0.61"}})
    now = {i["key"]: i for i in get("/api/settings").json()["items"]}
    assert now["judge_confidence"]["value"] == "0.61" and now["judge_confidence"]["overridden"]
    put("/api/settings", json={"values": {"judge_confidence": ""}})
    now = {i["key"]: i for i in get("/api/settings").json()["items"]}
    assert now["judge_confidence"]["value"] == "0.6", now["judge_confidence"]


# ---------------------------------------------------------------- 日志与余额
def t_logs() -> None:
    stats = get("/api/llm/stats").json()
    assert "total_calls" in stats and "today_tokens" in stats
    calls = get("/api/llm/calls?limit=5").json()
    assert "items" in calls and "total" in calls
    if calls["items"]:
        detail = get(f"/api/llm/calls/{calls['items'][0]['id']}").json()
        assert "request" in detail and "response" in detail


def t_balance() -> None:
    data = get("/api/balance").json()
    assert "supported" in data
    if data["supported"]:
        assert isinstance(data["total"], (int, float))


# ---------------------------------------------------------------- 会话
def t_sessions() -> None:
    items = get("/api/sessions?limit=5").json()["items"]
    assert isinstance(items, list)
    if items:
        doc = get(f"/api/sessions/{items[0]['id']}").json()
        assert "utterances" in doc and "questions" in doc


def t_session_flow() -> None:
    post("/api/session/stop")  # 先确保干净
    pos = get("/api/positions").json()["items"][0]
    start = post("/api/session/start", json={"engine": "local", "role": pos["name"]}).json()
    assert start.get("active") and start.get("session_id"), start
    time.sleep(1)
    post("/api/session/inject", json={"channel": "interviewer", "text": "如何衡量检索质量"})
    card = None
    for _ in range(25):
        time.sleep(1)
        doc = get("/api/session/latest").json()
        qs = doc.get("questions") or []
        if qs and qs[-1].get("answers"):
            card = qs[-1]
            break
    assert card, "没有生成卡片"
    answers = card.get("answers") or {}
    assert "standard" in answers, answers.keys()
    report = post("/api/session/finish").json()
    assert "overall_score" in report, report
    post("/api/session/stop")


def t_manual_ask() -> None:
    post("/api/session/stop")
    pos = get("/api/positions").json()["items"][0]
    post("/api/session/start", json={"engine": "local", "role": pos["name"]})
    time.sleep(1)
    post("/api/answer/generate", json={"question": "你做过最有挑战的项目是什么"})
    time.sleep(2)
    doc = get("/api/session/latest").json()
    assert doc.get("questions"), "手工提问没有产出卡片"
    post("/api/session/stop")


def t_simulate() -> None:
    post("/api/session/stop")
    pos = get("/api/positions").json()["items"][0]
    post("/api/session/start", json={"engine": "local", "role": pos["name"]})
    time.sleep(1)
    r = post(
        "/api/session/sim",
        json={"enabled": True, "audible": False, "mode": "bank"},
    ).json()
    assert r.get("ok") and r.get("enabled") and r.get("count", 0) > 0, r
    time.sleep(18)
    doc = get("/api/session/latest").json()
    post("/api/session/sim", json={"enabled": False})
    post("/api/session/stop")
    assert doc.get("questions"), "仿真没有产出卡片"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--slow", action="store_true")
    parser.add_argument("--fast", action="store_true", help="只跑接口面，跳过需要大模型的会话链路")
    args = parser.parse_args()

    print("== 基础 ==")
    for name, fn in [("health", t_health), ("页面资源", t_page), ("设备列表", t_devices)]:
        check(name, fn)

    print("== 岗位 / 题库 / 提示词 / 设置 ==")
    for name, fn in [
        ("岗位 CRUD", t_positions),
        ("题库 CRUD", t_bank),
        ("提示词 CRUD", t_prompts),
        ("设置读写", t_settings),
    ]:
        check(name, fn)

    print("== 日志 / 余额 / 历史 ==")
    for name, fn in [("调用日志", t_logs), ("余额", t_balance), ("历史会话", t_sessions)]:
        check(name, fn)

    if args.fast:
        print("== 会话链路（含模型调用）已跳过（--fast） ==")
    else:
        print("== 会话链路（含模型调用） ==")
        for name, fn in [("实时会话 + 三版答案 + 评分", t_session_flow), ("手工提问", t_manual_ask)]:
            check(name, fn)

    if args.slow:
        print("== 仿真（慢） ==")
        check("仿真面试", t_simulate)

    print("\n" + "=" * 56)
    print(f"通过 {len(passed)} / 失败 {len(failed)}")
    for name, err in failed:
        print(f"  ✗ {name}: {err}")
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
