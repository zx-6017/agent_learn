"""Memory 系统 — 持久化记忆（Hermes 设计风格 + SQLite 后端）

设计理念（直接借鉴 Hermes Agent）：
  - LLM 全权决定"写什么、何时写、何时更新"——程序不做语义判断
  - 程序只做基础设施：存储、字符上限、去重、子串匹配
  - 两个独立存储：memory（agent 笔记）和 user（用户画像）
  - Frozen snapshot 模式：会话启动时快照注入 system prompt，会话中写入不刷新

和 Hermes 的区别：
  - Hermes 用 Markdown 文件（§ 分割），我们用 SQLite
  - SQLite 天然支持原子写入、并发安全，不需要 flock
  - 保留了 id 主键，方便未来扩展（向量索引、时间排序等）

使用方式：
    store = MemoryStore("memory.db")
    store.load()                    # 会话开始时调用
    store.to_prompt("memory")       # 注入 system prompt

    # 以下由 LLM 通过 tool_use 调用（程序不做判断）：
    store.add("memory", "...")      # LLM 决定新增
    store.replace("memory", "旧文本", "新文本")  # LLM 决定更新
    store.remove("memory", "待删文本")         # LLM 决定删除
"""

from __future__ import annotations

import sqlite3
import os
from typing import Any


# ═══════════════════════════════════════════════════════════════
# MemoryStore — 核心存储引擎
# ═══════════════════════════════════════════════════════════════

class MemoryStore:
    """持久化记忆存储，两个独立 target：memory 和 user。

    字符上限（和 Hermes 一致）：
      - memory: 2200 字符（agent 笔记）
      - user:   1375 字符（用户画像，通常更少）

    Frozen snapshot 模式：
      - load() 在会话开始时调用，把当前 DB 内容读入 _snapshot
      - to_prompt() 返回 snapshot 内容（会话内不变，保证 prefix cache 稳定）
      - add/replace/remove 直接写 DB，不刷新 snapshot
      - 下次会话 load() 时会看到最新状态
    """

    def __init__(
        self,
        db_path: str = "memory.db",
        memory_char_limit: int = 2200,
        user_char_limit: int = 1375,
    ):
        self.db_path = db_path
        self.memory_limit = memory_char_limit
        self.user_limit = user_char_limit

        # Snapshot — 会话启动时冻结
        self._snapshot: dict[str, list[str]] = {"memory": [], "user": []}

        self._init_db()

    # ── 初始化 ──────────────────────────────────────────

    def _init_db(self):
        """创建表结构"""
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS memory_entries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    target TEXT NOT NULL CHECK(target IN ('memory','user')),
                    content TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            # 加速子串匹配查询
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_target ON memory_entries(target)
            """)

    # ── 会话生命周期 ────────────────────────────────────

    def load(self):
        """会话开始时调用：从 DB 加载当前状态作为 frozen snapshot。

        这个 snapshot 用于 to_prompt() 注入 system prompt，
        会话内不会改变（保持 prefix cache 稳定）。
        """
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.row_factory = sqlite3.Row
                for target in ("memory", "user"):
                    rows = conn.execute(
                        "SELECT content FROM memory_entries WHERE target = ? "
                        "ORDER BY created_at",
                        (target,),
                    ).fetchall()
                    self._snapshot[target] = [r["content"] for r in rows]
        except sqlite3.Error:
            # DB 损坏或不可读，保持空快照
            self._snapshot = {"memory": [], "user": []}

    def to_prompt(self, target: str = "memory") -> str:
        """将 frozen snapshot 格式化为 system prompt 注入文本。

        格式化和 Hermes 一致：
          ═══════════════
          MEMORY (your personal notes) [35% — 778/2,200 chars]
          ═══════════════
          条目1
          §
          条目2

        Returns:
            格式化的 prompt 文本，如无条目返回空字符串
        """
        entries = self._snapshot.get(target, [])
        if not entries:
            return ""

        limit = self.memory_limit if target == "memory" else self.user_limit
        joined = "\n§\n".join(entries)
        current = len(joined)
        pct = min(100, int((current / limit) * 100)) if limit > 0 else 0

        if target == "memory":
            header = f"MEMORY (your personal notes) [{pct}% — {current:,}/{limit:,} chars]"
        else:
            header = f"USER PROFILE (who the user is) [{pct}% — {current:,}/{limit:,} chars]"

        separator = "═" * 46
        return f"{separator}\n{header}\n{separator}\n{joined}"

    def to_prompt_all(self) -> str:
        """返回所有 target 的 prompt 文本，用双换行分隔"""
        parts = []
        for target in ("memory", "user"):
            text = self.to_prompt(target)
            if text:
                parts.append(text)
        return "\n\n".join(parts)

    # ── CRUD 操作（LLM 通过 tool_use 调用）─────────────

    def add(self, target: str, content: str) -> dict[str, Any]:
        """添加新条目。

        Returns:
            {"ok": bool, "message": str, "usage": str, "entries": [...]}
        """
        content = content.strip()
        if not content:
            return self._err("内容不能为空")

        # 安全检查：内容不能是危险指令
        scan_err = self._scan_content(content)
        if scan_err:
            return self._err(scan_err)

        limit = self._limit_for(target)

        try:
            with sqlite3.connect(self.db_path) as conn:
                # 检查去重
                existing = self._find_by_substring(conn, target, content)
                if any(e == content for e in existing):
                    return self._ok(
                        target, conn, "条目已存在（未重复添加）"
                    )

                # 检查字符上限
                current_total = self._char_count(conn, target)
                new_total = current_total + len(content) + 2  # +2 预留给分隔符
                if new_total > limit:
                    return self._err(
                        f"Memory 已使用 {current_total:,}/{limit:,} 字符。"
                        f"添加此条目（{len(content)} 字符）将超过上限。"
                        f"请先用 replace 合并或 remove 删除旧条目。"
                    )

                conn.execute(
                    "INSERT INTO memory_entries (target, content) VALUES (?, ?)",
                    (target, content),
                )

            return self._ok(target, None, "条目已添加")

        except sqlite3.Error as e:
            return self._err(f"数据库错误: {e}")

    def replace(self, target: str, old_text: str, new_content: str) -> dict[str, Any]:
        """更新已有条目：通过 old_text 子串匹配找到目标，替换为 new_content。

        Args:
            target: "memory" 或 "user"
            old_text: 用于匹配的旧文本子串（只需唯一匹配）
            new_content: 替换后的新内容

        Returns:
            {"ok": bool, "message": str, "matches": [...]|None, "usage": str}
        """
        old_text = old_text.strip()
        new_content = new_content.strip()

        if not old_text:
            return self._err("old_text 不能为空")
        if not new_content:
            return self._err("new_content 不能为空，删除请用 remove")

        scan_err = self._scan_content(new_content)
        if scan_err:
            return self._err(scan_err)

        limit = self._limit_for(target)

        try:
            with sqlite3.connect(self.db_path) as conn:
                matches = self._find_by_substring(conn, target, old_text)

                if not matches:
                    return self._err(f"没有找到匹配 '{old_text}' 的条目")

                if len(matches) > 1:
                    # 检查是否完全相同的重复条目
                    unique = list(set(matches))
                    if len(unique) > 1:
                        previews = [
                            m[:80] + ("..." if len(m) > 80 else "") for m in unique
                        ]
                        return {
                            "ok": False,
                            "message": f"'{old_text}' 匹配到 {len(unique)} 个不同条目，请更精确",
                            "matches": previews,
                            "usage": self._usage_string(conn, target),
                        }
                    # 完全相同 → 只更新第一条

                # 检查替换后是否超限
                old_entry = matches[0]
                current_total = self._char_count(conn, target)
                new_total = current_total - len(old_entry) + len(new_content)
                if new_total > limit:
                    return self._err(
                        f"替换后占用 {new_total:,}/{limit:,} 字符，超过上限。"
                        f"请精简新内容或先删除其他条目。"
                    )

                # 先删除所有匹配的重复条目，再插入新的
                # 这样可以处理重复条目的情况
                conn.execute(
                    "DELETE FROM memory_entries WHERE target = ? AND content = ?",
                    (target, old_entry),
                )
                conn.execute(
                    "INSERT INTO memory_entries (target, content) VALUES (?, ?)",
                    (target, new_content),
                )

            return self._ok(target, None, "条目已替换")

        except sqlite3.Error as e:
            return self._err(f"数据库错误: {e}")

    def remove(self, target: str, old_text: str) -> dict[str, Any]:
        """删除条目：通过 old_text 子串匹配找到目标，删除。

        Args:
            target: "memory" 或 "user"
            old_text: 用于匹配的文本子串

        Returns:
            {"ok": bool, "message": str, "usage": str}
        """
        old_text = old_text.strip()
        if not old_text:
            return self._err("old_text 不能为空")

        try:
            with sqlite3.connect(self.db_path) as conn:
                matches = self._find_by_substring(conn, target, old_text)

                if not matches:
                    return self._err(f"没有找到匹配 '{old_text}' 的条目")

                if len(matches) > 1:
                    unique = list(set(matches))
                    if len(unique) > 1:
                        previews = [
                            m[:80] + ("..." if len(m) > 80 else "") for m in unique
                        ]
                        return {
                            "ok": False,
                            "message": f"'{old_text}' 匹配到 {len(unique)} 个不同条目，请更精确",
                            "matches": previews,
                            "usage": self._usage_string(conn, target),
                        }

                # 删除匹配条目
                conn.execute(
                    "DELETE FROM memory_entries WHERE target = ? AND content = ?",
                    (target, matches[0]),
                )

            return self._ok(target, None, "条目已删除")

        except sqlite3.Error as e:
            return self._err(f"数据库错误: {e}")

    def read(self, target: str = "memory") -> dict[str, Any]:
        """读取当前所有条目（供 LLM 查看当前 memory 状态）"""
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.row_factory = sqlite3.Row
                rows = conn.execute(
                    "SELECT id, content, created_at FROM memory_entries "
                    "WHERE target = ? ORDER BY created_at",
                    (target,),
                ).fetchall()

                entries = [r["content"] for r in rows]
                return {
                    "ok": True,
                    "target": target,
                    "entries": entries,
                    "count": len(entries),
                    "usage": self._usage_string(None, target),
                }
        except sqlite3.Error as e:
            return self._err(f"数据库错误: {e}")

    # ── 内部方法 ──────────────────────────────────────────

    def _limit_for(self, target: str) -> int:
        return self.memory_limit if target == "memory" else self.user_limit

    @staticmethod
    def _find_by_substring(conn, target: str, text: str) -> list[str]:
        """通过子串匹配找到所有匹配的条目内容。

        使用 SQL LIKE 实现子串匹配（简单但足够）。
        返回匹配条目的 content 列表。
        """
        rows = conn.execute(
            "SELECT content FROM memory_entries WHERE target = ? AND content LIKE ?",
            (target, f"%{text}%"),
        ).fetchall()
        return [r[0] for r in rows]

    def _char_count(self, conn, target: str) -> int:
        """计算当前 target 的总字符数（含分隔符）"""
        rows = conn.execute(
            "SELECT content FROM memory_entries WHERE target = ?", (target,)
        ).fetchall()
        if not rows:
            return 0
        # 和 to_prompt 一样的计算方式：§ 分隔
        joined = "\n§\n".join(r[0] for r in rows)
        return len(joined)

    def _usage_string(self, conn_or_none, target: str) -> str:
        """生成用量字符串，如 '35% — 778/2,200 chars'"""
        if conn_or_none is None:
            try:
                with sqlite3.connect(self.db_path) as c:
                    current = self._char_count(c, target)
            except sqlite3.Error:
                current = 0
        else:
            current = self._char_count(conn_or_none, target)

        limit = self._limit_for(target)
        pct = min(100, int((current / limit) * 100)) if limit > 0 else 0
        return f"{pct}% — {current:,}/{limit:,} chars"

    def _ok(self, target: str, conn, message: str) -> dict[str, Any]:
        """构建成功响应"""
        return {
            "ok": True,
            "message": message,
            "usage": self._usage_string(conn, target),
        }

    @staticmethod
    def _err(message: str) -> dict[str, Any]:
        """构建错误响应"""
        return {"ok": False, "message": message}

    @staticmethod
    def _scan_content(content: str) -> str | None:
        """安全检查：拒绝明显的 prompt injection。

        简化版（完整版需要 AST 分析）：
        - 拒绝包含"忽略之前指令"、"ignore previous"等典型注入模式
        - 拒绝超长内容（>5000 字符）
        """
        if len(content) > 5000:
            return f"内容过长（{len(content)} 字符），最多 5000 字符"

        lower = content.lower()
        blocked_patterns = [
            "忽略之前",
            "ignore previous",
            "ignore all",
            "你是",
            "you are now",
            "from now on you are",
            "system prompt",
            "<system>",
            "</system>",
        ]
        for pattern in blocked_patterns:
            if pattern in lower:
                return f"内容包含疑似注入模式: '{pattern}'"

        return None  # 通过检查
