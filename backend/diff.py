"""Individual-level state diff between two simulation snapshots.

The diff answers "变化从哪来": picking two time steps of one run (or one step of
two different runs), it decides which individuals in snapshot B are the *same*
individuals as in snapshot A, then classifies every individual as:

* ``appeared``      — only in B (newly born / added / unmatched)
* ``disappeared``   — only in A (died / removed / unmatched)
* ``matched``       — present in both; possibly ``state_changed`` and/or
  ``moved`` (displacement beyond a threshold), otherwise ``unchanged``.

Identity matching has two deliberately different modes:

``id`` (same run)
    Individuals are paired by their stable ``(type, id)`` key.  Within one run
    an engine keeps an agent's id for its whole life (ids are *never* reused),
    so this stays correct when two agents stand on neighbouring cells, share
    the same state, or one of them has vanished — exactly the situations in
    which nearest-position matching would swap identities.

``spatial`` (two different runs)
    Ids are run-local (``p00001`` in run X is unrelated to ``p00001`` in run Y)
    and the populations may have different sizes, so ids cannot be compared.
    Individuals of the same type are paired geometrically: wrap-aware
    (toroidal) nearest-distance candidates within an adaptive gate are resolved
    by a *globally greedy* assignment (shortest edge first, one-to-one), which
    prevents the classic swap mistake — two close, equal-state agents each grab
    the other's nearest partner.  The gate derives from the median nearest
    neighbour spacing, so genuinely farther counterparts stay unmatched
    (reported as appeared/disappeared) instead of being force-paired.
"""

from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Tuple

Pos = Tuple[float, float]

# Gate = factor × median nearest-neighbour spacing (in normalised [0,1]² space).
_GATE_FACTOR = 3.5
_GATE_MIN = 0.004
_GATE_MAX = 0.3


# --------------------------------------------------------------------------- #
# Geometry helpers
# --------------------------------------------------------------------------- #
def wrap_axes(domain: str, model: str) -> Dict[str, bool]:
    """Which axes wrap around (torus) for a given engine.

    Epidemic/ecology worlds are toroidal on both axes; the traffic engines are
    rings (x wraps, lanes do not).
    """
    if domain == "traffic":
        return {"x": True, "y": False}
    return {"x": True, "y": True}


def _snap_wrap(snap: Dict[str, Any], domain: str, model: str) -> Dict[str, bool]:
    wrap = snap.get("wrap")
    if isinstance(wrap, dict) and "x" in wrap and "y" in wrap:
        return {"x": bool(wrap["x"]), "y": bool(wrap["y"])}
    return wrap_axes(domain, model)


def _bounds(snap: Dict[str, Any]) -> Tuple[float, float]:
    b = snap.get("bounds") or {}
    return max(1.0, float(b.get("width", 100.0))), max(
        1.0, float(b.get("height", 100.0)))


def _norm(ind: Dict[str, Any], w: float, h: float) -> Pos:
    return float(ind.get("x", 0.0)) / w, float(ind.get("y", 0.0)) / h


def torus_delta(pa: Pos, pb: Pos, wrap: Dict[str, bool]) -> Tuple[float, float]:
    """Shortest signed displacement ``pa - pb`` on an optionally wrapped plane."""
    dx = pa[0] - pb[0]
    dy = pa[1] - pb[1]
    if wrap["x"]:
        dx = (dx + 0.5) % 1.0 - 0.5
    if wrap["y"]:
        dy = (dy + 0.5) % 1.0 - 0.5
    return dx, dy


def _dist(pa: Pos, pb: Pos, wrap: Dict[str, bool]) -> float:
    dx, dy = torus_delta(pa, pb, wrap)
    return math.hypot(dx, dy)


# --------------------------------------------------------------------------- #
# Matching
# --------------------------------------------------------------------------- #
def _index_by_id(individuals: List[Dict[str, Any]]
                 ) -> Dict[Tuple[str, str], Dict[str, Any]]:
    return {(str(a.get("type", "")), str(a.get("id", ""))): a
            for a in individuals}


def match_by_id(a: List[Dict[str, Any]], b: List[Dict[str, Any]]
                ) -> Tuple[List[Tuple[Dict[str, Any], Dict[str, Any]]],
                           List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Pair same-run individuals by stable ``(type, id)``."""
    ma = _index_by_id(a)
    mb = _index_by_id(b)
    pairs = [(ma[k], mb[k]) for k in ma.keys() & mb.keys()]
    only_a = [ma[k] for k in ma.keys() - mb.keys()]
    only_b = [mb[k] for k in mb.keys() - ma.keys()]
    return pairs, only_a, only_b


def _median(values: List[float]) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    mid = len(s) // 2
    if len(s) % 2:
        return s[mid]
    return 0.5 * (s[mid - 1] + s[mid])


def _nearest_spacing(points: List[Pos], wrap: Dict[str, bool]) -> List[float]:
    """Distance to the nearest other point for every point (spatial-hash O(nk))."""
    if len(points) < 2:
        return []
    # One bucket per ~0.05 normalised unit gives ~400 cells worst case.
    cs = 0.05
    grid: Dict[Tuple[int, int], List[int]] = {}
    for i, p in enumerate(points):
        grid.setdefault((int(math.floor(p[0] / cs)),
                         int(math.floor(p[1] / cs))), []).append(i)
    out: List[float] = []
    for i, p in enumerate(points):
        cx, cy = int(math.floor(p[0] / cs)), int(math.floor(p[1] / cs))
        best = float("inf")
        for gx in range(cx - 2, cx + 3):
            for gy in range(cy - 2, cy + 3):
                if wrap["x"]:
                    gx %= max(1, int(math.ceil(1.0 / cs)))
                elif not 0 <= gx < math.ceil(1.0 / cs):
                    continue
                if wrap["y"]:
                    gy %= max(1, int(math.ceil(1.0 / cs)))
                elif not 0 <= gy < math.ceil(1.0 / cs):
                    continue
                for j in grid.get((gx, gy), ()):
                    if j == i:
                        continue
                    d = _dist(p, points[j], wrap)
                    if d < best:
                        best = d
        if best < float("inf"):
            out.append(best)
    return out


def _candidate_edges(pa: List[Pos], pb: List[Pos], gate: float,
                     wrap: Dict[str, bool]
                     ) -> Tuple[List[Tuple[float, int, int]], Dict[int, Tuple[float, float]]]:
    """All A→B pairs closer than ``gate`` (normalised), via a spatial hash.

    Also returns each A's best / second-best candidate distance so the caller
    can flag an ambiguous match (two close candidates at nearly equal range).
    """
    cs = max(gate, 1e-6)
    nx_cells = max(1, math.ceil(1.0 / cs))
    grid: Dict[Tuple[int, int], List[int]] = {}
    for j, p in enumerate(pb):
        key = (int(math.floor(p[0] / cs)) % nx_cells if wrap["x"]
               else int(math.floor(p[0] / cs)),
               int(math.floor(p[1] / cs)) % nx_cells if wrap["y"]
               else int(math.floor(p[1] / cs)))
        grid.setdefault(key, []).append(j)

    radius_cells = 2  # cell size == gate: a gate-distance point can be 2 buckets away
    edges: List[Tuple[float, int, int]] = []
    runners: Dict[int, Tuple[float, float]] = {}
    for i, p in enumerate(pa):
        cx = int(math.floor(p[0] / cs))
        cy = int(math.floor(p[1] / cs))
        best = second = float("inf")
        for gx in range(cx - radius_cells, cx + radius_cells + 1):
            for gy in range(cy - radius_cells, cy + radius_cells + 1):
                kx = gx % nx_cells if wrap["x"] else gx
                ky = gy % nx_cells if wrap["y"] else gy
                if not wrap["x"] and not 0 <= kx < nx_cells:
                    continue
                if not wrap["y"] and not 0 <= ky < nx_cells:
                    continue
                for j in grid.get((kx, ky), ()):
                    d = _dist(p, pb[j], wrap)
                    if d <= gate:
                        edges.append((d, i, j))
                    if d < best:
                        best, second = d, best
                    elif d < second:
                        second = d
        runners[i] = (best, second)
    return edges, runners


def match_spatial(
    a: List[Dict[str, Any]],
    b: List[Dict[str, Any]],
    wa: float, ha: float, wb: float, hb: float,
    wrap: Dict[str, bool],
    gate: float,
) -> Tuple[List[Tuple[Dict[str, Any], Dict[str, Any], float, float, bool]],
           List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Pair individuals across runs: one-to-one global-greedy nearest matching.

    Edges are ordered by (distance, same-state-first): a closer candidate
    always wins, but when two candidates tie (common on a lattice) the one with
    the same state is preferred.  Returns ``(pairs, only_a, only_b)`` where
    each pair carries the normalised distance, the gate used and an ambiguity
    flag (a close runner-up candidate existed).
    """
    pa = [_norm(x, wa, ha) for x in a]
    pb = [_norm(x, wb, hb) for x in b]
    states_a = [str(x.get("state", "")) for x in a]
    states_b = [str(x.get("state", "")) for x in b]
    edges0, runners = _candidate_edges(pa, pb, gate, wrap)
    # Global ordering: nearest edge first; on a tie prefer same-state partner.
    edges = [(d, 0 if states_a[i] == states_b[j] else 1, i, j)
             for d, i, j in edges0]
    edges.sort(key=lambda e: (e[0], e[1], e[2], e[3]))

    used_a: set = set()
    used_b: set = set()
    chosen: Dict[int, Tuple[int, float]] = {}
    for d, _ss, i, j in edges:
        if i in used_a or j in used_b:
            continue
        used_a.add(i)
        used_b.add(j)
        chosen[i] = (j, d)

    pairs = []
    for i, ind_a in enumerate(a):
        if i not in chosen:
            continue
        j, d = chosen[i]
        _, second = runners.get(i, (float("inf"), float("inf")))
        ambiguous = second < float("inf") and (second - d) <= 0.25 * gate
        pairs.append((ind_a, b[j], d, gate, ambiguous))

    only_a = [x for i, x in enumerate(a) if i not in used_a]
    only_b = [x for j, x in enumerate(b) if j not in used_b]
    return pairs, only_a, only_b


# --------------------------------------------------------------------------- #
# Diff
# --------------------------------------------------------------------------- #
def _confidence(d: float, gate: float, ambiguous: bool) -> str:
    base = "high" if d <= 0.4 * gate else "medium" if d <= 0.75 * gate else "low"
    if ambiguous and base == "high":
        return "medium"
    return base


def compute_diff(snap_a: Dict[str, Any], snap_b: Dict[str, Any],
                 domain: str, model: str, *, method: str = "auto",
                 move_threshold: float = 1.0,
                 match_radius: Optional[float] = None) -> Dict[str, Any]:
    """Compare two snapshots and return identity-resolved differences.

    ``move_threshold`` / ``match_radius`` are in B-snapshot *world units*
    (cells for CAs); ``match_radius`` None selects an adaptive radius.
    """
    inds_a = list(snap_a.get("individuals") or [])
    inds_b = list(snap_b.get("individuals") or [])
    wa, ha = _bounds(snap_a)
    wb, hb = _bounds(snap_b)
    wrap = _snap_wrap(snap_b, domain, model)
    # User-supplied radii are in B world units.  Normalise against the longest
    # axis — the ring road is 1000×60 but movement happens along the 1000 axis.
    radius_scale = max(wb, hb)

    if method not in ("auto", "id", "spatial"):
        raise ValueError(f"未知对比配对方式: {method}（可选 auto/id/spatial）")
    if method == "auto":
        method = "id" if snap_a.get("_run_id") == snap_b.get("_run_id") and \
            snap_a.get("_run_id") is not None else "spatial"

    details: List[Dict[str, Any]] = []
    by_type: Dict[str, Dict[str, int]] = {}

    def bucket(typ: str) -> Dict[str, int]:
        return by_type.setdefault(typ, {
            "a": 0, "b": 0, "matched": 0, "appeared": 0,
            "disappeared": 0, "state_changed": 0, "moved": 0, "unchanged": 0})

    groups_a: Dict[str, List[Dict[str, Any]]] = {}
    groups_b: Dict[str, List[Dict[str, Any]]] = {}
    for x in inds_a:
        groups_a.setdefault(str(x.get("type", "")), []).append(x)
    for x in inds_b:
        groups_b.setdefault(str(x.get("type", "")), []).append(x)

    transitions: Dict[Tuple[str, str, str], int] = {}
    all_dist: List[float] = []
    moved_dist: List[float] = []
    counts = {"a": len(inds_a), "b": len(inds_b), "matched": 0,
              "appeared": 0, "disappeared": 0, "state_changed": 0,
              "moved": 0, "unchanged": 0}
    gate_used_n = 0.0

    for typ in sorted(set(groups_a) | set(groups_b)):
        ga = groups_a.get(typ, [])
        gb = groups_b.get(typ, [])
        bt = bucket(typ)
        bt["a"], bt["b"] = len(ga), len(gb)

        if method == "id":
            pairs0, only_a, only_b = match_by_id(ga, gb)
            pairs = [(ia, ib, _dist(_norm(ia, wa, ha), _norm(ib, wb, hb), wrap),
                      0.0, False) for ia, ib in pairs0]
        else:
            # One adaptive gate per type (rabbits and foxes live at different
            # densities; a global gate would cross-pair sparse/dense groups).
            gate_n = _adaptive_gate_norm(ga, gb, wa, ha, wb, hb, wrap,
                                         match_radius / radius_scale
                                         if match_radius else None)
            gate_used_n = max(gate_used_n, gate_n)
            pairs, only_a, only_b = match_spatial(
                ga, gb, wa, ha, wb, hb, wrap, gate_n)

        for ia, ib, dnorm, gate, ambiguous in pairs:
            pna, pnb = _norm(ia, wa, ha), _norm(ib, wb, hb)
            dxn, dyn = torus_delta(pna, pnb, wrap)
            # Per-axis denormalisation keeps anisotropic worlds (ring roads:
            # 1000×60) honest about how far an individual actually moved.
            d = math.hypot(dxn * wb, dyn * hb)
            changed_state = ia.get("state") != ib.get("state")
            moved = d > float(move_threshold)
            all_dist.append(d)
            if moved:
                moved_dist.append(d)
            rec = {
                "kind": "matched",
                "type": typ,
                "a_id": ia.get("id"), "b_id": ib.get("id"),
                "a_state": ia.get("state"), "b_state": ib.get("state"),
                "a": {"x": ia.get("x"), "y": ia.get("y")},
                "b": {"x": ib.get("x"), "y": ib.get("y")},
                "distance": round(d, 3),
                "distance_norm": round(dnorm, 5),
                # Shortest wrap-aware displacement in B world units; the
                # frontend splits it at the canvas boundary on torus worlds.
                "dx": round(dxn * wb, 3), "dy": round(dyn * hb, 3),
                "state_changed": changed_state,
                "moved": moved,
                "ambiguous": bool(ambiguous) if method == "spatial" else False,
                "confidence": "id" if method == "id"
                else _confidence(dnorm, gate, ambiguous),
            }
            details.append(rec)
            counts["matched"] += 1
            bt["matched"] += 1
            if changed_state:
                counts["state_changed"] += 1
                bt["state_changed"] += 1
                key = (str(ia.get("state")), str(ib.get("state")), typ)
                transitions[key] = transitions.get(key, 0) + 1
            if moved:
                counts["moved"] += 1
                bt["moved"] += 1
            if not changed_state and not moved:
                counts["unchanged"] += 1
                bt["unchanged"] += 1

        for ia in only_a:
            details.append({
                "kind": "disappeared", "type": typ,
                "a_id": ia.get("id"), "b_id": None,
                "a_state": ia.get("state"), "b_state": None,
                "a": {"x": ia.get("x"), "y": ia.get("y")},
                "b": None, "distance": None, "distance_norm": None,
                "state_changed": False, "moved": False,
                "ambiguous": False, "confidence": "id" if method == "id" else "high",
            })
            counts["disappeared"] += 1
            bt["disappeared"] += 1

        for ib in only_b:
            details.append({
                "kind": "appeared", "type": typ,
                "a_id": None, "b_id": ib.get("id"),
                "a_state": None, "b_state": ib.get("state"),
                "a": None,
                "b": {"x": ib.get("x"), "y": ib.get("y")},
                "distance": None, "distance_norm": None,
                "state_changed": False, "moved": False,
                "ambiguous": False, "confidence": "id" if method == "id" else "high",
            })
            counts["appeared"] += 1
            bt["appeared"] += 1

    # Biggest changes first — matched by descending displacement, then births /
    # deaths — so the detail table immediately surfaces "变化从哪来".
    details.sort(key=lambda r: (
        0 if r["kind"] == "matched" and (r["moved"] or r["state_changed"])
        else 1 if r["kind"] == "disappeared"
        else 2 if r["kind"] == "appeared" else 3,
        -(r["distance"] or 0.0),
        str(r.get("a_id") or r.get("b_id"))))

    def _stats(values: List[float]) -> Dict[str, Any]:
        if not values:
            return {"count": 0, "mean": 0.0, "median": 0.0, "max": 0.0}
        return {"count": len(values),
                "mean": round(sum(values) / len(values), 3),
                "median": round(_median(values), 3),
                "max": round(max(values), 3)}

    def _state_counts(inds: List[Dict[str, Any]]) -> Dict[str, int]:
        out: Dict[str, int] = {}
        for x in inds:
            key = str(x.get("state"))
            out[key] = out.get(key, 0) + 1
        return out

    return {
        "method": method,
        "wrap": wrap,
        "bounds_a": {"width": wa, "height": ha},
        "bounds_b": {"width": wb, "height": hb},
        "move_threshold": float(move_threshold),
        "match_radius": round(gate_used_n * radius_scale, 3)
        if method == "spatial" else None,
        "counts": counts,
        "by_type": by_type,
        "transitions": [
            {"from": f, "to": t, "type": typ, "count": n}
            for (f, t, typ), n in sorted(transitions.items(),
                                         key=lambda kv: -kv[1])
        ],
        "displacement": {"all": _stats(all_dist), "moved": _stats(moved_dist)},
        "state_counts_a": _state_counts(inds_a),
        "state_counts_b": _state_counts(inds_b),
        "details": details,
    }


def _adaptive_gate_norm(ga: List[Dict[str, Any]], gb: List[Dict[str, Any]],
                        wa: float, ha: float, wb: float, hb: float,
                        wrap: Dict[str, bool],
                        override: Optional[float]) -> float:
    """Adaptive matching gate in normalised [0,1]² space for one type."""
    if override is not None:
        return min(_GATE_MAX, max(_GATE_MIN, float(override)))
    pa = [_norm(x, wa, ha) for x in ga]
    pb = [_norm(x, wb, hb) for x in gb]
    spacing = _median(_nearest_spacing(pa, wrap) + _nearest_spacing(pb, wrap))
    if spacing <= 0:
        return _GATE_MAX
    return min(_GATE_MAX, max(_GATE_MIN, _GATE_FACTOR * spacing))
