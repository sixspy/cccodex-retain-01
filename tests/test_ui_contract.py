"""页面契约：三处同改的关键字段必须真的接到接口上。

没有浏览器可用，这里对前端源码做结构化断言：换版后要展示的版本号/成败
在策略列表、分区详情、扫描队列三处都取到了后端字段，避免「后端改了、
页面还是旧列」。
"""

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APP_JS = (ROOT / "static" / "js" / "app.js").read_text(encoding="utf-8")
INDEX = (ROOT / "static" / "index.html").read_text(encoding="utf-8")


def test_nav_exposes_switch_and_landing_views():
    for view in ("policies", "switch", "landing", "partitions", "scan", "judgments", "reconcile"):
        assert f'data-view="{view}"' in INDEX
    # 页面固定展示本台所选换版路径
    assert "换版：整库原子切换" in INDEX


def test_policies_view_shows_active_version():
    block = APP_JS.split("async function renderPolicies")[1].split("async function renderSwitch")[0]
    assert "p.version" in block


def test_partitions_view_shows_partition_version_and_drift():
    block = APP_JS.split("async function renderPartitions")[1].split("function parseSnapshot")[0]
    assert "p.policy_version" in block
    assert "p.landed_version" in block
    assert "driftTag(p.policy_version, map[p.domain_code])" in block


def test_scan_view_shows_policy_snapshot():
    block = APP_JS.split("async function renderScan")[1].split("async function renderJudgments")[0]
    assert "j.policy_snapshot" in block
    assert "parseSnapshot" in block


def test_switch_view_posts_base_versions():
    block = APP_JS.split("async function renderSwitch")[1].split("async function renderLanding")[0]
    assert "/api/policy-switch" in block
    assert "base_versions" in block


def test_landing_view_posts_and_lists_version_stamp():
    block = APP_JS.split("async function renderLanding")[1].split("async function renderPartitions")[0]
    assert '"/api/landing-jobs"' in block
    assert "j.policy_version" in block
