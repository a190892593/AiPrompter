"""大模型统一调用层：OpenAI 兼容协议，支持 JSON 模式与流式（预留）。

没有配置 LLM_API_KEY 时抛出 MockRequired，由业务层改用本地 Mock 逻辑，
这样界面、语音、评分流程都能离线跑通，接上 Key 后行为自动切换。
"""

from __future__ import annotations

import json
import logging
import re
import time
from typing import Any, AsyncIterator

import httpx

from . import llmlog
from .config import settings

log = logging.getLogger("interview.llm")


class LLMError(RuntimeError):
    pass


class MockRequired(RuntimeError):
    """没有真实 Key，调用方应使用 Mock 分支。"""


_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)


def loads_json(text: str) -> dict[str, Any]:
    """宽容地解析模型返回的 JSON：去掉代码围栏、截取最外层花括号。"""
    cleaned = _FENCE.sub("", text).strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start != -1 and end > start:
        try:
            return json.loads(cleaned[start : end + 1])
        except json.JSONDecodeError as exc:
            raise LLMError(f"模型返回的 JSON 无法解析: {exc}") from exc
    raise LLMError("模型没有返回 JSON")


class LLM:
    def __init__(self) -> None:
        self._client: httpx.AsyncClient | None = None

    @property
    def live(self) -> bool:
        return settings.llm_live

    def _http(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                base_url=settings.llm_base_url,
                timeout=httpx.Timeout(settings.llm_timeout, connect=15.0),
            )
        return self._client

    def reset_client(self) -> None:
        """设置里改了接口地址/Key 之后重建客户端。"""
        self._client = None

    async def aclose(self) -> None:
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()

    async def chat(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float | None = None,
        json_mode: bool = False,
        max_tokens: int = 2000,
        model: str | None = None,
        trace: str = "",
        thinking: bool | None = None,
        web_search: bool = False,
        category: str = "",
    ) -> str:
        if not self.live:
            raise MockRequired()

        payload: dict[str, Any] = {
            "model": model or settings.llm_model,
            "messages": messages,
            "temperature": settings.llm_temperature if temperature is None else temperature,
            "max_tokens": max_tokens,
            "stream": False,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}

        # DeepSeek 原生能力：思考模式 + 服务端联网搜索（由服务端完成，客户端不执行工具）
        if thinking is not None:
            payload["thinking"] = {"type": "enabled" if thinking else "disabled"}
        if web_search:
            payload["tools"] = [{"type": "deepseek_web_search", "name": "deepseek_web_search"}]
            payload.pop("temperature", None)  # 联网时温度由服务端决定

        headers = {
            "Authorization": f"Bearer {settings.llm_api_key}",
            "Content-Type": "application/json",
        }

        started = time.perf_counter()
        kind_name = "chat_json" if json_mode else "chat"
        log.debug(
            "模型请求(%s): model=%s trace=%s 消息=%d条 max_tokens=%s thinking=%s 联网=%s",
            kind_name,
            payload["model"],
            trace or "-",
            len(messages),
            max_tokens,
            thinking,
            web_search,
        )
        last: Exception | None = None
        for attempt in range(2):
            try:
                resp = await self._http().post("/chat/completions", json=payload, headers=headers)
                if resp.status_code >= 400:
                    detail = resp.text[:400]
                    raise LLMError(f"模型接口返回 HTTP {resp.status_code}: {detail}")
                data = resp.json()
                content = data["choices"][0]["message"]["content"] or ""
                llmlog.record(
                    model=payload["model"],
                    kind=kind_name,
                    status="ok",
                    latency_ms=int((time.perf_counter() - started) * 1000),
                    messages=messages,
                    response=content,
                    usage=data.get("usage") or {},
                    trace=trace,
                    category=category,
                )
                usage = data.get("usage") or {}
                log.info(
                    "模型返回(%s): model=%s trace=%s 用时=%dms tokens=%s/%s 缓存命中=%s 字数=%d",
                    kind_name,
                    payload["model"],
                    trace or "-",
                    int((time.perf_counter() - started) * 1000),
                    usage.get("prompt_tokens", "?"),
                    usage.get("completion_tokens", "?"),
                    usage.get("prompt_cache_hit_tokens", "-"),
                    len(content),
                )
                return content
            except (httpx.HTTPError, LLMError, KeyError, IndexError) as exc:
                last = exc
                log.warning(
                    "模型调用失败(第 %s/2 次): model=%s trace=%s %s: %s",
                    attempt + 1,
                    payload["model"],
                    trace or "-",
                    type(exc).__name__,
                    exc,
                )

        llmlog.record(
            model=payload["model"],
            kind=kind_name,
            status="error",
            latency_ms=int((time.perf_counter() - started) * 1000),
            messages=messages,
            error=str(last),
            trace=trace,
            category=category,
        )
        log.error(
            "模型调用最终失败(%s): model=%s trace=%s 总用时=%dms 原因=%s",
            kind_name,
            payload["model"],
            trace or "-",
            int((time.perf_counter() - started) * 1000),
            last,
        )
        raise LLMError(f"模型调用失败: {last}")

    async def chat_json(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float | None = None,
        max_tokens: int = 2000,
        model: str | None = None,
        trace: str = "",
        thinking: bool | None = None,
        web_search: bool = False,
        category: str = "",
    ) -> dict[str, Any]:
        raw = await self.chat(
            messages,
            temperature=temperature,
            json_mode=True,
            max_tokens=max_tokens,
            model=model,
            trace=trace,
            thinking=thinking,
            web_search=web_search,
            category=category,
        )
        return loads_json(raw)

    async def chat_stream_events(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float | None = None,
        max_tokens: int = 800,
        model: str | None = None,
        trace: str = "",
        thinking: bool | None = None,
        web_search: bool = False,
        category: str = "",
    ) -> AsyncIterator[tuple[str, str]]:
        """流式产出 ("reasoning" | "content", 增量文本)。

        思考模式下模型会先输出 reasoning_content，再输出正文；
        普通模型只会产出 content。
        """
        """流式生成，逐 token 产出。首字延迟直接决定提词体验。"""
        if not self.live:
            raise MockRequired()

        chosen = model or settings.llm_model
        payload: dict[str, Any] = {
            "model": chosen,
            "messages": messages,
            "max_tokens": max_tokens,
            "stream": True,
        }
        # DeepSeek 原生：thinking 显式开关；web_search 声明服务端工具
        if thinking is not None:
            payload["thinking"] = {"type": "enabled" if thinking else "disabled"}
        if web_search:
            payload["tools"] = [{"type": "deepseek_web_search", "name": "deepseek_web_search"}]
        if "reasoner" not in chosen.lower():
            payload["temperature"] = settings.llm_temperature if temperature is None else temperature
            payload["stream_options"] = {"include_usage": True}
        headers = {
            "Authorization": f"Bearer {settings.llm_api_key}",
            "Content-Type": "application/json",
        }

        started = time.perf_counter()
        first_token_at: float | None = None
        log.debug(
            "流式请求: model=%s trace=%s 消息=%d条 max_tokens=%s thinking=%s 联网=%s",
            chosen,
            trace or "-",
            len(messages),
            max_tokens,
            thinking,
            web_search,
        )
        collected: list[str] = []
        reasoning_collected: list[str] = []
        annotations: list[dict] = []
        tool_calls: list[dict] = []
        usage: dict[str, Any] = {}
        status = "ok"
        error_text = ""

        try:
            async with self._http().stream(
                "POST", "/chat/completions", json=payload, headers=headers
            ) as response:
                if response.status_code >= 400:
                    body = await response.aread()
                    raise LLMError(f"模型接口返回 HTTP {response.status_code}: {body[:300]!r}")
                async for line in response.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    if chunk.get("usage"):
                        usage = chunk["usage"]
                    choices = chunk.get("choices") or []
                    if not choices:
                        continue
                    piece = choices[0].get("delta") or {}
                    if piece.get("annotations"):
                        annotations.extend(piece["annotations"])
                    if piece.get("server_tool_calls"):
                        tool_calls.extend(piece["server_tool_calls"])
                    reasoning = piece.get("reasoning_content")
                    delta = piece.get("content")
                    if first_token_at is None and (reasoning or delta):
                        first_token_at = time.perf_counter()
                        log.info(
                            "流式首包: model=%s trace=%s 首字=%dms",
                            chosen,
                            trace or "-",
                            int((first_token_at - started) * 1000),
                        )
                    if reasoning:
                        reasoning_collected.append(reasoning)
                        yield "reasoning", reasoning
                    if delta:
                        collected.append(delta)
                        yield "content", delta

            if annotations:
                import json as _json

                yield "sources", _json.dumps(
                    {"annotations": annotations, "tool_calls": tool_calls}, ensure_ascii=False
                )
        except Exception as exc:
            status = "error"
            error_text = str(exc)
            log.error(
                "流式调用失败: model=%s trace=%s 已收正文=%d字 原因=%s",
                chosen,
                trace or "-",
                len("".join(collected)),
                exc,
            )
            raise
        finally:
            llmlog.record(
                model=payload["model"],
                kind="stream",
                status=status,
                latency_ms=int((time.perf_counter() - started) * 1000),
                messages=messages,
                response="".join(collected),
                usage=usage,
                error=error_text,
                trace=trace,
                category=category,
            )
            log.info(
                "流式结束: model=%s trace=%s 状态=%s 首字=%sms 总用时=%dms tokens=%s/%s 思考=%d字 正文=%d字%s",
                chosen,
                trace or "-",
                status,
                "-" if first_token_at is None else int((first_token_at - started) * 1000),
                int((time.perf_counter() - started) * 1000),
                usage.get("prompt_tokens", "?"),
                usage.get("completion_tokens", "?"),
                len("".join(reasoning_collected)),
                len("".join(collected)),
                f" 错误={error_text[:200]}" if error_text else "",
            )


    async def chat_stream(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float | None = None,
        max_tokens: int = 800,
        model: str | None = None,
        trace: str = "",
        thinking: bool | None = None,
        web_search: bool = False,
        category: str = "",
    ) -> AsyncIterator[str]:
        """只要正文的简化版（评分、变体生成用）。"""
        async for kind, text in self.chat_stream_events(
            messages,
            temperature=temperature,
            max_tokens=max_tokens,
            model=model,
            trace=trace,
            thinking=thinking,
            web_search=web_search,
            category=category,
        ):
            if kind == "content":
                yield text


llm = LLM()
