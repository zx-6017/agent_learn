# AI Agent 学习计划

从入门到能独立开发 Agent 的学习路径。

---

## 学习阶段总览

```
Phase 1 (1-2天) ✅   Phase 2 (3-5天) ⚠️   Phase 3 (进行中)       Phase 4 (待开始)
基础概念               核心机制               动手实践              源码阅读
    │                     │                     │                     │
    ├─ ReAct 论文 ✅      ├─ Memory & Context   ├─ min_agent ✅       ├─ smolagents
    ├─ Toolformer ⬜      ├─ Planning ⬜        ├─ Memory 升级 ✅     ├─ Hermes Agent
    ├─ AutoGPT ✅         ├─ 吴恩达课程 ⬜       ├─ L1/L2 记忆 🔨     ├─ CrewAI
    └─ 李沐视频 ✅        └─ 论文精读 ⬜         ├─ min_claude 🔨     └─ LangGraph
                                                ├─ Phase 4 上下文
                                                ├─ Phase 5 Memory
                                                ├─ Phase 6 Planning
                                                ├─ Phase 7 Sub-agent
                                                └─ Phase 8 工程化
```

---

## Phase 1：基础概念（1-2 天）✅ 已完成

### 必读论文

| 论文 | 链接 | 核心思想 | 优先级 | 状态 |
|------|------|---------|--------|------|
| ReAct (2022) | https://arxiv.org/abs/2210.03629 | Thought→Action→Observation 循环 | ⭐⭐⭐ | ✅ |
| Toolformer (2023) | https://arxiv.org/abs/2302.04761 | LLM 如何学会用工具 | ⭐⭐ | ⬜ |

### 快速了解 AutoGPT/BabyAGI

不用读论文，直接看 GitHub README 和架构图：
- AutoGPT: https://github.com/Significant-Gravitas/AutoGPT
- BabyAGI: https://github.com/yoheinakajima/babyagi

**看完要能回答：**
- [x] Agent Loop 是怎么循环的？
- [x] 工具是怎么被调用的？
- [x] 任务是怎么分解的？
- [x] 什么情况下 Agent 会停下来？

### 中文视频

**B站搜索关键词：**
- "跟李沐学AI ReAct" — 论文精读，质量最高 ✅
- "吴恩达 AI Agent" — 有搬运翻译版，讲 LangChain Agent 系列 ⬜
- "李宏毅 LLM Agent" — 台大课程，讲得深入浅出 ⬜

---

## Phase 2：核心机制（3-5 天）⚠️ 部分完成

### Memory & Context — 4 级记忆架构

**重要发现：Hermes Agent 使用分级存储，不是单一 memory**

| 层级 | 名称 | 存储方式 | 生命周期 | 内容示例 | 实现状态 |
|------|------|---------|---------|---------|---------|
| **L1** | 工作记忆 | 内存环形缓冲区 | 当前任务 | 当前对话的输入、工具调用结果、中间推理 | ⬜ |
| **L2** | 会话记忆 | SQLite + FTS5 全文搜索 | 当前会话 | 本次会话全部历史消息，支持关键词检索 | ⬜ |
| **L3** | 事实记忆 | 纯文本文件 | 跨会话 | 用户偏好、环境配置、项目约定 | ✅ |
| **L4** | 程序性记忆 | Markdown 文件 (SKILL.md) | 跨会话 | 可复用的操作流程、踩坑经验、工作流 | ⬜ |

**四级记忆的数据流：**

```
用户输入
    ↓
┌─────────────────────────────────────┐
│  L4 程序性记忆 (Skills)              │  ← 按需匹配 + 动态注入
│  "处理 Python 项目时的流程..."       │
└─────────────────────────────────────┘
    ↓ 注入 system prompt
┌─────────────────────────────────────┐
│  L1 工作记忆 (Working Memory)        │  ← 环形缓冲区
│  系统提示词 + L3/L4 注入 + 最近 N 轮  │    当前推理的直接上下文
│  对话 + 工具调用结果                 │
└─────────────────────────────────────┘
    ↓ 会话结束时归档
┌─────────────────────────────────────┐
│  L2 会话记忆 (Session Memory)        │  ← SQLite + FTS5
│  完整对话历史，支持搜索              │    可回溯任意历史消息
│  "上次我们讨论过..."                │
└─────────────────────────────────────┘
    ↓ LLM 自主判断写入
┌─────────────────────────────────────┐
│  L3 事实记忆 (Factual Memory)        │  ← 纯文本文件
│  用户偏好、环境信息、项目约定        │    跨会话持久化
└─────────────────────────────────────┘
```

**当前实现映射：**

| 层级 | 对应代码 | 状态 |
|------|---------|------|
| L1 | agent/agent.py 的 `self.messages` | ✅ 基础版（无环缓冲，无 token 管理） |
| L2 | **待实现** | ⬜ |
| L3 | agent/memory.py（SQLite 版，Hermes 风格） | ✅ |
| L4 | **待实现**（min_claude M4: Skill 系统） | ⬜ |

### Memory 写入决策权

**核心原则：LLM 全权决定，程序只做基础设施**

```
程序做的事：                    LLM 做的事：
├─ 存储（SQLite / 文件）        ├─ 判断"该不该写"
├─ 字符上限检查                 ├─ 判断"该写什么"
├─ 去重                         ├─ 判断"何时更新"
├─ 注入检测                     ├─ 判断"何时删除"
├─ 子串匹配（replace/remove）   └─ 判断"写入优先级"
└─ Frozen snapshot 管理
```

### Planning

**论文：**
- Plan-and-Solve: https://arxiv.org/abs/2305.04091
- Tree of Thoughts (ToT): https://arxiv.org/abs/2305.10601
- B站搜 "思维树" 或 "LLM 规划"

### 免费课程

**吴恩达 DeepLearning.AI：**
- https://www.deeplearning.ai/courses/
- "Building Agentic RAG with LlamaIndex"（免费）
- "AI Agentic Design Patterns with AutoGen"（免费）

---

## Phase 3：动手实践（持续进行中）🔨

### min_agent ✅ 已完成

位置：`/Users/zx/Documents/my_code/agent_learn/agent/`

**已完成能力：**

| 能力 | 实现 | 说明 |
|------|------|------|
| LLM 客户端 | agent/llm.py | OpenAI 兼容 API，原生 tool_use，HTTP 自动重试 |
| 工具系统 | agent/tools.py | 7 个工具（含 memory）+ JSON Schema |
| ReAct Loop | agent/agent.py | 原生 function calling，100% 确定性 |
| L3 事实记忆 | agent/memory.py | SQLite 持久化，Hermes 风格的 add/replace/remove |
| 终端渲染 | agent/agent.py | ANSI 彩色输出，工具调用过程可视化 |
| 配置管理 | config.yaml | YAML 驱动，支持环境变量占位符 |

**架构要点：**

```
User Input → [System Prompt + L3 Memory + History]
  → LLM 推理（原生 tool_use，返回 JSON tool_calls）
    → 框架解析 JSON → 执行工具 → 结果喂回 LLM
    → LLM 输出文本 → 最终答案
```

**输出渲染三层架构：**
1. 工具调用 → 框架捕获 JSON → 执行 → 彩色终端展示
2. LLM 文本回复 → markdown → 终端直接渲染
3. 结构化的中间数据 → 框架决定"展示什么"和"喂给 LLM 什么"可以不同

---

### L2 会话记忆 🔨 下一步

**目标：** 实现 Hermes 同款的 SQLite + FTS5 会话记忆

**核心能力：**
- 每轮对话自动归档到 SQLite
- FTS5 全文搜索（"我们上次讨论过什么"）
- `session_search` 工具，Agent 可主动检索历史
- 会话边界管理（何时开始新会话）

**设计要点：**
```sql
CREATE TABLE messages (
    id INTEGER PRIMARY KEY,
    session_id TEXT,
    role TEXT,           -- user / assistant / tool
    content TEXT,
    tool_name TEXT,      -- 如果是 tool role
    created_at TIMESTAMP
);
CREATE VIRTUAL TABLE messages_fts USING fts5(content, role);
```

---

### min_claude 🔨 规划中

**目标：** 基于 min_agent 核心代码，构建类 Claude Code 的编程助手

**项目结构规划（更新版，融合 4 级记忆）：**

```
/Users/zx/Documents/my_code/agent_learn/min_claude/
├── main.py              # CLI 入口
├── config.yaml
│
├── cli/                 # 终端 UI 层
│   ├── app.py           # prompt_toolkit 交互循环
│   ├── renderer.py      # diff 渲染、语法高亮、彩色输出
│   └── completions.py   # 文件路径补全、命令补全
│
├── agent/               # 核心引擎
│   ├── loop.py          # ReAct 循环
│   ├── llm.py           # LLM 客户端
│   └── policy.py        # 安全策略（审批/沙箱/权限）
│
├── tools/               # 工具层
│   ├── registry.py      # 工具注册表
│   ├── file.py          # 读/写/编辑/搜索文件
│   ├── shell.py         # 执行命令
│   ├── git.py           # Git 操作
│   ├── web.py           # 网络请求
│   └── code.py          # 代码分析与生成（AST/LSP）
│
├── memory/              # 4 级记忆系统
│   ├── working.py       # L1 工作记忆（环形缓冲区 + token 预算）
│   ├── session.py       # L2 会话记忆（SQLite + FTS5）
│   ├── factual.py       # L3 事实记忆（纯文本，已实现）
│   └── retrieval.py     # 统一检索接口（跨 L2/L3/L4）
│
├── skills/              # L4 程序性记忆
│   ├── loader.py        # 加载 SKILL.md 文件
│   └── matcher.py       # 按场景匹配并注入
│
└── context/             # 上下文管理
    ├── manager.py       # Token 预算 + 滑动窗口
    ├── summarizer.py    # 历史压缩
    └── project.py       # AGENTS.md 自动注入
```

**分阶段路线（更新版）：**

| 阶段 | 内容 | 对应记忆层 | 核心产出 |
|------|------|-----------|---------|
| **当前** | L3 事实记忆 (memory) | L3 | ✅ SQLite 持久化 + LLM 自主写入 |
| **下一步** | L2 会话记忆 + FTS5 | L2 | 全文搜索历史对话 + session_search 工具 |
| M1 | 终端 UI | — | prompt_toolkit 交互 + 语法高亮 diff 渲染 |
| M2 | L1 工作记忆 + 上下文管理 | L1 | 环形缓冲区 + Token 预算 + 摘要压缩 |
| M3 | 工具扩展 | — | 文件编辑/search/git/web 等 15 个工具 |
| M4 | L4 程序性记忆 (Skills) | L4 | SKILL.md 加载 + 场景匹配 + 动态注入 |
| M5 | RAG | — | 代码库索引 + 语义检索 + 上下文注入 |
| M6 | 安全与策略 | — | 审批流程 + 工具权限 + 沙箱 |
| M7 | 统一记忆检索 | L2+L3+L4 | 跨层级检索 + 记忆衰减 + 自动写入策略 |

---

## 学习过程中积累的关键认知

### 4 级记忆架构的核心洞察

```
不是"一个 memory 解决所有问题"
而是"不同生命周期的信息用不同存储策略"

L1 工作记忆 → 毫秒级，内存，容量极小（当前推理上下文）
L2 会话记忆 → 秒级，SQLite，容量中（本次对话历史）
L3 事实记忆 → 跨会话，文件/DB，容量小（关键信息摘要）
L4 程序性记忆 → 跨会话，Markdown，容量中（可复用知识）
```

**写入策略：**
- L1/L2：自动写入（框架在每轮对话后自动归档）
- L3：LLM 主动写入（通过 memory 工具）
- L4：用户手动创建 + LLM 辅助维护（通过 skill 工具）

**检索策略：**
- L1：全量在上下文中（不需要检索）
- L2：FTS5 全文搜索 + 语义检索（session_search 工具）
- L3：会话启动时全量注入 system prompt（to_prompt）
- L4：按场景匹配注入（当前任务触发哪个 skill）

### 结构化输出的三层架构

| 层级 | 机制 | 确定性 | 用法 |
|------|------|--------|------|
| 第一层 | tool_use (function calling) | 100% | LLM 返回 JSON tool_calls，框架解析执行 |
| 第二层 | response_format (JSON Schema) | 100% | 强制 LLM 输出符合 schema 的 JSON |
| 第三层 | markdown 自由文本 | 不保证 | LLM 自然语言输出，终端渲染 |

### Agent 输出渲染的核心原则

```
工具结果 ──→ LLM（完整，不截断，保证推理质量）
         ──→ 用户（截断/格式化，为了可读性）
```

### Claude Code 的输出是怎么拼的

```
[read_file] /src/main.py                      ← 框架渲染的工具调用标签
  15| def calculate(x, y):                      ← 工具返回的内容，框架选择展示
  16|     return x * y

agent.py 里的 tool_calls 构建是这样的：       ← LLM 文本回复开始

\```python                                      ← LLM 自己写的 markdown 代码块
assistant_msg["tool_calls"] = [...]            ← 框架负责语法高亮
\```

# 三部分的来源：
# 1. [read_file] 标签 → 框架
# 2. 代码行 15-16 → 工具返回，框架展示
# 3. 自然语言 + 代码块 → LLM 自己写的 markdown
```

### 工具设计原则

- `description` 是写给模型的"说明书"，写得越具体 LLM 调用越准确
- 工具返回结构化的 JSON，不是裸字符串
- Schema 越精确，LLM 越少犯错
- 工具结果对 LLM 完整返回，对用户智能截断

---

## Phase 4：源码阅读（待开始）

### 推荐阅读顺序

从简单到复杂：

| 顺序 | 项目 | 语言 | 代码量 | 看什么 |
|------|------|------|--------|--------|
| 1 | **smolagents** (HuggingFace) | Python | ~1000 行 | 最简 Agent Loop |
| 2 | **TinyTroupe** (Microsoft) | Python | ~2000 行 | Agent 模拟 + Memory |
| 3 | **Hermes Agent** (Nous) | Python | 中型项目 | 全功能参考（4 级记忆、Skill 系统） |
| 4 | **CrewAI** | Python | 中型项目 | Multi-Agent 协作 |
| 5 | **LangGraph** (LangChain) | Python | 大型项目 | 状态机 Agent |

### 读源码的正确姿势

**不要从第一行读到最后一行！**

1. **找入口** — `main()`、`cli()`、`run()`，理解启动流程
2. **找循环** — 搜索 `while`，找到 Agent Loop 的核心循环
3. **追踪一条链路** — 从一个简单的用户请求开始，逐步 debug
4. **看数据结构** — Message、Tool、Memory 的内部表示
5. **最后看扩展** — Plugin、Skill 的加载和注册机制

---

## 资源汇总

### 论文合集

- awesome-ai-agents: https://github.com/e2b-dev/awesome-ai-agents
  - 按类别整理了几乎所有重要论文和项目

### 视频

| 平台 | 搜索关键词 | 说明 |
|------|-----------|------|
| B站 | "跟李沐学AI ReAct" | 论文精读，必看 |
| B站 | "吴恩达 AI Agent" | 系统性课程 |
| B站 | "李宏毅 LLM Agent" | 深入浅出 |
| B站 | "从零实现 AI Agent" | 代码实操 |
| YouTube | "AI Agent from scratch" | 英文代码教程 |

### 中文博客

- **知乎** 搜 "LLM Agent 综述" — 万字长文
- **公众号** "李rumor"、"夕小瑶" — 持续输出 Agent 内容
- **waytoagi** https://www.waytoagi.com/ — AI Agent 知识库

### 开源项目

| 项目 | 链接 | 用途 |
|------|------|------|
| smolagents | https://github.com/huggingface/smolagents | 学习核心 Loop |
| Hermes Agent | https://github.com/NousResearch/Hermes-Agent | 全功能参考（4 级记忆） |
| LangGraph | https://github.com/langchain-ai/langgraph | 状态机 Agent |
| CrewAI | https://github.com/crewAIInc/crewAI | Multi-Agent |
| AutoGen | https://github.com/microsoft/autogen | 微软多 Agent 框架 |

---

## 学习原则

1. **代码驱动学习** — 看完概念立刻写代码，哪怕只写 50 行
2. **先抄后创** — 抄 smolagents 的核心 Loop，理解后再自己写
3. **单步调试** — 打印每一步的 prompt、response、tool_result
4. **不要贪多** — 先把 ReAct 彻底搞懂，再学 Planning 和 Multi-Agent
5. **边学边记** — 用自己的话记录理解，代码注释就是最好的笔记
6. **工具描述的质量决定 Agent 的智商** — description 是写给 LLM 的说明书
7. **上下文管理是工程核心** — 不是事后补丁，而是架构基础
8. **记忆要分级** — 不同生命周期的信息用不同策略，L1-L4 各司其职
9. **LLM 决定写入，程序只做基础设施** — 不要替模型做语义判断

---

## 当前进度

```
min_agent ✅                     记忆系统:
├─ llm.py                        ├─ L1 工作记忆 ⬜ (agent.py messages 基础版)
├─ tools.py (7 tools)            ├─ L2 会话记忆 ⬜ (SQLite + FTS5，下一步)
├─ agent.py                      ├─ L3 事实记忆 ✅ (agent/memory.py)
├─ memory.py (L3 ✅)             └─ L4 程序性记忆 ⬜ (Skills 系统，M4)
├─ config.yaml
└─ main.py

min_claude 🔨
下一步: L2 会话记忆
```

**一句话：第一天就把代码跑起来，然后边改边学。光看不写，一个月还在原地。**
