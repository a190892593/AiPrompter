"""真实环回采集自检：让系统真的从默认扬声器放一段语音，验证环回采集 → 转写 → 意图识别。

注入音频只是走队列，测不到"物理环回"这一段；这里用系统默认输出（通常是会议用的那个
扬声器）播放 TTS，环回设备应该能听到并把整句话转出来。

用法：python tools/check_loopback.py
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.tts import synthesize_with_fallback  # noqa: E402

BASE = "http://127.0.0.1:8765"
c = httpx.Client(base_url=BASE, timeout=180)

TEXT = "请讲一下 RAG 的工作流程是什么"


def loopback_device() -> str:
    data = c.get("/api/devices").json()
    default_out = (data.get("default_output") or "").split(" (")[0].strip().lower()
    outputs = [item["name"] for item in data.get("outputs") or []]
    for name in outputs:
        if default_out and default_out in name.lower():
            return name
    return outputs[0] if outputs else ""


def main() -> int:
    state = c.get("/api/session/state").json()
    if state.get("active"):
        c.post(f"/api/interviews/{state['session_id']}/stop")

    loop_dev = loopback_device()
    data = c.get("/api/devices").json()
    mic = data.get("default_input") or (data.get("inputs") or [{}])[0].get("name", "")
    print(f"环回={loop_dev!r} 麦克风={mic!r}")

    wav = synthesize_with_fallback(TEXT)
    print("合成完毕:", wav, wav.stat().st_size if hasattr(wav, "stat") else "")

    payload = {
        "role": "环回自检岗位",
        "resume": "三年 RAG 经验。",
        "position_id": None,
        "position_name": "环回自检岗位",
        "level": "mid",
        "engine": "local",
        "answer_level": "standard",
        "output_device": loop_dev,
        "input_device": mic,
        "mode": "real",
        "background": "",
    }
    r = c.post("/api/interviews", json=payload)
    assert r.status_code == 200, r.text[:200]
    iid = r.json()["session_id"]
    print("面试:", iid, "设备:", r.json()["devices"])

    try:
        time.sleep(1.5)
        # 用系统默认输出播放（默认输出就是会议软件用的那个扬声器，这里和环回选的是同一个）
        player = subprocess.Popen(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                f"(New-Object Media.SoundPlayer '{wav}').PlaySync()",
            ]
        )
        player.wait(timeout=30)

        def heard():
            doc = c.get(f"/api/interviews/{iid}").json()
            lines = [u for u in doc.get("utterances") or [] if u["channel"] == "interviewer"]
            return lines[-1] if lines else None

        deadline = time.time() + 25
        line = None
        while time.time() < deadline:
            line = heard()
            if line:
                break
            time.sleep(1)
        assert line, "环回没有转出任何文字（检查默认输出是否就是会议用的扬声器）"
        print("环回转写:", line["text"])
        # 至少要对上一部分关键词，避免"有字但全是噪音"
        assert any(word in line["text"] for word in ("RAG", "工作流程", "检")), line["text"]

        def card():
            doc = c.get(f"/api/interviews/{iid}").json()
            return (doc.get("questions") or [None])[0]

        deadline = time.time() + 30
        first = None
        while time.time() < deadline:
            first = card()
            if first and first.get("answer"):
                break
            time.sleep(1)
        assert first and first.get("question"), "环回语音没有判出问题"
        print("意图识别:", first["question"], "| 答案字数:", len(first.get("answer") or ""))
        print("PASS 物理环回采集 → 转写 → 意图识别 → 话术 全通")
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
