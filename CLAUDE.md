# CLAUDE.md — min_agent 开发规范

> 本文件供 Claude Code、Codex、Hermes 等 AI Coding Agent 使用。
> 修改代码前请先通读本文件。

---

## 项目简介

基于原生 `tool_use`（function calling）的 ReAct Agent 学习项目。
核心架构：LLM → JSON tool_calls → 执行工具 → 结果喂回 LLM → 循环直到最终答案。

**启动方式：**

```bash
python main.py                   # 交互模式
python main.py "帮我算 123*456"  # 单次任务
python main.py --tools           # 列出工具
python main.py --memory          # 查看记忆
```

---

## 项目结构

```
agent/
  llm.py      — OpenAI 兼容 HTTP 客户端（纯标准库，零依赖）
  tools.py    — 工具定义、注册表、内置工具集
  memory.py   — SQLite 持久化记忆（L3 事实记忆）
  agent.py    — ReAct 循环核心
main.py       — 入口，读取 config.yaml
config.yaml   — 运行配置（API key 通过环境变量注入）
```

---

## 核心设计原则

1. **原生 tool_use，禁止文本解析**
   LLM 返回 JSON tool_calls，框架解析执行，不做正则匹配。

2. **工具结果：完整喂 LLM，截断展示给用户**
   `_execute_and_record` 中完整写入消息历史；`agent.py` 中只截断终端输出。

3. **LLM 决定写入 memory，程序只做基础设施**
   `MemoryStore` 不做语义判断，只管存储、上限、去重、子串匹配。

4. **tool description 质量决定 Agent 智商**
   description 是写给模型的说明书，重点写"何时用"，而不是"怎么用"。

---

## 代码风格

### Python 规范

- 所有文件顶部加 `from __future__ import annotations`（延迟类型求值）
- 使用 `TypedDict` 定义结构化数据，禁止裸 `dict`（尤其是跨模块传递的数据）
- 禁止 `dict[str, Any]` 作为函数公共接口的参数或返回类型，用 `TypedDict` 替代
- 使用 `dict[str, object]` 替代 `dict[str, Any]`（仅在内部动态结构中使用 `Any`）
- 函数必须有完整类型注解，包括 `-> None`

### 命名

- 模块和类文档字符串：中文
- 代码标识符（变量名、函数名、参数名）：英文
- 内部 TypedDict（不暴露给外部）以下划线开头，如 `_Config`

### 格式

- 禁止隐式字符串拼接（basedpyright `reportImplicitStringConcatenation` 已开启）
  ```python
  # ❌
  Color.dim(
      f"tokens: {n}"
      f"finish: {r}"
  )
  # ✅
  Color.dim(
      f"tokens: {n}"
      + f"finish: {r}"
  )
  ```

---

## 类型系统约定

### 跨模块共享的 TypedDict（在定义模块导出，调用方 import）

| TypedDict | 定义位置 | 用途 |
|---|---|---|
| `OpenAIConfig` | `llm.py` | LLMClient 配置 |
| `ParsedToolCall` | `llm.py` | 解析后的单次工具调用 |
| `LLMResponse` | `llm.py` | `LLMClient.chat()` 返回值 |
| `ToolResult` | `tools.py` | `Tool.execute()` 返回值 |
| `MemoryEntry` | `memory.py` | 记忆条目行结构 |
| `MemoryResult` | `memory.py` | 所有 CRUD 操作返回值 |

### 工具返回结构

```python
class ToolResult(TypedDict):
    ok: bool         # 是否成功
    output: str      # 工具输出文本
    error: str | None
    meta: dict[str, object]
```

---

## 记忆系统约定

### 分层结构

```
target  : memory（agent 笔记）| user（用户画像）
scope   : user（跨项目） | project（当前项目） | local（本机私有）
topic   : 主题名，如 testing / project-layout
pinned  : True → 注入启动 prompt；False → 按需 search/read
```

### 字符预算

- memory pinned 上限：2200 字符
- user pinned 上限：1375 字符
- 非 pinned 条目不占启动 prompt 预算

### Frozen snapshot

- `load()` 在每次 `agent.run()` 开始前调用，冻结当前 pinned 索引
- 会话内写入（add/replace/remove）不影响本轮 system prompt
- 写入成功后调用 `store.load()` 刷新 snapshot，下一轮 run 生效

---

## 修改工具时的注意事项

1. 修改 `func` 实现时，同步检查 JSON Schema 的 `description` 是否仍然准确
2. 新增工具时，`Tool` 构造函数的 `func` 必须是 `Callable[..., object]`
3. `func` 抛出异常时会被 `Tool.execute()` 捕获，不需要在工具函数内部包裹 try/except
4. 工具返回值最终由 `str(output)` 转为字符串，工具函数可以直接返回任意值

---

## 禁止事项

- ❌ 不得用文本/正则解析 LLM 输出来提取工具调用
- ❌ 不得在 `MemoryStore` 内部做语义判断（判断该不该写由 LLM 决定）
- ❌ 不得在 `Tool.execute()` 调用链之外单独执行工具函数
- ❌ 不得把 API Key 硬编码进代码或提交到 git（config.yaml 已在 .gitignore）
- ❌ 不得在公共接口使用裸 `Callable`（需加泛型参数 `Callable[..., str]` 等）
