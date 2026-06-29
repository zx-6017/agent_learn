"""工具系统 — 定义、注册和执行

设计要点:
  - Tool 对象 = name + description + JSON Schema + execute 函数
  - ToolRegistry 管理所有工具，对外暴露 get_schemas() 供 LLM 使用
  - 工具执行返回统一结构: {"ok": bool, "output": str, "error": str | None, "meta": dict}
  - Schema 越精确，LLM 调用越准确 —— description 是写给模型的"说明书"

和学习价值:
  这个文件的目的是让你理解 "工具是如何被 LLM 调用的":
    1. 你定义工具的 schema（名称、描述、参数类型）
    2. 框架把 schema 列表发给 LLM
    3. LLM 决定调用哪个工具、传什么参数（返回 JSON，不是文本！）
    4. 框架解析 JSON，找到对应函数，执行，把结果返回给 LLM
  全程没有文本解析，没有正则匹配，100% 确定性。
"""

from __future__ import annotations

import os
import json
import datetime
import subprocess
import glob as glob_module
from typing import Any, Callable, TypedDict

# ── Memory 导入（避免循环依赖）─────────
from .memory import MemoryStore


class ToolResult(TypedDict):
    ok: bool
    output: str
    error: str | None
    meta: dict[str, Any]


# ═══════════════════════════════════════════════════════════════
# Tool — 工具定义
# ═══════════════════════════════════════════════════════════════

class Tool:
    """一个工具的定义和执行。"""

    def __init__(
        self,
        name: str,
        description: str,
        parameters: dict[str, Any],
        func: Callable[..., object],
    ):
        self.name = name
        self.description = description
        self.parameters = parameters
        self._func: Callable[..., object] = func

    def execute(self, **kwargs: object) -> ToolResult:
        """执行工具，始终返回统一结构。

        Returns:
            {"ok": bool, "output": str, "error": str | None, "meta": dict}
        """
        try:
            output = self._func(**kwargs)
            return {
                "ok": True,
                "output": str(output),
                "error": None,
                "meta": {},
            }
        except Exception as e:
            return {
                "ok": False,
                "output": "",
                "error": f"{type(e).__name__}: {e}",
                "meta": {},
            }

    def to_openai_schema(self) -> dict[str, Any]:
        """生成 OpenAI 兼容的 function schema。

        这是发给 LLM 的 "工具说明书"。LLM 读了这个 schema 后，
        会决定 (a) 要不要用这个工具 (b) 传什么参数。
        返回的是 JSON，不是文本。
        """
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


# ═══════════════════════════════════════════════════════════════
# ToolRegistry — 工具注册表
# ═══════════════════════════════════════════════════════════════

class ToolRegistry:
    """按名称管理工具。"""

    def __init__(self):
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def get_schemas(self) -> list[dict[str, Any]]:
        """返回所有工具的 OpenAI Schema 列表，供 LLM 调用。"""
        return [t.to_openai_schema() for t in self._tools.values()]

    def list_names(self) -> list[str]:
        """列出所有已注册的工具名称。"""
        return list(self._tools.keys())

    def __len__(self) -> int:
        return len(self._tools)


# ═══════════════════════════════════════════════════════════════
# 工具实现
# ═══════════════════════════════════════════════════════════════

def _tool_get_time() -> str:
    """获取当前日期和时间"""
    now = datetime.datetime.now()
    return json.dumps({
        "datetime": now.strftime("%Y-%m-%d %H:%M:%S"),
        "weekday": now.strftime("%A"),
        "timestamp": int(now.timestamp()),
    }, ensure_ascii=False)


def _tool_calculator(expression: str) -> str:
    """安全计算数学表达式"""
    allowed = set("0123456789+-*/() .^%eE")
    if not all(c in allowed for c in expression):
        raise ValueError(f"不允许的字符。仅支持数字和 + - * / ** ( ) 运算符")
    result = eval(expression, {"__builtins__": {}}, {})
    return str(result)


def _tool_read_file(filepath: str, limit: int = 500) -> str:
    """读取文件内容（带行数限制）"""
    with open(filepath, "r", encoding="utf-8") as f:
        lines = f.readlines()

    total = len(lines)
    truncated = total > limit
    content = "".join(lines[:limit])

    return json.dumps({
        "total_lines": total,
        "shown_lines": min(total, limit),
        "truncated": truncated,
        "content": content,
    }, ensure_ascii=False)


def _tool_write_file(filepath: str, content: str) -> str:
    """写入文件"""
    os.makedirs(os.path.dirname(filepath) or ".", exist_ok=True)
    with open(filepath, "w", encoding="utf-8") as f:
        f.write(content)
    size = os.path.getsize(filepath)
    return json.dumps({
        "path": filepath,
        "bytes_written": size,
        "lines": content.count("\n") + (1 if content and not content.endswith("\n") else 0),
    }, ensure_ascii=False)


def _tool_list_files(directory: str = ".", pattern: str = "*") -> str:
    """列出目录下的文件"""
    if not os.path.isdir(directory):
        raise FileNotFoundError(f"目录不存在: {directory}")

    search_pattern = os.path.join(directory, pattern)
    files = glob_module.glob(search_pattern)

    dirs = []
    regular = []
    for f in files[:200]:
        if os.path.isdir(f):
            dirs.append(os.path.basename(f) + "/")
        else:
            regular.append(os.path.basename(f))

    return json.dumps({
        "directory": directory,
        "pattern": pattern,
        "total": len(files),
        "truncated": len(files) > 200,
        "directories": dirs,
        "files": regular,
    }, ensure_ascii=False)


def _tool_run_command(command: str, workdir: str | None = None) -> str:
    """执行 shell 命令并返回输出"""
    try:
        result = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            timeout=30,
            cwd=workdir or ".",
        )
        return json.dumps({
            "exit_code": result.returncode,
            "stdout": result.stdout[:5000],
            "stderr": result.stderr[:2000],
            "stdout_truncated": len(result.stdout) > 5000,
        }, ensure_ascii=False)
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"命令超时 (30s): {command}")


# ═══════════════════════════════════════════════════════════════
# Memory 工具（特殊处理：需要 MemoryStore 实例）
# ═══════════════════════════════════════════════════════════════

def _make_memory_func(store: MemoryStore) -> Callable[..., str]:
    """创建绑定了 MemoryStore 实例的 memory 工具函数。

    这是 closure 模式：工具执行时能访问实际的 store 实例，
    而不是全局变量。
    """
    def _tool_memory(
        action: str,
        target: str = "memory",
        content: str = "",
        old_text: str = "",
        scope: str = "",
        topic: str = "",
        pinned: bool | None = None,
        query: str = "",
        limit: int = 50,
    ) -> str:
        """持久化记忆操作。

        action: "add" | "replace" | "remove" | "read" | "search"
        target: "memory" (agent 笔记) | "user" (用户画像)
        """
        effective_target = target or "memory"

        if action == "add":
            result = store.add(
                effective_target,
                content,
                scope=scope,
                topic=topic,
                pinned=True if pinned is None else pinned,
            )
        elif action == "replace":
            result = store.replace(
                effective_target,
                old_text,
                content,
                scope=scope or None,
                topic=topic or None,
                pinned=pinned,
            )
        elif action == "remove":
            result = store.remove(effective_target, old_text, scope=scope or None, topic=topic or None)
        elif action == "read":
            result = store.read(
                effective_target,
                scope=scope or None,
                topic=topic or None,
                pinned=None,
                limit=limit,
            )
        elif action == "search":
            result = store.search(
                query,
                target=target or None,
                scope=scope or None,
                topic=topic or None,
                limit=limit,
            )
        else:
            result = {"ok": False, "message": f"未知 action: {action}，可用: add, replace, remove, read, search"}

        if result.get("ok") and action in ("add", "replace", "remove"):
            store.load()

        return json.dumps(result, ensure_ascii=False)

    return _tool_memory


# 发给 LLM 的 memory 工具 schema
# description 的设计参考 Hermes Agent：详细说明"何时用"比"怎么用"更重要
MEMORY_SCHEMA_DESCRIPTION = (
    "保存和检索分层长期记忆，跨会话保留。设计参考 Claude Code：启动 prompt 只放索引，细节按主题读取。\n"
    "\n"
    "何时使用（主动保存，不要等用户要求）：\n"
    '- 用户纠正你，或说"记住这个"、"以后都这样"\n'
    "- 用户分享了偏好、习惯、个人信息（名字、时区、编码风格）\n"
    "- 你发现了环境信息（OS、安装的工具、项目结构）\n"
    "- 你学到了一个项目约定、API 特性或值得记住的工作流程\n"
    "- 你意识到一个稳定的事实会被未来的会话用到\n"
    "\n"
    "优先级：用户偏好和纠正 > 环境事实 > 过程性知识\n"
    "最有价值的记忆是能让用户不必重复自己的那些。\n"
    "\n"
    "何时不用：\n"
    "- 临时的任务进度、会话结果、已完成的 TODO\n"
    "- 可轻易重新获取的信息\n"
    "- 琐碎/显而易见的事实\n"
    "- 原始数据转储\n"
    "\n"
    "target：\n"
    "- 'memory'：agent 笔记（项目约定、工具技巧、经验教训）\n"
    "- 'user'：用户画像（名字、偏好、沟通风格、忌讳）\n"
    "\n"
    "scope 分层：\n"
    "- 'user'：跨项目用户偏好和身份信息\n"
    "- 'project'：当前项目的约定、架构、命令、踩坑\n"
    "- 'local'：当前机器/私有环境细节，不应共享到项目规则\n"
    "\n"
    "topic：主题名，如 python, testing, project-layout, api-client。"
    "把细节拆到具体 topic，避免单一 memory 无限膨胀。\n"
    "\n"
    "pinned：是否默认注入启动 prompt。只把短小、稳定、会反复用到的索引设为 true；"
    "长流程、详细经验、命令输出摘要设为 false，并用 read/search 按需取回。\n"
    "\n"
    "操作：\n"
    "- add：新增条目\n"
    "- replace：更新已有条目（old_text 是用于匹配的旧文本子串，可用 scope/topic 缩小范围）\n"
    "- remove：删除条目（old_text 是用于匹配的文本子串，可用 scope/topic 缩小范围）\n"
    "- read：按 target/scope/topic 查看条目\n"
    "- search：按 query 搜索所有记忆或指定层级\n"
    "\n"
    "重要：如果用户纠正或变更姓名、偏好等已有画像，不要新增冲突条目；"
    "优先 search/read 找旧条目，再用 replace 更新。"
)


# ═══════════════════════════════════════════════════════════════
# 创建默认工具集
# ═══════════════════════════════════════════════════════════════

def create_default_registry(
    workdir: str = ".",
    memory_store: MemoryStore | None = None,
) -> ToolRegistry:
    """创建包含默认工具的注册表。

    Args:
        workdir: 工作目录
        memory_store: MemoryStore 实例（可选）。如果提供，会注册 memory 工具。
    """
    registry = ToolRegistry()

    # ── 信息获取类 ──

    registry.register(Tool(
        name="get_time",
        description="获取当前的日期、时间和星期。当你需要知道现在是什么时间时调用此工具。",
        parameters={
            "type": "object",
            "properties": {},
            "required": [],
        },
        func=_tool_get_time,
    ))

    # ── 文件操作类 ──

    registry.register(Tool(
        name="read_file",
        description=(
            "读取文件内容。返回文件的完整文本（带行数和截断信息）。"
            "当你需要查看文件内容时使用。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "filepath": {
                    "type": "string",
                    "description": "要读取的文件路径",
                },
                "limit": {
                    "type": "integer",
                    "description": "最多读取的行数，默认 500",
                },
            },
            "required": ["filepath"],
        },
        func=_tool_read_file,
    ))

    registry.register(Tool(
        name="write_file",
        description=(
            "将内容写入文件。如果文件所在的目录不存在，会自动创建。"
            "当你需要保存数据、创建配置文件、输出结果时使用。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "filepath": {
                    "type": "string",
                    "description": "要写入的文件路径",
                },
                "content": {
                    "type": "string",
                    "description": "要写入的文件内容",
                },
            },
            "required": ["filepath", "content"],
        },
        func=_tool_write_file,
    ))

    registry.register(Tool(
        name="list_files",
        description=(
            "列出目录下的文件和子目录。支持通配符过滤（如 '*.py'）。"
            "当你需要了解项目结构、查找文件时使用。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "directory": {
                    "type": "string",
                    "description": "要列出文件的目录路径，默认当前目录 '.'",
                },
                "pattern": {
                    "type": "string",
                    "description": "文件匹配模式，如 '*.py'，默认 '*'",
                },
            },
            "required": [],
        },
        func=_tool_list_files,
    ))

    # ── 计算与处理类 ──

    registry.register(Tool(
        name="calculator",
        description=(
            "安全地计算数学表达式。支持 + - * / ** ( ) 运算符。"
            "当你需要进行数学计算时使用。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "expression": {
                    "type": "string",
                    "description": "要计算的数学表达式，如 '(1 + 2) * 3' 或 '2 ** 10'",
                },
            },
            "required": ["expression"],
        },
        func=_tool_calculator,
    ))

    registry.register(Tool(
        name="run_command",
        description=(
            "在终端中执行 shell 命令并返回结果（stdout, stderr, exit code）。"
            "当你需要编译代码、运行脚本、执行系统命令时使用。"
            "注意：命令有 30 秒超时限制。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "要执行的 shell 命令",
                },
                "workdir": {
                    "type": "string",
                    "description": "命令执行的工作目录（可选）",
                },
            },
            "required": ["command"],
        },
        func=_tool_run_command,
    ))

    # ── Memory 工具（如果有 store 实例）──
    if memory_store:
        registry.register(Tool(
            name="memory",
            description=MEMORY_SCHEMA_DESCRIPTION,
            parameters={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["add", "replace", "remove", "read", "search"],
                        "description": "操作类型：add(新增), replace(更新), remove(删除), read(按层查看), search(关键词搜索)",
                    },
                    "target": {
                        "type": "string",
                        "enum": ["", "memory", "user"],
                        "description": "存储目标：'memory'（agent 笔记）或 'user'（用户画像）。search 时可留空表示搜索全部。",
                    },
                    "content": {
                        "type": "string",
                        "description": "条目内容。add 和 replace 时必需。",
                    },
                    "old_text": {
                        "type": "string",
                        "description": "用于匹配旧条目的文本子串。replace 和 remove 时必需。",
                    },
                    "scope": {
                        "type": "string",
                        "enum": ["", "user", "project", "local"],
                        "description": "记忆层级：user(跨项目用户偏好), project(当前项目), local(本机私有)。留空时按 target 默认选择。",
                    },
                    "topic": {
                        "type": "string",
                        "description": "主题名，如 testing、project-layout、api-client。read/search/replace/remove 可用来缩小范围。",
                    },
                    "pinned": {
                        "type": "boolean",
                        "description": "是否加入启动索引并默认注入 prompt。短小稳定事实用 true；长细节和流程用 false。",
                    },
                    "query": {
                        "type": "string",
                        "description": "search 时的关键词，可匹配 topic 或内容。",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "read/search 最多返回的条目数。",
                    },
                },
                "required": ["action"],
            },
            func=_make_memory_func(memory_store),
        ))

    return registry
