import sqlite3
from contextlib import contextmanager
from pathlib import Path

from app.config import Config

# 写事务默认等待时长（毫秒）。换版/扫描这类需要互斥的写操作可显式调小，
# 以便在锁被占用时立刻失败并回以「被拦截」，而不是长时间挂住。
BUSY_TIMEOUT_MS = 3000


class DatabaseLocked(RuntimeError):
    """写锁被其他会话占用。"""


def connect(db_path: str | None = None, *, busy_timeout_ms: int = BUSY_TIMEOUT_MS) -> sqlite3.Connection:
    path = db_path or Config.database()
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    # isolation_level=None：关闭 sqlite3 的隐式事务，由 get_db 显式 BEGIN/COMMIT，
    # 这样换版才能用 BEGIN IMMEDIATE 一次性拿到整库写锁。
    conn = sqlite3.connect(
        path,
        check_same_thread=False,
        isolation_level=None,
        timeout=busy_timeout_ms / 1000,
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute(f"PRAGMA busy_timeout = {int(busy_timeout_ms)}")
    return conn


@contextmanager
def get_db(
    db_path: str | None = None,
    *,
    immediate: bool = False,
    busy_timeout_ms: int = BUSY_TIMEOUT_MS,
):
    """显式事务。

    immediate=True 时用 BEGIN IMMEDIATE 先拿写锁：全库换版、扫描快照等
    需要「同一时刻只允许一个写者」的路径必须走它。拿不到锁时立刻抛
    DatabaseLocked，交由上层记「被拦截」并返回 409。
    """
    conn = connect(db_path, busy_timeout_ms=busy_timeout_ms)
    try:
        try:
            conn.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
        except sqlite3.OperationalError as exc:
            if _is_lock_error(exc):
                raise DatabaseLocked("写锁被其他会话占用，请稍后重试") from exc
            raise
        yield conn
        try:
            # delete 日志模式下 COMMIT 要独占锁；并发读仍持 SHARED 时会在
            # 这里等不到锁。把它也归为「被拦截」，而不是冒成 500。
            conn.execute("COMMIT")
        except sqlite3.OperationalError as exc:
            if _is_lock_error(exc):
                raise DatabaseLocked("提交时写锁被占用，本次变更已回滚") from exc
            raise
    except BaseException:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    finally:
        conn.close()


def _is_lock_error(exc: sqlite3.OperationalError) -> bool:
    msg = str(exc).lower()
    return "locked" in msg or "busy" in msg


SCHEMA = """
CREATE TABLE IF NOT EXISTS retain_policies (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  domain_code TEXT NOT NULL,
  domain_name TEXT NOT NULL,
  keep_days INTEGER NOT NULL,
  action_on_expire TEXT NOT NULL CHECK (action_on_expire IN ('归档','销毁')),
  version INTEGER NOT NULL DEFAULT 1,
  active INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  UNIQUE(domain_code, version)
);

CREATE TABLE IF NOT EXISTS partitions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  domain_code TEXT NOT NULL,
  biz_date TEXT NOT NULL,
  row_count INTEGER NOT NULL DEFAULT 0,
  storage_state TEXT NOT NULL CHECK (
    storage_state IN ('在线','已归档','已销毁','裁撤中')
  ),
  archive_uri TEXT,
  policy_version INTEGER NOT NULL DEFAULT 1,
  landed_version INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  UNIQUE(domain_code, biz_date)
);

CREATE TABLE IF NOT EXISTS scan_jobs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  as_of_date TEXT NOT NULL,
  status TEXT NOT NULL,
  expired_count INTEGER NOT NULL DEFAULT 0,
  policy_snapshot TEXT NOT NULL DEFAULT '{}',
  message TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS judgments (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  scan_job_id INTEGER NOT NULL,
  partition_id INTEGER NOT NULL,
  domain_code TEXT NOT NULL,
  biz_date TEXT NOT NULL,
  policy_version INTEGER NOT NULL,
  keep_days INTEGER NOT NULL,
  decided_action TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('待执行','执行中','已完成','已失败','已作废')),
  fail_reason TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  FOREIGN KEY(scan_job_id) REFERENCES scan_jobs(id),
  FOREIGN KEY(partition_id) REFERENCES partitions(id)
);

CREATE TABLE IF NOT EXISTS execute_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  judgment_id INTEGER NOT NULL UNIQUE,
  action TEXT NOT NULL,
  result_state TEXT NOT NULL,
  archive_uri TEXT,
  ok INTEGER NOT NULL,
  detail TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  FOREIGN KEY(judgment_id) REFERENCES judgments(id)
);

CREATE TABLE IF NOT EXISTS reconcile_reports (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  as_of_date TEXT NOT NULL,
  online_count INTEGER NOT NULL,
  archived_count INTEGER NOT NULL,
  destroyed_count INTEGER NOT NULL,
  pending_judgment_count INTEGER NOT NULL,
  mismatch_count INTEGER NOT NULL,
  version_drift_count INTEGER NOT NULL DEFAULT 0,
  stale_judgment_count INTEGER NOT NULL DEFAULT 0,
  summary TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS switch_jobs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  actor TEXT NOT NULL DEFAULT 'admin',
  status TEXT NOT NULL,
  base_versions TEXT NOT NULL DEFAULT '{}',
  target_versions TEXT NOT NULL DEFAULT '{}',
  partitions_rebased INTEGER NOT NULL DEFAULT 0,
  judgments_voided INTEGER NOT NULL DEFAULT 0,
  message TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  finished_at TEXT
);

CREATE TABLE IF NOT EXISTS landing_jobs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  domain_code TEXT NOT NULL,
  biz_date TEXT NOT NULL,
  row_count INTEGER NOT NULL,
  policy_version INTEGER NOT NULL,
  status TEXT NOT NULL,
  message TEXT NOT NULL DEFAULT '',
  partition_id INTEGER,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  FOREIGN KEY(partition_id) REFERENCES partitions(id)
);

CREATE TABLE IF NOT EXISTS meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
"""


SEED_POLICIES = [
    ("orders", "订单明细域", 7, "归档"),
    ("payments", "支付流水域", 14, "归档"),
    ("events", "埋点事件域", 3, "销毁"),
    ("inventory", "库存快照域", 10, "归档"),
]

SEED_PARTITIONS = [
    ("orders", "2026-11-01", 1200, "在线"),
    ("orders", "2026-11-05", 980, "在线"),
    ("orders", "2026-11-10", 1100, "在线"),
    ("orders", "2026-11-14", 1050, "在线"),
    ("payments", "2026-10-20", 800, "在线"),
    ("payments", "2026-11-01", 760, "在线"),
    ("payments", "2026-11-12", 790, "在线"),
    ("events", "2026-11-08", 5000, "在线"),
    ("events", "2026-11-12", 5200, "在线"),
    ("events", "2026-11-15", 5100, "在线"),
    ("inventory", "2026-10-30", 300, "在线"),
    ("inventory", "2026-11-11", 310, "在线"),
]


def _has_column(conn: sqlite3.Connection, table: str, column: str) -> bool:
    return any(r["name"] == column for r in conn.execute(f"PRAGMA table_info({table})"))


SCHEMA_VERSION = 2


def _migrate(conn: sqlite3.Connection) -> None:
    """把已落盘的旧库补齐到当前结构（不重建、不丢数据）。

    用 meta.schema_version 做闸门：整段迁移只跑一次，跑完打戳。这样回填
    不会被重复执行（例如换版后 landed_version=1 / policy_version=2 是合法
    审计，重复回填会把它抹掉）。若上次迁移中途被打断、戳没打上，下次启动
    会重跑；每条 ALTER 都先查列是否存在，可安全重入。
    """
    row = conn.execute(
        "SELECT value FROM meta WHERE key = 'schema_version'"
    ).fetchone()
    if row is not None and int(row["value"]) >= SCHEMA_VERSION:
        return

    added_policy_version = False
    if not _has_column(conn, "partitions", "policy_version"):
        conn.execute(
            "ALTER TABLE partitions ADD COLUMN policy_version INTEGER NOT NULL DEFAULT 1"
        )
        added_policy_version = True
    if added_policy_version:
        conn.execute(
            "UPDATE partitions SET policy_version = COALESCE("
            "(SELECT version FROM retain_policies p "
            " WHERE p.domain_code = partitions.domain_code AND p.active = 1), 1)"
        )

    if not _has_column(conn, "partitions", "landed_version"):
        conn.execute(
            "ALTER TABLE partitions ADD COLUMN landed_version INTEGER NOT NULL DEFAULT 1"
        )
        # 只在刚补列这一次回填；之后 landed_version 是真实落库审计，不可再动。
        conn.execute("UPDATE partitions SET landed_version = policy_version")

    if not _has_column(conn, "scan_jobs", "policy_snapshot"):
        conn.execute(
            "ALTER TABLE scan_jobs ADD COLUMN policy_snapshot TEXT NOT NULL DEFAULT '{}'"
        )
    if not _has_column(conn, "reconcile_reports", "version_drift_count"):
        conn.execute(
            "ALTER TABLE reconcile_reports ADD COLUMN version_drift_count INTEGER NOT NULL DEFAULT 0"
        )
    if not _has_column(conn, "reconcile_reports", "stale_judgment_count"):
        conn.execute(
            "ALTER TABLE reconcile_reports ADD COLUMN stale_judgment_count INTEGER NOT NULL DEFAULT 0"
        )

    conn.execute(
        "INSERT OR REPLACE INTO meta (key, value) VALUES ('schema_version', ?)",
        (str(SCHEMA_VERSION),),
    )


def init_db(db_path: str | None = None) -> None:
    conn = connect(db_path)
    try:
        conn.executescript(SCHEMA)
        _migrate(conn)
        n = conn.execute("SELECT COUNT(*) AS c FROM retain_policies").fetchone()["c"]
        if n == 0:
            conn.execute("BEGIN IMMEDIATE")
            try:
                for domain_code, domain_name, keep_days, action in SEED_POLICIES:
                    conn.execute(
                        "INSERT INTO retain_policies "
                        "(domain_code, domain_name, keep_days, action_on_expire, version, active) "
                        "VALUES (?,?,?,?,1,1)",
                        (domain_code, domain_name, keep_days, action),
                    )
                for domain_code, biz_date, row_count, state in SEED_PARTITIONS:
                    conn.execute(
                        "INSERT INTO partitions "
                        "(domain_code, biz_date, row_count, storage_state, "
                        " policy_version, landed_version) "
                        "VALUES (?,?,?,?,1,1)",
                        (domain_code, biz_date, row_count, state),
                    )
                conn.execute(
                    "INSERT OR REPLACE INTO meta (key, value) VALUES ('lab_name', ?)",
                    (Config.LAB_NAME,),
                )
                conn.execute(
                    "INSERT OR REPLACE INTO meta (key, value) VALUES ('skeleton', ?)",
                    (
                        "分区留存策略版本→入库落库→到期扫描裁决→归档或销毁执行→策略与实物分区对账",
                    ),
                )
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise
    finally:
        conn.close()
