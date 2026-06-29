# AGENTS.md — min_agent 开发规范

> 供 OpenAI Codex、CI Agent 及其他自动化工具使用。
> 与 CLAUDE.md 内容一致，并补充工具 schema 规范。

---

## 项目简介

基于原生 `tool_use`（function calling）的 ReAct Agent 学习项目。

**运行：**

```bash
python main.py                   # 交互模式
python main.py "帮我算 123*456"  # 单次任务
python main.py --tools           # 列出工具
```

**依赖安装：**

```bash
pip install pyyaml
```

---

## 项目结构

```
agent/
  llm.py      — LLM 客户端（OpenAI 兼容，纯标准库 urllib）
  tools.py    — 工具注册表和内置工具
  memory.py   — SQLite 持久化记忆
  agent.py    — ReAct 主循环
main.py       — 程序入口
config.yaml   — 运行配置（API key 从环境变量读取）
```

---

## 核心原则

1. **tool_use 替代文本解析** — LLM 返回 JSON，框架解析，不做正则
2. **工具结果完整喂 LLM** — 截断仅用于终端展示，不影响 LLM 输入
3. **LLM 决定写 memory** — 程序只做存储和上限检查
4. **description 决定 Agent 质量** — 重点描述"何时用"，而非"怎么用"

---

## 工具开发规范

### 添加新工具

在 `tools.py` 的 `create_default_registry()` 中注册：

```python
registry.register(Tool(
    name="my_tool",
    description=(
        "何时使用这个工具的描述。"
        "描述触发条件，不是实现细节。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "param_name": {
                "type": "string",
                "description": "参数说明",
            },
        },
        "required": ["param_name"],
    },
    func=_tool_my_tool,
))
```

### 工具函数规范

```python
def _tool_my_tool(param_name: str, optional_param: int = 10) -> str:
    """工具实现。直接 raise 异常，Tool.execute() 会捕获。"""
    ...
    return "结果字符串"
```

- 返回值通过 `str(output)` 转换，可以返回任意类型
- 不需要在工具函数内部 try/except，由 `Tool.execute()` 统一处理
- 函数签名参数名必须和 JSON Schema 的 `properties` key 一致

### 工具返回结构（`ToolResult`）

```python
{
    "ok": True,       # 是否成功
    "output": "...",  # 工具输出（字符串）
    "error": None,    # 错误信息（失败时有值）
    "meta": {}        # 扩展元数据（通常为空 dict）
}
```

---

## 代码风格

### 必须遵守

- 文件顶部加 `from __future__ import annotations`
- 公共接口使用 `TypedDict`，不用裸 `dict[str, Any]`
- `Callable` 必须加泛型参数：`Callable[..., str]`
- 字符串不允许隐式拼接，用 `+` 显式连接相邻字符串字面量

### TypedDict 清单

| TypedDict | 所在模块 | 说明 |
|---|---|---|
| `OpenAIConfig` | `agent/llm.py` | LLM 配置结构 |
| `ParsedToolCall` | `agent/llm.py` | 解析后的工具调用 `{id, name, arguments}` |
| `LLMResponse` | `agent/llm.py` | `chat()` 返回：`{content, tool_calls, finish_reason, usage}` |
| `ToolResult` | `agent/tools.py` | 工具执行结果 `{ok, output, error, meta}` |
| `MemoryEntry` | `agent/memory.py` | 记忆条目行 `{id, target, scope, topic, pinned, content, ...}` |
| `MemoryResult` | `agent/memory.py` | CRUD 操作返回 `{ok, message, usage?, entries?, ...}` |

---

## Memory 工具调用规范

当修改 memory 相关代码时：

| 操作 | 方法 | 必填参数 |
|---|---|---|
| 新增条目 | `store.add()` | `target`, `content` |
| 更新条目 | `store.replace()` | `target`, `old_text`, `new_content` |
| 删除条目 | `store.remove()` | `target`, `old_text` |
| 查看条目 | `store.read()` | （可选过滤） |
| 搜索条目 | `store.search()` | `query` |

- `old_text` 为子串匹配，确保唯一性才能成功 replace/remove
- `pinned=True` 的条目进入启动 prompt 预算（memory 上限 2200 字符）
- `pinned=False` 的细节按需读取，不占预算

---

## 禁止事项

- ❌ 不得解析 LLM 的文本输出来提取工具调用（必须用 `tool_calls` JSON）
- ❌ 不得在 `MemoryStore` 中做语义判断
- ❌ 不得硬编码 API Key，使用 `${OPENAI_API_KEY}` 占位符
- ❌ 不得修改 `Tool.execute()` 的错误捕获逻辑（工具函数直接 raise 即可）
- ❌ 不得绕过 `ToolRegistry` 直接调用工具函数
