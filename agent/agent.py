"""Agent 核心 — 基于原生 tool_use 的 ReAct Loop

这是整个项目的灵魂。理解这个文件，你就理解了 Agent 的工作方式。

ReAct 循环（基于 tool_use，非文本解析）:

  User Input → [System Prompt + History]
    → LLM 推理（API 原生 tool_use，返回 JSON tool_calls）
      → 有 tool_calls? → 框架解析 JSON → 执行工具 → 结果作为 tool role 消息喂回 LLM
      → 无 tool_calls? → LLM 输出文本 → 这是最终答案 → 返回给用户

和旧式 ReAct (文本解析) 的关键区别:
  旧: LLM 输出 "Action: calculator(expression='2+3')" → 正则匹配 → 脆弱
  新: LLM 返回 {"tool_calls": [{"name":"calculator", "arguments":{"expression":"2+3"}}]}
      → json.loads → 确定性，100% 可靠

输出渲染的三层架构:
  1. 工具调用 → 框架捕获 JSON → 执行 → 彩色终端展示执行过程
  2. 结构化最终答案 → response_format 约束 JSON Schema → 格式化展示
  3. 自由文本 → markdown → 终端直接渲染
"""

from __future__ import annotations

import json
import time
from typing import Any

from .llm import LLMClient, LLMResponse, ParsedToolCall
from .tools import ToolRegistry, ToolResult
from .memory import MemoryStore


# ═══════════════════════════════════════════════════════════════
# ANSI 颜色代码 — 用于终端友好输出
# ═══════════════════════════════════════════════════════════════

class Color:
    """终端 ANSI 颜色。用这个而不是 print 裸文本，让输出层次分明。"""
    RESET = "\033[0m"
    BOLD = "\033[1m"
    DIM = "\033[2m"

    # 前景色
    RED = "\033[91m"
    GREEN = "\033[92m"
    YELLOW = "\033[93m"
    BLUE = "\033[94m"
    MAGENTA = "\033[95m"
    CYAN = "\033[96m"
    WHITE = "\033[97m"

    # 背景色
    BG_BLUE = "\033[44m"
    BG_GREEN = "\033[42m"
    BG_RED = "\033[41m"

    @staticmethod
    def tool(name: str) -> str:
        return f"{Color.CYAN}{Color.BOLD}{name}{Color.RESET}"

    @staticmethod
    def ok(text: str) -> str:
        return f"{Color.GREEN}{text}{Color.RESET}"

    @staticmethod
    def err(text: str) -> str:
        return f"{Color.RED}{text}{Color.RESET}"

    @staticmethod
    def dim(text: str) -> str:
        return f"{Color.DIM}{text}{Color.RESET}"

    @staticmethod
    def warn(text: str) -> str:
        return f"{Color.YELLOW}{text}{Color.RESET}"

    @staticmethod
    def header(text: str) -> str:
        return f"{Color.BOLD}{Color.WHITE}{text}{Color.RESET}"


# ═══════════════════════════════════════════════════════════════
# System Prompt
# ═══════════════════════════════════════════════════════════════

SYSTEM_PROMPT = """你是一个 AI 助手，能够使用工具来完成任务。

## 工作方式
你可以调用工具来获取信息或执行操作。每次思考并决定下一步：
1. 如果需要信息或执行操作，调用合适的工具
2. 观察工具返回的结果
3. 基于结果继续推理，直到能给出最终答案

## 长期记忆
你可以使用 memory 工具来持久化保存信息，跨会话保留：
- 用户偏好、环境信息、重要教训等值得记住的内容
- 主动保存，不要等用户要求
- 不要保存临时的任务进度或琐碎信息
- 记忆按 scope/topic 分层：user=跨项目用户偏好，project=当前项目，local=本机私有环境
- pinned=true 的短条目会进入启动索引；长流程和细节用 pinned=false 存到具体 topic，需要时再 search/read

## 规则
- 始终用中文回复用户
- 工具返回的 JSON 中包含 "ok": false 表示失败，报告给用户并尝试其他方式
- 如果连续 3 次工具调用失败，停下来告诉用户
- 完成任务后直接给出答案，不要再调用无关工具
- 当前工作目录：{workdir}
"""


# ═══════════════════════════════════════════════════════════════
# Agent
# ═══════════════════════════════════════════════════════════════

class Agent:
    """ReAct Agent — 基于原生 tool_use 的推理-行动循环。

    使用方式:
        agent = Agent(llm=client, tools=registry)
        result = agent.run("帮我写一个 hello world 的 Python 文件")
        print(result)
    """

    def __init__(
        self,
        llm: LLMClient,
        tools: ToolRegistry,
        memory: MemoryStore | None = None,
        max_steps: int = 10,
        verbose: bool = True,
        workdir: str = ".",
    ):
        self.llm = llm
        self.tools = tools
        self.memory = memory
        self.max_steps = max_steps
        self.verbose = verbose
        self.workdir = workdir

        # 会话启动时加载 memory 快照（frozen snapshot 模式）
        if self.memory:
            self.memory.load()

        # 运行状态（每次 run() 重置）
        self.messages: list[dict[str, Any]] = []
        self.step_count = 0
        self.consecutive_errors = 0

    # ── 公共 API ──────────────────────────────────────────

    def run(self, user_input: str) -> str:
        """执行一次完整的 Agent 任务。

        Args:
            user_input: 用户的任务描述

        Returns:
            Agent 的最终回复文本
        """
        self._reset()
        self._init_messages(user_input)

        while self.step_count < self.max_steps:
            self.step_count += 1
            self._print_step_header()

            # ── 调用 LLM（带 tool schemas）─────
            tool_schemas = self.tools.get_schemas()
            response = self.llm.chat(self.messages, tools=tool_schemas)

            # ── 展示 LLM 返回的结构化信息 ──
            if self.verbose:
                u = response["usage"]
                if isinstance(u, dict):
                    print(Color.dim(
                        f"  tokens: {u.get('prompt_tokens', '?')}→{u.get('completion_tokens', '?')}  "
                        + f"|  finish: {response['finish_reason']}"
                    ))

            # ── 分支 1: LLM 返回纯文本 = 最终答案 ──
            if response["content"] and not response["tool_calls"]:
                return self._final_answer(response["content"])

            # ── 分支 2: LLM 想调用工具 ──
            if response["tool_calls"]:
                # 记录 assistant 消息（含 tool_calls）
                # 加入上下文
                self._record_assistant_message(response)

                # 逐个执行工具
                for tc in response["tool_calls"]:
                    self._execute_and_record(tc)

                # 错误过多 → 终止
                if self.consecutive_errors >= 3:
                    msg = f"\n{Color.warn('⚠️  连续 3 次工具调用失败，终止任务')}"
                    if self.verbose:
                        print(msg)
                    return "抱歉，工具调用连续失败，无法完成任务。"

            # ── 分支 3: 空响应（极少见）──
            else:
                if self.verbose:
                    print(f"\n{Color.warn('[WARN] LLM 返回了空响应')}")
                return "抱歉，我暂时无法处理这个请求。"

        # ── 达到最大步数 ──
        if self.verbose:
            print(f"\n{Color.warn(f'⚠️  达到最大步数限制 ({self.max_steps})，请求 LLM 总结')}")

        self.messages.append({
            "role": "user",
            "content": "你已经用完了所有步骤。请基于已获得的信息，给出你的最终答案。",
        })
        response = self.llm.chat(self.messages)
        return response["content"] or "无法完成任务。"

    # ── 内部方法 ──────────────────────────────────────────

    def _reset(self):
        """重置运行状态"""
        self.messages = []
        self.step_count = 0
        self.consecutive_errors = 0

    def _init_messages(self, user_input: str):
        """构建初始消息列表。

        消息格式是 OpenAI 标准:
          - system:  行为约束
          - user:    用户输入
          - assistant + tool_calls: 模型决定调工具
          - tool:    工具执行结果
        """
        system_text = SYSTEM_PROMPT.format(workdir=self.workdir)
        if self.memory:
            mem_text = self.memory.to_prompt_all()
            if mem_text:
                system_text += f"\n\n{mem_text}"

        self.messages = [
            {"role": "system", "content": system_text},
            {"role": "user", "content": user_input},
        ]

    def _record_assistant_message(self, response: LLMResponse):
        """将 LLM 的 assistant 消息（含 tool_calls）加入对话历史。

        这里的 tool_calls 格式是 OpenAI 原生格式:
          {
            "role": "assistant",
            "content": "让我查一下时间",
            "tool_calls": [
              {
                "id": "call_abc123",
                "type": "function",
                "function": {
                  "name": "get_time",
                  "arguments": "{}"       ← JSON 字符串，不是 dict
                }
              }
            ]
          }

        注意 arguments 必须是 JSON 字符串，因为 API 要求如此。
        """
        assistant_msg: dict[str, Any] = {
            "role": "assistant",
            "content": response.get("content"),
        }
        if response["tool_calls"]:
            assistant_msg["tool_calls"] = [
                {
                    "id": tc["id"],
                    "type": "function",
                    "function": {
                        "name": tc["name"],
                        "arguments": json.dumps(tc["arguments"], ensure_ascii=False),
                    },
                }
                for tc in response["tool_calls"]
            ]
        self.messages.append(assistant_msg)

    def _execute_and_record(self, tool_call: ParsedToolCall):
        """执行一个工具调用，展示结果，并写入消息历史。

        tool_call 结构（来自 llm.py 的 _parse_response）:
          {"id": "call_xxx", "name": "read_file", "arguments": {"filepath": "/tmp/a.txt"}}

        工具返回统一结构:
          {"ok": bool, "output": str, "error": str | None, "meta": dict}
        """
        name = tool_call["name"]
        args = tool_call["arguments"]
        call_id = tool_call["id"]

        # ── 查找工具 ──
        tool = self.tools.get(name)
        if tool is None:
            result: ToolResult = {
                "ok": False,
                "output": "",
                "error": f"未知工具: {name}",
                "meta": {},
            }
        else:
            result = tool.execute(**args)

        # ── 跟踪错误 ──
        if result["ok"]:
            self.consecutive_errors = 0
        else:
            self.consecutive_errors += 1

        # ── 终端渲染：结构化展示工具调用过程 ──
        # 这是 Claude Code 式输出的关键：框架决定"怎么展示"，模型只管"调什么工具"
        if self.verbose:
            # 1. 工具名 + 参数（紧凑格式）
            args_str = self._fmt_args(args)
            print(f"\n  🔧 {Color.tool(name)}({args_str})")

            # 2. 执行结果
            if result["ok"]:
                # 截断过长输出
                output = result["output"]
                if len(output) > 400:
                    output = output[:400] + Color.dim(f"\n  ... (已截断，共 {len(result['output'])} 字符)")
                print(f"     {Color.ok('✓')}  {output}")
            else:
                print(f"     {Color.err('✗')}  {result['error']}")

        # ── 写入消息历史（tool role）──
        # 工具结果以 tool role 消息形式加入到对话中
        # 这样 LLM 在下一轮推理时就能"看到"工具的执行结果
        if result["ok"]:
            tool_content = result["output"]
        else:
            tool_content = json.dumps({"error": result["error"]}, ensure_ascii=False)

        self.messages.append({
            "role": "tool",
            "tool_call_id": call_id,
            "content": tool_content,
        })

    def _final_answer(self, content: str) -> str:
        """展示最终答案并返回。"""
        if self.verbose:
            print(f"\n  {Color.BG_GREEN} {Color.header('FINAL ANSWER')} {Color.RESET}")
            print(f"  {content}")
        return content

    def _print_step_header(self) -> None:
        """打印步骤头"""
        if self.verbose:
            # 带颜色的分隔线
            line = f"{Color.dim('─' * 50)}"
            print(f"\n{line}")
            print(f"  {Color.header(f'Step {self.step_count}/{self.max_steps}')}")
            print(line)

    @staticmethod
    def _fmt_args(args: dict[str, object]) -> str:
        """格式化工具参数用于终端展示。

        输出如: filepath='config.yaml', limit=500
        """
        parts = []
        for k, v in args.items():
            s = str(v)
            if len(s) > 50:
                s = s[:50] + "..."
            parts.append(f"{Color.YELLOW}{k}{Color.RESET}={Color.GREEN}{repr(s)}{Color.RESET}")
        return ", ".join(parts) if parts else "无参数"
