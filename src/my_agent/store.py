"""持久化层：SQLite 保存任务、整体检查点和可续读事件。

runs 表是一条逻辑任务的当前事实，events 表是按序追加的观测记录。
所有业务输入通过 SQL 占位符绑定；只有占位符数量由程序生成。
connect 负责提交/回滚及关闭连接，避免把数据库连接当成永久全局状态。
执行检查点与会话历史分开：前者用于恢复进度，后者只提供最近的成功对话摘要。
本地数据库没有加密，也没有外部系统的分布式事务；部署时需补权限和隔离。
"""

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from .schemas import Report, RunRequest, RunView


class Conflict(ValueError):
    """业务状态冲突：重复幂等键不同请求、会话忙碌或非法状态转换。"""

    pass


def now() -> str:
    """数据库时间统一保存为带时区的 UTC ISO 字符串，展示时再转换。"""
    return datetime.now(timezone.utc).isoformat()


class Store:
    """封装数据库操作，不承担模型调用、权限决策或后台调度。"""

    def __init__(self, path: Path):
        """创建目录并幂等建表；不会清空已有任务或删除用户数据库。

        runs.checkpoint 是内部整体状态；result 是对外可展示的报告。
        events.seq 全库递增，每个任务按 run_id 过滤并用 seq 续读。
        """
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS runs (
                    run_id TEXT PRIMARY KEY, session_id TEXT NOT NULL, query TEXT NOT NULL,
                    status TEXT NOT NULL, approval INTEGER NOT NULL, result TEXT, error TEXT,
                    checkpoint TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    metrics TEXT NOT NULL DEFAULT '{}', idempotency_key TEXT UNIQUE,
                    fingerprint TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS events (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL,
                    type TEXT NOT NULL, payload TEXT NOT NULL, created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS events_run ON events(run_id, seq);
            """)

    @contextmanager
    def connect(self):
        """每次操作获取短生命周期连接，正常提交、异常回滚，最后总是关闭。

        sqlite3.Row 支持按列名读取；连接上的 with 负责事务，但不负责关闭连接。
        """
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def create(self, request: RunRequest, key: str | None = None) -> tuple[RunView, bool]:
        """创建 queued 任务，或返回相同幂等键的原任务及 created=False。

        排序 JSON 是稳定的请求指纹，比较时区分“同键同请求”与“同键不同请求”。
        同会话活跃任务互斥是单进程教学约束，并非多 worker 的分布式锁。
        """
        fingerprint = json.dumps(request.model_dump(), ensure_ascii=False, sort_keys=True)
        run_id, session_id = uuid4().hex, request.session_id or uuid4().hex
        timestamp = now()
        with self.connect() as db:
            if key:
                old = db.execute("SELECT * FROM runs WHERE idempotency_key=?", (key,)).fetchone()
                if old:
                    if old["fingerprint"] != fingerprint:
                        raise Conflict("同一 Idempotency-Key 不能用于不同请求")
                    return self.view(old), False
            busy = db.execute(
                "SELECT run_id FROM runs WHERE session_id=? AND status IN "
                "('queued','running','awaiting_approval','paused')",
                (session_id,),
            ).fetchone()
            if busy:
                raise Conflict("该会话已有活跃任务，请先完成或取消")
            db.execute(
                "INSERT INTO runs (run_id,session_id,query,status,approval,created_at,updated_at,"
                "idempotency_key,fingerprint) VALUES (?,?,?,'queued',?,?,?,?,?)",
                (
                    run_id,
                    session_id,
                    request.query,
                    request.require_approval,
                    timestamp,
                    timestamp,
                    key,
                    fingerprint,
                ),
            )
        return self.get(run_id), True

    @staticmethod
    def view(row) -> RunView:
        """将数据库行投影为对外视图；反序列化报告/指标，不泄漏内部 checkpoint。"""
        return RunView(
            **{
                k: row[k]
                for k in (
                    "run_id",
                    "session_id",
                    "query",
                    "status",
                    "error",
                    "created_at",
                    "updated_at",
                )
            },
            result=Report.model_validate_json(row["result"]) if row["result"] else None,
            metrics=json.loads(row["metrics"]),
        )

    def get(self, run_id: str) -> RunView:
        """查询可展示任务；不存在抛 KeyError，由 HTTP 层转成 404。"""
        with self.connect() as db:
            row = db.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
        if not row:
            raise KeyError(run_id)
        return self.view(row)

    def row(self, run_id: str) -> dict:
        """只供服务内部读取含检查点的完整行，不直接作为 API 响应返回。"""
        with self.connect() as db:
            row = db.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
        if not row:
            raise KeyError(run_id)
        return dict(row)

    def transition(
        self,
        run_id: str,
        allowed: tuple[str, ...],
        status: str,
        *,
        result: Report | None = None,
        error: str | None = None,
        metrics: dict | None = None,
    ) -> None:
        """带当前状态条件地更新任务，避免取消/完成之间相互覆盖。

        allowed 是服务层规定的合法旧状态，UPDATE 同时检查状态并写入新值。
        COALESCE 让未传 result/metrics 时保留原值；error 则会显式重置。
        动态拼接的仅是问号个数，所有业务值仍通过参数绑定，防止 SQL 注入。
        """
        placeholders = ",".join("?" for _ in allowed)
        with self.connect() as db:
            changed = db.execute(
                "UPDATE runs SET status=?,result=COALESCE(?,result),error=?,updated_at=?,"
                f"metrics=COALESCE(?,metrics) WHERE run_id=? AND status IN ({placeholders})",
                (
                    status,
                    result.model_dump_json() if result else None,
                    error,
                    now(),
                    json.dumps(metrics) if metrics is not None else None,
                    run_id,
                    *allowed,
                ),
            ).rowcount
        if not changed:
            raise Conflict("当前状态不允许该操作")

    def save_checkpoint(self, run_id: str, state: dict) -> None:
        """整体状态与累计指标在同次 UPDATE 中提交，只允许仍在 running 的任务写入。

        已取消/暂停的任务不会被迟到回调改写；不能单独只保存聊天消息。
        """
        metrics = {k: state[k] for k in ("turns", "total_tokens", "tool_calls")}
        with self.connect() as db:
            db.execute(
                "UPDATE runs SET checkpoint=?,metrics=?,updated_at=? "
                "WHERE run_id=? AND status='running'",
                (json.dumps(state, ensure_ascii=False), json.dumps(metrics), now(), run_id),
            )

    def event(self, run_id: str, kind: str, payload: dict) -> None:
        """追加观测事件；它与状态更新是独立事务，不是业务副作用的恰好一次保证。"""
        with self.connect() as db:
            db.execute(
                "INSERT INTO events(run_id,type,payload,created_at) VALUES (?,?,?,?)",
                (run_id, kind, json.dumps(payload, ensure_ascii=False), now()),
            )

    def events(self, run_id: str, after: int = 0) -> list[dict]:
        """按任务和序号返回最多 200 条事件，调用方更新游标后继续分页。"""
        with self.connect() as db:
            rows = db.execute(
                "SELECT * FROM events WHERE run_id=? AND seq>? ORDER BY seq LIMIT 200",
                (run_id, after),
            ).fetchall()
        return [{**dict(row), "payload": json.loads(row["payload"])} for row in rows]

    def history(self, session_id: str, exclude_run: str = "") -> list[dict]:
        """取最近三条 completed 任务，逆转为正时间序列，并截取总结注入上下文。

        待审核/失败任务不进入成功历史；这不是长期记忆，也不返回内部执行轨迹。
        """
        with self.connect() as db:
            rows = db.execute(
                "SELECT query,result FROM runs WHERE session_id=? "
                "AND status='completed' AND run_id!=? "
                "ORDER BY created_at DESC LIMIT 3",
                (session_id, exclude_run),
            ).fetchall()
        messages = []
        for row in reversed(rows):
            report = json.loads(row["result"])
            messages.extend(
                [
                    {"role": "user", "content": row["query"]},
                    {"role": "assistant", "content": report["summary"][:1500]},
                ]
            )
        return messages

    def recover(self) -> None:
        """启动时标记上次遗留 queued/running 为 paused，等待用户显式 resume。"""
        # 仅适合单 worker：不能在另一个 worker 仍执行时把它的任务当成遗留任务。
        # 不自动重放未知写操作；本 Demo 的工具只有只读检索和报告校验。
        with self.connect() as db:
            db.execute(
                "UPDATE runs SET status='paused',updated_at=? WHERE status IN ('running','queued')",
                (now(),),
            )

    def complete(
        self, run_id: str, allowed: tuple[str, ...], report: Report, metrics: dict | None = None
    ) -> None:
        """复用同一个受保护状态更新入口，把已有校验报告标记为 completed。"""
        self.transition(run_id, allowed, "completed", result=report, metrics=metrics)
