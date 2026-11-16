import sqlite3

_FIELDS = (
    "id, domain_code, domain_name, keep_days, action_on_expire, "
    "version, active, created_at"
)


def list_policies(conn: sqlite3.Connection, active_only: bool = False) -> list[dict]:
    sql = f"SELECT {_FIELDS} FROM retain_policies"
    if active_only:
        sql += " WHERE active = 1"
    sql += " ORDER BY domain_code, version DESC"
    return [dict(r) for r in conn.execute(sql).fetchall()]


def get_active_policy(conn: sqlite3.Connection, domain_code: str) -> dict | None:
    row = conn.execute(
        f"SELECT {_FIELDS} FROM retain_policies "
        "WHERE domain_code = ? AND active = 1 "
        "ORDER BY version DESC LIMIT 1",
        (domain_code,),
    ).fetchone()
    return dict(row) if row else None


def active_version_map(conn: sqlite3.Connection) -> dict[str, dict]:
    """一次性取回全部现行策略快照，键为主题域编码。

    扫描、换版、落库都以这一份快照为准，避免逐分区各查一次导致
    「同一任务内读到两个版本」。
    """
    rows = conn.execute(
        f"SELECT {_FIELDS} FROM retain_policies WHERE active = 1 "
        "ORDER BY domain_code, version DESC"
    ).fetchall()
    snapshot: dict[str, dict] = {}
    for row in rows:
        snapshot.setdefault(row["domain_code"], dict(row))
    return snapshot
