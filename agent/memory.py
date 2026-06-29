"""Memory 系统 — 分层持久化记忆（Claude Code 风格 + SQLite 后端）

设计理念：
  - LLM 全权决定"写什么、何时写、何时更新"——程序不做语义判断
  - 程序只做基础设施：存储、字符上限、去重、子串匹配
  - 两个 target：memory（agent/项目笔记）和 user（用户画像）
  - 三个 scope：user（跨项目个人偏好）、project（当前项目）、local（本机私有）
  - topic 用来把细节拆到主题层，避免所有记忆都挤进启动 prompt
  - pinned=True 的条目组成启动索引；pinned=False 的细节通过 read/search 按需读取
  - Frozen snapshot 模式：会话启动时只把 pinned 索引注入 system prompt

和 Claude Code 的对应关系：
  - CLAUDE.md / rules      -> scope + pinned 指令索引
  - MEMORY.md 入口索引      -> pinned=True 的简短条目
  - topic markdown files   -> topic + pinned=False 的详细条目
  - /memory 审计和编辑      -> read/search + CLI 展示

使用方式：
    store = MemoryStore("memory.db")
    store.load()                    # 会话开始时调用
    store.to_prompt("memory")       # 只注入 pinned 索引

    # 以下由 LLM 通过 tool_use 调用（程序不做判断）：
    store.add("memory", "...", topic="testing", pinned=False)
    store.replace("memory", "旧文本", "新文本")  # LLM 决定更新
    store.remove("memory", "待删文本")         # LLM 决定删除
"""

from __future__ import annotations

import sqlite3
import re
from typing import Any, TypedDict


class MemoryEntry(TypedDict):
    """一条记忆条目的完整结构，对应 memory_entries 表的一行。"""
    id: int
    target: str
    scope: str
    topic: str
    pinned: bool
    content: str
    created_at: str | None
    updated_at: str | None


class MemoryResult(TypedDict, total=False):
    """CRUD 操作的统一返回结构。

    ok 和 message 始终存在；其余字段按操作类型选填。
    """
    ok: bool
    message: str
    usage: str
    entries: list[MemoryEntry]
    count: int
    matches: list[str]
    query: str
    target: str
    scope: str
    topic: str
    pinned: bool | str


# ═══════════════════════════════════════════════════════════════
# MemoryStore — 核心存储引擎
# ═══════════════════════════════════════════════════════════════

class MemoryStore:
    """持久化记忆存储。

    字符上限约束只作用于 pinned 启动索引：
      - memory: 2200 字符（agent/项目笔记索引）
      - user:   1375 字符（用户画像索引，通常更少）

    Frozen snapshot 模式：
      - load() 在会话开始时调用，把当前 DB 的 pinned 条目读入 _snapshot
      - to_prompt() 返回 snapshot 内容（会话内不变，保证 prefix cache 稳定）
      - add/replace/remove 直接写 DB，不刷新 snapshot
      - 下次会话 load() 时会看到最新状态
    """

    VALID_TARGETS = {"memory", "user"}
    VALID_SCOPES = {"user", "project", "local"}
    DEFAULT_TOPIC = "general"

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
        self._snapshot: dict[str, list[MemoryEntry]] = {"memory": [], "user": []}

        self._init_db()

    # ── 初始化 ──────────────────────────────────────────

    def _init_db(self) -> None:
        """创建或迁移表结构。"""
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS memory_entries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    target TEXT NOT NULL CHECK(target IN ('memory','user')),
                    scope TEXT NOT NULL DEFAULT 'project',
                    topic TEXT NOT NULL DEFAULT 'general',
                    pinned INTEGER NOT NULL DEFAULT 1,
                    content TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            self._migrate_db(conn)
            # 加速子串匹配查询
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_target ON memory_entries(target)
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_scope_topic
                ON memory_entries(scope, topic)
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_pinned
                ON memory_entries(pinned)
            """)

    @staticmethod
    def _migrate_db(conn: sqlite3.Connection) -> None:
        """给旧版 memory_entries 表补充分层字段。

        SQLite 的 ALTER TABLE 能安全添加带默认值的新列；旧数据会默认进入
        project/general/pinned 层，从而保持原先"全部注入 prompt"的行为。
        """
        rows = conn.execute("PRAGMA table_info(memory_entries)").fetchall()
        columns = {row[1] for row in rows}
        migrations = [
            ("scope", "ALTER TABLE memory_entries ADD COLUMN scope TEXT NOT NULL DEFAULT 'project'"),
            ("topic", "ALTER TABLE memory_entries ADD COLUMN topic TEXT NOT NULL DEFAULT 'general'"),
            ("pinned", "ALTER TABLE memory_entries ADD COLUMN pinned INTEGER NOT NULL DEFAULT 1"),
            ("updated_at", "ALTER TABLE memory_entries ADD COLUMN updated_at TIMESTAMP"),
        ]
        for column, statement in migrations:
            if column not in columns:
                conn.execute(statement)
        conn.execute("UPDATE memory_entries SET scope = 'project' WHERE scope IS NULL OR scope = ''")
        conn.execute("UPDATE memory_entries SET topic = 'general' WHERE topic IS NULL OR topic = ''")
        conn.execute("UPDATE memory_entries SET pinned = 1 WHERE pinned IS NULL")
        conn.execute("UPDATE memory_entries SET updated_at = COALESCE(created_at, CURRENT_TIMESTAMP) WHERE updated_at IS NULL")

    # ── 会话生命周期 ────────────────────────────────────

    def load(self) -> None:
        """会话开始时调用：从 DB 加载当前状态作为 frozen snapshot。

        这个 snapshot 用于 to_prompt() 注入 system prompt，
        会话内不会改变（保持 prefix cache 稳定）。
        """
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.row_factory = sqlite3.Row
                for target in ("memory", "user"):
                    rows = conn.execute(
                        "SELECT id, target, scope, topic, pinned, content, created_at, updated_at "
                        "FROM memory_entries WHERE target = ? AND pinned = 1 "
                        "ORDER BY scope, topic, created_at",
                        (target,),
                    ).fetchall()
                    self._snapshot[target] = [self._row_to_dict(r) for r in rows]
        except sqlite3.Error:
            # DB 损坏或不可读，保持空快照
            self._snapshot = {"memory": [], "user": []}

    def to_prompt(self, target: str = "memory") -> str:
        """将 frozen snapshot 格式化为 system prompt 注入文本。

        格式化为索引：
          ═══════════════
          MEMORY INDEX (pinned, layered) [35% — 778/2,200 chars]
          ═══════════════
          [project/testing]
          - 条目1

        Returns:
            格式化的 prompt 文本，如无条目返回空字符串
        """
        entries = self._snapshot.get(target, [])
        if not entries:
            return ""

        limit = self.memory_limit if target == "memory" else self.user_limit
        joined = self._join_contents(entries)
        current = len(joined)
        pct = min(100, int((current / limit) * 100)) if limit > 0 else 0

        if target == "memory":
            header = f"MEMORY INDEX (pinned project notes) [{pct}% — {current:,}/{limit:,} chars]"
        else:
            header = f"USER PROFILE INDEX (pinned preferences) [{pct}% — {current:,}/{limit:,} chars]"

        separator = "═" * 46
        return f"{separator}\n{header}\n{separator}\n{self._format_prompt_entries(entries)}"

    def to_prompt_all(self) -> str:
        """返回所有 target 的 prompt 文本，用双换行分隔"""
        parts = [
            (
                "长期记忆按 scope/topic 分层保存。下面只注入 pinned 索引；"
                "如果需要更详细的主题记忆，调用 memory(action='search' 或 'read') 按需检索。"
            )
        ]
        for target in ("memory", "user"):
            text = self.to_prompt(target)
            if text:
                parts.append(text)
        return "\n\n".join(parts) if len(parts) > 1 else ""

    # ── CRUD 操作（LLM 通过 tool_use 调用）─────────────

    def add(
        self,
        target: str,
        content: str,
        scope: str | None = None,
        topic: str = DEFAULT_TOPIC,
        pinned: bool = True,
    ) -> MemoryResult:
        """添加新条目。

        Returns:
            {"ok": bool, "message": str, "usage": str, "entries": [...]}
        """
        target = self._normalize_target(target)
        scope = self._normalize_scope(scope, target)
        topic = self._normalize_topic(topic)
        pinned = bool(pinned)
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
                existing = self._find_entries_by_substring(conn, target, content)
                if any(e["content"] == content for e in existing):
                    return self._ok(
                        target, conn, "条目已存在（未重复添加）"
                    )

                replace_user_name = target == "user" and self._is_user_name_entry(content)
                current_total = self._char_count(conn, target, pinned_only=True)
                new_total = current_total + len(content) + 2  # +2 预留给分隔符

                # 只限制启动索引；非 pinned 细节不占启动 prompt 预算
                if pinned and new_total > limit:
                    return self._err(
                        f"Memory 已使用 {current_total:,}/{limit:,} 字符。"
                        f"添加此条目（{len(content)} 字符）将超过上限。"
                        f"请先用 replace 合并或 remove 删除旧条目。"
                    )

                if replace_user_name:
                    self._delete_user_name_entries(conn)

                conn.execute(
                    """
                    INSERT INTO memory_entries
                    (target, scope, topic, pinned, content, updated_at)
                    VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                    """,
                    (target, scope, topic, int(pinned), content),
                )

            message = "用户姓名条目已替换" if replace_user_name else "条目已添加"
            if not pinned:
                message += "（主题细节，不会默认注入启动 prompt）"
            return self._ok(target, None, message)

        except sqlite3.Error as e:
            return self._err(f"数据库错误: {e}")

    def replace(
        self,
        target: str,
        old_text: str,
        new_content: str,
        scope: str | None = None,
        topic: str | None = None,
        pinned: bool | None = None,
    ) -> MemoryResult:
        """更新已有条目：通过 old_text 子串匹配找到目标，替换为 new_content。

        Args:
            target: "memory" 或 "user"
            old_text: 用于匹配的旧文本子串（只需唯一匹配）
            new_content: 替换后的新内容
            scope/topic/pinned: 可选；不传则沿用旧条目的分层信息

        Returns:
            {"ok": bool, "message": str, "matches": [...]|None, "usage": str}
        """
        target = self._normalize_target(target)
        scope_filter = self._normalize_optional_scope(scope)
        topic_filter = self._normalize_optional_topic(topic)
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
                matches = self._find_entries_by_substring(
                    conn,
                    target=target,
                    text=old_text,
                    scope=scope_filter,
                    topic=topic_filter,
                )

                if not matches:
                    return self._err(f"没有找到匹配 '{old_text}' 的条目")

                if len(matches) > 1:
                    previews = [self._preview_match(m) for m in matches[:8]]
                    return MemoryResult(
                        ok=False,
                        message=f"'{old_text}' 匹配到 {len(matches)} 个条目，请用更精确的 old_text/scope/topic",
                        matches=previews,
                        usage=self._usage_string(conn, target),
                    )

                # 检查替换后是否超限
                old_entry = matches[0]
                new_scope = self._normalize_scope(scope, target) if scope else old_entry["scope"]
                new_topic = self._normalize_topic(topic) if topic else old_entry["topic"]
                new_pinned = bool(pinned) if pinned is not None else bool(old_entry["pinned"])

                current_total = self._char_count(conn, target, pinned_only=True)
                old_prompt_len = len(old_entry["content"]) if old_entry["pinned"] else 0
                new_prompt_len = len(new_content) if new_pinned else 0
                new_total = current_total - old_prompt_len + new_prompt_len
                if new_pinned and new_total > limit:
                    return self._err(
                        f"替换后占用 {new_total:,}/{limit:,} 字符，超过上限。"
                        f"请精简新内容或先删除其他条目。"
                    )

                conn.execute(
                    """
                    UPDATE memory_entries
                    SET scope = ?, topic = ?, pinned = ?, content = ?,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                    """,
                    (new_scope, new_topic, int(new_pinned), new_content, old_entry["id"]),
                )

            return self._ok(target, None, "条目已替换")

        except sqlite3.Error as e:
            return self._err(f"数据库错误: {e}")

    def remove(
        self,
        target: str,
        old_text: str,
        scope: str | None = None,
        topic: str | None = None,
    ) -> MemoryResult:
        """删除条目：通过 old_text 子串匹配找到目标，删除。

        Args:
            target: "memory" 或 "user"
            old_text: 用于匹配的文本子串

        Returns:
            {"ok": bool, "message": str, "usage": str}
        """
        target = self._normalize_target(target)
        scope_filter = self._normalize_optional_scope(scope)
        topic_filter = self._normalize_optional_topic(topic)
        old_text = old_text.strip()
        if not old_text:
            return self._err("old_text 不能为空")

        try:
            with sqlite3.connect(self.db_path) as conn:
                matches = self._find_entries_by_substring(
                    conn,
                    target=target,
                    text=old_text,
                    scope=scope_filter,
                    topic=topic_filter,
                )

                if not matches:
                    return self._err(f"没有找到匹配 '{old_text}' 的条目")

                if len(matches) > 1:
                    previews = [self._preview_match(m) for m in matches[:8]]
                    return MemoryResult(
                        ok=False,
                        message=f"'{old_text}' 匹配到 {len(matches)} 个条目，请用更精确的 old_text/scope/topic",
                        matches=previews,
                        usage=self._usage_string(conn, target),
                    )

                # 删除匹配条目
                conn.execute(
                    "DELETE FROM memory_entries WHERE id = ?",
                    (matches[0]["id"],),
                )

            return self._ok(target, None, "条目已删除")

        except sqlite3.Error as e:
            return self._err(f"数据库错误: {e}")

    def read(
        self,
        target: str = "memory",
        scope: str | None = None,
        topic: str | None = None,
        pinned: bool | None = None,
        limit: int = 50,
    ) -> MemoryResult:
        """读取当前条目（供 LLM 查看当前 memory 状态）。"""
        target = self._normalize_target(target)
        scope_filter = self._normalize_optional_scope(scope)
        topic_filter = self._normalize_optional_topic(topic)
        limit = max(1, min(int(limit), 200))
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.row_factory = sqlite3.Row
                where = ["target = ?"]
                params: list[Any] = [target]
                if scope_filter:
                    where.append("scope = ?")
                    params.append(scope_filter)
                if topic_filter:
                    where.append("topic = ?")
                    params.append(topic_filter)
                if pinned is not None:
                    where.append("pinned = ?")
                    params.append(int(bool(pinned)))
                params.append(limit)
                rows = conn.execute(
                    f"""
                    SELECT id, target, scope, topic, pinned, content, created_at, updated_at
                    FROM memory_entries
                    WHERE {' AND '.join(where)}
                    ORDER BY scope, topic, pinned DESC, updated_at DESC, created_at DESC
                    LIMIT ?
                    """,
                    params,
                ).fetchall()

                entries = [self._row_to_dict(r) for r in rows]
                return MemoryResult(
                    ok=True,
                    target=target,
                    scope=scope_filter or "all",
                    topic=topic_filter or "all",
                    pinned=pinned if pinned is not None else "all",
                    entries=entries,
                    count=len(entries),
                    usage=self._usage_string(None, target),
                )
        except sqlite3.Error as e:
            return self._err(f"数据库错误: {e}")

    def search(
        self,
        query: str,
        target: str | None = None,
        scope: str | None = None,
        topic: str | None = None,
        limit: int = 10,
    ) -> MemoryResult:
        """按关键词搜索分层记忆。"""
        query = query.strip()
        if not query:
            return self._err("query 不能为空")

        target_filter = self._normalize_optional_target(target)
        scope_filter = self._normalize_optional_scope(scope)
        topic_filter = self._normalize_optional_topic(topic)
        limit = max(1, min(int(limit), 50))

        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.row_factory = sqlite3.Row
                where = ["(content LIKE ? OR topic LIKE ?)"]
                params: list[Any] = [f"%{query}%", f"%{query}%"]
                if target_filter:
                    where.append("target = ?")
                    params.append(target_filter)
                if scope_filter:
                    where.append("scope = ?")
                    params.append(scope_filter)
                if topic_filter:
                    where.append("topic = ?")
                    params.append(topic_filter)
                params.append(limit)
                rows = conn.execute(
                    f"""
                    SELECT id, target, scope, topic, pinned, content, created_at, updated_at
                    FROM memory_entries
                    WHERE {' AND '.join(where)}
                    ORDER BY pinned DESC, updated_at DESC, created_at DESC
                    LIMIT ?
                    """,
                    params,
                ).fetchall()
                return MemoryResult(
                    ok=True,
                    query=query,
                    entries=[self._row_to_dict(r) for r in rows],
                    count=len(rows),
                )
        except sqlite3.Error as e:
            return self._err(f"数据库错误: {e}")

    # ── 内部方法 ──────────────────────────────────────────

    def _normalize_target(self, target: str) -> str:
        target = (target or "").strip()
        if target not in self.VALID_TARGETS:
            raise ValueError(f"target 必须是 memory 或 user，收到: {target}")
        return target

    def _normalize_optional_target(self, target: str | None) -> str | None:
        if target is None or str(target).strip() == "":
            return None
        return self._normalize_target(str(target))

    def _normalize_scope(self, scope: str | None, target: str) -> str:
        if scope is None or str(scope).strip() == "":
            return "user" if target == "user" else "project"
        scope = str(scope).strip()
        if scope not in self.VALID_SCOPES:
            raise ValueError(f"scope 必须是 user、project 或 local，收到: {scope}")
        return scope

    def _normalize_optional_scope(self, scope: str | None) -> str | None:
        if scope is None or str(scope).strip() == "":
            return None
        scope = str(scope).strip()
        if scope not in self.VALID_SCOPES:
            raise ValueError(f"scope 必须是 user、project 或 local，收到: {scope}")
        return scope

    def _normalize_topic(self, topic: str | None) -> str:
        topic = (topic or self.DEFAULT_TOPIC).strip().lower()
        topic = re.sub(r"[^\w.-]+", "-", topic).strip("-._")
        return topic[:64] or self.DEFAULT_TOPIC

    def _normalize_optional_topic(self, topic: str | None) -> str | None:
        if topic is None or str(topic).strip() == "":
            return None
        return self._normalize_topic(topic)

    def _limit_for(self, target: str) -> int:
        return self.memory_limit if target == "memory" else self.user_limit

    @staticmethod
    def _is_user_name_entry(content: str) -> bool:
        """识别用户姓名这类唯一画像事实。"""
        return bool(re.match(r"^用户(?:名|的名字)?(?:叫|是)\s*.+。?$", content.strip()))

    @staticmethod
    def _delete_user_name_entries(conn: sqlite3.Connection) -> None:
        """删除已有用户姓名条目，避免同一唯一事实互相冲突。"""
        rows = conn.execute(
            "SELECT content FROM memory_entries WHERE target = 'user'"
        ).fetchall()
        for (content,) in rows:
            if MemoryStore._is_user_name_entry(content):
                conn.execute(
                    "DELETE FROM memory_entries WHERE target = 'user' AND content = ?",
                    (content,),
                )

    def _find_entries_by_substring(
        self,
        conn: sqlite3.Connection,
        target: str,
        text: str,
        scope: str | None = None,
        topic: str | None = None,
    ) -> list[MemoryEntry]:
        """通过子串匹配找到所有匹配的条目。

        使用 SQL LIKE 实现子串匹配（简单但足够）。
        """
        conn.row_factory = sqlite3.Row
        where = ["target = ?", "content LIKE ?"]
        params: list[Any] = [target, f"%{text}%"]
        if scope:
            where.append("scope = ?")
            params.append(scope)
        if topic:
            where.append("topic = ?")
            params.append(topic)
        rows = conn.execute(
            f"""
            SELECT id, target, scope, topic, pinned, content, created_at, updated_at
            FROM memory_entries
            WHERE {' AND '.join(where)}
            ORDER BY scope, topic, created_at
            """,
            params,
        ).fetchall()
        return [self._row_to_dict(r) for r in rows]

    def _char_count(self, conn: sqlite3.Connection, target: str, pinned_only: bool = True) -> int:
        """计算当前 target 的字符数（默认只算 pinned 启动索引）。"""
        if pinned_only:
            rows = conn.execute(
                "SELECT content FROM memory_entries WHERE target = ? AND pinned = 1",
                (target,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT content FROM memory_entries WHERE target = ?", (target,)
            ).fetchall()
        if not rows:
            return 0
        # 和 to_prompt 一样的计算方式：§ 分隔
        joined = "\n§\n".join(r[0] for r in rows)
        return len(joined)

    def _usage_string(self, conn_or_none: sqlite3.Connection | None, target: str) -> str:
        """生成用量字符串，如 '35% — 778/2,200 chars'"""
        if conn_or_none is None:
            try:
                with sqlite3.connect(self.db_path) as c:
                    current = self._char_count(c, target, pinned_only=True)
            except sqlite3.Error:
                current = 0
        else:
            current = self._char_count(conn_or_none, target, pinned_only=True)

        limit = self._limit_for(target)
        pct = min(100, int((current / limit) * 100)) if limit > 0 else 0
        return f"{pct}% — {current:,}/{limit:,} pinned chars"

    def _ok(self, target: str, conn: sqlite3.Connection | None, message: str) -> MemoryResult:
        """构建成功响应"""
        return MemoryResult(
            ok=True,
            message=message,
            usage=self._usage_string(conn, target),
        )

    @staticmethod
    def _err(message: str) -> MemoryResult:
        """构建错误响应"""
        return MemoryResult(ok=False, message=message)

    @staticmethod
    def _row_to_dict(row: sqlite3.Row) -> MemoryEntry:
        """把 sqlite3.Row 变成稳定的 JSON 友好结构。"""
        return MemoryEntry(
            id=row["id"],
            target=row["target"],
            scope=row["scope"],
            topic=row["topic"],
            pinned=bool(row["pinned"]),
            content=row["content"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    @staticmethod
    def _join_contents(entries: list[MemoryEntry]) -> str:
        return "\n§\n".join(e["content"] for e in entries)

    @staticmethod
    def _format_prompt_entries(entries: list[MemoryEntry]) -> str:
        """按 scope/topic 分组，给启动 prompt 一个可扫描的索引。"""
        lines: list[str] = []
        current_group: tuple[str, str] | None = None
        for entry in entries:
            group = (entry["scope"], entry["topic"])
            if group != current_group:
                if lines:
                    lines.append("")
                lines.append(f"[{entry['scope']}/{entry['topic']}]")
                current_group = group
            lines.append(f"- {entry['content']}")
        return "\n".join(lines)

    @staticmethod
    def _preview_match(entry: MemoryEntry) -> str:
        content = entry["content"][:80] + ("..." if len(entry["content"]) > 80 else "")
        pin = "pinned" if entry["pinned"] else "topic"
        return f"[{entry['target']}/{entry['scope']}/{entry['topic']}/{pin}] {content}"

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
