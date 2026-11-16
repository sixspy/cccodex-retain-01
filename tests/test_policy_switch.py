"""换版同源：策略列表 / 分区详情 / 扫描队列三处同改，且版本号与分区存储态同源。"""


def _active_versions(authed):
    return {
        p["domain_code"]: p["version"]
        for p in authed.get("/api/policies").json()["items"]
    }


def _switch(authed, changes=None, base=None):
    payload = {"base_versions": base or _active_versions(authed)}
    if changes:
        payload["changes"] = changes
    return authed.post("/api/policy-switch", json=payload)


def _executed_summary(authed):
    report = authed.post(
        "/api/reconcile-reports", json={"as_of_date": "2026-11-16"}
    ).json()["report"]
    return report


def test_switch_bumps_every_domain_and_rebases_partitions(authed):
    before = _active_versions(authed)
    r = _switch(authed, changes=[{"domain_code": "orders", "keep_days": 5, "action_on_expire": "销毁"}])
    assert r.status_code == 200, r.text
    job = r.json()["job"]
    assert job["status"] == "成功"
    assert job["partitions_rebased"] > 0

    after = _active_versions(authed)
    # 整库切换：每个域都 +1，不只是被改的那个
    assert all(after[code] == before[code] + 1 for code in before)

    # 三处同改之一：策略列表
    policies = {p["domain_code"]: p for p in authed.get("/api/policies").json()["items"]}
    assert policies["orders"]["keep_days"] == 5
    assert policies["orders"]["action_on_expire"] == "销毁"

    # 三处同改之二：分区详情/列表全部重盖到新版本
    parts = authed.get("/api/partitions").json()["items"]
    assert all(p["policy_version"] == after[p["domain_code"]] for p in parts)
    # 同源：对账不应出现版本漂移
    report = _executed_summary(authed)
    assert report["version_drift_count"] == 0
    assert report["stale_judgment_count"] == 0


def test_switch_voids_old_open_judgments_and_scan_uses_new_version(authed):
    # 先用旧版扫描出一批待执行裁决
    penal = authed.post("/api/scan-jobs", json={"as_of_date": "2026-11-16"})
    assert penal.status_code == 200
    old_pending = authed.get("/api/judgments", params={"status": "待执行"}).json()["items"]
    assert old_pending, "换版前应有旧版待执行裁决"
    old_versions = {j["policy_version"] for j in old_pending}

    r = _switch(authed)
    assert r.status_code == 200, r.text
    assert r.json()["job"]["judgments_voided"] >= len(old_pending)

    # 旧版裁决全部作废，不会和新版互殴
    still_open = authed.get("/api/judgments", params={"status": "待执行"}).json()["items"]
    assert still_open == []
    voided = authed.get("/api/judgments", params={"status": "已作废"}).json()["items"]
    assert len(voided) >= len(old_pending)

    # 三处同改之三：换版后新扫描吃的是新版
    new = authed.post("/api/scan-jobs", json={"as_of_date": "2026-11-16"}).json()["job"]
    assert new["status"] == "已完成"
    active = _active_versions(authed)
    snapshot = __import__("json").loads(new["policy_snapshot"])
    assert snapshot == active
    fresh = authed.get("/api/judgments", params={"status": "待执行"}).json()["items"]
    assert fresh, "换版后重新扫描应产出新版裁决"
    assert {j["policy_version"] for j in fresh}.issubset(set(active.values()))
    assert old_versions.isdisjoint({j["policy_version"] for j in fresh})


def test_switch_is_atomic_whole_db(authed):
    """任一步失败 → 整体回滚，库内看不到半版。"""
    before = _active_versions(authed)
    parts_before = authed.get("/api/partitions").json()["items"]

    bad = _switch(authed, changes=[{"domain_code": "orders", "keep_days": 5, "action_on_expire": "乱写"}])
    assert bad.status_code == 400

    assert _active_versions(authed) == before
    assert authed.get("/api/partitions").json()["items"] == parts_before

    jobs = authed.get("/api/policy-switch/jobs").json()["items"]
    assert jobs and jobs[0]["status"] == "失败"


def test_switch_rolls_back_when_midway_check_fails(authed, monkeypatch):
    """同一事务内已写过新版本/重盖分区后失败，也必须整体回滚，不留半版。"""
    from app.services import switch as switch_mod

    before = _active_versions(authed)
    parts_before = authed.get("/api/partitions").json()["items"]

    def boom(conn, target):
        raise switch_mod.SwitchRejected("同源校验失败：模拟分区未跟随")

    monkeypatch.setattr(switch_mod, "_assert_same_source", boom)

    from app.config import Config
    from app.services.switch import SwitchRejected, switch_policies

    try:
        switch_policies(Config.database(), base_versions=before, actor="admin")
    except SwitchRejected as exc:
        assert "同源校验失败" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("应被同源校验拦截")

    # 事务内插入的新版本行与重盖的分区版本都已回滚
    assert _active_versions(authed) == before
    assert authed.get("/api/partitions").json()["items"] == parts_before
    jobs = authed.get("/api/policy-switch/jobs").json()["items"]
    assert jobs and jobs[0]["status"] == "失败"


def test_switch_rejects_unknown_domain_in_changes(authed):
    before = _active_versions(authed)
    r = _switch(authed, changes=[{"domain_code": "nope", "keep_days": 3, "action_on_expire": "归档"}])
    assert r.status_code == 400
    assert "未知主题域" in r.json()["detail"]
    assert _active_versions(authed) == before


def test_switch_rejects_incomplete_base_versions(authed):
    base = _active_versions(authed)
    del base["orders"]
    r = _switch(authed, base=base)
    assert r.status_code == 400
    assert "未覆盖" in r.json()["detail"]


def test_switch_rejects_stale_base_version(authed):
    stale_base = _active_versions(authed)
    stale_base["orders"] -= 1  # 模拟同事拿的是旧页面
    r = _switch(authed, base=stale_base)
    assert r.status_code == 409


def test_old_version_judgment_cannot_be_executed_after_switch(authed):
    """旧到期动作不许对新版分区下手。"""
    authed.post("/api/scan-jobs", json={"as_of_date": "2026-11-16"})
    pending = authed.get("/api/judgments", params={"status": "待执行"}).json()["items"]
    assert pending
    target = pending[0]

    assert _switch(authed).status_code == 200

    r = authed.post(f"/api/judgments/{target['id']}/execute")
    assert r.status_code == 409
    assert "现行" in r.json()["detail"]


def test_landing_stamps_version_and_is_verifiable(authed):
    versions = _active_versions(authed)
    r = authed.post(
        "/api/landing-jobs",
        json={
            "domain_code": "orders",
            "biz_date": "2026-11-16",
            "row_count": 640,
        },
    )
    assert r.status_code == 200, r.text
    job = r.json()["job"]
    assert job["status"] == "成功"
    assert job["policy_version"] == versions["orders"]

    parts = authed.get("/api/partitions", params={"domain_code": "orders"}).json()["items"]
    landed = next(p for p in parts if p["biz_date"] == "2026-11-16")
    assert landed["row_count"] == 640
    assert landed["policy_version"] == versions["orders"]
    assert landed["landed_version"] == versions["orders"]

    # 落库后换版，落库分区也随全库重盖
    assert _switch(authed).status_code == 200
    parts2 = authed.get("/api/partitions", params={"domain_code": "orders"}).json()["items"]
    landed2 = next(p for p in parts2 if p["biz_date"] == "2026-11-16")
    assert landed2["policy_version"] == versions["orders"] + 1
    assert landed2["landed_version"] == versions["orders"]  # 落库戳保留，便于追溯

    # 落库任务可见且可验
    jobs = authed.get("/api/landing-jobs").json()["items"]
    assert jobs and jobs[0]["domain_code"] == "orders"


def test_landing_rejects_version_not_matching_active(authed):
    r = authed.post(
        "/api/landing-jobs",
        json={
            "domain_code": "orders",
            "biz_date": "2026-11-17",
            "row_count": 100,
            "policy_version": 99,
        },
    )
    assert r.status_code == 409


def test_concurrent_switch_only_one_wins(authed):
    """两名同事同时提交整库换版：缺锁则双成功并盖掉对方，有锁则只成功一次。"""
    import threading

    from app.config import Config
    from app.services.switch import switch_policies

    base = _active_versions(authed)
    db_path = Config.database()
    results = []
    barrier = threading.Barrier(2)

    def worker():
        barrier.wait()
        try:
            job = switch_policies(db_path, base_versions=base, actor="admin")
            results.append(("ok", job["id"]))
        except Exception as exc:
            results.append(("blocked", type(exc).__name__))

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    outcomes = [kind for kind, _ in results]
    assert outcomes.count("ok") == 1, outcomes
    assert outcomes.count("blocked") == 1, outcomes

    # 只推进了一版，没有被第二个成功覆盖成两版
    after = _active_versions(authed)
    assert all(after[code] == base[code] + 1 for code in base)

    jobs = authed.get("/api/policy-switch/jobs").json()["items"]
    assert sum(1 for j in jobs if j["status"] == "成功") == 1
    assert any(j["status"] == "被拦截" for j in jobs)


def test_switch_and_execute_cannot_interleave(authed):
    """换版与执行抢同一把写锁：要么执行先完成（旧版合法），要么换版先完成
    （旧裁决被作废后执行被拦），不会出现「旧动作打在新版分区上」。"""
    import threading

    from app.config import Config
    from app.db import DatabaseLocked, get_db
    from app.services.execute import execute_judgment
    from app.services.switch import VersionConflict, switch_policies

    authed.post("/api/scan-jobs", json={"as_of_date": "2026-11-16"})
    pending = authed.get("/api/judgments", params={"status": "待执行"}).json()["items"]
    assert pending
    target = pending[0]
    base = _active_versions(authed)
    db_path = Config.database()

    barrier = threading.Barrier(2)
    results = []

    def do_exec():
        barrier.wait()
        try:
            with get_db(db_path, immediate=True) as conn:
                run = execute_judgment(conn, target["id"])
            results.append(("exec_ok", run["result_state"]))
        except (ValueError, DatabaseLocked) as exc:
            results.append(("exec_blocked", str(exc)))

    def do_switch():
        barrier.wait()
        try:
            switch_policies(db_path, base_versions=base)
            results.append(("switch_ok", None))
        except VersionConflict as exc:
            results.append(("switch_blocked", str(exc)))

    threads = [threading.Thread(target=do_exec), threading.Thread(target=do_switch)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)

    kinds = {k for k, _ in results}
    # 两者互斥地抢同一把写锁，因此终态只有两种合法组合：
    #   1) 执行先拿到锁并完成 → 换版随后照常成功（执行不改变策略版本）
    #   2) 换版先拿到锁并推开版本 → 旧版裁决被作废，执行被拦
    # 非法组合：换版成功却漏出一条仍可执行的旧版裁决（就是「互殴」）。
    if "switch_ok" in kinds and "exec_blocked" in kinds:
        detail = dict(results)["exec_blocked"]
        assert "现行" in detail or "作废" in detail or "锁" in detail, detail
    elif "switch_ok" in kinds and "exec_ok" in kinds:
        pass  # 执行先完成，合法
    else:  # pragma: no cover - 竞态下不应出现
        raise AssertionError(f"非预期的并发结局：{results}")

    # 终态必须同源：分区版本与现行版一致，且无旧版残留待执行裁决
    report = authed.post(
        "/api/reconcile-reports", json={"as_of_date": "2026-11-16"}
    ).json()["report"]
    assert report["version_drift_count"] == 0
    assert report["stale_judgment_count"] == 0


# ── 评审修复回归：以下每条都对应一个曾被证实可触发的缺陷 ──

def test_landing_rejects_invalid_biz_date_and_bad_numbers(authed):
    """脏业务日曾能落库并让后续每次扫描永久 500。"""
    bad_day = authed.post(
        "/api/landing-jobs",
        json={"domain_code": "orders", "biz_date": "not-a-date", "row_count": 1},
    )
    assert bad_day.status_code == 409
    assert "YYYY-MM-DD" in bad_day.json()["detail"]

    bad_rows = authed.post(
        "/api/landing-jobs",
        json={"domain_code": "orders", "biz_date": "2026-11-16", "row_count": "abc"},
    )
    assert bad_rows.status_code == 409

    bad_ver = authed.post(
        "/api/landing-jobs",
        json={
            "domain_code": "orders",
            "biz_date": "2026-11-16",
            "row_count": 10,
            "policy_version": "abc",
        },
    )
    assert bad_ver.status_code == 409

    # 脏数据没有落库，扫描仍能正常跑
    assert authed.post("/api/scan-jobs", json={"as_of_date": "2026-11-16"}).status_code == 200


def test_landing_cannot_resurrect_destroyed_or_archived_partition(authed):
    """曾能把已销毁分区重落成「已销毁 + 非零行数」，破坏分区层面同源。"""
    authed.post("/api/scan-jobs", json={"as_of_date": "2026-11-16"})
    pending = authed.get("/api/judgments", params={"status": "待执行"}).json()["items"]
    destroyed = next(j for j in pending if j["decided_action"] == "销毁")
    assert authed.post(f"/api/judgments/{destroyed['id']}/execute").status_code == 200

    r = authed.post(
        "/api/landing-jobs",
        json={
            "domain_code": destroyed["domain_code"],
            "biz_date": destroyed["biz_date"],
            "row_count": 999,
        },
    )
    assert r.status_code == 409
    assert "不能直接落库复活" in r.json()["detail"]

    part = next(
        p
        for p in authed.get("/api/partitions").json()["items"]
        if p["domain_code"] == destroyed["domain_code"]
        and p["biz_date"] == destroyed["biz_date"]
    )
    assert part["storage_state"] == "已销毁"
    assert part["row_count"] == 0


def test_scan_bad_as_of_date_returns_400_not_500(authed):
    r = authed.post("/api/scan-jobs", json={"as_of_date": "16/11/2026"})
    assert r.status_code == 400


def test_reconcile_flags_partition_without_active_policy(authed):
    """INNER JOIN 曾让「无现行策略的主题域」漂移隐身。"""
    from app.config import Config
    from app.db import get_db

    with get_db(Config.database(), immediate=True) as conn:
        conn.execute(
            "INSERT INTO partitions (domain_code, biz_date, row_count, storage_state, "
            " policy_version, landed_version) "
            "VALUES ('orphan', '2026-11-01', 5, '在线', 1, 1)"
        )
    report = authed.post(
        "/api/reconcile-reports", json={"as_of_date": "2026-11-16"}
    ).json()["report"]
    assert any(d["domain_code"] == "orphan" for d in report["version_drift"])


def test_failed_execution_audit_is_persisted(authed):
    """失败现场曾因调用方整体回滚而永远丢审计。

    用一个会让归档 UPDATE 抛错的连接代理，走真实的失败闭分支。
    """
    from app.config import Config
    from app.db import get_db

    authed.post("/api/scan-jobs", json={"as_of_date": "2026-11-16"})
    pending = authed.get("/api/judgments", params={"status": "待执行"}).json()["items"]
    target = next(j for j in pending if j["decided_action"] == "归档")

    import app.services.execute as exe_mod

    class FailArchiveConn:
        """只把「写已归档」那条 UPDATE 弄失败，其余委托给真连接。"""

        def __init__(self, conn):
            self._conn = conn

        def execute(self, sql, *args, **kwargs):
            if "storage_state = '已归档'" in sql:
                raise RuntimeError("模拟归档写入失败")
            return self._conn.execute(sql, *args, **kwargs)

        def __getattr__(self, name):
            return getattr(self._conn, name)

    with get_db(Config.database(), immediate=True) as conn:
        run = exe_mod.execute_judgment(FailArchiveConn(conn), target["id"])

    assert run["ok"] == 0
    assert "模拟归档写入失败" in run["detail"]

    judgments = authed.get("/api/judgments").json()["items"]
    failed = next(j for j in judgments if j["id"] == target["id"])
    assert failed["status"] == "已失败"
    assert "模拟归档写入失败" in failed["fail_reason"]

    runs = authed.get("/api/runs").json()["items"]
    assert any(r["judgment_id"] == target["id"] and r["ok"] == 0 for r in runs)


# ── 锁竞争：曾冒成 500，修后应为 409「被拦截」 ──

def _hold_write_lock(db_path, seconds):
    """在独立连接里持写锁若干秒，模拟并发写者。"""
    import threading

    from app.db import connect

    ready = threading.Event()
    release = threading.Event()

    def run():
        conn = connect(db_path)
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("UPDATE meta SET value = value WHERE key = 'lab_name'")
        ready.set()
        release.wait(seconds)
        conn.execute("ROLLBACK")
        conn.close()

    t = threading.Thread(target=run, daemon=True)
    t.start()
    ready.wait(5)
    return release, t


def test_reconcile_under_write_contention_returns_409_not_500(authed):
    from app.config import Config

    release, t = _hold_write_lock(Config.database(), 5)
    try:
        r = authed.post("/api/reconcile-reports", json={"as_of_date": "2026-11-16"})
        assert r.status_code == 409, r.text
        assert "被拦截" in r.json()["detail"]
    finally:
        release.set()
        t.join(5)


def test_landing_under_write_contention_returns_409_not_500(authed):
    from app.config import Config

    release, t = _hold_write_lock(Config.database(), 5)
    try:
        r = authed.post(
            "/api/landing-jobs",
            json={"domain_code": "orders", "biz_date": "2026-11-16", "row_count": 10},
        )
        assert r.status_code == 409, r.text
        assert "被拦截" in r.json()["detail"]
    finally:
        release.set()
        t.join(5)


def test_switch_under_read_lock_contention_returns_409_not_500(authed):
    """delete 日志模式下 COMMIT 需独占锁，并发读曾让换版冒 500 并误记「失败」。"""
    import threading
    import time

    from app.config import Config
    from app.db import connect

    db_path = Config.database()
    base = _active_versions(authed)

    ready = threading.Event()
    done = threading.Event()

    def reader():
        conn = connect(db_path)
        conn.execute("BEGIN")
        conn.execute("SELECT COUNT(*) FROM partitions").fetchone()
        ready.set()
        time.sleep(1.2)  # 持续持 SHARED，跨过换版的 300ms 预算
        conn.execute("COMMIT")
        conn.close()
        done.set()

    t = threading.Thread(target=reader, daemon=True)
    t.start()
    ready.wait(5)
    try:
        r = _switch(authed, base=base)
        assert r.status_code == 409, r.text
        assert "被拦截" in r.json()["detail"]
    finally:
        done.wait(5)
        t.join(5)

    jobs = authed.get("/api/policy-switch/jobs").json()["items"]
    # 被拦截，而不是「失败：database is locked」
    assert jobs[0]["status"] == "被拦截"
    assert _active_versions(authed) == base


def test_migration_backfills_once_and_preserves_landed_audit(tmp_path):
    """旧库升级要补齐版本列；但重复启动不得抹掉换版后的 landed_version 审计。"""
    import sqlite3

    from app.db import init_db

    db = tmp_path / "old.db"
    c = sqlite3.connect(db)
    c.executescript(
        """
        CREATE TABLE retain_policies (id INTEGER PRIMARY KEY AUTOINCREMENT,
          domain_code TEXT NOT NULL, domain_name TEXT NOT NULL, keep_days INTEGER NOT NULL,
          action_on_expire TEXT NOT NULL, version INTEGER NOT NULL DEFAULT 1,
          active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL DEFAULT (datetime('now')),
          UNIQUE(domain_code, version));
        CREATE TABLE partitions (id INTEGER PRIMARY KEY AUTOINCREMENT,
          domain_code TEXT NOT NULL, biz_date TEXT NOT NULL, row_count INTEGER NOT NULL DEFAULT 0,
          storage_state TEXT NOT NULL, archive_uri TEXT,
          created_at TEXT NOT NULL DEFAULT (datetime('now')), UNIQUE(domain_code, biz_date));
        INSERT INTO retain_policies (domain_code,domain_name,keep_days,action_on_expire,version,active)
          VALUES ('orders','订单明细域',7,'归档',3,1);
        INSERT INTO partitions (domain_code,biz_date,row_count,storage_state)
          VALUES ('orders','2026-11-01',10,'在线');
        """
    )
    c.commit()
    c.close()

    init_db(str(db))
    # 首启：按现行 v3 回填
    c = sqlite3.connect(db)
    c.row_factory = sqlite3.Row
    row = c.execute("SELECT policy_version, landed_version FROM partitions").fetchone()
    assert (row["policy_version"], row["landed_version"]) == (3, 3)
    # 模拟换版：分区版本推到 4，落库戳保留 3
    c.execute("UPDATE partitions SET policy_version = 4")
    c.commit()
    c.close()

    # 再启：schema_version 已打戳，不得把 landed_version 抹成 4
    init_db(str(db))
    c = sqlite3.connect(db)
    c.row_factory = sqlite3.Row
    row = c.execute("SELECT policy_version, landed_version FROM partitions").fetchone()
    assert row["policy_version"] == 4
    assert row["landed_version"] == 3
    c.close()


def test_scan_survives_poison_partition_row(authed):
    """一条脏业务日不应让整轮扫描永久 500。"""
    from app.config import Config
    from app.db import get_db

    with get_db(Config.database(), immediate=True) as conn:
        conn.execute(
            "INSERT INTO partitions (domain_code, biz_date, row_count, storage_state, "
            " policy_version, landed_version) "
            "VALUES ('orders', 'not-a-date', 5, '在线', 1, 1)"
        )
    r = authed.post("/api/scan-jobs", json={"as_of_date": "2026-11-16"})
    assert r.status_code == 200
    assert "业务日非法已跳过" in r.json()["job"]["message"]
