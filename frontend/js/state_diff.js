/* Individual-level state difference comparison. */

const PAGE_SIZE = 80;
let runs = [];
let runSteps = {};
let diff = null;
let filteredRows = [];
let page = 0;

async function loadRuns() {
  runs = await fillRunSelect(el("baseRun"), { includePlaceholder: false });
  const target = el("targetRun");
  target.innerHTML = runs.map((r) =>
    `<option value="${esc(r.id)}">${esc(r.name)} · 第${r.current_step}步</option>`).join("");
  el("baseRun").value = runs[0]?.id || "";
  el("targetRun").value = runs[0]?.id || "";
  await syncRunSelection();
}

async function stepsFor(runId) {
  if (!runId) return [];
  if (!runSteps[runId]) {
    const data = await get(`/api/runs/${runId}/steps`);
    runSteps[runId] = data.steps?.length ? data.steps : [0];
  }
  return runSteps[runId];
}

function fillStepSelect(sel, steps, preferred) {
  const value = preferred ?? steps[steps.length - 1];
  sel.innerHTML = steps.map((s) => `<option value="${s}">第 ${s} 步</option>`).join("");
  if (steps.includes(value)) sel.value = String(value);
}

async function syncRunSelection() {
  const same = el("sameRun").checked;
  const baseId = el("baseRun").value;
  el("targetRun").disabled = same;
  if (same) el("targetRun").value = baseId;

  const baseSteps = await stepsFor(baseId);
  fillStepSelect(el("baseStep"), baseSteps, Number(el("baseStep").value) || 0);
  const targetId = same ? baseId : el("targetRun").value;
  if (targetId) {
    const targetSteps = await stepsFor(targetId);
    fillStepSelect(el("targetStep"), targetSteps,
                   same ? Number(el("baseStep").value) : Number(el("targetStep").value) || 0);
  }
}

async function compare() {
  const baseRun = el("baseRun").value;
  const targetRun = el("sameRun").checked ? baseRun : el("targetRun").value;
  const baseStep = Number(el("baseStep").value);
  const targetStep = Number(el("targetStep").value);
  if (!baseRun || !targetRun) {
    showNotice(el("notice"), "请先选择要对比的运行。", "warn");
    return;
  }
  const params = new URLSearchParams({
    base_run: baseRun, target_run: targetRun,
    base_step: baseStep, target_step: targetStep,
  });
  const mt = el("moveThreshold").value;
  if (mt !== "") params.set("move_threshold", Number(mt));
  const mr = el("matchRadius").value;
  if (mr !== "") params.set("match_radius", Number(mr));

  showNotice(el("notice"), "正在比对个体身份与状态…", "");
  try {
    diff = await get(`/api/state-diff?${params.toString()}`);
    renderDiff();
    showNotice(el("notice"), "", "");
  } catch (e) {
    diff = null;
    showNotice(el("notice"), "对比失败：" + e.message, "error");
  }
}

function paletteLabel(state) {
  return diff?.palette?.[state]?.label || state;
}

function renderCounts() {
  const c = diff.counts;
  const cards = [
    ["新增", c.added, DIFF_COLORS.added],
    ["消失", c.removed, DIFF_COLORS.removed],
    ["状态转变", c.state_changed, DIFF_COLORS.state_changed],
    ["发生位移", c.moved, DIFF_COLORS.moved],
    ["位置+状态变化", c.state_changed_and_moved, DIFF_COLORS.both],
    ["成功匹配", c.matched, "#95a5a6"],
    ["未变化", c.unchanged, "#6b7785"],
    ["变化个体合计", c.changed, "#ffffff"],
  ];
  el("countTiles").innerHTML = cards.map(([label, value, color]) => `
    <div class="stat"><div class="k">${esc(label)}</div>
    <div class="v" style="color:${color}">${value}</div>
    <div class="d">基准 ${c.base_total} → 对比 ${c.target_total}</div></div>`).join("");
}

function renderStateDelta() {
  const rows = Object.entries(diff.state_delta || {});
  const transitions = diff.state_transitions || [];
  el("stateDeltaTable").innerHTML = `
    <thead><tr><th>状态</th><th class="num">基准</th><th class="num">对比</th><th class="num">净变化</th></tr></thead>
    <tbody>${rows.map(([state, delta]) => `
      <tr><td>${esc(paletteLabel(state))}</td>
      <td class="num">${diff.base_snapshot.individuals.filter((a) => a.state === state).length}</td>
      <td class="num">${diff.target_snapshot.individuals.filter((a) => a.state === state).length}</td>
      <td class="num" style="color:${delta > 0 ? DIFF_COLORS.added : delta < 0 ? DIFF_COLORS.removed : ""}">${delta > 0 ? "+" : ""}${delta}</td></tr>`).join("") ||
      `<tr><td colspan="4" class="muted">无数据</td></tr>`}</tbody>` +
    (transitions.length ? `
    <tfoot><tr><th colspan="4">状态转变（${transitions.length} 种）</th></tr>
    ${transitions.map((t) => `<tr><td colspan="3">${esc(paletteLabel(t.from))} → ${esc(paletteLabel(t.to))}</td><th class="num">${t.count}</th></tr>`).join("")}
    </tfoot>` : "");
}

function renderDisplacement() {
  const moved = diff.displacement.moved;
  const matched = diff.displacement.matched;
  const row = (label, s) => `
    <tr><td>${label}</td><td class="num">${s.count}</td><td class="num">${fmt(s.min)}</td>
    <td class="num">${fmt(s.median)}</td><td class="num">${fmt(s.mean)}</td>
    <td class="num">${fmt(s.max)}</td><td class="num">${fmt(s.total)}</td></tr>`;
  el("displacementTable").innerHTML = `
    <thead><tr><th>范围</th><th class="num">数量</th><th class="num">最小</th>
    <th class="num">中位</th><th class="num">平均</th><th class="num">最大</th><th class="num">合计</th></tr></thead>
    <tbody>${row("发生位移", moved)}${row("全部匹配", matched)}</tbody>`;
}

function allRows() {
  const rows = [];
  for (const m of diff.matches || []) {
    const hasState = m.kinds.includes("state_changed");
    const hasMove = m.kinds.includes("moved");
    rows.push({
      category: hasState && hasMove ? "state_changed_and_moved"
        : hasState ? "state_changed" : hasMove ? "moved" : "unchanged",
      baseId: m.base_id, targetId: m.target_id, type: m.target_type || m.base_type,
      detail: hasState ? `${m.base_state}→${m.target_state}` : "—",
      distance: m.distance, dx: m.dx, dy: m.dy,
      method: m.match_method, confidence: m.confidence,
    });
  }
  for (const a of diff.added || []) rows.push({
    category: "added", baseId: "—", targetId: a.id, type: a.type,
    detail: paletteLabel(a.state), distance: null, dx: null, dy: null,
    method: "none", confidence: 0,
  });
  for (const a of diff.removed || []) rows.push({
    category: "removed", baseId: a.id, targetId: "—", type: a.type,
    detail: paletteLabel(a.state), distance: null, dx: null, dy: null,
    method: "none", confidence: 0,
  });
  return rows;
}

function categoryLabel(c) {
  return {
    added: "新增", removed: "消失", state_changed: "状态转变",
    moved: "仅位移", state_changed_and_moved: "状态+位移", unchanged: "未变化",
  }[c] || c;
}

function applyFilter() {
  const kind = el("kindFilter").value;
  const rows = allRows();
  filteredRows = rows.filter((r) => kind === ""
    ? r.category !== "unchanged"
    : r.category === kind ||
      (kind === "state_changed" && r.category === "state_changed_and_moved"));
  page = 0;
  renderTable();
}

function renderTable() {
  const start = page * PAGE_SIZE;
  const rows = filteredRows.slice(start, start + PAGE_SIZE);
  el("diffTable").innerHTML = `
    <thead><tr><th>类别</th><th>类型</th><th>基准 ID</th><th>对比 ID</th>
    <th>状态变化</th><th class="num">位移</th><th class="num">dx</th><th class="num">dy</th>
    <th>匹配</th><th class="num">可信度</th></tr></thead>
    <tbody>${rows.map((r) => {
      const color = DIFF_COLORS[r.category === "state_changed_and_moved" ? "both" : r.category] || "#ccc";
      return `<tr>
        <td><span style="color:${color};font-weight:600">${esc(categoryLabel(r.category))}</span></td>
        <td>${esc(r.type || "")}</td><td class="mono">${esc(r.baseId)}</td><td class="mono">${esc(r.targetId)}</td>
        <td>${r.detail === "—" ? "—" : r.detail.split("→").map((s) => esc(paletteLabel(s))).join(" →")}</td>
        <td class="num">${r.distance == null ? "—" : fmt(r.distance)}</td>
        <td class="num">${r.dx == null ? "—" : fmt(r.dx)}</td>
        <td class="num">${r.dy == null ? "—" : fmt(r.dy)}</td>
        <td>${r.method === "id" ? "ID谱系" : r.method === "spatial" ? "空间" : r.method === "spatial_nearest" ? "全局最近" : "未匹配"}</td>
        <td class="num">${fmt(r.confidence, 2)}</td></tr>`;
    }).join("") || `<tr><td colspan="10" class="muted">没有符合条件的差异。</td></tr>`}</tbody>`;
  const pages = Math.max(1, Math.ceil(filteredRows.length / PAGE_SIZE));
  el("pageInfo").textContent = `${filteredRows.length} 条 · 第 ${page + 1} / ${pages} 页`;
  el("prevBtn").disabled = page === 0;
  el("nextBtn").disabled = page + 1 >= pages;
}

function renderLegend() {
  el("diffLegend").innerHTML = [
    ["added", "新增"], ["removed", "消失"], ["state_changed", "状态转变"],
    ["moved", "位移"], ["both", "状态+位移"], ["unchanged", "未变化"],
  ].map(([k, label]) =>
    `<span class="item"><span class="swatch" style="background:${DIFF_COLORS[k]}"></span>${label}</span>`).join("");
}

function renderDiff() {
  renderCounts();
  renderStateDelta();
  renderDisplacement();
  renderLegend();
  renderDiffOverlay(el("canvas"), diff, {
    showUnchanged: el("showUnchanged").checked,
    showArrows: el("showArrows").checked,
  });
  const m = diff.matching;
  el("matchInfo").textContent =
    `身份匹配：ID ${m.id_matches} · 空间 ${m.spatial_matches} · 全局最近 ${m.nearest_matches || 0}` +
    (m.radius_auto ? " · 自动半径" : ` · 半径 ${fmt(m.radius)}`);
  applyFilter();
}

async function init() {
  el("refreshBtn").onclick = async () => { runSteps = {}; await loadRuns(); };
  el("compareBtn").onclick = compare;
  el("baseRun").onchange = syncRunSelection;
  el("targetRun").onchange = syncRunSelection;
  el("sameRun").onchange = syncRunSelection;
  el("baseStep").onchange = () => {
    if (el("sameRun").checked) el("targetStep").value = el("baseStep").value;
  };
  el("kindFilter").onchange = applyFilter;
  el("prevBtn").onclick = () => { if (page > 0) { page--; renderTable(); } };
  el("nextBtn").onclick = () => {
    if ((page + 1) * PAGE_SIZE < filteredRows.length) { page++; renderTable(); }
  };
  for (const id of ["showUnchanged", "showArrows"]) {
    el(id).onchange = () => diff && renderDiffOverlay(el("canvas"), diff, {
      showUnchanged: el("showUnchanged").checked,
      showArrows: el("showArrows").checked,
    });
  }
  await loadRuns();
}

window.addEventListener("resize", () => {
  if (diff) renderDiffOverlay(el("canvas"), diff, {
    showUnchanged: el("showUnchanged").checked,
    showArrows: el("showArrows").checked,
  });
});

init().catch((e) => showNotice(el("notice"), "初始化失败：" + e.message, "error"));
