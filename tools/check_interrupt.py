"""打断自检：第一题答案还在流式输出时就来第二题，第一题要保留半截答案。

同时检查两路采集是否真的都活着（snapshot.captures）。
"""

from __future__ import annotations

import sys
import time

import httpx

BASE = "http://127.0.0.1:8765"
c = httpx.Client(base_url=BASE, timeout=180)


def devices() -> tuple[str, str]:
    data = c.get("/api/devices").json()
    out = (data.get("outputs") or [{}])[0].get("name", "")
    ins = [i["name"] for i in data.get("inputs") or [] if "Loopback" not in i["name"]]
    return out, ins[0] if ins else ""


def main() -> int:
    state = c.get("/api/session/state").json()
    if state.get("active"):
        c.post(f"/api/interviews/{state['session_id']}/stop")

    out_dev, in_dev = devices()
    payload = {
        "role": "打断测试岗位",
        "resume": "三年 RAG 经验，Recall@10 0.62→0.81。",
        "position_id": None,
        "position_name": "打断测试岗位",
        "level": "mid",
        "engine": "local",
        "answer_level": "standard",
        "output_device": out_dev,
        "input_device": in_dev,
        "mode": "real",
        "background": "",
    }
    r = c.post("/api/interviews", json=payload)
    assert r.status_code == 200, r.text[:200]
    iid = r.json()["session_id"]
    print("面试:", iid)

    state = c.get("/api/session/state").json()
    caps = state.get("captures") or {}
    assert caps.get("interviewer") is True and caps.get("candidate") is True, caps
    print("PASS 两路采集都活着:", caps)

    try:
        # 第一题
        c.post("/api/session/inject_audio", json={"channel": "interviewer", "text": "请讲一下 RAG 的工作流程是什么"})
        # 等到出卡、答案开始流式（约 4 秒）就马上问第二题，制造打断
        deadline = time.time() + 30
        card1 = None
        while time.time() < deadline:
            doc = c.get(f"/api/interviews/{iid}").json()
            questions = doc.get("questions") or []
            if questions and questions[0].get("question"):
                card1 = questions[0]
                break
            time.sleep(0.5)
        assert card1, "第一题没出卡"
        time.sleep(2.5)

        c.post("/api/session/inject_audio", json={"channel": "interviewer", "text": "那向量检索和关键词检索有什么区别"})

        # 等第二题出卡
        deadline = time.time() + 40
        while time.time() < deadline:
            doc = c.get(f"/api/interviews/{iid}").json()
            if len(doc.get("questions") or []) >= 2:
                break
            time.sleep(1)

        doc = c.get(f"/api/interviews/{iid}").json()
        questions = doc.get("questions") or []
        assert len(questions) >= 2, f"第二题没出卡: {len(questions)}"
        first = questions[0]
        print(
            "第一题: answer=%d字 answers=%s user_answer=%s"
            % (
                len(first.get("answer") or ""),
                sorted((first.get("answers") or {}).keys()),
                "有" if first.get("user_answer") else "无",
            )
        )
        assert first.get("answer"), "被下一题打断后，第一题的半截答案没有保存"
        print("PASS 被打断的答案保留了已生成部分")

        # 第二题正常完成
        deadline = time.time() + 40
        while time.time() < deadline:
            doc = c.get(f"/api/interviews/{iid}").json()
            second = (doc.get("questions") or [])[-1]
            if second.get("answer") and (second.get("answers") or {}).get("standard"):
                break
            time.sleep(1)
        second = (doc.get("questions") or [])[-1]
        assert second.get("answer"), "第二题没有答案"
        print("PASS 第二题正常出答案（%d 字，版本=%s）" % (len(second["answer"]), sorted((second.get("answers") or {}).keys())))

        r = c.post(f"/api/interviews/{iid}/stop")
        body = r.json()
        assert body["interview"]["status"] == "finished"
        total = sum(1 for q in c.get(f"/api/interviews/{iid}").json().get("questions") or [] if q.get("answer"))
        print(f"PASS 停止并评分：{total} 题有答案，总分 {(body.get('report') or {}).get('overall_score')}")
        return 0
    finally:
        try:
            c.post(f"/api/interviews/{iid}/stop")
        except Exception:  # noqa: BLE001
            pass
        try:
            c.delete(f"/api/interviews/{iid}")
        except Exception:  # noqa: BLE001
            pass


if __name__ == "__main__":
    sys.exit(main())
