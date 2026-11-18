"""本地桥 HTTP 服务：设备、会话控制、事件推送、静态页面。

浏览器在这里只是显示器：音频全部由本进程采集，前端连一帧 PCM 都不用碰。
"""

from __future__ import annotations

import asyncio
import re
import logging
import time

import httpx
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException
from pydantic import BaseModel

from . import (
    resume_parse,
    asr,
    bank,
    interviews,
    llmlog,
    memory,
    positions,
    prompts,
    report,
    resumes,
    settings_store,
    store,
    judge,
)
from .audio import enumerate_devices
from .config import PROJECT_DIR, settings
from .llm import llm
from .logging_setup import mask, setup
from .schemas import FeedbackIn, InjectIn, InterviewIn, SessionConfig
from .simulator import Simulator
from .state import session

setup()  # 统一日志：控制台 + data/logs/app.log（app/__init__ 已配过一次，这里兜底）
log = logging.getLogger("server")


class Clients:
    def __init__(self) -> None:
        self._clients: set[WebSocket] = set()

    async def add(self, socket: WebSocket) -> None:
        self._clients.add(socket)

    def discard(self, socket: WebSocket) -> None:
        self._clients.discard(socket)

    async def broadcast(self, payload: dict) -> None:
        dead = []
        for socket in list(self._clients):
            try:
                await socket.send_json(payload)
            except Exception as exc:  # noqa: BLE001
                dead.append(socket)
                log.debug("推送 %s 到一个客户端失败，丢弃该连接: %s", payload.get("type"), exc)
        for socket in dead:
            self._clients.discard(socket)
        if dead:
            log.info("清理失效的事件通道 %d 个，剩 %d 个", len(dead), len(self._clients))

    @property
    def count(self) -> int:
        return len(self._clients)


clients = Clients()


@asynccontextmanager
async def lifespan(_: FastAPI):
    settings_store.apply_all()
    log.info("=" * 24 + " 服务启动 " + "=" * 24)
    log.info("地址: http://%s:%s  数据目录: %s", settings.host, settings.port, settings.data_dir)
    log.info(
        "日志: %s（级别 %s）",
        settings.log_path if settings.log_file else "仅控制台",
        settings.log_level,
    )
    log.info(
        "大模型: %s 意图识别=%s 参考答案=%s thinking=%s key=%s",
        settings.llm_base_url,
        settings.llm_model,
        settings.answer_model,
        "开" if settings.llm_thinking else "关",
        mask(settings.llm_api_key),
    )
    log.info(
        "识别: provider=%s 火山Key=%s 本地模型目录=%s",
        settings.asr_provider,
        mask(settings.volc_speech_key),
        settings.models_dir,
    )
    log.info(
        "参数: 能量门限=%.4f 静音判定=%sms 意图识别去抖=%sms 意图识别超时=%.0fs 置信度门槛=%.2f",
        settings.vad_threshold,
        settings.vad_silence_ms,
        settings.judge_debounce_ms,
        settings.judge_timeout,
        settings.judge_confidence,
    )
    # 启动巡检：上次进程留下的「进行中」记录，一律改成「已中断（可继续）」
    await asyncio.to_thread(interviews.reconcile_orphans)
    watchdog = asyncio.create_task(_interview_watchdog())
    # 预热意图识别提示词前缀（DeepSeek 前缀缓存）+ HTTP 连接，降低首题延迟
    asyncio.create_task(judge.warmup())
    yield
    watchdog.cancel()
    if session.active:
        log.info("退出前结束仍在运行的会话: %s", session.session_id)
        await session.stop()
    await llm.aclose()
    log.info("=" * 24 + " 服务已停止 " + "=" * 24)


app = FastAPI(title="AI 面试副驾 · 本地桥", version="2.0.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------- 状态与设备


@app.get("/api/health")
async def health() -> dict:
    return {
        "status": "ok",
        "active": session.active,
        "engine": session.engine_kind,
        "llm_live": llm.live,
        "model": settings.llm_model if llm.live else "rules",
        "devices": session.devices,
    }


@app.get("/api/devices")
async def devices() -> dict:
    try:
        return await asyncio.to_thread(enumerate_devices)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(500, f"枚举音频设备失败：{exc}") from exc


@app.get("/api/engines")
async def engines() -> dict:
    return {"engines": asr.available_engines(), "llm_live": llm.live}


class MonitorIn(BaseModel):
    channel: str = "both"  # speaker | mic
    output_device: str = ""
    input_device: str = ""


_monitors: dict[str, object] = {}
_monitor_levels: dict[str, float] = {"interviewer": 0.0, "candidate": 0.0}
_monitor_task: asyncio.Task | None = None


async def _stop_monitors() -> None:
    global _monitor_task
    for capture in list(_monitors.values()):
        try:
            await asyncio.to_thread(capture.stop)  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            pass
    _monitors.clear()
    _monitor_levels["interviewer"] = 0.0
    _monitor_levels["candidate"] = 0.0
    if _monitor_task and not _monitor_task.done():
        _monitor_task.cancel()
    _monitor_task = None


async def _monitor_loop() -> None:
    while _monitors:
        await clients.broadcast({"type": "monitor", "levels": dict(_monitor_levels)})
        await asyncio.sleep(0.1)


@app.post("/api/devices/monitor")
async def monitor_start(body: MonitorIn) -> dict:
    """实时电平检测（像系统声音设置那样看波形条），不启动识别、不产生费用。"""
    from .audio import CaptureThread, resolve_input, resolve_loopback

    global _monitor_task
    if session.active:
        raise HTTPException(409, "面试进行中，请先结束再检测设备")

    keys = {"speaker": ["interviewer"], "mic": ["candidate"], "both": ["interviewer", "candidate"]}.get(
        body.channel
    )
    if not keys:
        raise HTTPException(400, "channel 必须是 speaker / mic / both")

    await _stop_monitors()

    devices: dict[str, str] = {}
    for key in keys:
        resolver = resolve_loopback if key == "interviewer" else resolve_input
        name = body.output_device if key == "interviewer" else body.input_device
        try:
            device = await asyncio.to_thread(resolver, name or None)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(500, f"找不到设备：{exc}") from exc

        def make_sink(channel: str):
            def sink(_channel: str, _pcm: bytes, rms: float) -> None:
                _monitor_levels[channel] = rms

            return sink

        capture = CaptureThread(
            device=device, channel=key, frame_ms=settings.frame_ms, sink=make_sink(key)
        )
        capture.start()
        _monitors[key] = capture
        devices[key] = device["name"]

    if _monitor_task is None or _monitor_task.done():
        _monitor_task = asyncio.create_task(_monitor_loop())
    return {"ok": True, "devices": devices}


@app.post("/api/devices/monitor/stop")
async def monitor_stop() -> dict:
    await _stop_monitors()
    await clients.broadcast({"type": "monitor", "levels": {"interviewer": 0.0, "candidate": 0.0}})
    return {"ok": True}


# ---------------------------------------------------------------- 会话


@app.post("/api/session/start")
async def start(body: SessionConfig) -> dict:
    await _stop_monitors()  # 检测用的采集必须先释放设备
    await _stop_simulation()
    log.info(
        "开始面试(接口): 岗位=%r 级别=%s 引擎=%s JD=%d字 扬声器=%r 麦克风=%r",
        body.role,
        body.level,
        body.engine,
        len(body.background.strip()),
        body.output_device,
        body.input_device,
    )
    try:
        started = await session.start(body, clients.broadcast)
        asyncio.create_task(asyncio.to_thread(bank.warmup))  # 预热检索，降低首字延迟
        log.info(
            "面试已启动: 会话=%s 引擎=%s 设备=%s",
            started.get("session_id"),
            started.get("engine"),
            started.get("devices"),
        )
        return started
    except RuntimeError as exc:
        log.warning("开始面试被拒: %s", exc)
        raise HTTPException(409, str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        log.exception("启动失败: 岗位=%r 引擎=%s", body.role, body.engine)
        raise HTTPException(500, f"启动失败：{exc}") from exc


@app.post("/api/session/stop")
async def stop() -> dict:
    log.info("停止面试(接口): 会话=%s", session.session_id or "-")
    await _stop_simulation()
    snapshot = await session.stop()
    llmlog.set_interview("")
    log.info("已停止: 转写=%d 句 卡片=%d 张", len(session.utterances), snapshot.get("turns", 0))
    return snapshot


class DeviceSwitchIn(BaseModel):
    output_device: str = ""
    input_device: str = ""


@app.post("/api/session/devices")
async def session_devices(body: DeviceSwitchIn) -> dict:
    """面试进行中切换扬声器环回/麦克风，不重启会话。"""
    if not session.active:
        raise HTTPException(409, "还没有开始会话")
    log.info("切换设备: 扬声器=%r 麦克风=%r", body.output_device, body.input_device)
    try:
        snapshot = await session.switch_devices(body.output_device, body.input_device)
    except RuntimeError as exc:
        log.warning("切换设备失败: %s", exc)
        raise HTTPException(400, str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        log.exception("切换设备失败")
        raise HTTPException(500, f"切换失败：{exc}") from exc
    await clients.broadcast({"type": "session", "active": True, **snapshot})
    return {"ok": True, **snapshot}


class EngineSwitchIn(BaseModel):
    engine: str


@app.post("/api/session/engine")
async def session_engine(body: EngineSwitchIn) -> dict:
    """面试进行中切换识别引擎（本地/火山/云端），不重开会话。"""
    if not session.active:
        raise HTTPException(409, "还没有开始会话")
    log.info("切换识别引擎: %s", body.engine)
    try:
        snapshot = await session.switch_engine(body.engine)
    except RuntimeError as exc:
        log.warning("切换识别引擎失败: %s", exc)
        raise HTTPException(400, str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        log.exception("切换识别引擎失败")
        raise HTTPException(500, f"切换失败：{exc}") from exc
    await clients.broadcast(
        {"type": "session", "active": True, "engine": session.engine_kind, **snapshot}
    )
    return {"ok": True, **snapshot}


_sim: Simulator | None = None


async def _stop_simulation() -> None:
    global _sim
    if _sim is not None:
        await _sim.stop()
        _sim = None


async def _start_simulation(
    questions: list[str],
    *,
    audible: bool = True,
    more_questions=None,
    mode: str = "bank",
    max_questions: int | None = None,
) -> int:
    """用给定题目启动仿真朗读；more_questions 用于题库一轮问完后继续抽题。"""
    global _sim
    await _stop_simulation()
    cleaned = [question.strip() for question in questions if question and question.strip()]
    if not cleaned and mode != "resume":
        return 0
    limit = settings.sim_question_limit if max_questions is None else max_questions
    _sim = Simulator(
        session,
        cleaned,
        audible=audible,
        more_questions=more_questions,
        mode=mode,
        max_questions=limit,
    )
    _sim.start()
    return min(len(cleaned), limit) if limit > 0 else len(cleaned)


async def _apply_simulation(
    *,
    enabled: bool,
    audible: bool | None = None,
    mode: str | None = None,
    questions: list[str] | None = None,
    more_questions=None,
    limit: int | None = None,
) -> int:
    """仿真的唯一入口：开/关、改参数、启动都在这里。

    已经在跑时只更新参数（不重启、不换题）；关闭时停掉仿真器。
    """
    if not enabled:
        await _stop_simulation()
        session.meta["sim_enabled"] = False
        session.meta["mode"] = "real"
        await session._persist()
        return 0

    sim_mode = mode or str(session.meta.get("sim_mode") or "resume")
    if sim_mode not in ("bank", "resume"):
        sim_mode = "resume"

    if _sim is not None:
        if audible is not None:
            _sim.audible = bool(audible)
        if mode is not None:
            _sim.mode = sim_mode
        session.meta["sim_mode"] = sim_mode
        session.meta["sim_enabled"] = True
        session.meta["mode"] = "simulate"
        await session._persist()
        return _sim.max_questions or len(_sim.questions)

    size = settings.sim_question_limit if limit is None else limit
    enable_audio = True if audible is None else bool(audible)
    position = str(session.meta.get("position_name") or "")
    if questions is None:
        questions = await asyncio.to_thread(bank.pick_questions, size, position)
    if more_questions is None:
        more_questions = lambda: bank.pick_questions(size, position)  # noqa: E731
    count = await _start_simulation(
        questions,
        audible=enable_audio,
        more_questions=more_questions,
        mode=sim_mode,
        max_questions=size,
    )
    if count == 0 and sim_mode == "bank":
        raise HTTPException(400, "题库是空的，先去「面试题库」加几道题")
    session.meta["sim_mode"] = sim_mode
    session.meta["sim_enabled"] = True
    session.meta["mode"] = "simulate"
    await session._persist()
    return count


class SimToggleIn(BaseModel):
    enabled: bool | None = None
    audible: bool | None = None
    mode: str | None = None


@app.post("/api/session/sim")
async def session_sim_toggle(body: SimToggleIn) -> dict:
    """面试进行中的「仿真开关」：打开=自动出题+TTS 音频输入+回答回放；关闭=恢复真实采集。"""
    if not session.active:
        raise HTTPException(409, "面试没在运行，先开始一场面试")
    if body.enabled is False:
        await _apply_simulation(enabled=False)
        log.info("仿真开关: 关闭，恢复真实采集")
        count = 0
    else:
        if _sim is None and body.enabled is not True:
            raise HTTPException(409, "当前没有仿真在跑，先开启仿真输入")
        count = await _apply_simulation(
            enabled=True, audible=body.audible, mode=body.mode
        )
        log.info("仿真开关: 参数已更新 题目上限=%d 模式=%s", count, body.mode or "-")
    await clients.broadcast({"type": "session", "active": True, **session.snapshot()})
    return {
        "ok": True,
        "enabled": _sim is not None,
        "count": count,
        **session.snapshot(),
    }


@app.post("/api/session/finish")
async def finish() -> dict:
    rounds = len(session.pipeline.qa) if session.pipeline else 0
    log.info("生成评分报告: 轮次=%d", rounds)
    result = await session.finish()
    log.info(
        "评分完成: 总分=%s 判定=%r 降级=%s",
        result.get("overall_score"),
        str(result.get("verdict", ""))[:40],
        result.get("degraded"),
    )
    return result


@app.get("/api/session/state")
async def state() -> dict:
    return session.snapshot()


# ---------------------------------------------------------------- 面试记录（一场一条）
#
# 新建面试 → 立即落一条「进行中」记录并开始计时；
# 只有手动停止才算正常结束；超过 2 小时未停止的，一律标记系统自动中断；
# 没结束的记录可以重新进入，对话流与提词卡片原样恢复。


@app.get("/api/interviews")
async def interview_list(status: str = "", limit: int = 100) -> dict:
    items = await asyncio.to_thread(interviews.list_all, min(500, limit), status)
    return {"items": items}


@app.get("/api/interviews/running")
async def interview_running() -> dict:
    """还没结束的面试：进行中（alive）或已中断（可继续）。"""
    docs = await asyncio.to_thread(interviews.open_interviews, 1)
    if not docs:
        return {"item": None}
    item = interviews.summary(docs[0])
    item["alive"] = bool(session.active and session.session_id == item["id"])
    return {"item": item}


@app.get("/api/interviews/{interview_id}")
async def interview_detail(interview_id: str) -> dict:
    doc = await asyncio.to_thread(interviews.load, interview_id)
    if doc is None:
        raise HTTPException(404, "面试记录不存在")
    return doc


class BatchDeleteIn(BaseModel):
    ids: list[str] = []


@app.delete("/api/interviews/{interview_id}")
async def interview_delete(interview_id: str) -> dict:
    """删除一场面试记录（转写、提词、评分一起删）。进行中的必须先结束。"""
    if session.active and session.session_id == interview_id:
        raise HTTPException(409, "这场面试还在进行中，请先点「结束」再删除")
    doc = await asyncio.to_thread(interviews.load, interview_id)
    if doc is None:
        raise HTTPException(404, "面试记录不存在")
    if doc.get("status") == interviews.STATUS_RUNNING:
        raise HTTPException(409, "这场面试还在进行中，请先点「结束」再删除")
    if not await asyncio.to_thread(store.delete, interview_id):
        raise HTTPException(404, "面试记录不存在")
    log.info("删除面试记录: id=%s 岗位=%r", interview_id, doc.get("position_name"))
    return {"ok": True, "id": interview_id}


@app.post("/api/interviews/batch-delete")
async def interview_batch_delete(body: BatchDeleteIn) -> dict:
    """批量删除面试记录；进行中的会跳过并说明原因。"""
    deleted: list[str] = []
    skipped: list[dict[str, str]] = []
    for interview_id in body.ids[:200]:
        if session.active and session.session_id == interview_id:
            skipped.append({"id": interview_id, "reason": "进行中"})
            continue
        doc = await asyncio.to_thread(interviews.load, interview_id)
        if doc is None:
            skipped.append({"id": interview_id, "reason": "不存在"})
            continue
        if doc.get("status") == interviews.STATUS_RUNNING:
            skipped.append({"id": interview_id, "reason": "进行中"})
            continue
        if await asyncio.to_thread(store.delete, interview_id):
            deleted.append(interview_id)
        else:
            skipped.append({"id": interview_id, "reason": "不存在"})
    log.info("批量删除面试记录: 成功=%d 跳过=%d", len(deleted), len(skipped))
    return {"ok": True, "deleted": len(deleted), "skipped": skipped}


@app.post("/api/interviews")
async def interview_create(body: InterviewIn) -> dict:
    """新建面试：岗位 + 简历必填；建记录的同时开始计时。

    如果已经有一场在跑，不拦着——先把那场挂起成「已中断（可继续）」再开新的，
    旧记录留在列表里，随时能点「继续」。
    """
    await _stop_monitors()
    await _stop_simulation()
    if session.active:
        previous = session.session_id
        log.info("已有面试在跑（%s），先挂起再开新的", previous)
        if session.meta.get("status") == "running":
            session.meta["status"] = "interrupted"
            session.meta["ended_at"] = time.time()
            session.meta["end_reason"] = "开始新面试时自动挂起"
        await session.stop()
        await clients.broadcast(
            {"type": "interview_stopped", "id": previous, "interview": session.snapshot().get("interview")}
        )

    role = body.role.strip()
    resume = body.resume.strip()
    if not role:
        raise HTTPException(400, "请先选择岗位")
    if not resume:
        raise HTTPException(400, "请先填写个人简历")

    cfg = body.to_config()
    # 岗位 JD/技能要求没随请求传时，从岗位库补上；简历提问模式需要它
    if body.position_id and not cfg.background.strip():
        _, position_context = await asyncio.to_thread(positions.context_text, body.position_id)
        if position_context.strip():
            cfg = cfg.model_copy(update={"background": position_context})
    log.info(
        "新建面试: 岗位=%r 级别=%s 模式=%s 引擎=%s 简历=%d字 扬声器=%r 麦克风=%r",
        role,
        body.level,
        body.mode,
        body.engine,
        len(resume),
        body.output_device,
        body.input_device,
    )
    doc = await asyncio.to_thread(
        interviews.create,
        cfg.model_dump(),
        position_id=body.position_id,
        position_name=body.position_name or role,
        resume=resume,
        mode="real",
        engine=body.engine,
    )
    try:
        snapshot = await session.start(cfg, clients.broadcast, interview=doc)
    except RuntimeError as exc:
        # 起不来（设备被占/没有环回）就把这条空记录删掉，别在列表里留垃圾
        await session.abort_start()
        await asyncio.to_thread(store.delete, doc["id"])
        raise HTTPException(409, str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        log.exception("新建面试启动失败")
        await session.abort_start()
        await asyncio.to_thread(store.delete, doc["id"])
        raise HTTPException(500, f"启动失败：{exc}") from exc

    asyncio.create_task(asyncio.to_thread(bank.warmup))

    session.meta["sim_enabled"] = session.meta.get("mode") == "simulate"
    await session._persist()  # 立刻把设备/引擎落到这条记录上
    doc["sim_mode"] = session.meta.get("sim_mode", "")
    await clients.broadcast({"type": "session", "active": True, **snapshot})
    log.info("面试已开始: id=%s 岗位=%r", doc["id"], role)
    # 注意顺序：snapshot 里也有 interview 字段，这里要用完整 summary 覆盖它
    return {"ok": True, **snapshot, "interview": interviews.summary(doc)}


@app.post("/api/interviews/{interview_id}/stop")
async def interview_stop(interview_id: str) -> dict:
    """手动停止：结束计时、停止采集、生成评分报告。"""
    doc = await asyncio.to_thread(interviews.load, interview_id)
    if doc is None:
        raise HTTPException(404, "面试记录不存在")
    if doc.get("status") == "auto_interrupted":
        raise HTTPException(409, "这场面试已因超时被标记为系统自动中断")
    if doc.get("status") == "finished":
        if not session.active:
            llmlog.set_interview("")
        return {"ok": True, "already": True, "interview": interviews.summary(doc)}

    live = session.active and session.session_id == interview_id
    log.info("停止面试: id=%s live=%s", interview_id, live)
    result: dict | None = doc.get("report") or None
    if live:
        await _stop_simulation()
        await session.stop()
        # 停采会清掉 llmlog 的面试绑定，但接下来生成评分报告也是这场的调用，
        # 先绑回来，结束流程走完再清
        llmlog.set_interview(interview_id)
        try:
            result = await session.finish()
        except Exception:  # noqa: BLE001
            log.exception("停止面试时评分失败")
        await session._persist()
    else:
        # 已经不在了（比如服务重启过）：直接把记录收尾，并按已有问答补一份评分
        await asyncio.to_thread(interviews.finish, doc)
        questions = doc.get("questions") or []
        if questions and not doc.get("report"):
            try:
                cfg = SessionConfig(
                    **{
                        key: value
                        for key, value in (doc.get("config") or {}).items()
                        if key in SessionConfig.model_fields
                    }
                )
                previous_interview = llmlog.current_interview()
                llmlog.set_interview(interview_id)
                try:
                    result = await report.build_report(cfg, questions)
                finally:
                    llmlog.set_interview(previous_interview if session.active else "")
                doc["report"] = result
            except Exception:  # noqa: BLE001
                log.exception("补评分失败")
        await asyncio.to_thread(store.save, doc)

    fresh = await asyncio.to_thread(interviews.load, interview_id) or doc
    # 结算本场模型费用（包含刚生成的评分报告调用）
    if fresh.get("status") == "finished" and not fresh.get("usage"):
        usage = await asyncio.to_thread(
            llmlog.usage_between, float(fresh.get("started_at") or 0), time.time()
        )
        fresh["usage"] = usage
        await asyncio.to_thread(store.save, fresh)
    if not session.active:
        llmlog.set_interview("")
    await clients.broadcast(
        {"type": "interview_stopped", "id": interview_id, "interview": interviews.summary(fresh)}
    )
    log.info(
        "面试已结束: id=%s 用时=%ss 总分=%s",
        interview_id,
        interviews.elapsed(fresh),
        (fresh.get("report") or {}).get("overall_score"),
    )
    return {
        "ok": True,
        **session.snapshot(),
        "interview": interviews.summary(fresh),
        "report": result or fresh.get("report"),
    }


@app.post("/api/interviews/{interview_id}/resume")
async def interview_resume(interview_id: str) -> dict:
    """重新进入一场没结束的面试：恢复采集、对话流与提词。"""
    if session.active:
        if session.session_id == interview_id:
            return {"ok": True, "already": True, **session.snapshot()}
        raise HTTPException(409, "已经有另一场面试在进行中")
    doc = await asyncio.to_thread(interviews.load, interview_id)
    if doc is None:
        raise HTTPException(404, "面试记录不存在")
    if not interviews.resume_ready(doc):
        raise HTTPException(
            409,
            "这场面试已经结束（或已因超时自动中断），不能继续；想再练请新建一场",
        )

    await _stop_monitors()
    await _stop_simulation()
    progress = interviews.elapsed(doc)
    interviews.reopen(doc)  # 计时从上次的进度接着走，中间的停机时间不算
    log.info("继续面试: id=%s 之前已进行=%ss", interview_id, progress)
    try:
        snapshot = await session.resume_from(doc, clients.broadcast)
    except RuntimeError as exc:
        await session.abort_start()
        raise HTTPException(409, str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        log.exception("继续面试失败")
        await session.abort_start()
        raise HTTPException(500, f"继续失败：{exc}") from exc
    asyncio.create_task(asyncio.to_thread(bank.warmup))

    # 仿真面试没有「继续」的题库游标：按题库重新抽一批没问过的题接着问，
    # 否则恢复后只会恢复采集，不会再有面试官提问。
    if (doc.get("mode") or "") == "simulate":
        asked = {
            str((item or {}).get("question") or (item or {}).get("raw") or "").strip()
            for item in (doc.get("questions") or [])
        }
        limit = settings.sim_question_limit
        pool = await asyncio.to_thread(
            bank.pick_questions, limit, doc.get("position_name") or ""
        )
        remaining = [text for text in pool if text and text not in asked]
        more = lambda: bank.pick_questions(limit, doc.get("position_name") or "")  # noqa: E731
        mode = str(doc.get("sim_mode") or "resume")
        count = await _apply_simulation(
            enabled=True,
            audible=True,
            mode=mode,
            questions=remaining,
            more_questions=more,
            limit=limit,
        )
        if mode == "resume":
            await session.broadcast_note("继续仿真面试：按简历 + 追问的方式提问")
        elif count:
            await session.broadcast_note(f"继续仿真面试：本轮 {count} 道题")
        else:
            await session.broadcast_note("题库里的题都练过了，这场仿真没有新题可问了")

    await session._persist()
    await clients.broadcast({"type": "session", "active": True, **snapshot})
    return {"ok": True, "resumed": True, **snapshot}


async def _interview_watchdog() -> None:
    """每 30 秒扫一次：超过 2 小时还没停的面试，标记系统自动中断。"""
    while True:
        await asyncio.sleep(30)
        try:
            if session.active and session.meta.get("status") == "running":
                deadline = float(session.meta.get("started_at") or 0) + interviews.AUTO_INTERRUPT_SECONDS
                if time.time() > deadline:
                    session.mark_auto_interrupted(deadline)
                    await _stop_simulation()
                    await clients.broadcast(
                        {
                            "type": "interview_auto_stopped",
                            "id": session.session_id,
                            "interview": await asyncio.to_thread(
                                lambda: interviews.summary(
                                    interviews.load(session.session_id) or {}
                                )
                            ),
                            "message": "超过 2 小时未手动结束，本场面试已标记为系统自动中断",
                        }
                    )
                    await session.stop()
            await asyncio.to_thread(interviews.list_all, 200, "running")  # 读取时顺便判定超时
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception("面试超时巡检失败")


@app.post("/api/session/resume")
async def resume_session() -> dict:
    """兼容旧入口：接续最近一场没结束的面试。"""
    if session.active:
        return {"ok": True, "already": True, **session.snapshot()}
    docs = await asyncio.to_thread(interviews.running, 1)
    if not docs:
        log.warning("恢复会话失败: 没有进行中的面试")
        raise HTTPException(404, "没有可以继续的面试")
    return await interview_resume(str(docs[0]["id"]))


@app.get("/api/session/latest")
async def latest_session() -> dict:
    """最近一次会话的完整记录，用于刷新后恢复对话流与提词。"""
    docs = await asyncio.to_thread(store.latest, 1)
    return docs[0] if docs else {}


class InjectAudioIn(BaseModel):
    channel: str = "interviewer"
    text: str


@app.post("/api/session/inject_audio")
async def inject_audio(body: InjectAudioIn) -> dict:
    """把一段文字用 TTS 合成后注入指定通道，模拟这个人在说话（仿真回放用）。"""
    import wave

    import numpy as np

    from .audio.resample import Resampler, to_int16
    from .tts import synthesize

    if not session.active:
        raise HTTPException(409, "还没有开始会话")
    if body.channel not in ("interviewer", "candidate"):
        raise HTTPException(400, "channel 必须是 interviewer 或 candidate")

    def prepare(text: str) -> bytes:
        wav_path = synthesize(text)
        with wave.open(str(wav_path), "rb") as handle:
            rate = handle.getframerate()
            channels = handle.getnchannels()
            raw = handle.readframes(handle.getnframes())
        data = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
        if channels > 1:
            data = data.reshape(-1, channels).mean(axis=1)
        return to_int16(Resampler(rate).process(data))

    pcm = await asyncio.to_thread(prepare, body.text)
    # 等音频真的"说完"再返回：否则调用方会以为已经说完，提前注入下一句，
    # 造成回答和问题错位一拍（仿真时踩过）。
    await session.feed_audio(body.channel, pcm)
    return {"ok": True, "seconds": round(len(pcm) / 2 / 16000, 1)}


class AnswerEditIn(BaseModel):
    question: str
    raw: str = ""
    thinking: bool | None = None   # None=跟随全局设置
    web_search: bool = False


class AnswerSuggestIn(BaseModel):
    raw: str
    current: str = ""


@app.post("/api/answer/suggest")
async def answer_suggest(body: AnswerSuggestIn) -> dict:
    """猜面试官可能真正想问的 5 个问题。"""
    if session.pipeline is None:
        raise HTTPException(409, "还没有开始会话")
    if not body.raw.strip():
        raise HTTPException(400, "缺少面试官原话")
    log.info("猜测真实问题: 原话=%r", body.raw[:80])
    questions = await session.pipeline.suggest_questions(body.raw, body.current)
    log.info("猜测结果: %d 个候选", len(questions))
    return {"questions": questions}


@app.post("/api/answer/generate")
async def answer_generate(body: AnswerEditIn) -> dict:
    """人工指定问题，重新生成话术卡片。"""
    if session.pipeline is None:
        raise HTTPException(409, "还没有开始会话")
    log.info(
        "人工改问题生成话术: 问题=%r thinking=%s 联网=%s",
        body.question[:80],
        body.thinking,
        body.web_search,
    )
    try:
        result = await session.pipeline.ask_manually(
            body.question, body.raw, thinking=body.thinking, web_search=body.web_search
        )
        log.info("人工问题已建卡: id=%s", result.get("id"))
        return result
    except ValueError as exc:
        log.warning("人工问题被拒: %s", exc)
        raise HTTPException(400, str(exc)) from exc


@app.post("/api/session/inject")
async def inject(body: InjectIn) -> dict:
    """没有真实音频时手动灌一句，用于联调与演示。"""
    if session.pipeline is None:
        log.warning("手动注入失败: 还没有开始会话")
        raise HTTPException(409, "还没有开始会话")
    log.info("手动注入: 通道=%s 文本=%r", body.channel, body.text[:120])
    # 走和真实识别一样的定稿路径（含同一段话被切碎后的合并逻辑）
    await session.emit_final(body.channel, body.text)
    return {"ok": True}


# ---------------------------------------------------------------- 意图识别经验


@app.post("/api/feedback")
async def feedback(body: FeedbackIn) -> dict:
    if body.correct_type not in ("question", "instruction", "statement", "chitchat", "aside"):
        log.warning("意图识别纠正被拒: correct_type=%r", body.correct_type)
        raise HTTPException(400, "correct_type 不合法")
    record_id = memory.add(body.model_dump())
    # 人工纠错也算一次意图识别质量信号，立即推给提词器统计条
    if session.pipeline is not None:
        session.pipeline.stats["corrections"] = session.pipeline.stats.get("corrections", 0) + 1
        try:
            await session.pipeline._broadcast_stats()
        except Exception:  # noqa: BLE001 - 统计失败不影响反馈落库
            log.debug("纠错统计广播失败", exc_info=True)
    log.info(
        "意图识别纠正已记录: id=%s 原判=%s 纠正为=%s 文本=%r",
        record_id,
        body.predicted_type or "-",
        body.correct_type,
        body.last_text[:60],
    )
    return {"id": record_id, "stats": memory.stats()}


@app.get("/api/stats")
async def stats() -> dict:
    return memory.stats()


# ---------------------------------------------------------------- 面试题库


class BankIn(BaseModel):
    category: str = "通用"
    question: str
    answer: str = ""


@app.get("/api/bank")
async def bank_list(category: str = "") -> dict:
    items = await asyncio.to_thread(bank.list_items, category)
    cats = await asyncio.to_thread(bank.categories)
    return {"items": items, "categories": cats}


@app.post("/api/bank")
async def bank_add(body: BankIn) -> dict:
    try:
        return await asyncio.to_thread(bank.add, body.category, body.question, body.answer)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.put("/api/bank/{item_id}")
async def bank_update(item_id: int, body: BankIn) -> dict:
    ok = await asyncio.to_thread(bank.update, item_id, body.category, body.question, body.answer)
    if not ok:
        raise HTTPException(404, "题目不存在")
    return {"ok": True}


@app.delete("/api/bank/{item_id}")
async def bank_delete(item_id: int) -> dict:
    ok = await asyncio.to_thread(bank.delete, item_id)
    if not ok:
        raise HTTPException(404, "题目不存在")
    return {"ok": True}


# ---------------------------------------------------------------- 岗位管理


class PositionIn(BaseModel):
    name: str
    jd: str = ""
    skills: str = ""


@app.get("/api/positions")
async def positions_list() -> dict:
    items = await asyncio.to_thread(positions.list_items)
    return {"items": items}


@app.post("/api/positions")
async def positions_add(body: PositionIn) -> dict:
    try:
        return await asyncio.to_thread(positions.add, body.name, body.jd, body.skills)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.put("/api/positions/{item_id}")
async def positions_update(item_id: int, body: PositionIn) -> dict:
    ok = await asyncio.to_thread(positions.update, item_id, body.name, body.jd, body.skills)
    if not ok:
        raise HTTPException(404, "岗位不存在")
    return {"ok": True}


@app.delete("/api/positions/{item_id}")
async def positions_delete(item_id: int) -> dict:
    ok = await asyncio.to_thread(positions.delete, item_id)
    if not ok:
        raise HTTPException(404, "岗位不存在")
    return {"ok": True}


@app.get("/api/positions/{item_id}/context")
async def positions_context(item_id: int) -> dict:
    name, background = await asyncio.to_thread(positions.context_text, item_id)
    if not name:
        raise HTTPException(404, "岗位不存在")
    return {"name": name, "background": background}


# ---------------------------------------------------------------- 语音自检


class TtsCheckIn(BaseModel):
    text: str = "这是语音合成测试，能听到就说明仿真可以出声"


@app.post("/api/tts/check")
async def tts_check(body: TtsCheckIn) -> dict:
    """合成一句并播放，返回每一步结果，方便判断"没声音"卡在哪。"""
    from . import tts

    return await asyncio.to_thread(tts.self_check, body.text)


# ---------------------------------------------------------------- 简历库


class ResumeIn(BaseModel):
    name: str
    content: str = ""


class ResumeParseIn(BaseModel):
    filename: str = ""
    data_base64: str


@app.post("/api/resumes/parse")
async def resumes_parse(body: ResumeParseIn) -> dict:
    """把上传的 PDF / Word 简历转成 Markdown（前端读文件后按 base64 提交）。"""
    import base64

    if not body.filename:
        raise HTTPException(400, "缺少文件名")
    try:
        data = base64.b64decode(body.data_base64 or "")
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, f"文件内容解码失败：{exc}") from exc
    if len(data) > 12 * 1024 * 1024:
        raise HTTPException(400, "文件太大（超过 12MB）")
    try:
        markdown, kind = await asyncio.to_thread(resume_parse.parse, body.filename, data)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        log.exception("简历解析失败: %s", body.filename)
        raise HTTPException(500, f"解析失败：{exc}") from exc
    if not markdown.strip():
        raise HTTPException(400, "没从文件里读到文字（可能是扫描件/图片），请换一份或手动粘贴")
    name = re.sub(r"\.(pdf|docx?|PDF|DOCX?)$", "", body.filename).strip() or "上传的简历"
    return {"ok": True, "kind": kind, "name": name, "content": markdown, "chars": len(markdown)}


@app.get("/api/resumes")
async def resumes_list() -> dict:
    return {"items": await asyncio.to_thread(resumes.list_items)}


@app.get("/api/resumes/{item_id}")
async def resumes_get(item_id: int) -> dict:
    item = await asyncio.to_thread(resumes.get, item_id)
    if item is None:
        raise HTTPException(404, "简历不存在")
    return item


@app.post("/api/resumes")
async def resumes_add(body: ResumeIn) -> dict:
    try:
        return await asyncio.to_thread(resumes.add, body.name, body.content)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.put("/api/resumes/{item_id}")
async def resumes_update(item_id: int, body: ResumeIn) -> dict:
    ok = await asyncio.to_thread(resumes.update, item_id, body.name, body.content)
    if not ok:
        raise HTTPException(404, "简历不存在")
    return {"ok": True}


@app.delete("/api/resumes/{item_id}")
async def resumes_delete(item_id: int) -> dict:
    ok = await asyncio.to_thread(resumes.delete, item_id)
    if not ok:
        raise HTTPException(404, "简历不存在")
    return {"ok": True}


# ---------------------------------------------------------------- 长期记忆


@app.get("/api/profile")
async def profile_snapshot() -> dict:
    from . import profile as profile_store

    return {"items": await asyncio.to_thread(profile_store.snapshot)}




# ---------------------------------------------------------------- 历史会话与设置


@app.get("/api/sessions")
async def sessions_list(limit: int = 50) -> dict:
    return {"items": await asyncio.to_thread(store.summaries, min(200, limit))}


@app.get("/api/sessions/{session_id}")
async def session_detail(session_id: str) -> dict:
    doc = await asyncio.to_thread(store.get, session_id)
    if doc is None:
        raise HTTPException(404, "会话不存在")
    return doc


class CleanupIn(BaseModel):
    mode: str = "short"  # short | empty | all


@app.post("/api/sessions/cleanup")
async def sessions_cleanup(body: CleanupIn) -> dict:
    deleted = await asyncio.to_thread(store.cleanup, body.mode)
    return {"ok": True, "deleted": deleted}


@app.delete("/api/sessions/{session_id}")
async def session_delete(session_id: str) -> dict:
    ok = await asyncio.to_thread(store.delete, session_id)
    if not ok:
        raise HTTPException(404, "会话不存在")
    return {"ok": True}


@app.get("/api/settings")
async def settings_list() -> dict:
    return {"items": settings_store.public()}


class SettingsIn(BaseModel):
    values: dict[str, str]


@app.put("/api/settings")
async def settings_save(body: SettingsIn) -> dict:
    try:
        result = await asyncio.to_thread(settings_store.save, body.values)
        log.info("设置已保存并生效: %s", ", ".join(result.get("changed") or []) or "无变化")
        return result
    except ValueError as exc:
        log.warning("设置被拒: %s", exc)
        raise HTTPException(400, str(exc)) from exc


# ---------------------------------------------------------------- 余额与调用日志


@app.get("/api/balance")
async def balance() -> dict:
    """查询大模型账户余额（目前只有 DeepSeek 官方提供接口）。"""
    if not llm.live:
        return {"supported": False, "reason": "未配置 LLM_API_KEY"}

    base = settings.llm_base_url
    if "deepseek" not in base:
        return {"supported": False, "reason": "该服务商没有公开的余额查询接口"}

    root = base.split("/v1")[0].rstrip("/")
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                f"{root}/user/balance",
                headers={"Authorization": f"Bearer {settings.llm_api_key}"},
            )
        if resp.status_code != 200:
            return {"supported": False, "reason": f"查询失败 HTTP {resp.status_code}"}
        data = resp.json()
        infos = data.get("balance_infos") or []
        first = infos[0] if infos else {}
        return {
            "supported": True,
            "available": bool(data.get("is_available")),
            "currency": first.get("currency", "CNY"),
            "total": float(first.get("total_balance") or 0),
            "granted": float(first.get("granted_balance") or 0),
            "topped_up": float(first.get("topped_up_balance") or 0),
            "raw": data,
        }
    except Exception as exc:  # noqa: BLE001
        return {"supported": False, "reason": f"查询异常：{exc}"}


@app.get("/api/llm/stats")
async def llm_stats() -> dict:
    return await asyncio.to_thread(llmlog.stats)


@app.get("/api/llm/calls")
async def llm_calls(
    limit: int = 50,
    offset: int = 0,
    only_error: bool = False,
    trace: str = "",
    interview_id: str = "",
    category: str = "",
) -> dict:
    return await asyncio.to_thread(
        llmlog.list_calls,
        min(200, limit),
        offset,
        only_error,
        trace,
        interview_id.strip(),
        category.strip(),
    )


@app.get("/api/llm/calls/{call_id}")
async def llm_call_detail(call_id: int) -> dict:
    item = await asyncio.to_thread(llmlog.get_call, call_id)
    if item is None:
        raise HTTPException(404, "日志不存在")
    return item


@app.delete("/api/llm/calls/{call_id}")
async def llm_call_delete(call_id: int) -> dict:
    deleted = await asyncio.to_thread(llmlog.delete_call, call_id)
    if not deleted:
        raise HTTPException(404, "日志不存在")
    return {"ok": True, "id": call_id}


@app.delete("/api/llm/calls")
async def llm_calls_clear() -> dict:
    count = await asyncio.to_thread(llmlog.clear)
    return {"deleted": count}


# ---------------------------------------------------------------- 提示词模板


class PromptIn(BaseModel):
    content: str


@app.get("/api/prompts")
async def list_prompts() -> dict:
    return {"items": prompts.all_prompts()}


@app.put("/api/prompts/{key}")
async def update_prompt(key: str, body: PromptIn) -> dict:
    try:
        prompts.set_prompt(key, body.content)
    except KeyError as exc:
        log.warning("改提示词被拒: 未知 key=%r", key)
        raise HTTPException(404, f"未知提示词：{key}") from exc
    except ValueError as exc:
        log.warning("改提示词被拒: key=%r %s", key, exc)
        raise HTTPException(400, str(exc)) from exc
    missing = prompts.check_placeholders(key, body.content)
    log.info(
        "提示词已更新: key=%s %d字 未识别占位符=%s",
        key,
        len(body.content),
        missing or "无",
    )
    return {"ok": True, "unknown_placeholders": missing}


@app.post("/api/prompts/{key}/reset")
async def reset_prompt(key: str) -> dict:
    try:
        prompts.reset(key)
    except KeyError as exc:
        log.warning("恢复提示词被拒: 未知 key=%r", key)
        raise HTTPException(404, f"未知提示词：{key}") from exc
    log.info("提示词已恢复默认: key=%s", key)
    return {"ok": True}


# ---------------------------------------------------------------- 事件通道


@app.websocket("/ws/events")
async def events(websocket: WebSocket) -> None:
    await websocket.accept()
    await clients.add(websocket)
    log.info("界面已连接（当前 %d 个）: %s", clients.count, websocket.client)
    try:
        await websocket.send_json({"type": "session", **session.snapshot()})
        while True:
            payload = await websocket.receive_json()
            action = payload.get("type")
            if action == "ping":
                await websocket.send_json({"type": "pong"})
            elif action == "stop":
                log.info("界面请求停止面试")
                await session.stop()
                await clients.broadcast({"type": "stopped"})
    except WebSocketDisconnect:
        log.info("界面已断开（剩 %d 个）", max(0, clients.count - 1))
    except Exception:  # noqa: BLE001
        log.exception("事件通道异常")
    finally:
        clients.discard(websocket)


# ---------------------------------------------------------------- 静态页面


_DIST = Path(PROJECT_DIR) / "frontend" / "build"
_SRC = Path(PROJECT_DIR) / "frontend" / "src"


class _SPAStaticFiles(StaticFiles):
    """前端用 history 路由：/interviews/<id> 这类页面路径直接回落到 index.html。

    只对「看起来像页面」的路径兜底；assets/ 和带扩展名的静态文件 404 就保持 404。
    """

    async def get_response(self, path: str, scope):
        try:
            return await super().get_response(path, scope)
        except StarletteHTTPException as exc:
            if exc.status_code != 404 or path.startswith("assets/") or "." in path:
                raise
            return await super().get_response("index.html", scope)


def _frontend_stale() -> bool:
    """前端源码比构建产物新，说明忘了 pnpm build。"""
    index = _DIST / "index.html"
    if not index.is_file():
        return False
    built = index.stat().st_mtime
    src_dir = _SRC if _SRC.is_dir() else None
    if not src_dir:
        return False
    for path in src_dir.rglob("*"):
        if path.is_file() and path.suffix in (".ts", ".tsx", ".css") and path.stat().st_mtime > built:
            return True
    return False


if _DIST.is_dir():
    app.mount("/", _SPAStaticFiles(directory=str(_DIST), html=True), name="web")
    if _frontend_stale():
        logging.getLogger("server").warning(
            "前端源码比 frontend/build 新：界面可能不是最新的，请 cd frontend && pnpm build"
        )
else:

    @app.get("/", response_class=HTMLResponse)
    async def placeholder() -> str:
        return """
        <html><body style="font-family:sans-serif;padding:40px">
        <h2>前端还没构建</h2>
        <p>执行 <code>cd frontend &amp;&amp; pnpm build</code> 后刷新本页。</p>
        </body></html>
        """
