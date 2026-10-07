/* Canvas renderer shared by the real-time visualization and replay pages.

 * `renderSnapshot(canvas, snap, domain, model)` draws a simulation snapshot
 * (bounds + palette + individuals + optional substrate) onto a 2D canvas.  It
 * is domain-agnostic: colors come from the snapshot palette, and the drawing
 * mode (road strip / lattice cells / continuous points) is chosen from the
 * domain/model plus the coordinate type of the individuals.
 */

function colorOf(palette, state, type) {
  const c = (palette[state] || palette[type] || {}).color;
  return c || "#cccccc";
}

function isCellLike(inds) {
  if (!inds.length) return false;
  const sample = inds.slice(0, 10);
  return sample.every((a) => Number.isInteger(a.x) && Number.isInteger(a.y));
}

function drawPoints(ctx, inds, cw, ch, W, H, palette) {
  const r = Math.max(1.5, Math.min(4, cw / 320));
  for (const a of inds) {
    ctx.fillStyle = colorOf(palette, a.state, a.type);
    ctx.beginPath();
    ctx.arc(a.x / W * cw, a.y / H * ch, r, 0, Math.PI * 2);
    ctx.fill();
  }
}

function drawCells(ctx, inds, cw, ch, W, H, palette) {
  const cw1 = cw / W, ch1 = ch / H;
  for (const a of inds) {
    ctx.fillStyle = colorOf(palette, a.state, a.type);
    ctx.fillRect(a.x * cw1, a.y * ch1, Math.max(1, cw1), Math.max(1, ch1));
  }
}

function drawLanes(ctx, cw, ch, lanes) {
  ctx.strokeStyle = "#1f2a37";
  ctx.lineWidth = 1;
  for (let i = 1; i < lanes; i++) {
    const y = i / lanes * ch;
    ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(cw, y); ctx.stroke();
  }
}

function drawRing(ctx, inds, cw, ch, L, palette) {
  const mid = ch / 2;
  ctx.strokeStyle = "#26313f";
  ctx.lineWidth = 3;
  ctx.beginPath(); ctx.moveTo(0, mid); ctx.lineTo(cw, mid); ctx.stroke();
  for (const a of inds) {
    ctx.fillStyle = colorOf(palette, a.state, a.type);
    ctx.fillRect(a.x / L * cw - 2, mid - 5, 4, 10);
  }
}

function drawSubstrate(ctx, substrate, cw, ch) {
  const gh = substrate.length, gw = substrate[0].length || 1;
  const cw1 = cw / gw, ch1 = ch / gh;
  for (let y = 0; y < gh; y++) {
    for (let x = 0; x < gw; x++) {
      ctx.fillStyle = substrate[y][x] ? "#1e3b2a" : "#0c1116";
      ctx.fillRect(x * cw1, y * ch1, cw1 + 0.5, ch1 + 0.5);
    }
  }
}

function renderSnapshot(canvas, snap, domain, model) {
  const ctx = canvas.getContext("2d");
  const bounds = snap.bounds || { width: 100, height: 100 };
  const W = Math.max(1, bounds.width), H = Math.max(1, bounds.height);
  const inds = snap.individuals || [];
  const palette = snap.palette || {};
  const substrate = snap.substrate || null;
  const wrap = canvas.parentElement;
  const maxW = Math.max(320, wrap.clientWidth || 900);
  const dpr = window.devicePixelRatio || 1;

  let cw, ch;
  const isRing = domain === "traffic" && model === "abm";
  const isTrafficCA = domain === "traffic" && model === "ca";

  if (isRing) {
    cw = maxW; ch = 70;
  } else if (isTrafficCA) {
    const laneH = 26;
    cw = maxW; ch = Math.max(3, H) * laneH;
  } else if (substrate) {
    const gh = substrate.length, gw = substrate[0].length || 1;
    cw = maxW; ch = Math.round(cw * gh / gw);
  } else {
    cw = maxW; ch = Math.round(cw * H / W);
  }

  canvas.width = Math.round(cw * dpr);
  canvas.height = Math.round(ch * dpr);
  canvas.style.height = ch + "px";
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.fillStyle = "#0a0e12";
  ctx.fillRect(0, 0, cw, ch);

  if (isRing) {
    drawRing(ctx, inds, cw, ch, W, palette);
  } else if (isTrafficCA) {
    drawLanes(ctx, cw, ch, H);
    drawCells(ctx, inds, cw, ch, W, H, palette);
  } else if (substrate) {
    drawSubstrate(ctx, substrate, cw, ch);
    drawCells(ctx, inds, cw, ch, W, H, palette);
  } else if (isCellLike(inds)) {
    drawCells(ctx, inds, cw, ch, W, H, palette);
  } else {
    drawPoints(ctx, inds, cw, ch, W, H, palette);
  }

  return { cw, ch };
}

function renderLegend(container, palette) {
  if (!container) return;
  container.innerHTML = Object.entries(palette)
    .map(([key, v]) => `<span class="item"><span class="swatch" style="background:${v.color}"></span>${esc(v.label || key)}</span>`)
    .join("");
}

const DIFF_COLORS = {
  added: "#2ecc71",
  removed: "#e74c3c",
  state_changed: "#f39c12",
  moved: "#4f8cff",
  both: "#e056fd",
  unchanged: "rgba(139,152,165,.28)",
};

function primaryDiffKind(ann = {}) {
  const kinds = ann.kinds || [];
  if (kinds.includes("added")) return "added";
  if (kinds.includes("removed")) return "removed";
  if (kinds.includes("state_changed") && kinds.includes("moved")) return "both";
  if (kinds.includes("state_changed")) return "state_changed";
  if (kinds.includes("moved")) return "moved";
  return "unchanged";
}

function canvasGeometry(canvas, snap, domain, model) {
  const wrap = canvas.parentElement;
  const maxW = Math.max(320, wrap.clientWidth || 900);
  const bounds = snap.bounds || { width: 100, height: 100 };
  const W = Math.max(1, bounds.width), H = Math.max(1, bounds.height);
  const substrate = snap.substrate || null;
  let cw, ch;
  if (domain === "traffic" && model === "abm") {
    cw = maxW; ch = 70;
  } else if (domain === "traffic" && model === "ca") {
    cw = maxW; ch = Math.max(3, H) * 26;
  } else if (substrate) {
    const gh = substrate.length, gw = substrate[0].length || 1;
    cw = maxW; ch = Math.round(cw * gh / gw);
  } else {
    cw = maxW; ch = Math.round(cw * H / W);
  }
  return { cw, ch, W, H };
}

function prepareCanvas(canvas, geom) {
  const dpr = window.devicePixelRatio || 1;
  canvas.width = Math.round(geom.cw * dpr);
  canvas.height = Math.round(geom.ch * dpr);
  canvas.style.height = geom.ch + "px";
  const ctx = canvas.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.fillStyle = "#0a0e12";
  ctx.fillRect(0, 0, geom.cw, geom.ch);
  return ctx;
}

function diffPoint(x, y, cw, ch, W, H, domain, model, cell = false) {
  if (domain === "traffic" && model === "abm") {
    return { x: x / W * cw, y: ch / 2, ring: true };
  }
  if (cell) return { x: (x + .5) / W * cw, y: (y + .5) / H * ch };
  return { x: x / W * cw, y: y / H * ch };
}

function wrapSegments(x1, y1, x2, y2, W, H) {
  let dx = x2 - x1, dy = y2 - y1;
  if (W) dx = ((dx + W / 2) % W + W) % W;
  if (dx > W / 2) dx -= W;
  if (H) dy = ((dy + H / 2) % H + H) % H;
  if (dy > H / 2) dy -= H;
  const starts = [{ x: x1, y: y1 }];
  if (W && Math.abs(dx) > W / 2 - 1) starts.push({ x: x1 + Math.sign(dx) * W, y: y1 });
  if (H && Math.abs(dy) > H / 2 - 1) starts.push({ x: x1, y: y1 + Math.sign(dy) * H });
  if (W && H && Math.abs(dx) > W / 2 - 1 && Math.abs(dy) > H / 2 - 1) {
    starts.push({ x: x1 + Math.sign(dx) * W, y: y1 + Math.sign(dy) * H });
  }
  return starts.map((p) => ({ x1: p.x, y1: p.y, x2: p.x + dx, y2: p.y + dy }));
}

function drawArrow(ctx, x1, y1, x2, y2, color, ring = false) {
  ctx.strokeStyle = color;
  ctx.fillStyle = color;
  ctx.globalAlpha = .72;
  ctx.lineWidth = 1.4;
  ctx.beginPath(); ctx.moveTo(x1, y1); ctx.lineTo(x2, y2); ctx.stroke();
  if (Math.hypot(x2 - x1, y2 - y1) < 3) return;
  const ang = Math.atan2(y2 - y1, x2 - x1);
  const size = ring ? 5 : 6;
  ctx.beginPath();
  ctx.moveTo(x2, y2);
  ctx.lineTo(x2 - size * Math.cos(ang - .45), y2 - size * Math.sin(ang - .45));
  ctx.lineTo(x2 - size * Math.cos(ang + .45), y2 - size * Math.sin(ang + .45));
  ctx.closePath(); ctx.fill();
  ctx.globalAlpha = 1;
}

function drawDiffShape(ctx, p, color, kind, cell, ring) {
  ctx.fillStyle = color;
  ctx.strokeStyle = color;
  if (cell) {
    const s = Math.max(4, Math.min(12, Math.min(cell.w, cell.h) * .9));
    ctx.globalAlpha = kind === "unchanged" ? .18 : .92;
    if (kind === "removed") {
      ctx.lineWidth = 2;
      ctx.strokeRect(p.x - s / 2, p.y - s / 2, s, s);
    } else {
      ctx.fillRect(p.x - s / 2, p.y - s / 2, s, s);
      if (kind === "added") {
        ctx.strokeRect(p.x - s / 2 - 2, p.y - s / 2 - 2, s + 4, s + 4);
      }
    }
    ctx.globalAlpha = 1;
    return;
  }
  const r = ring ? 3.2 : Math.max(2.2, Math.min(5, cell.w / 70));
  ctx.globalAlpha = kind === "unchanged" ? .16 : .95;
  ctx.beginPath(); ctx.arc(p.x, p.y, r, 0, Math.PI * 2); ctx.fill();
  if (kind === "removed") {
    ctx.lineWidth = 2;
    ctx.beginPath(); ctx.arc(p.x, p.y, r + 3, 0, Math.PI * 2); ctx.stroke();
  }
  if (kind === "added") {
    ctx.lineWidth = 2;
    ctx.beginPath(); ctx.arc(p.x, p.y, r + 3.5, 0, Math.PI * 2); ctx.stroke();
  }
  ctx.globalAlpha = 1;
}

/* Draw the target snapshot with added/changed/moved highlights plus removed
   individuals from the base snapshot and displacement arrows. */
function renderDiffOverlay(canvas, diff, options = {}) {
  const domain = diff.domain, model = diff.model;
  const snap = diff.target_snapshot;
  const geom = canvasGeometry(canvas, snap, domain, model);
  const ctx = prepareCanvas(canvas, geom);
  const { cw, ch, W, H } = geom;
  const cell = domain === "traffic" && model === "abm"
    ? { w: cw / W, h: 12 }
    : { w: cw / W, h: ch / H };
  const isCell = !!(domain === "traffic" && model === "ca") ||
    !!(snap.substrate) || isCellLike(snap.individuals || []);
  const showUnchanged = options.showUnchanged !== false;
  const showArrows = options.showArrows !== false;
  const matchKey = (id, x, y) => `${id}@${Number(x).toFixed(4)},${Number(y).toFixed(4)}`;
  const baseByTarget = new Map((diff.matches || []).map((m) =>
    [matchKey(m.target_id, m.target_x, m.target_y), m]));

  if (domain === "traffic" && model === "ca") drawLanes(ctx, cw, ch, H);
  if (snap.substrate) drawSubstrate(ctx, snap.substrate, cw, ch);
  if (domain === "traffic" && model === "abm") {
    ctx.strokeStyle = "#26313f"; ctx.lineWidth = 3;
    ctx.beginPath(); ctx.moveTo(0, ch / 2); ctx.lineTo(cw, ch / 2); ctx.stroke();
  }

  for (const a of diff.removed || []) {
    const p = diffPoint(a.x, a.y, cw, ch, W, H, domain, model, isCell);
    drawDiffShape(ctx, p, DIFF_COLORS.removed, "removed", cell, p.ring);
  }

  for (const a of snap.individuals || []) {
    const ann = a._diff || { kinds: [] };
    const kind = primaryDiffKind(ann);
    if (kind === "unchanged" && !showUnchanged) continue;
    const p = diffPoint(a.x, a.y, cw, ch, W, H, domain, model, isCell);
    const match = baseByTarget.get(matchKey(a.id, a.x, a.y));
    if (showArrows && kind !== "added" && kind !== "removed" &&
        kind !== "unchanged" && match) {
      const q = diffPoint(match.base_x, match.base_y, cw, ch, W, H, domain, model, isCell);
      const color = kind === "state_changed" ? DIFF_COLORS.state_changed
        : kind === "both" ? DIFF_COLORS.both : DIFF_COLORS.moved;
      wrapSegments(q.x, q.y, p.x, p.y, cw, ch).forEach((s) =>
        drawArrow(ctx, s.x1, s.y1, s.x2, s.y2, color, p.ring));
    }
    drawDiffShape(ctx, p, DIFF_COLORS[kind], kind, cell, p.ring);
  }
  return geom;
}
