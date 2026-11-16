from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from itsdangerous import BadSignature, URLSafeSerializer

from app.config import Config
from app.db import DatabaseLocked, get_db, init_db
from app.services import (
    LandingRejected,
    SwitchRejected,
    VersionConflict,
    execute_judgment,
    land_partition,
    list_judgments,
    list_landing_jobs,
    list_partitions,
    list_policies,
    list_reports,
    list_runs,
    list_scan_jobs,
    list_switch_jobs,
    preflight,
    run_expire_scan,
    run_reconcile,
    switch_policies,
)
from app.services.scan import SCAN_LOCK_WAIT_MS

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "static"
serializer = URLSafeSerializer(Config.SECRET, salt="cc-retain-session")

app = FastAPI(title="cc-retain-lab")
app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")


def _read_session(request: Request) -> dict:
    raw = request.cookies.get("retain_session")
    if not raw:
        return {}
    try:
        data = serializer.loads(raw)
        return data if isinstance(data, dict) else {}
    except BadSignature:
        return {}


def require_user(request: Request) -> str:
    user = _read_session(request).get("user")
    if user != Config.ADMIN_USER:
        raise HTTPException(status_code=401, detail="未登录")
    return user


@app.on_event("startup")
def _startup() -> None:
    init_db()


@app.get("/")
def root(request: Request):
    if _read_session(request).get("user") == Config.ADMIN_USER:
        return RedirectResponse("/app/", status_code=302)
    return RedirectResponse("/login", status_code=302)


@app.get("/login")
def login_page():
    return FileResponse(STATIC / "login.html")


@app.get("/app/")
def spa_page(request: Request):
    if _read_session(request).get("user") != Config.ADMIN_USER:
        return RedirectResponse("/login", status_code=302)
    return FileResponse(STATIC / "index.html")


@app.post("/api/login")
async def api_login(request: Request):
    body = await request.json()
    user = str(body.get("username", "")).strip()
    pwd = str(body.get("password", ""))
    if user != Config.ADMIN_USER or pwd != Config.ADMIN_PASSWORD:
        return JSONResponse({"ok": False, "error": "账号或密码错误"}, status_code=400)
    resp = JSONResponse({"ok": True, "user": user})
    resp.set_cookie(
        "retain_session",
        serializer.dumps({"user": user}),
        httponly=True,
        samesite="lax",
    )
    return resp


@app.post("/api/logout")
def api_logout():
    resp = JSONResponse({"ok": True})
    resp.delete_cookie("retain_session")
    return resp


@app.get("/api/meta")
def api_meta(request: Request):
    require_user(request)
    return {
        "lab_name": Config.LAB_NAME,
        "lab_code": Config.LAB_CODE,
        "skeleton": "分区留存策略版本→入库落库→到期扫描裁决→归档或销毁执行→策略与实物分区对账",
        "switch_mode": "整库原子切换",
    }


@app.get("/api/policies")
def api_policies(request: Request):
    require_user(request)
    with get_db() as conn:
        return {"items": list_policies(conn, active_only=True)}


@app.get("/api/partitions")
def api_partitions(request: Request, domain_code: str | None = None):
    require_user(request)
    with get_db() as conn:
        return {"items": list_partitions(conn, domain_code=domain_code)}


@app.get("/api/scan-jobs")
def api_scan_jobs(request: Request):
    require_user(request)
    with get_db() as conn:
        return {"items": list_scan_jobs(conn)}


@app.post("/api/scan-jobs")
async def api_create_scan(request: Request):
    require_user(request)
    body = await request.json()
    as_of = str(body.get("as_of_date", "")).strip()
    if not as_of:
        raise HTTPException(status_code=400, detail="缺少 as_of_date")
    # 扫描与换版互斥：BEGIN IMMEDIATE 让二者整段串行，扫描不会跨在换版
    # 前后读到两套策略版本。
    try:
        with get_db(immediate=True, busy_timeout_ms=SCAN_LOCK_WAIT_MS) as conn:
            job = run_expire_scan(conn, as_of)
    except DatabaseLocked as exc:
        raise HTTPException(status_code=409, detail=f"扫描被拦截：{exc}") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, "job": job}


@app.get("/api/judgments")
def api_judgments(request: Request, status: str | None = None):
    require_user(request)
    with get_db() as conn:
        return {"items": list_judgments(conn, status=status)}


@app.post("/api/judgments/{judgment_id}/execute")
def api_execute(request: Request, judgment_id: int):
    require_user(request)
    try:
        # 与换版/扫描同一把写锁：读版本→写分区的整段串行，避免读到旧版
        # 之后才升级写锁，撞上并发换版。
        with get_db(immediate=True) as conn:
            run = execute_judgment(conn, judgment_id)
    except DatabaseLocked as exc:
        raise HTTPException(status_code=409, detail=f"执行被拦截：{exc}") from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    # 失败现场已在事务内提交（run.ok=0），对外报错但不丢审计。
    if not run or not run["ok"]:
        reason = (run or {}).get("detail") or "执行失败"
        raise HTTPException(status_code=409, detail=f"执行失败：{reason}")
    return {"ok": True, "run": run}


@app.get("/api/runs")
def api_runs(request: Request):
    require_user(request)
    with get_db() as conn:
        return {"items": list_runs(conn)}


@app.get("/api/reconcile-reports")
def api_reports(request: Request):
    require_user(request)
    with get_db() as conn:
        return {"items": list_reports(conn)}


@app.post("/api/reconcile-reports")
async def api_reconcile(request: Request):
    require_user(request)
    body = await request.json()
    as_of = str(body.get("as_of_date", "")).strip()
    if not as_of:
        raise HTTPException(status_code=400, detail="缺少 as_of_date")
    try:
        # 对账要读多张表再写报告；用 IMMEDIATE 先拿写锁，避免并发换版
        # 卡在 SHARED→RESERVED 升级上直接报锁死。
        with get_db(immediate=True) as conn:
            report = run_reconcile(conn, as_of)
    except DatabaseLocked as exc:
        raise HTTPException(status_code=409, detail=f"对账被拦截：{exc}") from exc
    return {"ok": True, "report": report}


@app.get("/api/policy-switch/preflight")
def api_switch_preflight(request: Request):
    require_user(request)
    return preflight()


@app.post("/api/policy-switch")
async def api_switch(request: Request):
    """整库原子换版：一次请求切换全库所有主题域。"""
    actor = require_user(request)
    body = await request.json()
    base_versions = body.get("base_versions")
    if not isinstance(base_versions, dict) or not base_versions:
        raise HTTPException(status_code=400, detail="缺少 base_versions（须覆盖全部主题域）")
    changes = body.get("changes") or []
    if not isinstance(changes, list):
        raise HTTPException(status_code=400, detail="changes 必须是数组")
    try:
        job = switch_policies(
            None, base_versions=base_versions, changes=changes, actor=actor
        )
    except VersionConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except SwitchRejected as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, "job": job}


@app.get("/api/policy-switch/jobs")
def api_switch_jobs(request: Request):
    require_user(request)
    with get_db() as conn:
        return {"items": list_switch_jobs(conn)}


@app.get("/api/landing-jobs")
def api_landing_jobs(request: Request):
    require_user(request)
    with get_db() as conn:
        return {"items": list_landing_jobs(conn)}


@app.post("/api/landing-jobs")
async def api_land(request: Request):
    require_user(request)
    body = await request.json()
    try:
        with get_db(db_path=None, immediate=True) as conn:
            job = land_partition(
                conn,
                domain_code=body.get("domain_code", ""),
                biz_date=body.get("biz_date", ""),
                row_count=body.get("row_count", 0),
                policy_version=body.get("policy_version"),
                actor=body.get("actor", "admin"),
            )
    except LandingRejected as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except DatabaseLocked as exc:
        raise HTTPException(status_code=409, detail=f"落库被拦截：{exc}") from exc
    return {"ok": True, "job": job}
