"""下载本地流式识别模型（sherpa-onnx paraformer，中英双语）。

默认从 hf-mirror.com 拉取（国内可直连），约 230MB，只需下载一次。

用法：
    python tools/download_models.py
"""

from __future__ import annotations

import argparse
import sys
import time
import urllib.request
from pathlib import Path

# Windows 控制台默认 GBK，打印 ✓ / ✗ 这类符号会崩，统一转成 UTF-8
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass


DEFAULT_REPO = "csukuangfj/sherpa-onnx-streaming-paraformer-bilingual-zh-en"
MIRROR = "https://hf-mirror.com"
FILES = ["encoder.int8.onnx", "decoder.int8.onnx", "tokens.txt"]

# 题库语义召回用的向量模型（约 23MB，纯 CPU 推理）
SEMANTIC_REPO = "Xenova/bge-small-zh-v1.5"
SEMANTIC_FILES = [("onnx/model_quantized.onnx", "model.onnx"), ("tokenizer.json", "tokenizer.json")]

MODELS_DIR = Path(__file__).resolve().parent.parent / "models"


def download(url: str, target: Path) -> None:
    if target.is_file() and target.stat().st_size > 0:
        print(f"  跳过（已存在）{target.name}")
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".part")
    started = time.time()

    with urllib.request.urlopen(url, timeout=60) as response, open(tmp, "wb") as handle:
        total = int(response.headers.get("Content-Length") or 0)
        done = 0
        while True:
            chunk = response.read(1024 * 256)
            if not chunk:
                break
            handle.write(chunk)
            done += len(chunk)
            if total:
                percent = done * 100 / total
                speed = done / max(0.001, time.time() - started) / 1024 / 1024
                sys.stdout.write(f"\r  {target.name} {percent:5.1f}%  {speed:.1f} MB/s")
                sys.stdout.flush()
    print()
    tmp.replace(target)


def list_files(repo: str, mirror: str) -> list[str]:
    """从镜像的 API 拿仓库文件列表，自动挑模型与词表（不同模型文件名不一样）。"""
    import json
    import urllib.request

    url = f"{mirror}/api/models/{repo}"
    with urllib.request.urlopen(url, timeout=30) as response:
        payload = json.loads(response.read().decode("utf-8"))
    names = [item["rfilename"] for item in payload.get("siblings", [])]
    wanted = [n for n in names if n.endswith(".onnx") or n == "tokens.txt"]
    return wanted or FILES


def download_semantic(mirror: str) -> int:
    """下载 bge-small-zh 向量模型（题库语义召回用）。"""
    target_dir = MODELS_DIR / "bge-small-zh-v1.5"
    print(f"语义向量模型目录: {target_dir}")
    for remote, local in SEMANTIC_FILES:
        url = f"{mirror}/{SEMANTIC_REPO}/resolve/main/{remote}"
        print(f"下载 {local}")
        try:
            download(url, target_dir / local)
        except Exception as exc:  # noqa: BLE001
            print(f"  失败: {exc}")
            return 1
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", default=DEFAULT_REPO)
    parser.add_argument("--mirror", default=MIRROR)
    parser.add_argument("--semantic", action="store_true", help="同时下载题库语义召回模型（约 23MB）")
    parser.add_argument("--only-semantic", action="store_true", help="只下载语义召回模型")
    args = parser.parse_args()

    if args.semantic or args.only_semantic:
        code = download_semantic(args.mirror)
        if code != 0 or args.only_semantic:
            return code
        print()

    if args.only_semantic:
        print("\n完成。")
        return 0

    target_dir = MODELS_DIR / args.repo.split("/")[-1]
    print(f"模型目录: {target_dir}")
    print("查询文件列表…")
    try:
        files = list_files(args.repo, args.mirror)
    except Exception as exc:  # noqa: BLE001
        print(f"  列表获取失败，用默认文件名: {exc}")
        files = FILES

    for name in files:
        url = f"{args.mirror}/{args.repo}/resolve/main/{name}"
        print(f"下载 {name}")
        try:
            download(url, target_dir / name)
        except Exception as exc:  # noqa: BLE001
            print(f"  失败: {exc}")
            return 1

    print("\n完成。重启后端后 /api/engines 中 local.available 应为 true。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
