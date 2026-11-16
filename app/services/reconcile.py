import sqlite3


def run_reconcile(conn: sqlite3.Connection, as_of_date: str) -> dict:
    """策略视角与实物分区对账。

    除存储态计数外，额外核对两类「换版同源」错位：
      - version_drift：分区 policy_version 不等于该域现行版本（同源被破坏）
      - stale_judgment：仍待执行/执行中的裁决来自旧版（换版未作废干净）
    """
    counts = {
        r["storage_state"]: r["c"]
        for r in conn.execute(
            "SELECT storage_state, COUNT(*) AS c FROM partitions GROUP BY storage_state"
        ).fetchall()
    }
    online = int(counts.get("在线", 0))
    archived = int(counts.get("已归档", 0))
    destroyed = int(counts.get("已销毁", 0))
    pending = conn.execute(
        "SELECT COUNT(*) AS c FROM judgments WHERE status IN ('待执行','执行中')"
    ).fetchone()["c"]

    mismatches = conn.execute(
        """
        SELECT j.id AS judgment_id, j.decided_action, j.status AS judgment_status,
               p.id AS partition_id, p.storage_state
        FROM judgments j
        JOIN partitions p ON p.id = j.partition_id
        WHERE j.status = '已完成'
          AND (
            (j.decided_action = '归档' AND p.storage_state != '已归档')
            OR (j.decided_action = '销毁' AND p.storage_state != '已销毁')
          )
        """
    ).fetchall()
    mismatch_count = len(mismatches)

    drift = conn.execute(
        """
        SELECT p.id AS partition_id, p.domain_code, p.biz_date,
               p.policy_version, r.version AS active_version
        FROM partitions p
        LEFT JOIN retain_policies r
          ON r.domain_code = p.domain_code AND r.active = 1
        WHERE r.version IS NULL OR p.policy_version != r.version
        """
    ).fetchall()
    version_drift_count = len(drift)

    stale = conn.execute(
        """
        SELECT j.id AS judgment_id, j.domain_code, j.policy_version,
               r.version AS active_version
        FROM judgments j
        LEFT JOIN retain_policies r
          ON r.domain_code = j.domain_code AND r.active = 1
        WHERE j.status IN ('待执行','执行中')
          AND (r.version IS NULL OR j.policy_version != r.version)
        """
    ).fetchall()
    stale_judgment_count = len(stale)

    summary = (
        f"在线{online} / 已归档{archived} / 已销毁{destroyed} / "
        f"待执行裁决{pending} / 错位{mismatch_count} / "
        f"版本漂移{version_drift_count} / 旧版残留裁决{stale_judgment_count}"
    )
    cur = conn.execute(
        "INSERT INTO reconcile_reports "
        "(as_of_date, online_count, archived_count, destroyed_count, "
        "pending_judgment_count, mismatch_count, version_drift_count, "
        "stale_judgment_count, summary) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (
            as_of_date,
            online,
            archived,
            destroyed,
            int(pending),
            mismatch_count,
            version_drift_count,
            stale_judgment_count,
            summary,
        ),
    )
    report_id = int(cur.lastrowid)
    row = conn.execute(
        "SELECT id, as_of_date, online_count, archived_count, destroyed_count, "
        "pending_judgment_count, mismatch_count, version_drift_count, "
        "stale_judgment_count, summary, created_at "
        "FROM reconcile_reports WHERE id = ?",
        (report_id,),
    ).fetchone()
    report = dict(row)
    report["mismatches"] = [dict(m) for m in mismatches]
    report["version_drift"] = [dict(d) for d in drift]
    report["stale_judgments"] = [dict(s) for s in stale]
    return report


def list_reports(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT id, as_of_date, online_count, archived_count, destroyed_count, "
        "pending_judgment_count, mismatch_count, version_drift_count, "
        "stale_judgment_count, summary, created_at "
        "FROM reconcile_reports ORDER BY id DESC"
    ).fetchall()
    return [dict(r) for r in rows]
