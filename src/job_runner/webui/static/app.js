/* Job Runner web UI — Dashboard, Find jobs, Score, Results, Settings */

const $ = (sel, root = document) => root.querySelector(sel);

/** Last pipeline subprocess started from this UI (discover / score); used by Stop. */
let activePipelineTaskId = null;
let applyCostBaselineUsd = null;
let applyCostLastPollMs = 0;
const APPLY_PREFS_KEY = "jr_apply_prefs_v1";

async function handleApiResponse401(r) {
  if (r.status !== 401) return;
  const sr = await fetch("/api/session", { credentials: "same-origin" });
  const sj = await sr.json().catch(() => ({}));
  if (sj.auth_enabled) window.location.href = "/login.html";
}

function api(path, opts = {}) {
  return fetch("/api" + path, {
    credentials: "same-origin",
    headers: { "Content-Type": "application/json", ...opts.headers },
    ...opts,
  }).then(async (r) => {
    await handleApiResponse401(r);
    return r;
  });
}

async function ensureUiSession() {
  const r = await fetch("/api/session", { credentials: "same-origin" });
  const j = await r.json().catch(() => ({}));
  if (j.auth_enabled && !j.authenticated) {
    window.location.href = "/login.html";
    return false;
  }
  const lo = document.getElementById("btn-logout");
  if (lo) lo.hidden = !j.auth_enabled;
  return true;
}

function terminalSet(text) {
  const el = $("#terminal-output");
  if (!el) return;
  el.textContent = text == null ? "" : String(text);
  el.scrollTop = el.scrollHeight;
}

function terminalAppend(text) {
  const el = $("#terminal-output");
  if (!el) return;
  const add = String(text);
  el.textContent = (el.textContent ? el.textContent + (el.textContent.endsWith("\n") ? "" : "\n") : "") + add;
  el.scrollTop = el.scrollHeight;
}

async function refreshUsage() {
  try {
    const r = await api("/usage");
    const u = await r.json();
    const v = $("#cost-value");
    const m = $("#cost-meta");
    if (v) v.textContent = "$" + Number(u.current_month_estimated_usd ?? u.total_estimated_usd ?? 0).toFixed(2);
    if (m) {
      const calls = u.current_month_llm_calls ?? u.llm_calls ?? 0;
      const tin = u.current_month_input_tokens ?? u.total_input_tokens ?? 0;
      const tout = u.current_month_output_tokens ?? u.total_output_tokens ?? 0;
      const mm = u.month_key || "";
      m.textContent = ` · ${mm} · ${calls} LLM calls · ${(tin + tout).toLocaleString()} tok`;
    }
  } catch (_) {}
}

async function getUsageSummary() {
  const r = await api("/usage");
  return r.json();
}

async function repairJobSpy() {
  terminalAppend("\n── " + new Date().toLocaleString() + " ──\n");
  terminalAppend("Reinstalling python-jobspy (localhost repair)…\n");
  try {
    const r = await api("/deps/repair-jobspy", { method: "POST", body: "{}" });
    const j = await r.json().catch(() => ({}));
    if (j.log) terminalAppend(j.log);
    if (j.hint) terminalAppend("\n" + j.hint + "\n");
    if (!r.ok) terminalAppend("\n" + (apiDetailMessage(j) || "Request failed") + "\n");
    else if (!j.ok) terminalAppend("\nRepair finished with errors (see log above).\n");
    else terminalAppend("\nJobSpy repair OK. Try discover again; restart the server if it still fails.\n");
  } catch (e) {
    terminalAppend(String(e.message || e) + "\n");
  }
}

function toggleTerminalMinimized() {
  const dock = $("#terminal-dock");
  const btn = $("#btn-terminal-toggle");
  if (!dock) return;
  dock.classList.toggle("terminal-minimized");
  document.body.classList.toggle("terminal-minimized");
  if (btn) btn.textContent = dock.classList.contains("terminal-minimized") ? "Show" : "Hide";
}

function showSection(id) {
  document.querySelectorAll(".section").forEach((el) => el.classList.remove("active"));
  document.querySelectorAll(".nav-btn").forEach((el) => el.classList.remove("active"));
  const sec = document.getElementById("section-" + id);
  if (sec) sec.classList.add("active");
  const nav = document.querySelector(`.nav-btn[data-section="${id}"]`);
  if (nav) nav.classList.add("active");
  const loaders = {
    dashboard: loadDashboard,
    find: loadFind,
    score: loadScore,
    results: loadResults,
    settings: loadSettings,
  };
  if (loaders[id]) loaders[id]();
  refreshUsage();
}

async function loadMeta() {
  try {
    const r = await api("/meta");
    const j = await r.json();
    const el = $("#app-version");
    if (el) el.textContent = "v" + (j.version || "?") + " · Tier " + (j.tier ?? "?");
  } catch (_) {}
  refreshUsage();
}

async function loadDashboard() {
  const status = $("#dash-status");
  if (status) status.textContent = "Loading…";
  try {
    const r = await api("/dashboard");
    const s = await r.json();
    const grid = $("#dash-stats");
    if (!grid) return;
    const items = [
      ["Total jobs", s.total],
      ["Scored", s.scored, "ok"],
      ["Strong fit (7+)", s.high_fit, "ok"],
      ["Tailored", s.tailored],
      ["Applied", s.applied],
    ];
    grid.innerHTML = items
      .map(([label, val, cls]) => {
        const c = cls ? ` ${cls}` : "";
        return `<div class="stat-card"><div class="label">${esc(label)}</div><div class="value${c}">${esc(
          String(val ?? "—"),
        )}</div></div>`;
      })
      .join("");
    if (status) status.textContent = "Updated " + new Date().toLocaleString();
  } catch (e) {
    if (status) status.textContent = String(e.message || e);
  }
}

function resumeSelectOptionsHtml(files, selected) {
  const opts = ['<option value="">— None —</option>'];
  const seen = new Set();
  for (const f of files || []) {
    const fn = f.filename || "";
    if (!fn) continue;
    seen.add(fn);
    const sel = selected === fn ? " selected" : "";
    opts.push(`<option value="${escAttr(fn)}"${sel}>${esc(fn)}</option>`);
  }
  if (selected && !seen.has(selected)) {
    opts.push(`<option value="${escAttr(selected)}" selected>${esc(selected)}</option>`);
  }
  return opts.join("");
}

function renderFindSlots(search_slots, files) {
  const tbody = $("#find-slots-tbody");
  if (!tbody) return;
  const rows = Array.isArray(search_slots) && search_slots.length ? search_slots : [];
  const parts = [];
  for (let i = 0; i < 10; i++) {
    const row = rows[i] || {};
    const q = row.query || "";
    const rf = row.resume_filename || "";
    const selHtml = resumeSelectOptionsHtml(files, rf);
    parts.push(
      `<tr>
        <td class="find-slots-num">${i + 1}</td>
        <td class="find-slots-titles-cell">
          <input type="text" class="find-slot-title" data-slot="${i}" value="${escAttr(q)}" spellcheck="false" placeholder="Main job title…" />
          <label class="find-slot-subs-label">Additional search titles <span class="find-hint">(one per line)</span></label>
          <textarea class="find-slot-subs" data-slot="${i}" rows="3" spellcheck="false" placeholder="e.g. Technical PM"></textarea>
        </td>
        <td class="find-slots-resume-cell">
          <select class="find-slot-resume" data-slot="${i}">${selHtml}</select>
          <label class="find-slot-upload-label btn btn-ghost btn-tiny">Upload
            <input type="file" class="find-slot-file" data-slot="${i}" accept=".pdf,.txt,.doc,.docx" />
          </label>
        </td>
      </tr>`,
    );
  }
  tbody.innerHTML = parts.join("");
  for (let i = 0; i < 10; i++) {
    const row = rows[i] || {};
    const ta = document.querySelector(`.find-slot-subs[data-slot="${i}"]`);
    if (ta) ta.value = row.sub_titles || "";
  }
}

function collectSearchSlots() {
  const out = [];
  for (let i = 0; i < 10; i++) {
    const t = document.querySelector(`.find-slot-title[data-slot="${i}"]`);
    const ta = document.querySelector(`.find-slot-subs[data-slot="${i}"]`);
    const s = document.querySelector(`.find-slot-resume[data-slot="${i}"]`);
    const q = (t && t.value) || "";
    const subTitles = (ta && ta.value) || "";
    const fn = s && s.value ? s.value : null;
    out.push({ query: q, sub_titles: subTitles, resume_filename: fn });
  }
  return out;
}

async function loadFind() {
  const status = $("#find-status");
  try {
    const r = await api("/config/find-jobs");
    const f = await r.json();
    const box = $("#find-boards");
    if (box && Array.isArray(f.known_boards)) {
      const sel = new Set(f.boards || []);
      box.innerHTML = f.known_boards
        .map((name) => {
          const ck = sel.has(name) ? " checked" : "";
          return `<label><input type="checkbox" name="board" value="${escAttr(name)}"${ck} /> ${esc(name)}</label>`;
        })
        .join("");
    }
    if ($("#find-run-jobspy")) $("#find-run-jobspy").checked = f.run_jobspy !== false;
    if ($("#find-run-workday")) $("#find-run-workday").checked = f.run_workday !== false;
    if ($("#find-run-smartextract")) $("#find-run-smartextract").checked = f.run_smart_extract !== false;
    if ($("#find-city")) $("#find-city").value = f.city_location || "";
    if ($("#find-include-remote")) $("#find-include-remote").checked = f.include_remote !== false;
    if ($("#find-results-per-site")) $("#find-results-per-site").value = String(f.results_per_site ?? 100);
    if ($("#find-hours-old")) $("#find-hours-old").value = String(f.hours_old ?? 72);
    if ($("#find-country")) $("#find-country").value = f.country || "USA";
    let files = [];
    try {
      const rr = await api("/role-resumes");
      const jr = await rr.json();
      files = jr.files || [];
    } catch (_) {}
    renderFindSlots(f.search_slots || [], files);
    if (status) status.textContent = "Loaded.";
  } catch (e) {
    if (status) status.textContent = String(e.message || e);
  }
}

function collectFindBoards() {
  return Array.from(document.querySelectorAll('#find-boards input[name="board"]:checked')).map((i) => i.value);
}

function collectFindJobsBody() {
  return {
    boards: collectFindBoards(),
    run_jobspy: $("#find-run-jobspy") ? $("#find-run-jobspy").checked : true,
    run_workday: $("#find-run-workday") ? $("#find-run-workday").checked : true,
    run_smart_extract: $("#find-run-smartextract") ? $("#find-run-smartextract").checked : true,
    city_location: ($("#find-city") && $("#find-city").value) || "",
    include_remote: $("#find-include-remote") ? $("#find-include-remote").checked : true,
    search_slots: collectSearchSlots(),
    results_per_site: parseInt(($("#find-results-per-site") && $("#find-results-per-site").value) || "100", 10),
    hours_old: parseInt(($("#find-hours-old") && $("#find-hours-old").value) || "72", 10),
    country: ($("#find-country") && $("#find-country").value) || "USA",
  };
}

async function saveFindJobsInternal() {
  const body = collectFindJobsBody();
  const r = await api("/config/find-jobs", { method: "PUT", body: JSON.stringify(body) });
  const j = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(j.detail || r.statusText || "Save failed");
}

async function saveFindJobs() {
  const status = $("#find-status");
  try {
    await saveFindJobsInternal();
    if (status) status.textContent = "Saved.";
  } catch (e) {
    if (status) status.textContent = String(e.message || e);
  }
}

function pollTask(taskId, prefix, options) {
  const pre = prefix || "";
  const opt = options || {};
  const statusSel = opt.statusEl || "#find-status";
  const okText = opt.okText != null ? opt.okText : "Finished.";
  const errText = opt.errText != null ? opt.errText : "Finished with errors.";
  const cancelText = opt.cancelText != null ? opt.cancelText : "Stopped.";
  const onTick = typeof opt.onTick === "function" ? opt.onTick : null;
  activePipelineTaskId = taskId;
  const t = setInterval(async () => {
    try {
      const r = await api("/tasks/" + taskId);
      const j = await r.json();
      const tail = (j.log || "") + (j.error ? "\n" + j.error : "");
      terminalSet(pre + tail);
      if (onTick) {
        try {
          await onTick(j);
        } catch (_) {}
      }
      if (j.status === "done" || j.status === "error" || j.status === "cancelled") {
        clearInterval(t);
        activePipelineTaskId = null;
        const code = j.returncode;
        const line =
          j.status === "cancelled"
            ? "\n[stopped]\n"
            : j.status === "error"
              ? "\n[finished with error]\n"
              : "\n[exit " + (code != null ? code : 0) + "]\n";
        terminalAppend(line);
        refreshUsage();
        loadDashboard();
        void resetAndFetchResults();
        const st = $(statusSel);
        if (st) {
          if (j.status === "cancelled") st.textContent = cancelText;
          else st.textContent = j.status === "done" ? okText : errText;
        }
      }
    } catch (_) {
      clearInterval(t);
      activePipelineTaskId = null;
    }
  }, 600);
}

async function updateApplyCostDelta() {
  const el = $("#results-apply-cost");
  if (!el || applyCostBaselineUsd == null) return;
  const now = Date.now();
  if (now - applyCostLastPollMs < 1500) return;
  applyCostLastPollMs = now;
  try {
    const u = await getUsageSummary();
    const current = Number(u.total_estimated_usd || 0);
    const delta = Math.max(0, current - applyCostBaselineUsd);
    el.textContent = `Apply run cost (est): $${delta.toFixed(4)} · live`;
  } catch (_) {}
}

async function runResultsApply() {
  const status = $("#results-status");
  const costEl = $("#results-apply-cost");
  const body = {
    agent: ($("#apply-agent") && $("#apply-agent").value) || "openai",
    model: ($("#apply-model") && $("#apply-model").value.trim()) || "",
    limit: parseInt((($("#apply-limit") && $("#apply-limit").value) || "5"), 10) || 5,
    workers: parseInt((($("#apply-workers") && $("#apply-workers").value) || "1"), 10) || 1,
    min_score: 7,
    dry_run: false,
    headless: false,
  };
  saveApplyPrefs(body);
  if (status) status.textContent = "Starting apply…";
  if (costEl) costEl.textContent = "Apply run cost (est): preparing baseline…";
  try {
    const u = await getUsageSummary();
    applyCostBaselineUsd = Number(u.total_estimated_usd || 0);
    applyCostLastPollMs = 0;
    const r = await api("/pipeline/apply", { method: "POST", body: JSON.stringify(body) });
    const j = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(apiDetailMessage(j) || r.statusText || "Apply run failed");
    const tid = j.task_id;
    const cmd = (j.command || []).join(" ");
    terminalAppend("\n── " + new Date().toLocaleString() + " ──\n");
    pollTask(tid, "$ " + cmd + "\n\n", {
      statusEl: "#results-status",
      okText: "Apply finished.",
      errText: "Apply finished with errors.",
      cancelText: "Apply stopped.",
      onTick: async () => {
        await updateApplyCostDelta();
      },
    });
  } catch (e) {
    if (status) status.textContent = String(e.message || e);
    if (costEl) costEl.textContent = "";
    terminalAppend(String(e.message || e) + "\n");
  }
}

function saveApplyPrefs(body) {
  try {
    const prefs = {
      agent: body.agent || "openai",
      model: body.model || "",
      limit: Number(body.limit || 5),
      workers: Number(body.workers || 1),
    };
    localStorage.setItem(APPLY_PREFS_KEY, JSON.stringify(prefs));
  } catch (_) {}
}

function loadApplyPrefs() {
  try {
    const raw = localStorage.getItem(APPLY_PREFS_KEY);
    if (!raw) return;
    const p = JSON.parse(raw);
    if ($("#apply-agent") && p.agent) $("#apply-agent").value = String(p.agent);
    if ($("#apply-model") && p.model) $("#apply-model").value = String(p.model);
    if ($("#apply-limit") && p.limit != null) $("#apply-limit").value = String(p.limit);
    if ($("#apply-workers") && p.workers != null) $("#apply-workers").value = String(p.workers);
    syncModelPresetFromText();
  } catch (_) {}
}

function syncModelPresetFromText() {
  const preset = $("#apply-model-preset");
  const model = $("#apply-model");
  if (!preset || !model) return;
  const v = (model.value || "").trim();
  const has = Array.from(preset.options).some((o) => o.value === v && v);
  preset.value = has ? v : "";
}

function wireApplyModelControls() {
  const preset = $("#apply-model-preset");
  const model = $("#apply-model");
  if (!preset || !model) return;
  preset.addEventListener("change", () => {
    if (preset.value) model.value = preset.value;
  });
  model.addEventListener("input", () => syncModelPresetFromText());
  syncModelPresetFromText();
}

async function stopTerminalPipeline() {
  if (!activePipelineTaskId) {
    terminalAppend("\n(nothing running)\n");
    return;
  }
  const tid = activePipelineTaskId;
  try {
    const r = await api("/tasks/" + tid + "/cancel", { method: "POST" });
    const j = await r.json().catch(() => ({}));
    if (!r.ok || !j.ok) terminalAppend("\n" + (apiDetailMessage(j) || "Stop failed") + "\n");
  } catch (e) {
    terminalAppend(String(e.message || e) + "\n");
  }
}

async function runDiscover() {
  const status = $("#find-status");
  if (status) status.textContent = "Saving…";
  try {
    await saveFindJobsInternal();
    if (status) status.textContent = "Starting discover…";
    terminalAppend("\n── " + new Date().toLocaleString() + " ──\n");
    const r = await api("/pipeline/run", { method: "POST", body: JSON.stringify({ stages: ["discover"] }) });
    const j = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(apiDetailMessage(j) || r.statusText || "Run failed");
    const tid = j.task_id;
    const cmd = (j.command || []).join(" ");
    pollTask(tid, "$ " + cmd + "\n\n", {
      statusEl: "#find-status",
      okText: "Discover finished.",
      errText: "Discover finished with errors.",
    });
  } catch (e) {
    if (status) status.textContent = String(e.message || e);
    terminalAppend(String(e.message || e) + "\n");
  }
}

async function runDiscoverEachSlot() {
  const status = $("#find-status");
  if (status) status.textContent = "Saving…";
  try {
    await saveFindJobsInternal();
    if (status) status.textContent = "Starting discover (each slot)…";
    terminalAppend("\n── " + new Date().toLocaleString() + " ──\n");
    const r = await api("/pipeline/discover-slots", { method: "POST", body: "{}" });
    const j = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(apiDetailMessage(j) || r.statusText || "Run failed");
    const tid = j.task_id;
    const nq = (j.queries && j.queries.length) || 0;
    const pw = j.parallel != null ? j.parallel : Math.min(15, nq || 1);
    const pre = `discover-slots (${nq} queries, parallel=${pw} auto)\n\n`;
    pollTask(tid, pre, {
      statusEl: "#find-status",
      okText: "Discover (each slot) finished.",
      errText: "Discover (each slot) finished with errors.",
    });
  } catch (e) {
    if (status) status.textContent = String(e.message || e);
    terminalAppend(String(e.message || e) + "\n");
  }
}

async function loadScore() {
  const status = $("#score-status");
  try {
    const r = await api("/config/criteria");
    const j = await r.json();
    const c = j.criteria || {};
    $("#crit-relevance").checked = !!c.relevance;
    $("#crit-seniority").checked = !!c.seniority;
    $("#crit-years").value = c.years_experience ?? 5;
    $("#crit-travel").checked = c.filter_travel_over_25 !== false;
    $("#crit-skills-gap").checked = c.required_skills_gap !== false;
    if ($("#crit-uploads-only")) $("#crit-uploads-only").checked = c.fallback_to_profile_resume === false;
    if (status) status.textContent = "Loaded criteria.";
  } catch (e) {
    if (status) status.textContent = String(e.message || e);
  }
}

function collectCriteriaBody() {
  const uploadsOnly = $("#crit-uploads-only") && $("#crit-uploads-only").checked;
  return {
    relevance: $("#crit-relevance").checked,
    seniority: $("#crit-seniority").checked,
    years_experience: parseInt($("#crit-years").value, 10) || 0,
    filter_travel_over_25: $("#crit-travel").checked,
    required_skills_gap: $("#crit-skills-gap").checked,
    fallback_to_profile_resume: !uploadsOnly,
  };
}

async function saveCriteria() {
  const body = collectCriteriaBody();
  const status = $("#score-status");
  try {
    const r = await api("/config/criteria", { method: "PUT", body: JSON.stringify(body) });
    if (!r.ok) throw new Error((await r.json()).detail || "Save failed");
    if (status) status.textContent = "Criteria saved.";
  } catch (e) {
    if (status) status.textContent = String(e.message || e);
  }
}

async function saveCriteriaInternal() {
  const body = collectCriteriaBody();
  const r = await api("/config/criteria", { method: "PUT", body: JSON.stringify(body) });
  const j = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(apiDetailMessage(j) || "Save failed");
}

async function runScore() {
  const status = $("#score-status");
  if (status) status.textContent = "Saving…";
  try {
    await saveCriteriaInternal();
    if (status) status.textContent = "Starting score…";
    terminalAppend("\n── " + new Date().toLocaleString() + " ──\n");
    const r = await api("/pipeline/run", { method: "POST", body: JSON.stringify({ stages: ["score"] }) });
    const j = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(apiDetailMessage(j) || r.statusText || "Run failed");
    const tid = j.task_id;
    const cmd = (j.command || []).join(" ");
    pollTask(tid, "$ " + cmd + "\n\n", {
      statusEl: "#score-status",
      okText: "Scoring finished.",
      errText: "Scoring finished with errors.",
    });
  } catch (e) {
    if (status) status.textContent = String(e.message || e);
    terminalAppend(String(e.message || e) + "\n");
  }
}

const RESULTS_PAGE = 100;
const TRACK_OPTIONS = [
  { v: "open", label: "Open" },
  { v: "applied", label: "Applied" },
  { v: "follow_up", label: "Follow-up Required" },
  { v: "interview", label: "Interview" },
];

let resultsNextOffset = 0;
let resultsHasMore = true;
let resultsLoading = false;
let resultsScrollObserver = null;
let filterDebounceTimer = null;
/** Sort from Results table Score column only: score_desc | score_asc */
let resultsSortScore = "score_desc";

function trackSelectValue(raw) {
  const t = String(raw || "")
    .trim()
    .toLowerCase();
  if (!t) return "open";
  const allowed = new Set(["open", "applied", "follow_up", "interview"]);
  return allowed.has(t) ? t : "open";
}

function trackSelectHtml(url, rawTrack) {
  const v = trackSelectValue(rawTrack);
  const opts = TRACK_OPTIONS.map(
    (o) => `<option value="${escAttr(o.v)}"${o.v === v ? " selected" : ""}>${esc(o.label)}</option>`,
  ).join("");
  return `<select class="track-select" data-track-url="${escAttr(url)}">${opts}</select>`;
}

function buildResultRowHtml(row) {
  const fs = row.fit_score;
  const pill =
    fs == null ? "—" : `<span class="score-pill ${scoreClass(fs)}">${esc(String(fs))}</span>`;
  const title = esc(row.title || "—");
  const url = row.url || "";
  const roleStar =
    row.has_role_resume_for_query
      ? '<span class="role-resume-star" title="Discovered with this search keyword and a role-specific résumé">*</span>'
      : "";
  const why =
    row.score_reasoning && String(row.score_reasoning).trim()
      ? `<button type="button" class="btn btn-ghost" data-why="${escAttr(url)}">Why</button>`
      : "—";
  const trk = trackSelectHtml(url, row.application_track);
  const sq = esc((row.search_query || "").slice(0, 48) || "—");
  return `<tr data-url="${escAttr(url)}">
    <td><a href="${escAttr(url)}" target="_blank" rel="noopener">${title}</a>${roleStar}</td>
    <td>${esc(row.site || "—")}</td>
    <td>${pill}</td>
    <td class="cell-muted">${sq}</td>
    <td class="cell-track">${trk}</td>
    <td>${why}</td>
  </tr>`;
}

function disconnectResultsObserver() {
  if (resultsScrollObserver) {
    resultsScrollObserver.disconnect();
    resultsScrollObserver = null;
  }
}

function ensureResultsObserver() {
  const sent = $("#results-sentinel");
  if (!sent) return;
  disconnectResultsObserver();
  resultsScrollObserver = new IntersectionObserver(
    (entries) => {
      if (!entries[0] || !entries[0].isIntersecting) return;
      appendNextResultsPage();
    },
    { root: null, rootMargin: "240px", threshold: 0 },
  );
  resultsScrollObserver.observe(sent);
}

function resultsQueryParams() {
  const q = ($("#filter-q") && $("#filter-q").value) || "";
  const site = ($("#filter-site") && $("#filter-site").value) || "";
  const params = new URLSearchParams({
    limit: String(RESULTS_PAGE),
    sort: resultsSortScore,
  });
  if (q.trim()) params.set("q", q.trim());
  if (site) params.set("site", site);
  return params;
}

function syncResultsScoreSortUi() {
  const th = $("#results-th-score-col");
  const btn = $("#results-th-score");
  const ind = btn && btn.querySelector(".results-th-sort-indicator");
  if (th) th.setAttribute("aria-sort", resultsSortScore === "score_desc" ? "descending" : "ascending");
  if (ind) ind.textContent = resultsSortScore === "score_desc" ? "↓" : "↑";
}

function toggleResultsScoreSort() {
  resultsSortScore = resultsSortScore === "score_desc" ? "score_asc" : "score_desc";
  syncResultsScoreSortUi();
  resetAndFetchResults();
}

function closeResultsActionsMenu() {
  const det = document.getElementById("results-actions-menu");
  if (det && det.tagName === "DETAILS") det.open = false;
}

async function fetchResultsPage(reset) {
  const tbody = $("#results-tbody");
  const status = $("#results-status");
  if (!tbody) return;
  if (resultsLoading) return;
  if (reset) {
    jobCache.clear();
    tbody.innerHTML = "";
    resultsNextOffset = 0;
    resultsHasMore = true;
  }
  if (!reset && !resultsHasMore) return;

  resultsLoading = true;
  if (reset && status) status.textContent = "Loading…";

  const params = resultsQueryParams();
  params.set("offset", String(reset ? 0 : resultsNextOffset));

  try {
    const r = await api("/jobs?" + params.toString());
    const j = await r.json();
    const rows = j.jobs || [];
    for (const row of rows) {
      if (row.url) jobCache.set(row.url, row);
    }
    tbody.insertAdjacentHTML("beforeend", rows.map(buildResultRowHtml).join(""));
    resultsNextOffset = (reset ? 0 : resultsNextOffset) + rows.length;
    resultsHasMore = !!j.has_more;
    const total = j.total ?? 0;
    const shown = Math.min(resultsNextOffset, total);
    if (status) {
      if (!resultsHasMore || total === 0) {
        status.textContent = total ? `Showing all ${total} job(s).` : "No jobs match.";
      } else {
        status.textContent = `Showing ${shown} of ${total} — scroll for more.`;
      }
    }
  } catch (e) {
    if (status) status.textContent = String(e.message || e);
    resultsHasMore = false;
  } finally {
    resultsLoading = false;
  }
}

async function appendNextResultsPage() {
  if (resultsLoading || !resultsHasMore) return;
  await fetchResultsPage(false);
}

async function resetAndFetchResults() {
  disconnectResultsObserver();
  await fetchResultsPage(true);
  ensureResultsObserver();
}

function scheduleResultsRefetch() {
  clearTimeout(filterDebounceTimer);
  filterDebounceTimer = setTimeout(() => {
    resetAndFetchResults();
  }, 320);
}

async function loadResults() {
  syncResultsScoreSortUi();
  await loadResultSites();
  await resetAndFetchResults();
}

async function loadResultSites() {
  const siteSel = $("#filter-site");
  if (!siteSel || siteSel.options.length > 1) return;
  try {
    const r = await api("/jobs/sites");
    const j = await r.json();
    for (const s of j.sites || []) {
      const o = document.createElement("option");
      o.value = s;
      o.textContent = s;
      siteSel.appendChild(o);
    }
  } catch (_) {}
}

async function postJobTrack(url, track) {
  try {
    const r = await api("/jobs/track", { method: "POST", body: JSON.stringify({ url, track }) });
    const j = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(apiDetailMessage(j));
    const row = jobCache.get(url);
    if (row) row.application_track = track;
    const st = $("#results-status");
    if (st) {
      st.textContent = "Status saved.";
      setTimeout(() => {
        if (st.textContent === "Status saved.") st.textContent = "";
      }, 1800);
    }
  } catch (e) {
    alert(String(e.message || e));
    resetAndFetchResults();
  }
}

function scoreClass(n) {
  if (n >= 7) return "score-high";
  if (n >= 5) return "score-mid";
  return "score-low";
}

function openWhy(url) {
  const modal = $("#modal-why");
  const body = $("#modal-why-body");
  if (!modal || !body) return;
  const row = findJobRowByUrl(url);
  if (!row) {
    body.innerHTML = "<p>No data.</p>";
    modal.classList.add("open");
    return;
  }
  const fs = row.fit_score;
  const overall =
    fs != null && fs !== ""
      ? `<p class="why-overall"><strong>Overall</strong> <span class="score-pill ${scoreClass(Number(fs))}">${esc(String(fs))}</span></p>`
      : "";
  const kw = esc(row.keywords_line || "");
  const reason = esc(row.reasoning_text || "");
  const rows = row.criteria_rows;
  let criteriaBlock = "";
  if (Array.isArray(rows) && rows.length) {
    criteriaBlock = `
      <p class="why-section-title"><strong>Criteria</strong></p>
      <table class="why-criteria">
        <thead><tr><th>Criterion</th><th>Score</th><th>Note</th></tr></thead>
        <tbody>
          ${rows
            .map(
              (r) =>
                `<tr><td>${esc(r.label || "")}</td><td class="why-score">${esc(r.score || "—")}</td><td>${esc(
                  r.note || "",
                )}</td></tr>`,
            )
            .join("")}
        </tbody>
      </table>`;
  } else if ((row.criteria_text || "").trim()) {
    const crit = esc(row.criteria_text || "");
    criteriaBlock = `<p class="why-section-title"><strong>Criteria</strong></p><pre class="criteria criteria-fallback">${crit}</pre>`;
  }
  body.innerHTML = `
    ${overall}
    <p><strong>Keywords</strong><br/>${kw || "—"}</p>
    ${criteriaBlock}
    <p><strong>Reasoning</strong><br/>${reason || "—"}</p>
  `;
  modal.classList.add("open");
}

const jobCache = new Map();

function findJobRowByUrl(url) {
  return jobCache.get(url);
}

async function loadSettings() {
  const box = $("#settings-paths");
  if (!box) return;
  try {
    const r = await api("/paths");
    const p = await r.json();
    box.innerHTML = Object.entries(p)
      .map(([k, v]) => `<div><strong>${esc(k)}</strong><code>${esc(String(v))}</code></div>`)
      .join("");
    const meta = await api("/meta");
    const m = await meta.json();
    const tierEl = $("#settings-tier");
    if (tierEl) tierEl.textContent = "Tier " + m.tier;
  } catch (e) {
    box.innerHTML = "<p>" + esc(String(e.message || e)) + "</p>";
  }
  const resBox = $("#settings-resumes");
  const resSt = $("#settings-resumes-status");
  if (resBox) {
    try {
      const r = await api("/role-resumes");
      const j = await r.json();
      const files = j.files || [];
      if (!files.length) {
        resBox.innerHTML = '<p class="hint">No files in role résumés folder yet.</p>';
      } else {
        resBox.innerHTML = `<table class="data data--compact"><thead><tr><th>File</th><th>Size</th><th>Keywords</th><th></th></tr></thead><tbody>${files
          .map((f) => {
            const kws = (f.keywords || []).join(", ") || "—";
            const sz = f.size_bytes != null ? `${Math.round(f.size_bytes / 102.4) / 10} KB` : "—";
            return `<tr>
              <td><code>${esc(f.filename)}</code></td>
              <td>${esc(sz)}</td>
              <td class="cell-muted">${esc(kws)}</td>
              <td><button type="button" class="btn btn-danger btn-tiny btn-delete-resume" data-resume-file="${escAttr(
                f.filename,
              )}">Delete</button></td>
            </tr>`;
          })
          .join("")}</tbody></table>`;
      }
      if (resSt) resSt.textContent = "";
    } catch (e) {
      resBox.innerHTML = "<p>Could not load résumés.</p>";
      if (resSt) resSt.textContent = String(e.message || e);
    }
  }
}

async function deleteScoredJobs() {
  closeResultsActionsMenu();
  if (
    !confirm(
      "Delete every job that has a fit score? Jobs without a score are kept. This cannot be undone.",
    )
  ) {
    return;
  }
  const status = $("#results-status");
  if (status) status.textContent = "Deleting…";
  try {
    const r = await api("/jobs/scored", { method: "DELETE" });
    const j = await r.json();
    if (!r.ok) throw new Error(j.detail || "Failed");
    if (status) status.textContent = `Removed ${j.deleted} scored job(s).`;
    await resetAndFetchResults();
    loadDashboard();
  } catch (e) {
    if (status) status.textContent = String(e.message || e);
  }
}

async function deleteAllJobs() {
  closeResultsActionsMenu();
  if (!confirm("Delete ALL jobs from the database? This cannot be undone.")) return;
  const status = $("#results-status");
  if (status) status.textContent = "Deleting…";
  try {
    const r = await api("/jobs/all", { method: "DELETE" });
    const j = await r.json();
    if (!r.ok) throw new Error(j.detail || "Failed");
    if (status) status.textContent = `Removed ${j.deleted} job(s).`;
    await resetAndFetchResults();
    loadDashboard();
  } catch (e) {
    if (status) status.textContent = String(e.message || e);
  }
}

async function exportHtmlDashboard() {
  const st = $("#settings-export-status");
  if (st) st.textContent = "Exporting…";
  try {
    const r = await api("/export/html-dashboard", { method: "POST", body: "{}" });
    const j = await r.json();
    if (!r.ok) throw new Error(j.detail || "failed");
    if (st) st.textContent = "Wrote: " + j.path;
  } catch (e) {
    if (st) st.textContent = String(e.message || e);
  }
}

async function postRestart() {
  if (!confirm("Restart the Job Runner web server?")) return;
  try {
    const r = await api("/server/restart", { method: "POST", body: "{}" });
    const j = await r.json();
    alert(j.detail || "Restart scheduled.");
    setTimeout(() => location.reload(), 2500);
  } catch (e) {
    alert(String(e.message || e));
  }
}

async function syncMonthlyCost() {
  const costMeta = $("#cost-meta");
  try {
    const r = await api("/usage/sync-month", { method: "POST", body: "{}" });
    const j = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(apiDetailMessage(j) || "Sync failed");
    await refreshUsage();
    if (costMeta) {
      const at = (j.usage && j.usage.last_month_sync_at) || "";
      if (at) costMeta.textContent += ` · synced ${at.replace("T", " ").replace("Z", " UTC")}`;
    }
  } catch (e) {
    alert(String(e.message || e));
  }
}

async function uploadResumeForSlot(fileInput) {
  if (!fileInput || !fileInput.files || !fileInput.files[0]) return;
  const row = parseInt(fileInput.getAttribute("data-slot"), 10);
  const st = $("#upload-status");
  const titleInput = document.querySelector(`.find-slot-title[data-slot="${row}"]`);
  const subTa = document.querySelector(`.find-slot-subs[data-slot="${row}"]`);
  const main = titleInput && titleInput.value.trim();
  const firstSub =
    subTa &&
    subTa.value
      .split(/\n/)
      .map((l) => l.trim())
      .find(Boolean);
  const kw = main || firstSub;
  if (!kw) {
    if (st) st.textContent = "Enter a main job title or at least one additional title in this row before uploading.";
    fileInput.value = "";
    return;
  }
  if (st) st.textContent = "Saving…";
  try {
    await saveFindJobsInternal();
  } catch (e) {
    if (st) st.textContent = String(e.message || e);
    fileInput.value = "";
    return;
  }
  if (st) st.textContent = "Uploading…";
  const fd = new FormData();
  fd.append("keyword", kw);
  fd.append("file", fileInput.files[0]);
  try {
    const r = await fetch("/api/interests/upload", { method: "POST", credentials: "same-origin", body: fd });
    await handleApiResponse401(r);
    if (r.status === 401) return;
    const j = await r.json();
    if (!r.ok) throw new Error(apiDetailMessage(j));
    const fn = j.filename;
    const rr = await api("/role-resumes");
    const jr = await rr.json();
    const files = jr.files || [];
    document.querySelectorAll(".find-slot-resume").forEach((sel) => {
      const prev = sel.value;
      const slot = sel.getAttribute("data-slot");
      sel.innerHTML = resumeSelectOptionsHtml(files, prev);
      if (slot === String(row) && fn) sel.value = fn;
    });
    await saveFindJobsInternal();
    if (st) st.textContent = fn ? "Saved: " + fn + " (applied to main + extra titles)" : "Uploaded.";
  } catch (e) {
    if (st) st.textContent = String(e.message || e);
  } finally {
    fileInput.value = "";
  }
}

function apiDetailMessage(j) {
  const d = j && j.detail;
  if (typeof d === "string") return d;
  if (Array.isArray(d)) return d.map((x) => (x && x.msg) || JSON.stringify(x)).join("; ") || "Request failed";
  return "Request failed";
}

function esc(s) {
  const d = document.createElement("div");
  d.textContent = s;
  return d.innerHTML;
}

function escAttr(s) {
  return String(s)
    .replace(/&/g, "&amp;")
    .replace(/"/g, "&quot;")
    .replace(/</g, "&lt;");
}

function wireNav() {
  document.querySelectorAll(".nav-btn[data-section]").forEach((btn) => {
    btn.addEventListener("click", () => showSection(btn.getAttribute("data-section")));
  });
  $("#btn-restart") && $("#btn-restart").addEventListener("click", postRestart);
  $("#btn-sync-month-cost") && $("#btn-sync-month-cost").addEventListener("click", syncMonthlyCost);
  $("#btn-save-find") && $("#btn-save-find").addEventListener("click", saveFindJobs);
  $("#btn-run-discover") && $("#btn-run-discover").addEventListener("click", runDiscover);
  $("#btn-run-discover-slots") &&
    $("#btn-run-discover-slots").addEventListener("click", () => void runDiscoverEachSlot());
  $("#btn-repair-jobspy") && $("#btn-repair-jobspy").addEventListener("click", () => void repairJobSpy());
  $("#btn-terminal-stop") && $("#btn-terminal-stop").addEventListener("click", () => void stopTerminalPipeline());
  $("#btn-terminal-clear") &&
    $("#btn-terminal-clear").addEventListener("click", () => terminalSet(""));
  $("#btn-terminal-toggle") && $("#btn-terminal-toggle").addEventListener("click", toggleTerminalMinimized);
  $("#btn-save-criteria") && $("#btn-save-criteria").addEventListener("click", saveCriteria);
  $("#btn-run-score") && $("#btn-run-score").addEventListener("click", runScore);
  $("#btn-delete-scored") && $("#btn-delete-scored").addEventListener("click", deleteScoredJobs);
  $("#btn-delete-all-jobs") && $("#btn-delete-all-jobs").addEventListener("click", deleteAllJobs);
  $("#btn-results-apply") && $("#btn-results-apply").addEventListener("click", runResultsApply);
  $("#btn-results-refresh") && $("#btn-results-refresh").addEventListener("click", () => resetAndFetchResults());
  ["filter-q", "filter-site"].forEach((id) => {
    const el = document.getElementById(id);
    if (!el) return;
    el.addEventListener(id === "filter-q" ? "input" : "change", () => scheduleResultsRefetch());
  });
  $("#btn-export-html") && $("#btn-export-html").addEventListener("click", exportHtmlDashboard);
  $("#modal-why-close") &&
    $("#modal-why-close").addEventListener("click", () => $("#modal-why").classList.remove("open"));
  $("#modal-why") &&
    $("#modal-why").addEventListener("click", (ev) => {
      if (ev.target.id === "modal-why") $("#modal-why").classList.remove("open");
    });
}

function wireResultsSection() {
  const sec = $("#section-results");
  if (!sec || sec.dataset.wired) return;
  sec.dataset.wired = "1";
  const scoreBtn = $("#results-th-score");
  if (scoreBtn) scoreBtn.addEventListener("click", () => toggleResultsScoreSort());
  sec.addEventListener("click", (ev) => {
    const btn = ev.target.closest("[data-why]");
    if (btn) openWhy(btn.getAttribute("data-why"));
  });
  sec.addEventListener("change", (ev) => {
    const sel = ev.target.closest(".track-select");
    if (!sel) return;
    const url = sel.getAttribute("data-track-url");
    if (url) postJobTrack(url, sel.value);
  });
}

function wireSettingsResumes() {
  const panel = $("#settings-resumes-panel");
  if (!panel || panel.dataset.wired) return;
  panel.dataset.wired = "1";
  panel.addEventListener("click", (ev) => {
    const btn = ev.target.closest(".btn-delete-resume");
    if (!btn) return;
    const fn = btn.getAttribute("data-resume-file");
    if (!fn) return;
    if (!confirm(`Delete ${fn}? This removes the file and clears it from keywords.`)) return;
    const st = $("#settings-resumes-status");
    (async () => {
      if (st) st.textContent = "Deleting…";
      try {
        const r = await api("/role-resumes/" + encodeURIComponent(fn), { method: "DELETE" });
        const j = await r.json().catch(() => ({}));
        if (!r.ok) throw new Error(apiDetailMessage(j));
        if (st) st.textContent = "Deleted.";
        loadSettings();
      } catch (e) {
        if (st) st.textContent = String(e.message || e);
      }
    })();
  });
}

function wireFindSlotsSection() {
  const sec = $("#section-find");
  if (!sec || sec.dataset.slotWired) return;
  sec.dataset.slotWired = "1";
  sec.addEventListener("change", (ev) => {
    const t = ev.target;
    if (t && t.classList && t.classList.contains("find-slot-file")) {
      void uploadResumeForSlot(t);
    }
  });
}

document.addEventListener("DOMContentLoaded", async () => {
  if (!(await ensureUiSession())) return;
  document.getElementById("btn-logout")?.addEventListener("click", async () => {
    await fetch("/api/logout", { method: "POST", credentials: "same-origin" });
    window.location.href = "/login.html";
  });
  wireNav();
  wireResultsSection();
  wireSettingsResumes();
  wireFindSlotsSection();
  wireApplyModelControls();
  loadApplyPrefs();
  loadMeta();
  showSection("dashboard");
  setInterval(refreshUsage, 20000);
});
