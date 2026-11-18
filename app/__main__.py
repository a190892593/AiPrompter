"""`python -m app` 入口。

拉起 uvicorn。默认**不打开浏览器、不保留控制台窗口**（日志写 data/logs/app.log）；
需要调试时加 --console，需要自动开页时加 --open-browser。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

if ROOT.is_dir() and str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _hide_console_if_dedicated() -> None:
    """由 cmd start / 快捷方式单独拉起的控制台窗口，启动后自动隐藏。

    控制台里如果只有 python 进程（venv 启动器 + 真正的解释器），说明是后台拉起的；
    如果还挂着 powershell/cmd/explorer，说明是从终端手动启动的，不能藏（否则终端一起没了）。
    """
    if sys.platform != "win32":
        return
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.windll.kernel32
        processes = (ctypes.c_uint * 16)()
        count = kernel32.GetConsoleProcessList(processes, 16)
        if count <= 0:
            return

        names: list[str] = []
        for pid in list(processes)[:count]:
            handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
            if not handle:
                continue
            try:
                buffer = ctypes.create_unicode_buffer(320)
                size = wintypes.DWORD(len(buffer))
                if kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
                    names.append(buffer.value.rsplit("\\", 1)[-1].lower())
            finally:
                kernel32.CloseHandle(handle)

        if names and all(name in ("python.exe", "pythonw.exe") for name in names):
            hwnd = kernel32.GetConsoleWindow()
            if hwnd:
                ctypes.windll.user32.ShowWindow(hwnd, 0)  # SW_HIDE
    except Exception:  # noqa: BLE001 - 隐藏失败不影响服务
        pass


def _open_browser_later(url: str) -> None:
    """服务起来之后再打开，避免浏览器先到、连接被拒。"""
    import threading
    import time
    import webbrowser

    def worker() -> None:
        time.sleep(1.8)
        try:
            webbrowser.open(url)
        except Exception:  # noqa: BLE001 - 打不开就让用户手动点
            pass

    threading.Thread(target=worker, name="open-browser", daemon=True).start()


def main() -> int:
    import faulthandler

    # 音频库是原生实现，崩溃时没有 Python traceback；打开它能留下调用栈
    faulthandler.enable()

    parser = argparse.ArgumentParser(description="AI 面试副驾 · 本地桥")
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--reload", action="store_true", help="开发模式，代码改动自动重启")
    parser.add_argument("--open-browser", action="store_true", help="启动后自动打开浏览器")
    parser.add_argument("--no-browser", action="store_true", help="兼容旧参数：不打开浏览器")
    parser.add_argument("--console", action="store_true", help="保留控制台窗口，方便看日志")
    args = parser.parse_args()

    if not args.console:
        _hide_console_if_dedicated()

    import uvicorn

    from .config import settings

    host = args.host or settings.host
    port = args.port or settings.port
    # 让启动日志里的地址和实际监听一致（--port 会覆盖配置）
    settings.host, settings.port = host, port
    url = f"http://{'127.0.0.1' if host in ('0.0.0.0', '::') else host}:{port}/"

    print()
    print("  ┌─────────────────────────────────────────────┐")
    print("  │  面试副驾已启动                              │")
    print(f"  │  打开这个地址：{url:<28}│")
    print("  └─────────────────────────────────────────────┘")
    print(f"  意图识别模型: {settings.llm_model}    参考答案模型: {settings.answer_model}")
    print(f"  识别引擎: {'火山豆包流式 2.0' if settings.volc_speech_key else '未配置（会退回演示脚本）'}")
    print(f"  日志文件: {settings.log_path if settings.log_file else '仅控制台'}")
    print()

    if args.open_browser and not args.no_browser:
        _open_browser_later(url)

    # log_config=None：不让 uvicorn 另起一套 handler，日志统一进我们的控制台 + 文件
    if args.reload:
        uvicorn.run("app.server:app", host=host, port=port, reload=True, log_config=None)
    else:
        from .server import app

        uvicorn.run(app, host=host, port=port, log_config=None)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
