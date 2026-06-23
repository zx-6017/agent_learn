#!/usr/bin/env python3
"""入口 — 基于原生 tool_use 的 ReAct Agent

三种运行模式:
    python main.py                          # 交互模式
    python main.py "帮我算一下 123 * 456"    # 单次任务
    python main.py --tools                  # 列出所有可用工具
    python main.py --memory                 # 查看 memory 内容

架构要点:
    - LLM 和 Agent 之间通过 JSON tool_calls 通信（不是文本解析）
    - 工具执行结果以 tool role 消息形式返回给 LLM
    - 终端输出是框架渲染的（彩色、截断、格式化），LLM 只管意图
    - Memory 系统：SQLite 持久化，LLM 自主决定写入，frozen snapshot 模式
"""

import sys
import os
import yaml

from agent.llm import LLMClient
from agent.memory import MemoryStore
from agent.tools import create_default_registry
from agent.agent import Agent

# 配置文件路径 — 和 main.py 同目录
CONFIG_PATH = os.path.join(os.path.dirname(__file__), "config.yaml")


def load_config() -> dict:
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def list_tools(registry):
    """列出所有可用工具及其 schema"""
    print("=" * 60)
    print("  可用工具列表")
    print("=" * 60)
    for schema in registry.get_schemas():
        func = schema["function"]
        name = func["name"]
        desc = func["description"]
        params = func.get("parameters", {}).get("properties", {})

        print(f"\n  📦 {name}")
        # 描述只显示第一行
        first_line = desc.split("\n")[0].strip()
        print(f"     {first_line}")
        if params:
            print(f"     参数: {', '.join(params.keys())}")
    print()


def show_memory(store: MemoryStore):
    """展示当前 memory 快照"""
    print("=" * 60)
    print("  Memory 快照")
    print("=" * 60)

    for target in ("user", "memory"):
        entries = store._snapshot.get(target, [])
        limit = store.memory_limit if target == "memory" else store.user_limit
        joined = "\n§\n".join(entries)
        current = len(joined)

        status = f"[{len(entries)} 条, {current:,}/{limit:,} chars]"
        tag = "👤 USER PROFILE" if target == "user" else "🧠 MEMORY"
        print(f"\n{tag} {status}")

        if not entries:
            print("  (空)")
        else:
            for i, entry in enumerate(entries, 1):
                # 截断显示
                display = entry[:120] + ("..." if len(entry) > 120 else "")
                print(f"  {i}. {display}")
    print()


def main():
    config = load_config()

    # ── 初始化 LLM 客户端 ──
    try:
        llm = LLMClient.from_config(config["openai"])
    except Exception as e:
        print(f"❌ LLM 初始化失败: {e}")
        print("\n请检查 config.yaml 中的 API Key 和 Base URL 配置。")
        print("可以用环境变量: export OPENAI_API_KEY=sk-xxx")
        sys.exit(1)

    # ── 初始化 Memory ──
    mem_cfg = config.get("memory", {})
    memory_store = MemoryStore(
        db_path=mem_cfg.get("db_path", os.path.join(os.path.dirname(__file__), "memory.db")),
        memory_char_limit=mem_cfg.get("memory_limit", 2200),
        user_char_limit=mem_cfg.get("user_limit", 1375),
    )
    memory_store.load()  # 加载现有数据作为 frozen snapshot

    # ── 初始化工具（传入 memory_store）──
    workdir = os.getcwd()
    tools = create_default_registry(workdir=workdir, memory_store=memory_store)

    # ── --memory 模式 ──
    if len(sys.argv) > 1 and sys.argv[1] == "--memory":
        show_memory(memory_store)
        return

    # ── --tools 模式 ──
    if len(sys.argv) > 1 and sys.argv[1] == "--tools":
        list_tools(tools)
        return

    # ── 初始化 Agent ──
    agent = Agent(
        llm=llm,
        tools=tools,
        memory=memory_store,
        max_steps=config["agent"]["max_steps"],
        verbose=config["agent"]["verbose"],
        workdir=config["agent"].get("workdir", workdir),
    )

    print("=" * 60)
    print("  AI Agent 学习项目 — 基于原生 tool_use 的 ReAct Agent")
    print(f"  模型: {config['openai']['model']}")
    print(f"  工具: {', '.join(tools.list_names())}")
    print(f"  Memory: {'启用' if memory_store else '禁用'} "
          f"(memory: {len(memory_store._snapshot.get('memory', []))}条, "
          f"user: {len(memory_store._snapshot.get('user', []))}条)")
    print(f"  最大步数: {config['agent']['max_steps']}")
    print("=" * 60)

    # ── 单次任务模式 ──
    if len(sys.argv) > 1:
        task = " ".join(sys.argv[1:])
        print(f"\n📝 任务: {task}")
        result = agent.run(task)
        print(f"\n{'─'*60}")
        print(f"最终答案:\n{result}")
        return

    # ── 交互模式 ──
    print("\n输入任务（输入 quit 退出，/tools 查看工具，/memory 查看记忆）:\n")
    while True:
        try:
            user_input = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n👋 再见！")
            break

        if not user_input:
            continue
        if user_input.lower() in ("quit", "exit", "q"):
            print("👋 再见！")
            break
        if user_input == "/tools":
            list_tools(tools)
            continue
        if user_input == "/memory":
            show_memory(memory_store)
            continue

        result = agent.run(user_input)
        print(f"\n{'─'*60}")
        print(f"最终答案:\n{result}")


if __name__ == "__main__":
    main()
