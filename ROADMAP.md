# AI Agent 项目开发路线图

从 0 开发一个 AI Agent 项目，按迭代顺序逐步构建。

---

## 当前状态

```
✅ Phase 1: 最简 Agent Loop     → llm.py + 1 tool + 消息管理
✅ Phase 2: 工具系统             → 6 tools + JSON Schema + 注册表
✅ Phase 3: ReAct 推理循环       → 原生 function calling 替代文本解析
⬜ Phase 4: 上下文管理           → Token 预算 + 滑动窗口 + 摘要压缩
⬜ Phase 5: Memory 记忆系统      → 自动写入策略 + 向量检索
⬜ Phase 6: Planning 显式规划   → Plan-then-Execute + Replan
⬜ Phase 7: Sub-agent 子代理     → 并行派发 + 上下文隔离
⬜ Phase 8: 工程化               → 可观测性 + 安全 + 扩展性

🔨 min_claude: 新项目骨架搭建中
```

---

## 总体架构

```
┌─────────────────────────────────────────────────┐
│                   User Interface                  │
├─────────────────────────────────────────────────┤
│                  Agent Runtime                    │
│  ┌─────────┐  ┌──────────┐  ┌────────────────┐  │
│  │  Loop   │  │  Context  │  │   Terminate    │  │
│  │  State  │  │  Manager  │  │   Conditions   │  │
│  └─────────┘  └──────────┘  └────────────────┘  │
├─────────────────────────────────────────────────┤
│     Tools        Memory         Planning          │
│  ┌──────────┐ ┌──────────┐ ┌──────────────────┐  │
│  │ Registry │ │ Working  │ │ ReAct / Plan-then│  │
│  │ Executor │ │ Short    │ │ -Execute / ToT   │  │
│  │ Sandbox  │ │ Long     │ │ Replan           │  │
│  └──────────┘ └──────────┘ └──────────────────┘  │
├─────────────────────────────────────────────────┤
│              Sub-agent / Delegation               │
│  ┌──────────┐ ┌──────────┐ ┌──────────────────┐  │
│  │ Spawn    │ │ Context  │ │ Result            │  │
│  │ Control  │ │ Passing  │ │ Verification      │  │
│  └──────────┘ └──────────┘ └──────────────────┘  │
├─────────────────────────────────────────────────┤
│        Infrastructure (Log/Security/Config)       │
└─────────────────────────────────────────────────┘
```

---

## 开发迭代（按顺序）

### Phase 1：最简 Agent Loop ✅

**目标：LLM + 1 个工具，跑通完整链路**

**架构：** 原生 tool_use（function calling），非文本解析

```
while not done:
    response = llm.chat(messages, tools=tool_schemas)
    if response has tool_calls:
        result = execute_tool(tool_call)
        messages.append(tool_role_message(result))
    else:
        done = True
```

**产出：**
- [x] LLM 调用封装（OpenAI 兼容 API，urllib 纯标准库实现）
- [x] 原生 tool_use 支持（JSON tool_calls 解析，非正则匹配）
- [x] 基础消息管理（system/user/assistant/tool 四种 role）
- [x] 重试机制（HTTP 5xx 指数退避重试，4xx 直接报错）
- [x] 终止条件判断（max_steps + 连续错误）

**验证：** ✅ Agent 能用多个工具链式完成复合任务

---

### Phase 2：工具系统 ✅

**目标：多工具 + 统一接口 + 结构化展示**

**关键设计：**

```
Tool Schema (JSON)
    ↓
LLM 返回 tool_calls (JSON)
    ↓
Tool Executor (统一返回 {"ok": bool, "output": str, ...})
    ↓
Result → 完整喂 LLM / 截断展示给用户
```

**产出：**
- [x] 工具注册表（ToolRegistry，按名称查找）
- [x] JSON Schema 生成（to_openai_schema，含完整 description）
- [x] 统一执行接口（execute → {ok, output, error, meta}）
- [x] 6 个内置工具：get_time / calculator / read_file / write_file / list_files / run_command
- [x] 工具结果智能截断（>400 字符自动裁剪，LLM 端完整保留）
- [x] 彩色终端渲染（ANSI 颜色，成功/错误视觉区分）
- [x] 工具输出可扩展（add_tool / register 接口）

**验证：** ✅ Agent 能自主选择工具并链式完成写文件→统计→执行等多步任务

---

### Phase 3：ReAct 推理循环 ✅

**目标：Reasoning + Acting，带可视化**

**核心变更：从文本解析升级为原生 function calling**

```
旧（文本解析）：
  LLM → "Action: calculator(expr='2+3')" → 正则匹配 → 容易失败

新（原生 tool_use）：
  LLM → {"tool_calls": [{"name":"calculator", "arguments": {"expr":"2+3"}}]}
      → json.loads → 100% 确定
```

**产出：**
- [x] ReAct prompt 模板（中文 system prompt + 工具调用规则）
- [x] 原生 tool_use 循环（非 Thought/Action/Observation 文本解析）
- [x] 每步可视化（工具名、参数、结果、token 消耗）
- [x] 步骤追踪和截断输出
- [x] 连续错误检测（≥3 次自动终止）
- [x] 最终答案格式化展示

**验证：** ✅ Agent 可视化每一步的推理和工具调用过程

---

### Phase 4：上下文管理 ⬜

**目标：Token 预算控制 + 历史压缩 + 滑动窗口**

**Context Window Management：**

```
┌─────────────────────────────────────┐
│ System Prompt     (固定，~500 tokens) │
│ Tools Schema      (固定，~1000 tokens)│
│ Memory Injection  (按需，~500 tokens) │
├─────────────────────────────────────┤
│ Conversation History                 │
│  - 最近 N 轮完整保留                │
│  - 更早的自动摘要压缩               │
├─────────────────────────────────────┤
│ Scratchpad       (当前推理空间)      │
│ Budget: total - fixed - history      │
└─────────────────────────────────────┘
```

**产出：**
- [ ] Token 计数器
- [ ] 滑动窗口（最近 N 轮完整保留）
- [ ] 历史摘要压缩（LLM 自动生成摘要）
- [ ] Token 预算分配策略
- [ ] 工具结果智能裁剪（大文件读半截，小文件全读）

**验证：** 长对话不会 OOM，关键信息不丢失

---

### Phase 5：Memory 记忆系统 ⬜

**目标：跨会话持久化 + 智能检索 + 自动写入**

**三层记忆架构：**

| 层级 | 生命周期 | 存储方式 | 用途 |
|------|---------|---------|------|
| Working | 当前任务 | 上下文中的 scratchpad | 中间推理结果 |
| Short-term | 当前会话 | 消息列表 + 滑动窗口 | 对话历史 |
| Long-term | 跨会话 | SQLite / 向量 DB | 用户偏好、环境信息 |

**关键原则：**

- 写入策略比检索策略更重要 — 什么该记、什么该忘
- Memory 注入时机：system prompt 注入 vs 工具检索
- 声明式存储（declarative facts），而非指令式

**产出：**
- [x] Memory 存储后端（SQLite，基础版）✅
- [ ] 自动写入策略（用户偏好、环境信息、重要决策）
- [ ] 向量检索（语义相似度匹配）
- [ ] 冲突处理（覆盖 vs 追加 vs 版本化）
- [ ] 记忆衰减（不常用的自动降权）

**验证：** 新会话能记住之前的用户偏好和环境信息

---

### Phase 6：Planning 显式规划 ⬜

**目标：复杂任务先规划再执行**

```
Plan → Execute Step 1 → Verify → Execute Step 2 → Verify → ...
                                             ↓ (偏差过大)
                                         Replan → Execute → ...
```

**规划模式：**

- **Plan-then-Execute**：先生成完整计划，逐步执行
- **Replan**：执行中发现偏差，动态调整计划
- **Plan as Tool**：规划本身暴露为工具，Agent 可显式调用

**产出：**
- [ ] Plan 生成提示词
- [ ] 计划存储和状态追踪
- [ ] 执行偏差检测
- [ ] 动态重规划触发条件

**验证：** Agent 面对陌生任务能先生成计划，再按计划执行

---

### Phase 7：Sub-agent 子代理 ⬜

**目标：复杂任务分治，多 Agent 协作**

```
        Orchestrator
       /     |     \
  Worker1  Worker2  Worker3
  (独立上下文)(独立上下文)(独立上下文)
```

**关键设计：**

- 上下文隔离：子 Agent 看不到父 Agent 完整历史
- 显式 Context 传递：父 Agent 通过 `context` 字段传递必要信息
- 深度限制：防止无限递归
- 并发控制：最多同时运行 N 个子 Agent
- 结果验证：子 Agent 报告不可信，关键操作需父 Agent 验证
- 中断传播：父 Agent 被停止时，子 Agent 也应终止

**产出：**
- [ ] 子 Agent 生成和生命周期管理
- [ ] 上下文传递协议
- [ ] 并发执行引擎
- [ ] 结果收集和验证

**验证：** 父 Agent 能同时派发 3 个子 Agent 并行工作

---

### Phase 8：工程化 ⬜

**目标：生产可用**

**可观测性：**
- [ ] 每步 token 消耗、耗时日志
- [ ] 工具调用链路追踪
- [ ] 会话日志和回放

**安全：**
- [ ] Prompt injection 防护
- [ ] 工具权限最小化
- [ ] 敏感信息脱敏
- [ ] 文件操作审批流程

**扩展性：**
- [ ] Plugin / Skill 机制
- [ ] 多 Provider 支持（OpenAI / Anthropic / DeepSeek）
- [ ] 配置管理（YAML / 环境变量）
- [ ] Cron / 定时任务

**验证：** 第三方可以为 Agent 开发插件，扩展能力

---

## min_claude 项目（新）

**目标：** 基于 min_agent 核心代码，构建类 Claude Code 的编程助手

位置：`/Users/zx/Documents/my_code/agent_learn/min_claude/`

**新增模块：**

| 模块 | 说明 |
|------|------|
| cli/ | 终端 UI（prompt_toolkit + 语法高亮 + diff 渲染） |
| skills/ | 可扩展 Skill 系统（SKILL.md 加载 + 场景匹配） |
| rag/ | 代码库索引 + 语义检索 |
| context/ | Token 预算 + 滑动窗口 + 摘要压缩 |

**开发里程碑：**

| 阶段 | 内容 | 核心产出 |
|------|------|---------|
| M1 | 终端 UI | prompt_toolkit 交互 + 语法高亮 diff 渲染 |
| M2 | 上下文管理 | Token 计数 + 滑动窗口 + 摘要压缩 |
| M3 | 工具扩展 | 文件编辑/search/git/web 等 15 个工具 |
| M4 | Skill 系统 | SKILL.md 加载 + 场景匹配 + 动态注入 |
| M5 | RAG | 代码库索引 + 语义检索 + 上下文注入 |
| M6 | 安全与策略 | 审批流程 + 工具权限 + 沙箱 |
| M7 | 记忆升级 | 自动写入策略 + 向量检索 + 记忆衰减 |

---

## 核心设计原则

1. **先让单 Agent 跑稳，再搞多 Agent**
2. **工具描述的质量决定 Agent 的智商** — description 是写给 LLM 的说明书
3. **Memory 写入策略比检索更重要**
4. **子 Agent 的输出永远需要验证** — 自报告不可信
5. **上下文管理是工程核心，不是事后补丁**
6. **简单模型 + 好工具 > 强模型 + 烂工具**
7. **工具结果完整喂 LLM，截断展示给用户** — 两个受众，两种策略
8. **原生 tool_use 替代文本解析** — 确定性 > 灵活性
