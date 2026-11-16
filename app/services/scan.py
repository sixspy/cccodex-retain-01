import json
import sqlite3
from datetime import date, datetime

from app.services.policies import active_version_map

# 扫描拿锁等待上限：换版进行中时，扫描要么等到最新版，要么快速失败，
# 不允许读到「半版」的策略快照。
SCAN_LOCK_WAIT_MS = 3000


class ScanBusy(RuntimeError):
    """换版/其他写者占用写锁，本次扫描未启动。"""


def _parse_day(value: str) -> date:
    return datetime.strptime(value, "%Y-%m-%d").date()


def run_expire_scan(conn: sqlite3.Connection, as_of_date: str) -> dict:
    """扫描在线分区：超过策略保留天数则生成待执行裁决。

    关键约束（修「扫描任务吃错版本」）：本次扫描在事务开头**一次性**取
    现行策略快照，之后整轮只认这一份；任务里记录快照版本。裁决各自带上
    快照中的 policy_version，与分区当时的 policy_version 对齐。
    """
    try:
        as_of = _parse_day(as_of_date)
    except ValueError as exc:
        raise ValueError(f"基准日必须是 YYYY-MM-DD：{as_of_date!r}") from exc
    snapshot = active_version_map(conn)
    snapshot_versions = {code: int(p["version"]) for code, p in snapshot.items()}

    cur = conn.execute(
        "INSERT INTO scan_jobs (as_of_date, status, message, policy_snapshot) "
        "VALUES (?,?,?,?)",
        (
            as_of_date,
            "进行中",
            "",
            json.dumps(snapshot_versions, ensure_ascii=False, sort_keys=True),
        ),
    )
    job_id = int(cur.lastrowid)
    expired = 0
    stale_skipped = 0
    invalid_skipped = 0

    online = conn.execute(
        "SELECT id, domain_code, biz_date, storage_state, policy_version "
        "FROM partitions WHERE storage_state = '在线'"
    ).fetchall()

    for part in online:
        policy = snapshot.get(part["domain_code"])
        if not policy:
            continue
        try:
            biz_day = _parse_day(part["biz_date"])
        except ValueError:
            # 单条脏数据不能拖垮整轮扫描（否则会永久 500）。
            invalid_skipped += 1
            continue
        age = (as_of - biz_day).days
        if age < int(policy["keep_days"]):
            continue
        # 分区版本必须与本次扫描快照同源；否则说明换版前后数据打架，不开单。
        if int(part["policy_version"]) != int(policy["version"]):
            stale_skipped += 1
            continue
        # 同一分区若已有未完成裁决，不再重复开单。
        exists = conn.execute(
            "SELECT id FROM judgments WHERE partition_id = ? "
            "AND status IN ('待执行','执行中')",
            (part["id"],),
        ).fetchone()
        if exists:
            continue
        conn.execute(
            "INSERT INTO judgments "
            "(scan_job_id, partition_id, domain_code, biz_date, policy_version, "
            "keep_days, decided_action, status) "
            "VALUES (?,?,?,?,?,?,?, '待执行')",
            (
                job_id,
                part["id"],
                part["domain_code"],
                part["biz_date"],
                policy["version"],
                policy["keep_days"],
                policy["action_on_expire"],
            ),
        )
        expired += 1

    message = f"生成待执行裁决 {expired} 条"
    if stale_skipped:
        message += f"；{stale_skipped} 个分区版本与扫描快照不同源已跳过"
    if invalid_skipped:
        message += f"；{invalid_skipped} 个分区业务日非法已跳过"
    conn.execute(
        "UPDATE scan_jobs SET status = ?, expired_count = ?, message = ? WHERE id = ?",
        ("已完成", expired, message, job_id),
    )
    return get_scan_job(conn, job_id)


def get_scan_job(conn: sqlite3.Connection, job_id: int) -> dict | None:
    row = conn.execute(
        "SELECT id, as_of_date, status, expired_count, policy_snapshot, message, created_at "
        "FROM scan_jobs WHERE id = ?",
        (job_id,),
    ).fetchone()
    return dict(row) if row else None


def list_scan_jobs(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT id, as_of_date, status, expired_count, policy_snapshot, message, created_at "
        "FROM scan_jobs ORDER BY id DESC"
    ).fetchall()
    return [dict(r) for r in rows]


def list_judgments(
    conn: sqlite3.Connection,
    status: str | None = None,
    scan_job_id: int | None = None,
) -> list[dict]:
    clauses: list[str] = []
    params: list[object] = []
    if status:
        clauses.append("status = ?")
        params.append(status)
    if scan_job_id is not None:
        clauses.append("scan_job_id = ?")
        params.append(scan_job_id)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    rows = conn.execute(
        "SELECT id, scan_job_id, partition_id, domain_code, biz_date, "
        "policy_version, keep_days, decided_action, status, fail_reason, created_at "
        "FROM judgments"
        + where
        + " ORDER BY id DESC",
        params,
    ).fetchall()
    return [dict(r) for r in rows]
