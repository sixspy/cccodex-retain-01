(() => {
  const main = document.getElementById("main");
  let view = "policies";

  async function api(path, opts) {
    const res = await fetch(path, {
      headers: { "Content-Type": "application/json" },
      ...opts,
    });
    if (res.status === 401) {
      location.href = "/login";
      throw new Error("未登录");
    }
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      throw new Error(data.detail || data.error || "请求失败");
    }
    return data;
  }

  function esc(value) {
    return String(value == null ? "" : value).replace(/[&<>"']/g, (c) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    })[c]);
  }

  function toast(message, kind) {
    const el = document.getElementById("toast");
    el.textContent = message;
    el.className = `toast ${kind || ""}`;
    el.hidden = false;
    clearTimeout(toast.timer);
    toast.timer = setTimeout(() => { el.hidden = true; }, 4200);
  }

  function tag(state) {
    const cls =
      state === "在线" || state === "待执行" || state === "成功"
        ? "warn"
        : state === "已完成" || state === "已归档" || state === "已销毁" || state === "已重落"
          ? "ok"
          : state === "已失败" || state === "失败"
            ? "bad"
            : state === "被拦截" || state === "已作废"
              ? "muted"
              : "";
    return `<span class="tag ${cls}">${esc(state)}</span>`;
  }

  function driftTag(partitionVersion, activeVersion) {
    if (partitionVersion === activeVersion) return `<span class="tag ok">同源</span>`;
    return `<span class="tag bad" title="分区版本与现行版不一致">偏 v${esc(partitionVersion)}≠v${esc(activeVersion)}</span>`;
  }

  async function activeVersionMap() {
    const data = await api("/api/policies");
    const map = {};
    data.items.forEach((p) => { map[p.domain_code] = p.version; });
    return { map, items: data.items };
  }

  async function renderPolicies() {
    const data = await api("/api/policies");
    main.innerHTML = `
      <section class="panel">
        <h2>现行留存策略</h2>
        <p class="sub">每个主题域一条现行版本：保留天数与到期动作（归档或销毁）。换版为整库原子切换，全台同一时刻只有一套现行版本。</p>
        <table>
          <thead><tr><th>主题域</th><th>编码</th><th>保留天</th><th>到期动作</th><th>现行版本</th></tr></thead>
          <tbody>
            ${data.items.map((p) => `<tr>
              <td>${esc(p.domain_name)}</td>
              <td class="mono">${esc(p.domain_code)}</td>
              <td>${esc(p.keep_days)}</td>
              <td>${tag(p.action_on_expire)}</td>
              <td class="mono strong">v${esc(p.version)}</td>
            </tr>`).join("")}
          </tbody>
        </table>
      </section>`;
  }

  async function renderSwitch() {
    const pre = await api("/api/policy-switch/preflight");
    const jobs = await api("/api/policy-switch/jobs");
    const base = {};
    pre.domains.forEach((d) => { base[d.domain_code] = d.version; });

    main.innerHTML = `
      <section class="panel">
        <h2>整库原子切换</h2>
        <p class="sub">一次提交切换全库所有主题域：升版本 → 重盖分区版本 → 作废旧版未完成裁决。任一步不同源即整体回滚。可留空表示沿用现值换版。</p>
        <table>
          <thead><tr><th>主题域</th><th>编码</th><th>现行版本</th><th>保留天</th><th>到期动作</th><th>分区数</th><th>待作废裁决</th><th>同源</th></tr></thead>
          <tbody>
            ${pre.domains.map((d) => `<tr>
              <td>${esc(d.domain_name)}</td>
              <td class="mono">${esc(d.domain_code)}</td>
              <td class="mono strong">v${esc(d.version)}</td>
              <td><input class="mini" data-keep="${esc(d.domain_code)}" value="${esc(d.keep_days)}" /></td>
              <td>
                <select class="mini" data-action="${esc(d.domain_code)}">
                  <option value="归档" ${d.action_on_expire === "归档" ? "selected" : ""}>归档</option>
                  <option value="销毁" ${d.action_on_expire === "销毁" ? "selected" : ""}>销毁</option>
                </select>
              </td>
              <td>${esc(d.partition_count)}</td>
              <td>${esc(d.pending_judgment_count)}</td>
              <td>${d.version_drift_count === 0
                ? `<span class="tag ok">同源</span>`
                : `<span class="tag bad">${esc(d.version_drift_count)} 个分区漂移</span>`}</td>
            </tr>`).join("")}
          </tbody>
        </table>
        <div class="actions">
          <button type="button" id="btn-switch">整库换版</button>
          <span class="sub" id="switch-hint">基线版本：${pre.domains.map((d) => `${esc(d.domain_code)} v${esc(d.version)}`).join(" · ")}</span>
        </div>
      </section>
      <section class="panel">
        <h2>换版任务</h2>
        <table>
          <thead><tr><th>编号</th><th>状态</th><th>基准版本</th><th>目标版本</th><th>重盖分区</th><th>作废裁决</th><th>说明</th><th>时间</th></tr></thead>
          <tbody>
            ${jobs.items.length ? jobs.items.map((j) => `<tr>
              <td>${esc(j.id)}</td>
              <td>${tag(j.status)}</td>
              <td class="mono">${esc(j.base_versions)}</td>
              <td class="mono">${esc(j.target_versions)}</td>
              <td>${esc(j.partitions_rebased)}</td>
              <td>${esc(j.judgments_voided)}</td>
              <td>${esc(j.message)}</td>
              <td class="mono">${esc(j.finished_at || j.created_at)}</td>
            </tr>`).join("") : `<tr><td colspan="8" class="empty">尚无换版任务</td></tr>`}
          </tbody>
        </table>
      </section>`;

    document.getElementById("btn-switch").onclick = async () => {
      const changes = pre.domains.map((d) => ({
        domain_code: d.domain_code,
        keep_days: Number(document.querySelector(`[data-keep="${d.domain_code}"]`).value),
        action_on_expire: document.querySelector(`[data-action="${d.domain_code}"]`).value,
      }));
      try {
        const r = await api("/api/policy-switch", {
          method: "POST",
          body: JSON.stringify({ base_versions: base, changes }),
        });
        toast(`换版成功：${r.job.message}`, "ok");
      } catch (err) {
        toast(err.message, "bad");
      }
      await renderSwitch();
    };
  }

  async function renderLanding() {
    const { map, items } = await activeVersionMap();
    const jobs = await api("/api/landing-jobs");
    main.innerHTML = `
      <div class="toolbar">
        <label class="field">主题域
          <select id="land-domain">
            ${items.map((p) => `<option value="${esc(p.domain_code)}">${esc(p.domain_name)} · v${esc(p.version)}</option>`).join("")}
          </select>
        </label>
        <label class="field">业务日<input id="land-date" value="2026-11-16" /></label>
        <label class="field">行数<input id="land-rows" value="1000" /></label>
        <button type="button" id="btn-land">入库落库</button>
      </div>
      <section class="panel">
        <h2>入库落库任务</h2>
        <p class="sub">落库即盖上当时的现行策略版本戳；换版后存量分区随全库重盖，落库戳保留以便追溯。</p>
        <table>
          <thead><tr><th>编号</th><th>主题域</th><th>业务日</th><th>行数</th><th>落库版本</th><th>状态</th><th>说明</th><th>时间</th></tr></thead>
          <tbody>
            ${jobs.items.length ? jobs.items.map((j) => `<tr>
              <td>${esc(j.id)}</td>
              <td class="mono">${esc(j.domain_code)}</td>
              <td class="mono">${esc(j.biz_date)}</td>
              <td>${esc(j.row_count)}</td>
              <td class="mono strong">v${esc(j.policy_version)}</td>
              <td>${tag(j.status)}</td>
              <td>${esc(j.message)}</td>
              <td class="mono">${esc(j.created_at)}</td>
            </tr>`).join("") : `<tr><td colspan="8" class="empty">尚无落库任务</td></tr>`}
          </tbody>
        </table>
      </section>`;
    document.getElementById("btn-land").onclick = async () => {
      try {
        const r = await api("/api/landing-jobs", {
          method: "POST",
          body: JSON.stringify({
            domain_code: document.getElementById("land-domain").value,
            biz_date: document.getElementById("land-date").value.trim(),
            row_count: Number(document.getElementById("land-rows").value),
          }),
        });
        toast(`${r.job.status}：${r.job.message}`, "ok");
      } catch (err) {
        toast(err.message, "bad");
      }
      await renderLanding();
    };
  }

  async function renderPartitions() {
    const [{ map }, data] = await Promise.all([activeVersionMap(), api("/api/partitions")]);
    main.innerHTML = `
      <section class="panel">
        <h2>实物分区</h2>
        <p class="sub">在线 / 已归档 / 已销毁 / 裁撤中。分区版本与现行版本同源；「落库版本」记录写入当时的版本。</p>
        <table>
          <thead><tr><th>主题域</th><th>业务日</th><th>行数</th><th>存储态</th><th>分区版本</th><th>同源</th><th>落库版本</th><th>归档地址</th></tr></thead>
          <tbody>
            ${data.items.map((p) => `<tr>
              <td class="mono">${esc(p.domain_code)}</td>
              <td class="mono">${esc(p.biz_date)}</td>
              <td>${esc(p.row_count)}</td>
              <td>${tag(p.storage_state)}</td>
              <td class="mono">v${esc(p.policy_version)}</td>
              <td>${driftTag(p.policy_version, map[p.domain_code])}</td>
              <td class="mono">v${esc(p.landed_version)}</td>
              <td class="mono">${esc(p.archive_uri || "—")}</td>
            </tr>`).join("")}
          </tbody>
        </table>
      </section>`;
  }

  function parseSnapshot(raw) {
    try {
      const obj = JSON.parse(raw || "{}");
      return Object.keys(obj).map((k) => `${k} v${obj[k]}`).join(" · ") || "—";
    } catch (e) {
      return "—";
    }
  }

  async function renderScan() {
    const jobs = await api("/api/scan-jobs");
    const { map } = await activeVersionMap();
    main.innerHTML = `
      <div class="toolbar">
        <label class="field">基准日
          <input id="as-of" value="2026-11-16" />
        </label>
        <button type="button" id="btn-scan">跑到期扫描</button>
      </div>
      <section class="panel">
        <h2>扫描任务</h2>
        <p class="sub">扫描开头一次性锁定现行策略快照，整轮只认这一份；任务记录快照版本，与扫描时的分区版本对齐。</p>
        <table>
          <thead><tr><th>编号</th><th>基准日</th><th>状态</th><th>到期数</th><th>策略快照</th><th>说明</th></tr></thead>
          <tbody>
            ${jobs.items.length ? jobs.items.map((j) => `<tr>
              <td>${esc(j.id)}</td>
              <td class="mono">${esc(j.as_of_date)}</td>
              <td>${tag(j.status)}</td>
              <td>${esc(j.expired_count)}</td>
              <td class="mono">${esc(parseSnapshot(j.policy_snapshot))}</td>
              <td>${esc(j.message)}</td>
            </tr>`).join("") : `<tr><td colspan="6" class="empty">尚无扫描任务</td></tr>`}
          </tbody>
        </table>
      </section>`;
    document.getElementById("btn-scan").onclick = async () => {
      const as_of_date = document.getElementById("as-of").value.trim();
      try {
        const r = await api("/api/scan-jobs", {
          method: "POST",
          body: JSON.stringify({ as_of_date }),
        });
        toast(`扫描完成：${r.job.message}`, "ok");
      } catch (err) {
        toast(err.message, "bad");
      }
      await renderScan();
    };
  }

  async function renderJudgments() {
    const data = await api("/api/judgments");
    const { map } = await activeVersionMap();
    main.innerHTML = `
      <section class="panel">
        <h2>裁决清单</h2>
        <p class="sub">待执行可点「执行」。裁决版本必须与现行版本同源，旧版裁决会被明确拦下。</p>
        <table>
          <thead><tr><th>编号</th><th>主题域</th><th>业务日</th><th>裁决版本</th><th>现行</th><th>保留天</th><th>动作</th><th>状态</th><th>操作</th></tr></thead>
          <tbody>
            ${data.items.length ? data.items.map((j) => `<tr>
              <td>${esc(j.id)}</td>
              <td class="mono">${esc(j.domain_code)}</td>
              <td class="mono">${esc(j.biz_date)}</td>
              <td class="mono">v${esc(j.policy_version)}</td>
              <td class="mono">v${esc(map[j.domain_code] ?? "?")}</td>
              <td>${esc(j.keep_days)}天</td>
              <td>${esc(j.decided_action)}</td>
              <td>${tag(j.status)}${j.fail_reason ? `<div class="fail">${esc(j.fail_reason)}</div>` : ""}</td>
              <td>${j.status === "待执行" || j.status === "已失败"
                ? `<button type="button" data-exec="${esc(j.id)}">执行</button>` : "—"}</td>
            </tr>`).join("") : `<tr><td colspan="9" class="empty">尚无裁决，请先跑到期扫描</td></tr>`}
          </tbody>
        </table>
      </section>`;
    main.querySelectorAll("[data-exec]").forEach((btn) => {
      btn.onclick = async () => {
        try {
          const r = await api(`/api/judgments/${btn.dataset.exec}/execute`, { method: "POST" });
          toast(`执行完成：${r.run.result_state}`, "ok");
        } catch (err) {
          toast(err.message, "bad");
        }
        await renderJudgments();
      };
    });
  }

  async function renderReconcile() {
    const reports = await api("/api/reconcile-reports");
    main.innerHTML = `
      <div class="toolbar">
        <label class="field">基准日
          <input id="as-of-r" value="2026-11-16" />
        </label>
        <button type="button" id="btn-rec">生成对账报告</button>
      </div>
      <section class="panel">
        <h2>对账报告</h2>
        <p class="sub">除存储态错位外，额外核对版本漂移（分区版本≠现行版本）与旧版残留裁决。</p>
        <table>
          <thead><tr><th>编号</th><th>基准日</th><th>在线</th><th>归档</th><th>销毁</th><th>待裁决</th><th>错位</th><th>版本漂移</th><th>旧版残留裁决</th><th>摘要</th></tr></thead>
          <tbody>
            ${reports.items.length ? reports.items.map((r) => `<tr>
              <td>${esc(r.id)}</td>
              <td class="mono">${esc(r.as_of_date)}</td>
              <td>${esc(r.online_count)}</td>
              <td>${esc(r.archived_count)}</td>
              <td>${esc(r.destroyed_count)}</td>
              <td>${esc(r.pending_judgment_count)}</td>
              <td>${esc(r.mismatch_count)}</td>
              <td class="${r.version_drift_count ? "bad-cell" : ""}">${esc(r.version_drift_count)}</td>
              <td class="${r.stale_judgment_count ? "bad-cell" : ""}">${esc(r.stale_judgment_count)}</td>
              <td>${esc(r.summary)}</td>
            </tr>`).join("") : `<tr><td colspan="10" class="empty">尚无对账报告</td></tr>`}
          </tbody>
        </table>
      </section>`;
    document.getElementById("btn-rec").onclick = async () => {
      const as_of_date = document.getElementById("as-of-r").value.trim();
      try {
        const r = await api("/api/reconcile-reports", {
          method: "POST",
          body: JSON.stringify({ as_of_date }),
        });
        const rep = r.report;
        toast(
          rep.version_drift_count || rep.stale_judgment_count
            ? `对账完成：版本漂移 ${rep.version_drift_count}，旧版残留裁决 ${rep.stale_judgment_count}`
            : "对账完成：版本与存储态全部同源",
          rep.version_drift_count || rep.stale_judgment_count ? "bad" : "ok",
        );
      } catch (err) {
        toast(err.message, "bad");
      }
      await renderReconcile();
    };
  }

  const renderers = {
    policies: renderPolicies,
    switch: renderSwitch,
    landing: renderLanding,
    partitions: renderPartitions,
    scan: renderScan,
    judgments: renderJudgments,
    reconcile: renderReconcile,
  };

  async function show(name) {
    view = name;
    document.querySelectorAll("#nav button").forEach((b) => {
      b.classList.toggle("active", b.dataset.view === name);
    });
    await renderers[name]();
  }

  document.getElementById("nav").addEventListener("click", (e) => {
    const btn = e.target.closest("button[data-view]");
    if (btn) show(btn.dataset.view);
  });

  document.getElementById("btn-logout").onclick = async () => {
    await api("/api/logout", { method: "POST" });
    location.href = "/login";
  };

  api("/api/meta").then((m) => {
    document.getElementById("lab-title").textContent = m.lab_name;
    document.getElementById("lab-sub").textContent = m.skeleton;
    if (m.switch_mode) {
      document.getElementById("switch-mode").textContent = `换版：${m.switch_mode}`;
    }
  });

  show(view);
})();
