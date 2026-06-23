"""LLM client — OpenAI 兼容 API 封装

三层结构化输出机制（按确定性从高到低）：

  第一层: tool_use (function calling)
    模型输出的是 JSON，不是文本。框架解析 tool_calls → 执行真实工具 → 把结果喂回模型。
    这是确定性的 —— 不会出现"正则匹配 Action: xxx 失败"的问题。

  第二层: response_format (JSON Schema 约束)
    当需要模型返回结构化数据（如文件列表、分析报告），用 response_format
    告诉 API "请严格按照这个 JSON Schema 输出"，模型会保证输出符合 schema。
    不需要 post-processing 去猜格式。

  第三层: markdown 自由文本
    纯问答场景，模型自由输出 markdown，终端直接渲染。
    这是灵活性最高的兜底策略，但不是"结构化的主要通道"。

架构对比:
  旧 ReAct (文本解析): LLM 输出文本 → 正则匹配 Action → 脆弱，容易断
  新 ReAct (tool_use):  LLM 输出 JSON → 框架解析 → 确定性，100% 可靠
"""

from __future__ import annotations

import os
import json
import time
from typing import Any
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError


class LLMClient:
    """封装 OpenAI 兼容的 Chat Completion API。

    支持三种调用模式:
      chat(messages)                    → 自由文本 (第三层)
      chat(messages, tools=[...])       → tool_use 模式 (第一层)
      chat(messages, response_format=...) → 结构化输出 (第二层)

    纯标准库实现，零外部依赖，方便看透每一次 HTTP 调用。
    """

    def __init__(
        self,
        api_key: str,
        base_url: str,
        model: str,
        max_retries: int = 3,
        timeout: int = 120,
    ):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.max_retries = max_retries
        self.timeout = timeout

    # ── 公共 API ──────────────────────────────────────────

    def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        response_format: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """发送消息到 LLM，返回统一结构。

        Args:
            messages:  对话历史
            tools:     工具的 OpenAI Schema 列表（触发第一层 tool_use）
            response_format: JSON Schema 约束输出格式（触发第二层 structured output）
                           例: {"type": "json_schema", "json_schema": {...}}

        Returns:
            {
                "content": str | None,         # 文本回复 (可能为 None)
                "tool_calls": [                 # 工具调用列表
                    {
                        "id": "call_xxx",
                        "name": "read_file",
                        "arguments": {"filepath": "/tmp/a.txt"}  # 已解析为 dict
                    },
                    ...
                ],
                "finish_reason": "stop" | "tool_calls" | "length",
                "usage": {"prompt_tokens": N, "completion_tokens": N}  # 可选
            }
        """
        body: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
        }

        # ── 第一层: tool_use ──
        if tools:
            body["tools"] = tools
            body["tool_choice"] = "auto"

        # ── 第二层: structured output ──
        if response_format:
            body["response_format"] = response_format

        # ── 调用（带重试）──────────
        # 注意: HTTPError 是 URLError 的子类，所以 HTTPError except 必须在前！
        for attempt in range(1, self.max_retries + 1):
            try:
                raw = self._send(body)
                return self._parse_response(raw)

            except HTTPError as e:
                # 4xx 不重试（API Key 错、参数错等）
                if 400 <= e.code < 500:
                    error_body = self._read_error_body(e)
                    raise RuntimeError(
                        f"LLM API 错误 {e.code}（客户端错误，不重试）: {error_body}"
                    )
                # 5xx 重试
                if attempt == self.max_retries:
                    error_body = self._read_error_body(e)
                    raise RuntimeError(
                        f"LLM API 错误 {e.code}（服务端错误，重试 {self.max_retries} 次后放弃）: {error_body}"
                    )
                wait = 2 ** attempt
                time.sleep(wait)

            except URLError as e:
                if attempt == self.max_retries:
                    raise RuntimeError(
                        f"LLM 网络错误（重试 {self.max_retries} 次后放弃）: {e}"
                    )
                wait = 2 ** attempt
                time.sleep(wait)

            except TimeoutError as e:
                if attempt == self.max_retries:
                    raise RuntimeError(
                        f"LLM 请求超时（重试 {self.max_retries} 次后放弃）: {e}"
                    )
                wait = 2 ** attempt
                time.sleep(wait)

        raise RuntimeError("不可达")

    # ── 内部实现 ──────────────────────────────────────────

    def _send(self, body: dict[str, Any]) -> dict[str, Any]:
        """发送 HTTP 请求，返回原始 JSON"""
        url = f"{self.base_url}/chat/completions"
        safe_body = self._sanitize_for_json(body)
        payload = json.dumps(safe_body, ensure_ascii=False).encode("utf-8")
        req = Request(
            url,
            data=payload,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
        )
        with urlopen(req, timeout=self.timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))

    @staticmethod
    def _sanitize_for_json(value: Any) -> Any:
        """Remove invalid Unicode surrogates before UTF-8 JSON encoding."""
        if isinstance(value, str):
            return value.encode("utf-8", errors="replace").decode("utf-8")
        if isinstance(value, list):
            return [LLMClient._sanitize_for_json(item) for item in value]
        if isinstance(value, dict):
            return {
                LLMClient._sanitize_for_json(k): LLMClient._sanitize_for_json(v)
                for k, v in value.items()
            }
        return value

    @staticmethod
    def _read_error_body(e: HTTPError) -> str:
        """安全读取 HTTP 错误响应体"""
        try:
            return e.read().decode("utf-8")[:1000]
        except Exception:
            return str(e)

    @staticmethod
    def _parse_response(data: dict[str, Any]) -> dict[str, Any]:
        """解析 API 原始响应 → 统一内部格式

        这是"结构化输出"的关键：不返回原始 JSON，而是返回
        解析好的 dict，调用方不需要知道 API 的嵌套结构。
        """
        choice = data["choices"][0]
        msg = choice["message"]

        result: dict[str, Any] = {
            "content": msg.get("content"),
            "tool_calls": [],
            "finish_reason": choice.get("finish_reason", "stop"),
            "usage": data.get("usage"),
        }

        # ── 解析 tool_calls ──
        # API 返回的 arguments 是 JSON 字符串，这里解析为 Python dict
        raw_tool_calls = msg.get("tool_calls") or []
        for tc in raw_tool_calls:
            func = tc["function"]
            try:
                args = json.loads(func["arguments"])
            except (json.JSONDecodeError, KeyError):
                args = {}

            result["tool_calls"].append({
                "id": tc["id"],
                "name": func["name"],
                "arguments": args,
            })

        return result

    # ── 工厂方法 ──────────────────────────────────────────

    @classmethod
    def from_config(cls, config: dict[str, Any]) -> "LLMClient":
        """从配置字典创建实例。

        支持 ${ENV_VAR} 占位符:
            api_key: "${OPENAI_API_KEY}"  → 从环境变量读取
        """
        api_key = config.get("api_key", "")
        if api_key.startswith("${") and api_key.endswith("}"):
            env_var = api_key[2:-1]
            api_key = os.environ.get(env_var, "")

        if not api_key:
            raise ValueError(
                "API Key 未配置。请在 config.yaml 中设置 api_key，"
                "或设置环境变量后使用 ${VAR_NAME} 占位符。"
            )

        return cls(
            api_key=api_key,
            base_url=config.get("base_url", "https://api.openai.com"),
            model=config.get("model", "gpt-4"),
            max_retries=config.get("max_retries", 3),
            timeout=config.get("timeout", 120),
        )
