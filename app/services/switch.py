"""整库原子换版。

全台只贯彻这一条路径：一次请求、一个事务内把**全库所有主题域**的留存
策略整体升到下一版本，并同步重盖分区版本、作废旧版未完成裁决。任一步
不满足「版本号与分区存储态同源」即整体回滚并记失败——也就是「换版偏了
即拦」。

并发：事务以 BEGIN IMMEDIATE 起手，全库只允许一个写者。第二个请求要么
拿不到锁（DatabaseLocked → 409 被拦截），要么拿到锁后发现 base 版本已被
对方推进（CAS 失败 → 409），因此不可能出现「双成功并盖掉对方」。
"""

import json
import sqlite3
from collections.abc import Iterable

from app.db import DatabaseLocked, get_db
from app.services.policies import active_version_map

VALID_ACTIONS = ("归档", "销毁")

# 换版事务拿锁的等待上限：拿不到就当「被拦截」快速失败，而不是长时间挂住。
SWITCH_LOCK_WAIT_MS = 300

# 补记「失败/被拦截」审计行时的等待上限。要比换版窗口宽，否则赢家持锁
# 稍久，输家的审计行就丢了。
RECORD_LOCK_WAIT_MS = 8000


class SwitchRejected(ValueError):
    """换版被业务规则拒绝（参数/版本/CAS/同源校验）。"""


class VersionConflict(SwitchRejected):
    """base 版本与库内现行版本不一致（并发或陈旧提交）。"""


def _snapshot_versions(snapshot: dict[str, dict]) -> dict[str, int]:
    return {code: int(p["version"]) for code, p in snapshot.items()}


def _normalize_changes(changes: Iterable[dict]) -> dict[str, dict]:
    normalized: dict[str, dict] = {}
    for raw in changes or []:
        code = str(raw.get("domain_code", "")).strip()
        if not code:
            raise SwitchRejected("变更项缺少 domain_code")
        if code in normalized:
            raise SwitchRejected(f"变更项重复：{code}")
        try:
            keep_days = int(raw["keep_days"])
        except (KeyError, TypeError, ValueError) as exc:
            raise SwitchRejected(f"{code} 的 keep_days 非法") from exc
        if keep_days < 0:
            raise SwitchRejected(f"{code} 的 keep_days 不能为负")
        action = str(raw.get("action_on_expire", "")).strip()
        if action not in VALID_ACTIONS:
            raise SwitchRejected(f"{code} 的到期动作必须是 归档 或 销毁")
        normalized[code] = {"keep_days": keep_days, "action_on_expire": action}
    return normalized


def _record_job(db_path: str | None, *, status: str, actor: str, base: dict, target: dict,
                rebased: int = 0, voided: int = 0, message: str = "") -> dict:
    """在独立连接里落一条换版任务记录（失败/被拦截时事后补记）。"""
    with get_db(db_path, busy_timeout_ms=RECORD_LOCK_WAIT_MS) as conn:
        cur = conn.execute(
            "INSERT INTO switch_jobs "
            "(actor, status, base_versions, target_versions, partitions_rebased, "
            " judgments_voided, message, finished_at) "
            "VALUES (?,?,?,?,?,?,?, datetime('now'))",
            (
                actor,
                status,
                json.dumps(base, ensure_ascii=False, sort_keys=True),
                json.dumps(target, ensure_ascii=False, sort_keys=True),
                rebased,
                voided,
                message,
            ),
        )
        return get_switch_job(conn, int(cur.lastrowid))


def switch_policies(
    db_path: str | None,
    *,
    base_versions: dict[str, int],
    changes: Iterable[dict] | None = None,
    actor: str = "admin",
) -> dict:
    """整库原子换版。成功返回 switch_job；失败抛 SwitchRejected/VersionConflict。"""
    base: dict[str, int] = {}
    for code, version in (base_versions or {}).items():
        try:
            base[str(code)] = int(version)
        except (TypeError, ValueError) as exc:
            raise SwitchRejected(f"base_versions 中 {code} 的版本非法") from exc

    try:
        normalized = _normalize_changes(changes or [])
        with get_db(db_path, immediate=True, busy_timeout_ms=SWITCH_LOCK_WAIT_MS) as conn:
            snapshot = active_version_map(conn)
            if not snapshot:
                raise SwitchRejected("库内没有现行策略，无法换版")

            current = _snapshot_versions(snapshot)
            unknown = sorted(set(base) - set(current))
            if unknown:
                raise SwitchRejected(f"未知主题域：{', '.join(unknown)}")
            unknown_changes = sorted(set(normalized) - set(current))
            if unknown_changes:
                raise SwitchRejected(f"变更项含未知主题域：{', '.join(unknown_changes)}")
            missing = sorted(set(current) - set(base))
            if missing:
                raise SwitchRejected(
                    f"base_versions 未覆盖全部主题域，缺少：{', '.join(missing)}"
                )
            stale = sorted(code for code in current if base[code] != current[code])
            if stale:
                detail = "、".join(
                    f"{code} 期望 v{base[code]} 实为 v{current[code]}" for code in stale
                )
                # 已拿锁却发现版本已被推走 → 与「没抢到锁」同属被拦截，
                # 只是拦截发生在锁内。这样两名同事并发只有一个成功。
                raise VersionConflict(f"现行版本已变化，换版被拦截：{detail}")

            target: dict[str, int] = {}
            rebased = 0
            voided = 0
            for code in sorted(current):
                policy = snapshot[code]
                override = normalized.get(code)
                keep_days = (
                    override["keep_days"] if override else int(policy["keep_days"])
                )
                action = (
                    override["action_on_expire"]
                    if override
                    else policy["action_on_expire"]
                )
                next_version = int(policy["version"]) + 1

                conn.execute(
                    "UPDATE retain_policies SET active = 0 "
                    "WHERE domain_code = ? AND active = 1",
                    (code,),
                )
                conn.execute(
                    "INSERT INTO retain_policies "
                    "(domain_code, domain_name, keep_days, action_on_expire, version, active) "
                    "VALUES (?,?,?,?,?,1)",
                    (code, policy["domain_name"], keep_days, action, next_version),
                )
                cur = conn.execute(
                    "UPDATE partitions SET policy_version = ? WHERE domain_code = ?",
                    (next_version, code),
                )
                rebased += cur.rowcount
                nulled = conn.execute(
                    "UPDATE judgments SET status = '已作废', "
                    "fail_reason = '换版作废：策略已升至 v' || ? "
                    "WHERE domain_code = ? AND status IN ('待执行','执行中')",
                    (next_version, code),
                )
                voided += nulled.rowcount
                target[code] = next_version

            _assert_same_source(conn, target)

            cur = conn.execute(
                "INSERT INTO switch_jobs "
                "(actor, status, base_versions, target_versions, partitions_rebased, "
                " judgments_voided, message, finished_at) "
                "VALUES (?, '成功', ?, ?, ?, ?, ?, datetime('now'))",
                (
                    actor,
                    json.dumps(current, ensure_ascii=False, sort_keys=True),
                    json.dumps(target, ensure_ascii=False, sort_keys=True),
                    rebased,
                    voided,
                    (
                        f"整库换版成功：{len(target)} 个主题域升至 "
                        f"{', '.join(f'{c} v{v}' for c, v in sorted(target.items()))}"
                    ),
                ),
            )
            job_id = int(cur.lastrowid)
            job = get_switch_job(conn, job_id)
        return job
    except DatabaseLocked as exc:
        _safe_record_job(
            db_path, status="被拦截", actor=actor, base=base, target={},
            message=f"换版被拦截：{exc}",
        )
        raise VersionConflict(f"换版被拦截：{exc}") from exc
    except SwitchRejected as exc:
        status = "被拦截" if isinstance(exc, VersionConflict) else "失败"
        _safe_record_job(
            db_path, status=status, actor=actor, base=base, target={},
            message=str(exc),
        )
        raise
    except Exception as exc:
        _safe_record_job(
            db_path, status="失败", actor=actor, base=base, target={},
            message=f"换版失败：{exc}",
        )
        raise


def _safe_record_job(db_path: str | None, **kwargs) -> None:
    """补记失败/被拦截任务；补记本身出错也不能盖掉原始异常。"""
    try:
        _record_job(db_path, **kwargs)
    except Exception:
        # 有意吞掉：换版原始的 VersionConflict/SwitchRejected 才是要抛给调用方的。
        pass


def _assert_same_source(conn: sqlite3.Connection, target: dict[str, int]) -> None:
    """同源闸门：版本号与分区版本必须一致，偏一点即回滚。

    注意它是**防御性**的：正常路径上换版刚在同一事务里逐域重盖过分区版本，
    所以这里读到的必然一致。它真正拦的是「重盖 SQL 写错行 / 别处并发插入了
    旧版本分区 / 将来有人改了换版流程却漏了重盖」——把不变量写成提交前的
    显式断言，任何一条不满足就整体回滚，而不是把半版留在库里。
    """
    active = active_version_map(conn)
    if set(active) != set(target):
        raise SwitchRejected("同源校验失败：现行主题域集合与目标不一致")
    for code, version in target.items():
        row = active[code]
        if int(row["version"]) != int(version):
            raise SwitchRejected(
                f"同源校验失败：{code} 现行 v{row['version']} 与目标 v{version} 不一致"
            )
        dup = conn.execute(
            "SELECT COUNT(*) AS c FROM retain_policies WHERE domain_code = ? AND active = 1",
            (code,),
        ).fetchone()["c"]
        if dup != 1:
            raise SwitchRejected(f"同源校验失败：{code} 现行版本数应为 1，实为 {dup}")
        drift = conn.execute(
            "SELECT COUNT(*) AS c FROM partitions "
            "WHERE domain_code = ? AND policy_version != ?",
            (code, version),
        ).fetchone()["c"]
        if drift:
            raise SwitchRejected(
                f"同源校验失败：{code} 有 {drift} 个分区版本未跟随 v{version}"
            )


def preflight(db_path: str | None = None) -> dict:
    """只读预检：现行版本、分区版本分布、待作废裁决数，供页面确认后提交。"""
    with get_db(db_path) as conn:
        snapshot = active_version_map(conn)
        domains = []
        for code, policy in sorted(snapshot.items()):
            drift = conn.execute(
                "SELECT COUNT(*) AS c FROM partitions "
                "WHERE domain_code = ? AND policy_version != ?",
                (code, int(policy["version"])),
            ).fetchone()["c"]
            partitions = conn.execute(
                "SELECT COUNT(*) AS c FROM partitions WHERE domain_code = ?", (code,)
            ).fetchone()["c"]
            open_judgments = conn.execute(
                "SELECT COUNT(*) AS c FROM judgments WHERE domain_code = ? "
                "AND status IN ('待执行','执行中')",
                (code,),
            ).fetchone()["c"]
            domains.append(
                {
                    "domain_code": code,
                    "domain_name": policy["domain_name"],
                    "keep_days": int(policy["keep_days"]),
                    "action_on_expire": policy["action_on_expire"],
                    "version": int(policy["version"]),
                    "partition_count": int(partitions),
                    "pending_judgment_count": int(open_judgments),
                    "version_drift_count": int(drift),
                }
            )
        return {"domains": domains}


def list_switch_jobs(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT id, actor, status, base_versions, target_versions, "
        "partitions_rebased, judgments_voided, message, created_at, finished_at "
        "FROM switch_jobs ORDER BY id DESC"
    ).fetchall()
    return [dict(r) for r in rows]


def get_switch_job(conn: sqlite3.Connection, job_id: int) -> dict | None:
    row = conn.execute(
        "SELECT id, actor, status, base_versions, target_versions, "
        "partitions_rebased, judgments_voided, message, created_at, finished_at "
        "FROM switch_jobs WHERE id = ?",
        (job_id,),
    ).fetchone()
    return dict(row) if row else None
