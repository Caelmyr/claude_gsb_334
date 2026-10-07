/* State-diff page: compare two steps of one run, or one step of two runs.
 *
 * The backend (/api/diff) resolves identities — by stable id within a run, by
 * adaptive spatial matching across runs — and returns every difference plus
 * aggregate counts.  This page draws snapshot B as the base layer and overlays
 * highlights: green = appeared, red X = disappeared, yellow ring = state
 * transition, cyan ring/arrow = displacement.
 */

const PAGE_SIZE = 100;
const KIND_COLORS = {
  appeared: "#2ecc71",
  disappeared: "#e74c3c",
  state_changed: "#f1c40f",
  moved: "#3fc6e0",
  unchanged: "#7f8c8d",
};
const KIND_LABELS = {
  appeared: "新增", disappeared: "消失", state_changed: "状态转变",
  moved: "位移", unchanged: "未变化",
};
const CONF_LABELS = { id: "ID 确定", high: "高", medium: "中", low: "低" };

let runsCache = [];
let stepsA = [];
let stepsB = [];
let diffResult = null;
let snapB = null;
let allRows = [];
let filteredRows = [];
let page = 0;
let activeFilter = "changed";
let focusId = null;

/* ------------------------------------------------------------------ */
/* Controls                                                           */
/* ------------------------------------------------------------------ */
function mode() {
  return document.querySelector('input[name="mode"]:checked').value;
}

function optionList(steps, sel) {
  sel.innerHTML = steps.map((s) => `<option value="${s}">第 ${s} 步</option>`).join("");
}

async function refreshRuns(keepA, keepB) {
  runsCache = (await get("/api/runs")).runs || [];
  const opts = runsCache.map((r) =>
    `<option value="${esc(r.id)}">${esc(r.name)} · 第${r.current_step}步</option>`).join("");
  el("runA").innerHTML = opts;
  el("runB").innerHTML = opts;
  if (keepA && el("runA").querySelector(`option[value="${keepA}"]`)) el("runA").value = keepA;
  if (keepB && el("runB").querySelector(`option[value="${keepB}"]`)) el("runB").value = keepB;
}

async function loadSteps(which) {
  const runId = which === "A" ? el("runA").value : el("runB").value;
  const sel = which === "A" ? el("stepA") : el("stepB");
  if (!runId) { sel.innerHTML = ""; return; }
  const { steps } = await get(`/api/runs/${runId}/steps`);
  const list = steps.length ? steps : [0];
  if (which === "A") stepsA = list; else stepsB = list;
  optionList(list, sel);
  // Convenience defaults: A at the earliest step, B at the latest.
  if (list.length) sel.value = String(which === "A" ? list[0] : list[list.length - 1]);
}

async function syncMode() {
  const cross = mode() === "cross";
  el("runBWrap").style.display = cross ? "" : "none";
  if (cross) {
    await loadSteps("B");
  } else {
    el("runB").value = el("runA").value;
    stepsB = stepsA.slice();
    optionList(stepsB, el("stepB"));
    if (stepsB.length) el("stepB").value = String(stepsB[stepsB.length - 1]);
  }
  syncThresholdDefault();
}

function selectedRunMeta(runId) {
  return runsCache.find((r) => r.id === runId);
}

/* CAs move on a discrete lattice: default the move threshold to half a cell. */
function syncThresholdDefault() {
  const meta = selectedRunMeta(el("runA").value);
  if (meta && meta.model === "ca" && !el("moveThreshold").dataset.touched) {
    el("moveThreshold").value = "0.5";
  }
}

/* ------------------------------------------------------------------ */
/* Compare                                                            */
/* ------------------------------------------------------------------ */
async function compare() {
  focusId = null;
  const aRun = el("runA").value;
  const bRun = mode() === "cross" ? el("runB").value : aRun;
  if (!aRun || !bRun) { showNotice(el("notice"), "请先选择运行。", "warn"); return; }
  const aStep = parseInt(el("stepA").value, 10);
  const bStep = parseInt(el("stepB").value, 10);
  if (aRun === bRun && aStep === bStep) {
    showNotice(el("notice"), "A 与 B 是同一个时间步，请选择不同的时间步。", "warn");
    return;
  }
  const mt = parseFloat(el("moveThreshold").value);
  const body = {
    a_run: aRun, b_run: bRun, a_step: aStep, b_step: bStep,
    move_threshold: Number.isFinite(mt) && mt >= 0 ? mt : 1,
  };
  const radius = el("matchRadius").value.trim();
  if (radius !== "") {
    const rv = parseFloat(radius);
    if (Number.isFinite(rv) && rv > 0) body.match_radius = rv;
  }

  showNotice(el("notice"), "对比中…");
  try {
    diffResult = await post("/api/diff", body);
    snapB = await get(`/api/runs/${bRun}/snapshot?step=${bStep}`);
    showNotice(el("notice"), "");
    el("notice").style.display = "none";
    renderAll();
  } catch (e) {
    showNotice(el("notice"), "对比失败：" + e.message, "error");
  }
}

/* ------------------------------------------------------------------ */
/* Summary                                                            */
/* ------------------------------------------------------------------ */
function renderSummary() {
  const r = diffResult;
  el("resultArea").style.display = "";
  el("cmpTitle").textContent =
    `${r.a.name} #${r.a.step}  →  ${r.b.name} #${r.b.step}`;
  el("methodBadge").textContent = r.method === "id"
    ? "配对方式：同运行 ID 追踪（身份可靠）"
    : `配对方式：跨运行空间匹配（自适应半径 ${r.match_radius}，身份为几何推断）`;

  const c = r.counts;
  const tiles = [
    ["新增", c.appeared, "B 中新出现", KIND_COLORS.appeared],
    ["消失", c.disappeared, "A 有 B 无", KIND_COLORS.disappeared],
    ["状态转变", c.state_changed, "同一身份状态变化", KIND_COLORS.state_changed],
    ["发生位移", c.moved, `位移超过 ${r.move_threshold}`, KIND_COLORS.moved],
    ["配对成功", c.matched, `共 ${c.a} → ${c.b} 个个体`, "#4f8cff"],
    ["完全未变", c.unchanged, "状态与位置均未变", KIND_COLORS.unchanged],
  ];
  el("statTiles").innerHTML = tiles.map(([k, v, d, col]) => `
    <div class="stat">
      <div class="k">${k}</div>
      <div class="v" style="color:${col}">${v}</div>
      <div class="d">${d}</div>
    </div>`).join("");

  el("transBox").innerHTML = r.transitions.length
    ? `<div class="muted small" style="margin-bottom:6px">状态转变明细：</div>
       <div class="row gap-6">${r.transitions.map((t) => `
         <span class="diff-tag">${esc(t.type)}：${esc(t.from)} → ${esc(t.to)}
           <b style="color:${KIND_COLORS.state_changed}">×${t.count}</b></span>`).join("")}
       </div>` : "";

  const da = r.displacement.all, dm = r.displacement.moved;
  const methodNote = r.method === "spatial"
    ? `；匹配门限 ${r.match_radius}，低置信度配对请结合画布核对` : "";
  el("dispLine").textContent =
    `配对个体位移（B 世界单位）：平均 ${da.mean}，中位 ${da.median}，最大 ${da.max}` +
    (dm.count ? `；发生位移的 ${dm.count} 个平均 ${dm.mean}${methodNote}` : methodNote);
}

/* ------------------------------------------------------------------ */
/* Canvas overlay                                                     */
/* ------------------------------------------------------------------ */
function layerOn(kind) {
  const t = el("layerToggles").querySelector(`input[data-kind="${kind}"]`);
  return t ? t.checked : true;
}

function rowKind(row) {
  if (row.kind === "appeared") return "appeared";
  if (row.kind === "disappeared") return "disappeared";
  if (row.state_changed) return "state_changed";
  if (row.moved) return "moved";
  return "unchanged";
}

function renderCanvas() {
  const r = diffResult;
  const canvas = el("canvas");
  renderSnapshot(canvas, snapB, r.domain, r.model);
  const info = projectGeometry(canvas, snapB, r.domain, r.model);
  drawDiffOverlay(canvas.getContext("2d"), info, r);
  renderDiffLegend(el("legend"), snapB.palette || {});
}

function projectGeometry(canvas, snap, domain, model) {
  const wrap = canvas.parentElement;
  const maxW = Math.max(320, wrap.clientWidth || 900);
  const b = snap.bounds || { width: 100, height: 100 };
  const W = Math.max(1, b.width), H = Math.max(1, b.height);
  const isRing = domain === "traffic" && model === "abm";
  const isTrafficCA = domain === "traffic" && model === "ca";
  let cw, ch;
  if (isRing) { cw = maxW; ch = 70; }
  else if (isTrafficCA) { cw = maxW; ch = Math.max(3, H) * 26; }
  else if (snap.substrate) {
    cw = maxW;
    ch = Math.round(cw * (snap.substrate.length) / (snap.substrate[0].length || 1));
  } else { cw = maxW; ch = Math.round(cw * H / W); }
  return {
    W, H, cw, ch, isRing, isTrafficCA,
    cellLike: !isRing && (isTrafficCA || !!snap.substrate || isCellLike(snap.individuals || [])),
  };
}

function project(x, y, g) {
  return { px: x / g.W * g.cw, py: g.isRing ? g.ch / 2 : y / g.H * g.ch };
}

function drawDiffOverlay(ctx, g, r) {
  const rows = r.details;
  const showArrows = el("showArrows").checked;

  // Pass 1: displacement arrows (under markers).
  if (showArrows && layerOn("moved")) {
    ctx.strokeStyle = KIND_COLORS.moved;
    ctx.fillStyle = KIND_COLORS.moved;
    ctx.lineWidth = 1.2;
    ctx.globalAlpha = 0.75;
    for (const row of rows) {
      if (row.kind !== "matched" || !row.moved || !rowShouldShow(row)) continue;
      const p0 = project(row.a.x, row.a.y, g);
      const p1 = project(row.b.x, row.b.y, g);
      drawArrow(ctx, p0.px, p0.py, p1.px, p1.py, g, row);
    }
    ctx.globalAlpha = 1;
  }

  // Pass 2: markers.
  for (const row of rows) {
    if (row.kind === "matched" && !row.state_changed && !row.moved) continue;
    if (!rowShouldShow(row)) continue;
    const kind = rowKind(row);
    if (!layerOn(kind)) continue;
    if (row.kind === "disappeared") {
      const p = project(row.a.x, row.a.y, g);
      drawCross(ctx, p.px, p.py, g, KIND_COLORS.disappeared);
    } else if (row.kind === "appeared") {
      const p = project(row.b.x, row.b.y, g);
      drawHalo(ctx, p.px, p.py, g, KIND_COLORS.appeared, true);
    } else {
      const p = project(row.b.x, row.b.y, g);
      if (row.state_changed && layerOn("state_changed")) {
        drawHalo(ctx, p.px, p.py, g, KIND_COLORS.state_changed, false);
      }
      if (row.moved && layerOn("moved")) {
        drawHalo(ctx, p.px, p.py, g, KIND_COLORS.moved, false);
      }
    }
    if (focusId && (row.a_id === focusId || row.b_id === focusId)) {
      const pos = row.kind === "disappeared"
        ? project(row.a.x, row.a.y, g) : project(row.b.x, row.b.y, g);
      const rr = markerRadius(true, g) + 3;
      ctx.strokeStyle = "#fff";
      ctx.lineWidth = 2;
      ctx.beginPath(); ctx.arc(pos.px, pos.py, rr, 0, Math.PI * 2); ctx.stroke();
    }
  }
}

function rowShouldShow(row) {
  const k = rowKind(row);
  if (activeFilter === "changed") {
    return k !== "unchanged";
  }
  return k === activeFilter;
}

function markerRadius(cellLike, g) {
  if (g.isRing) return 6;
  if (cellLike) {
    // Ring the cell, slightly inset so neighbouring highlights stay distinct.
    return Math.max(4, Math.min(9, g.cw / g.W * 0.7));
  }
  return Math.max(3, Math.min(6, g.cw / 320));
}

function drawHalo(ctx, px, py, g, color, filled) {
  const rr = markerRadius(true, g);
  ctx.strokeStyle = color;
  ctx.lineWidth = 2;
  ctx.beginPath(); ctx.arc(px, py, rr, 0, Math.PI * 2); ctx.stroke();
  if (filled) {
    ctx.globalAlpha = 0.25;
    ctx.fillStyle = color;
    ctx.fill();
    ctx.globalAlpha = 1;
  }
}

function drawCross(ctx, px, py, g, color) {
  const rr = markerRadius(true, g) - 1;
  ctx.strokeStyle = color;
  ctx.lineWidth = 2;
  ctx.beginPath();
  ctx.moveTo(px - rr, py - rr); ctx.lineTo(px + rr, py + rr);
  ctx.moveTo(px + rr, py - rr); ctx.lineTo(px - rr, py + rr);
  ctx.stroke();
}

function drawArrow(ctx, x0, y0, x1, y1, g, row) {
  // On a wrapped axis the shortest path crosses a boundary; the backend gives
  // that shortest signed displacement (row.dx/dy).  Draw the wrapped path as
  // two edge-to-edge segments instead of one line spanning the whole canvas.
  const wrap = diffResult.wrap || { x: true, y: true };
  const segs = [];
  if (wrap.x && row.dx != null && Math.abs(row.dx / g.W) > 0.5 - 1e-9) {
    // x0->x1 crossed the x seam (the straight pixel jump is the long way).
    const wrappedLeft = x1 < x0; // short motion went right-to-left over seam 0
    const edge0 = wrappedLeft ? 0 : g.cw;
    const edge1 = wrappedLeft ? g.cw : 0;
    segs.push([x0, y0, edge0, y0]);
    segs.push([edge1, y1, x1, y1]);
  } else if (wrap.y && row.dy != null && Math.abs(row.dy / g.H) > 0.5 - 1e-9) {
    const wrappedTop = y1 < y0;
    const edge0 = wrappedTop ? 0 : g.ch;
    const edge1 = wrappedTop ? g.ch : 0;
    segs.push([x0, y0, x0, edge0]);
    segs.push([x1, edge1, x1, y1]);
  } else {
    segs.push([x0, y0, x1, y1]);
  }
  for (const [ax, ay, bx, by] of segs) strokeSegment(ctx, ax, ay, bx, by);
  const last = segs[segs.length - 1];
  arrowHead(ctx, last[2], last[3], last[0], last[1]);
}

function strokeSegment(ctx, x0, y0, x1, y1) {
  if (Math.hypot(x1 - x0, y1 - y0) < 1.5) return;
  ctx.beginPath(); ctx.moveTo(x0, y0); ctx.lineTo(x1, y1); ctx.stroke();
}

function arrowHead(ctx, x, y, fromx, fromy) {
  const ang = Math.atan2(y - fromy, x - fromx);
  const head = 5;
  ctx.beginPath();
  ctx.moveTo(x, y);
  ctx.lineTo(x - head * Math.cos(ang - 0.4), y - head * Math.sin(ang - 0.4));
  ctx.lineTo(x - head * Math.cos(ang + 0.4), y - head * Math.sin(ang + 0.4));
  ctx.closePath();
  ctx.fill();
}

function renderDiffLegend(container, palette) {
  const items = Object.entries(KIND_LABELS)
    .filter(([k]) => k !== "unchanged")
    .map(([k, v]) => `<span class="item">
      <span class="swatch" style="background:${KIND_COLORS[k]}"></span>${v}</span>`);
  const pal = Object.entries(palette)
    .map(([k, v]) => `<span class="item"><span class="swatch" style="background:${v.color}"></span>${esc(v.label || k)}（B 状态）</span>`);
  container.innerHTML = items.join("") +
    '<span class="item" style="margin-left:8px;border-left:1px solid var(--border);padding-left:12px"></span>' +
    pal.join("");
}

/* ------------------------------------------------------------------ */
/* Detail table                                                       */
/* ------------------------------------------------------------------ */
function renderChips() {
  const c = diffResult.counts;
  const chips = [
    ["changed", "全部变化", c.appeared + c.disappeared + c.state_changed + c.moved],
    ["appeared", "新增", c.appeared],
    ["disappeared", "消失", c.disappeared],
    ["state_changed", "状态转变", c.state_changed],
    ["moved", "位移", c.moved],
  ];
  el("filterChips").innerHTML = chips.map(([k, label, n]) => `
    <button class="btn small diff-chip ${activeFilter === k ? "primary" : ""}"
      data-filter="${k}">${label} <span class="mono">${n}</span></button>`).join("");
  el("filterChips").querySelectorAll(".diff-chip").forEach((btn) => {
    btn.onclick = () => { activeFilter = btn.dataset.filter; page = 0; renderChips(); renderTable(); renderCanvas(); };
  });
}

function applyTableFilter() {
  const q = el("searchInput").value.trim().toLowerCase();
  const tf = el("typeFilter").value;
  filteredRows = allRows.filter((row) => {
    const kind = rowKind(row);
    if (activeFilter !== "changed" && kind !== activeFilter) return false;
    if (activeFilter === "changed" && kind === "unchanged") return false;
    if (tf && row.type !== tf) return false;
    if (q) {
      const ids = `${row.a_id || ""} ${row.b_id || ""}`.toLowerCase();
      if (!ids.includes(q)) return false;
    }
    return true;
  });
  page = 0;
}

function renderTable() {
  const start = page * PAGE_SIZE;
  const rows = filteredRows.slice(start, start + PAGE_SIZE);
  const head = ["类别", "类型", "A 个体", "B 个体", "A 状态", "B 状态", "位移", "置信度"]
    .map((h) => `<th>${h}</th>`).join("");
  const body = rows.map((row) => {
    const kind = rowKind(row);
    const color = KIND_COLORS[kind];
    const clickable = row.a_id || row.b_id;
    const aid = row.a_id ? `${esc(String(row.a_id))}` : '<span class="muted">—</span>';
    const bid = row.b_id ? `${esc(String(row.b_id))}` : '<span class="muted">—</span>';
    const ast = row.a_state != null ? esc(row.a_state) : '<span class="muted">—</span>';
    const bst = row.b_state != null ? esc(row.b_state) : '<span class="muted">—</span>';
    const conf = CONF_LABELS[row.confidence] || row.confidence;
    return `<tr data-focus="${esc(clickable || "")}" style="cursor:${clickable ? "pointer" : "default"}">
      <td><span class="badge" style="color:${color};border-color:${color}">${KIND_LABELS[kind]}</span>
        ${row.ambiguous ? '<span class="badge" title="存在距离相近的另一个候选，配对可能存在歧义">歧义?</span>' : ""}</td>
      <td>${esc(row.type)}</td>
      <td class="mono">${aid}</td>
      <td class="mono">${bid}</td>
      <td>${ast}</td><td>${bst}</td>
      <td class="num">${row.distance == null ? "—" : fmt(row.distance)}</td>
      <td class="small muted">${esc(conf)}</td>
    </tr>`;
  }).join("");
  el("dtable").innerHTML =
    `<thead><tr>${head}</tr></thead>
     <tbody>${body || '<tr><td colspan="8" class="muted">该筛选下无差异记录</td></tr>'}</tbody>`;
  el("dtable").querySelectorAll("tr[data-focus]").forEach((tr) => {
    tr.onclick = () => {
      focusId = tr.dataset.focus || null;
      renderCanvas();
    };
  });
  el("pageInfo").textContent =
    `第 ${page + 1} / ${Math.max(1, Math.ceil(filteredRows.length / PAGE_SIZE))} 页，共 ${filteredRows.length} 条`;
  el("prevBtn").disabled = page === 0;
  el("nextBtn").disabled = (page + 1) * PAGE_SIZE >= filteredRows.length;
}

function renderAll() {
  allRows = diffResult.details;
  const types = [...new Set(allRows.map((r) => r.type))];
  el("typeFilter").innerHTML = '<option value="">全部类型</option>' +
    types.map((t) => `<option>${esc(t)}</option>`).join("");
  renderSummary();
  renderChips();
  applyTableFilter();
  renderTable();
  renderCanvas();
}

/* ------------------------------------------------------------------ */
/* Init                                                               */
/* ------------------------------------------------------------------ */
function init() {
  document.querySelectorAll('input[name="mode"]').forEach((r) => (r.onchange = syncMode));
  el("refreshBtn").onclick = async () => {
    const a = el("runA").value, b = el("runB").value;
    await refreshRuns(a, b);
    await loadSteps("A");
    await syncMode();
  };
  el("runA").onchange = async () => { await loadSteps("A"); await syncMode(); };
  el("runB").onchange = () => loadSteps("B");
  el("moveThreshold").oninput = (e) => { e.target.dataset.touched = "1"; };
  el("compareBtn").onclick = compare;
  el("searchInput").oninput = debounce(() => { applyTableFilter(); renderTable(); }, 200);
  el("typeFilter").onchange = () => { applyTableFilter(); renderTable(); };
  el("prevBtn").onclick = () => { if (page > 0) { page--; renderTable(); } };
  el("nextBtn").onclick = () => { if ((page + 1) * PAGE_SIZE < filteredRows.length) { page++; renderTable(); } };
  el("layerToggles").querySelectorAll("input").forEach((i) => (i.onchange = () => diffResult && renderCanvas()));
  window.addEventListener("resize", debounce(() => diffResult && renderCanvas(), 200));

  (async () => {
    await refreshRuns();
    if (!runsCache.length) {
      showNotice(el("notice"), "还没有任何运行，请先在「实时可视化」或「场景配置」中创建并运行仿真。", "warn");
      return;
    }
    await loadSteps("A");
    await syncMode();
    el("matchHint").textContent =
      "同一运行按个体 ID 精确追踪（位置相近、状态相同或个体消失都不会认错）；" +
      "跨运行时编号体系不同，将按几何位置做自适应最近邻配对，并给出置信度。";
  })();
}

init();
