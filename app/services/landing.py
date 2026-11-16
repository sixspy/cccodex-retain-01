"""入库落库：分区数据写入本机库，并盖上落库当时的策略版本戳。

这是「换版后版本号与分区存储态必须同源」的上游锚点：落库任务记录写入时
的现行版本，写入的分区 policy_version / landed_version 都取该版本。换版会
把存量分区版本整体重盖；此后新落库的分区沿用新版本。
"""

import sqlite3
from datetime import datetime

from app.services.policies import get_active_policy


class LandingRejected(ValueError):
    """落库被拒绝（参数/版本不符）。"""


def _parse_day(value: str, field: str) -> str:
    try:
        datetime.strptime(value, "%Y-%m-%d")
    except ValueError as exc:
        raise LandingRejected(f"{field} 必须是 YYYY-MM-DD：{value!r}") from exc
    return value


def land_partition(
    conn: sqlite3.Connection,
    *,
    domain_code: str,
    biz_date: str,
    row_count: int,
    policy_version: int | None = None,
    actor: str = "admin",
) -> dict:
    code = str(domain_code or "").strip()
    day = str(biz_date or "").strip()
    if not code or not day:
        raise LandingRejected("落库需要 domain_code 与 biz_date")
    _parse_day(day, "biz_date")
    try:
        rows = int(row_count)
    except (TypeError, ValueError) as exc:
        raise LandingRejected("row_count 非法") from exc
    if rows < 0:
        raise LandingRejected("row_count 不能为负")

    policy = get_active_policy(conn, code)
    if not policy:
        raise LandingRejected(f"主题域 {code} 没有现行策略，无法落库")
    current = int(policy["version"])
    if policy_version is not None:
        try:
            requested = int(policy_version)
        except (TypeError, ValueError) as exc:
            raise LandingRejected("policy_version 非法") from exc
        if requested != current:
            raise LandingRejected(
                f"落库版本 v{requested} 与现行 v{current} 不同源"
            )

    existing = conn.execute(
        "SELECT id, storage_state FROM partitions "
        "WHERE domain_code = ? AND biz_date = ?",
        (code, day),
    ).fetchone()
    if existing:
        # 已归档/已销毁的分区不允许「落库」复活：那会让存储态与版本戳
        # 自相矛盾（同源闸门在分区层面被打破）。
        if existing["storage_state"] != "在线":
            raise LandingRejected(
                f"分区已是「{existing['storage_state']}」，不能直接落库复活；"
                "如需重入请另建业务日"
            )
        # 幂等重落：行数覆盖，版本戳刷新到现行版，存储态保持在线。
        conn.execute(
            "UPDATE partitions SET row_count = ?, policy_version = ?, "
            "landed_version = ? WHERE id = ?",
            (rows, current, current, existing["id"]),
        )
        partition_id = int(existing["id"])
        status = "已重落"
        message = f"分区已存在，按当前 v{current} 重落"
    else:
        cur = conn.execute(
            "INSERT INTO partitions "
            "(domain_code, biz_date, row_count, storage_state, "
            " policy_version, landed_version) "
            "VALUES (?,?,?, '在线', ?, ?)",
            (code, day, rows, current, current),
        )
        partition_id = int(cur.lastrowid)
        status = "成功"
        message = f"新分区按当前 v{current} 落库"

    cur = conn.execute(
        "INSERT INTO landing_jobs "
        "(domain_code, biz_date, row_count, policy_version, status, message, partition_id) "
        "VALUES (?,?,?,?,?,?,?)",
        (code, day, rows, current, status, message, partition_id),
    )
    return get_landing_job(conn, int(cur.lastrowid))


def get_landing_job(conn: sqlite3.Connection, job_id: int) -> dict | None:
    row = conn.execute(
        "SELECT id, domain_code, biz_date, row_count, policy_version, status, "
        "message, partition_id, created_at FROM landing_jobs WHERE id = ?",
        (job_id,),
    ).fetchone()
    return dict(row) if row else None


def list_landing_jobs(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT id, domain_code, biz_date, row_count, policy_version, status, "
        "message, partition_id, created_at FROM landing_jobs ORDER BY id DESC"
    ).fetchall()
    return [dict(r) for r in rows]
