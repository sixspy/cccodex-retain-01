import sqlite3

from app.services.partitions import get_partition
from app.services.policies import get_active_policy


class JudgmentRejected(ValueError):
    """裁决不可执行（状态/版本/分区态不符）。"""


class ExecutionFailed(JudgmentRejected):
    """执行已失败，失败现场（回退在线 + 失败原因 + 执行流水）已写入当前事务。

    与「拒绝」不同：拒绝不改库，调用方应回滚；失败要把审计留下，调用方必须
    **正常提交**当前事务。因此执行动作失败时返回 `run`（`ok=0`）而非抛异常，
    避免异常穿出 with 块导致回滚。
    """


def execute_judgment(conn: sqlite3.Connection, judgment_id: int) -> dict:
    """执行单条裁决：归档写入 archive_uri；销毁清空在线态。

    版本同源闸门：裁决记录的 policy_version 必须等于该主题域现行版，
    否则说明换版后旧裁决还想动新版分区（旧到期动作与新版互殴），直接拒绝。
    """
    row = conn.execute(
        "SELECT * FROM judgments WHERE id = ?", (judgment_id,)
    ).fetchone()
    if not row:
        raise JudgmentRejected("裁决不存在")
    judgment = dict(row)
    # 先判版本同源，再判状态：换版后旧裁决（含已被作废的）应明确提示
    # 「来自旧版」，而不是笼统的状态错误。
    policy = get_active_policy(conn, judgment["domain_code"])
    if not policy:
        raise JudgmentRejected("该主题域没有现行策略版本")
    if int(judgment["policy_version"]) != int(policy["version"]):
        raise JudgmentRejected(
            f"裁决来自 v{judgment['policy_version']}，现行已是 "
            f"v{policy['version']}，请重新扫描而非执行旧裁决"
        )

    if judgment["status"] not in ("待执行", "已失败"):
        raise JudgmentRejected(f"当前状态不可执行：{judgment['status']}")

    part = get_partition(conn, judgment["partition_id"])
    if not part:
        raise JudgmentRejected("分区不存在")
    if part["storage_state"] not in ("在线", "裁撤中"):
        raise JudgmentRejected(f"分区状态不可裁撤：{part['storage_state']}")
    if int(part["policy_version"]) != int(policy["version"]):
        raise JudgmentRejected(
            f"分区版本 v{part['policy_version']} 与现行 v{policy['version']} 不同源"
        )

    conn.execute(
        "UPDATE judgments SET status = '执行中', fail_reason = '' WHERE id = ?",
        (judgment_id,),
    )
    conn.execute(
        "UPDATE partitions SET storage_state = '裁撤中' WHERE id = ?",
        (part["id"],),
    )

    action = judgment["decided_action"]
    try:
        if action == "归档":
            uri = f"archive://{part['domain_code']}/{part['biz_date']}"
            conn.execute(
                "UPDATE partitions SET storage_state = '已归档', archive_uri = ? WHERE id = ?",
                (uri, part["id"]),
            )
            result_state = "已归档"
            archive_uri = uri
            detail = "分区已归档并保留可查归档地址"
        elif action == "销毁":
            conn.execute(
                "UPDATE partitions SET storage_state = '已销毁', archive_uri = NULL, "
                "row_count = 0 WHERE id = ?",
                (part["id"],),
            )
            result_state = "已销毁"
            archive_uri = None
            detail = "分区已销毁，在线行数清零"
        else:
            raise JudgmentRejected(f"未知动作：{action}")

        conn.execute(
            "UPDATE judgments SET status = '已完成' WHERE id = ?",
            (judgment_id,),
        )
        conn.execute(
            "INSERT OR REPLACE INTO execute_runs "
            "(judgment_id, action, result_state, archive_uri, ok, detail) "
            "VALUES (?,?,?,?,1,?)",
            (judgment_id, action, result_state, archive_uri, detail),
        )
    except Exception as exc:
        conn.execute(
            "UPDATE partitions SET storage_state = '在线', archive_uri = NULL WHERE id = ?",
            (part["id"],),
        )
        conn.execute(
            "UPDATE judgments SET status = '已失败', fail_reason = ? WHERE id = ?",
            (str(exc), judgment_id),
        )
        conn.execute(
            "INSERT OR REPLACE INTO execute_runs "
            "(judgment_id, action, result_state, archive_uri, ok, detail) "
            "VALUES (?,?,?,?,0,?)",
            (judgment_id, action, "在线", None, str(exc)),
        )
        # 失败现场已写进当前事务。正常返回（不抛），让调用方提交事务，
        # 否则异常穿出 with 块会让失败审计一起被回滚。
        return get_run_by_judgment(conn, judgment_id)

    return get_run_by_judgment(conn, judgment_id)


def get_run_by_judgment(conn: sqlite3.Connection, judgment_id: int) -> dict | None:
    row = conn.execute(
        "SELECT id, judgment_id, action, result_state, archive_uri, ok, detail, created_at "
        "FROM execute_runs WHERE judgment_id = ?",
        (judgment_id,),
    ).fetchone()
    return dict(row) if row else None


def list_runs(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT id, judgment_id, action, result_state, archive_uri, ok, detail, created_at "
        "FROM execute_runs ORDER BY id DESC"
    ).fetchall()
    return [dict(r) for r in rows]
