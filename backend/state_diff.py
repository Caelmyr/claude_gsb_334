"""Individual-level state difference calculation between two snapshots.

The matcher is deliberately conservative about identity:

* Within one run, an individual's ``id`` is a stable lineage identifier.  It is
  used directly whenever it is unambiguous, so an individual that disappears is
  never confused with a nearby newcomer that merely has the same state.
* Between runs, shared unique ids are still treated as the strongest identity
  signal (they represent the same generated entity in two counterfactual runs).
  Everything else is matched by a one-to-one, globally optimised spatial
  assignment inside an adaptive gate.  A final one-to-one nearest fallback
  pairs any remaining entities between two unrelated runs so equal/different
  populations remain comparable; genuine surplus is still left added/removed.
* Individuals are never matched across different types.
"""

from __future__ import annotations

import math
from collections import defaultdict, deque
from statistics import median
from typing import Any, Dict, List, Optional, Tuple


def compare_snapshots(base_snap: Dict[str, Any],
                      target_snap: Dict[str, Any],
                      base_meta: Optional[Dict[str, Any]] = None,
                      target_meta: Optional[Dict[str, Any]] = None,
                      same_run: bool = False,
                      move_threshold: Optional[float] = None,
                      match_radius: Optional[float] = None,
                      verified_ids: Optional[set] = None) -> Dict[str, Any]:
    """Return individual-level differences between two full snapshots.

    ``move_threshold`` is in world/cell units.  It defaults to zero, meaning any
    detectable position change is reported.  ``match_radius`` overrides the
    adaptive gate used for cross-run and ambiguous id matching.
    """
    domain, model = _model_key(base_meta, target_meta)
    bounds = _compatible_bounds(base_snap, target_snap)
    width = float(bounds["width"])
    height = float(bounds["height"])

    base = list(base_snap.get("individuals") or [])
    target = list(target_snap.get("individuals") or [])
    threshold = max(0.0, float(move_threshold or 0.0))
    radius = (None if match_radius is None
              else max(0.0, float(match_radius)))

    trust_shared_ids = same_run or (
        base_meta and target_meta
        and base_meta.get("scene_id")
        and base_meta.get("scene_id") == target_meta.get("scene_id"))
    matches, effective_radius = _match_individuals(
        base, target, domain, model, width, height, same_run, radius,
        trust_shared_ids, verified_ids=verified_ids or set())

    matched_base = {i: j for i, j, _, _ in matches}
    matched_target = {j: i for i, j, _, _ in matches}
    added_idx = [j for j in range(len(target)) if j not in matched_target]
    removed_idx = [i for i in range(len(base)) if i not in matched_base]

    match_records: List[Dict[str, Any]] = []
    state_changed = moved = both = 0
    moved_distances: List[float] = []
    all_distances: List[float] = []
    transitions: Dict[Tuple[str, str], int] = defaultdict(int)

    match_by_target: Dict[int, Dict[str, Any]] = {}
    for i, j, confidence, match_method in matches:
        a, b = base[i], target[j]
        dx, dy, distance = _delta(a, b, domain, model, width, height)
        all_distances.append(distance)
        kinds: List[str] = []
        old_state = str(a.get("state", ""))
        new_state = str(b.get("state", ""))
        if old_state != new_state:
            kinds.append("state_changed")
            state_changed += 1
            transitions[(old_state, new_state)] += 1
        is_moved = distance > threshold + 1e-9
        if is_moved:
            kinds.append("moved")
            moved += 1
            moved_distances.append(distance)
        if old_state != new_state and is_moved:
            both += 1
        if not kinds:
            kinds.append("unchanged")
        match_records.append({
            "base_id": a.get("id"), "target_id": b.get("id"),
            "base_type": a.get("type"), "target_type": b.get("type"),
            "base_state": old_state, "target_state": new_state,
            "base_x": _num(a.get("x")), "base_y": _num(a.get("y")),
            "target_x": _num(b.get("x")), "target_y": _num(b.get("y")),
            "dx": round(dx, 4), "dy": round(dy, 4),
            "distance": round(distance, 4),
            "kinds": kinds,
            "match_method": match_method,
            "confidence": round(confidence, 3),
        })
        match_by_target[j] = match_records[-1]

    id_count = sum(1 for r in match_records if r["match_method"] == "id")
    spatial_count = sum(1 for r in match_records
                        if r["match_method"] == "spatial")
    nearest_count = sum(1 for r in match_records
                        if r["match_method"] == "spatial_nearest")
    added = [_public(target[j], "added") for j in added_idx]
    removed = [_public(base[i], "removed") for i in removed_idx]
    target_annotated: List[Dict[str, Any]] = []
    for j, individual in enumerate(target):
        item = _public(individual, None)
        item["_diff"] = {"base_id": None, "distance": None,
                         "kinds": ["added"], "match_method": "none",
                         "confidence": 0.0}
        if j in matched_target:
            record = match_by_target[j]
            item["_diff"] = {
                "base_id": record["base_id"],
                "distance": record["distance"],
                "kinds": [k for k in record["kinds"] if k != "unchanged"],
                "match_method": record["match_method"],
                "confidence": record["confidence"],
            }
        target_annotated.append(item)

    base_counts = _state_counts(base)
    target_counts = _state_counts(target)
    states = sorted(set(base_counts) | set(target_counts))
    changed_total = added_count = len(added)
    removed_count = len(removed)
    changed_total = added_count + removed_count + state_changed + moved - both

    target_snapshot = dict(target_snap)
    target_snapshot["individuals"] = target_annotated
    base_out = dict(base_snap)
    base_out["individuals"] = [_public(a, None) for a in base]
    base_out["substrate"] = None

    return {
        "domain": domain,
        "model": model,
        "same_run": same_run,
        "bounds": bounds,
        "palette": target_snap.get("palette") or base_snap.get("palette") or {},
        "base": _endpoint(base_meta, base_snap),
        "target": _endpoint(target_meta, target_snap),
        "base_snapshot": base_out,
        "target_snapshot": target_snapshot,
        "matches": sorted(match_records,
                          key=lambda r: r["distance"], reverse=True),
        "added": added,
        "removed": removed,
        "counts": {
            "base_total": len(base),
            "target_total": len(target),
            "matched": len(matches),
            "added": added_count,
            "removed": removed_count,
            "state_changed": state_changed,
            "moved": moved,
            "state_changed_and_moved": both,
            "unchanged": len(matches) - state_changed - moved + both,
            "changed": changed_total,
        },
        "state_transitions": [
            {"from": old, "to": new, "count": count}
            for (old, new), count in sorted(transitions.items(),
                                            key=lambda kv: (-kv[1], kv[0]))
        ],
        "state_delta": {
            state: target_counts.get(state, 0) - base_counts.get(state, 0)
            for state in states
        },
        "displacement": _displacement_stats(all_distances, moved_distances),
        "matching": {
            "id_matches": id_count,
            "spatial_matches": spatial_count,
            "nearest_matches": nearest_count,
            "radius": round(effective_radius, 4),
            "radius_auto": radius is None,
        },
    }


# --------------------------------------------------------------------------- #
# Identity matching
# --------------------------------------------------------------------------- #
def _match_individuals(base: List[Dict[str, Any]],
                       target: List[Dict[str, Any]],
                       domain: str, model: str,
                       width: float, height: float,
                       same_run: bool,
                       radius_override: Optional[float],
                       trust_shared_ids: bool,
                       verified_ids: Optional[set] = None
                       ) -> Tuple[List[Tuple[int, int, float, str]], float]:
    base_ids = _id_index(base)
    target_ids = _id_index(target)
    verified_ids = verified_ids or set()
    matches: List[Tuple[int, int, float, str]] = []
    used_base: set = set()
    used_target: set = set()

    # Unambiguous shared ids are lineage facts.  In a same-run comparison this
    # also prevents a newcomer at the old position from stealing a dead id.
    for raw_id, base_positions in base_ids.items():
        if not trust_shared_ids:
            continue
        target_positions = target_ids.get(raw_id, [])
        if len(base_positions) == 1 and len(target_positions) == 1:
            if same_run and raw_id not in verified_ids:
                continue
            i, j = base_positions[0], target_positions[0]
            if base[i].get("type") == target[j].get("type"):
                matches.append((i, j, 1.0, "id"))
                used_base.add(i)
                used_target.add(j)

    remaining_a = [i for i in range(len(base)) if i not in used_base]
    remaining_b = [j for j in range(len(target)) if j not in used_target]
    if same_run:
        # A same-run lineage is represented by the stable id.  A nearby
        # newcomer with another id must remain added, even if it occupies the
        # dead individual's old position with the same state.
        return matches, 0.0
    if not remaining_a or not remaining_b:
        return matches, radius_override or 0.0

    radius = radius_override
    if radius is None:
        radius = _auto_radius(
            domain, model,
            [base[i] for i in remaining_a],
            [target[j] for j in remaining_b],
            width, height)
    spatial = _spatial_matches(
        base, target, remaining_a, remaining_b,
        domain, model, width, height, radius)
    matches.extend(spatial)
    return matches, radius


def _spatial_matches(base: List[Dict[str, Any]],
                     target: List[Dict[str, Any]],
                     remaining_a: List[int],
                     remaining_b: List[int],
                     domain: str, model: str,
                     width: float, height: float,
                     radius: float) -> List[Tuple[int, int, float, str]]:
    out: List[Tuple[int, int, float, str]] = []
    by_type_a = _group_by_type(base, remaining_a)
    by_type_b = _group_by_type(target, remaining_b)
    for typ in set(by_type_a) & set(by_type_b):
        idxs_a = by_type_a[typ]
        idxs_b = by_type_b[typ]
        edges = _candidate_edges(
            base, target, idxs_a, idxs_b, domain, model,
            width, height, radius)
        for i, j, score in _assign_edges(idxs_a, idxs_b, edges):
            out.append((i, j, score, "spatial"))

        # A conservative gate is useful for deciding whether an apparent
        # disappearance is real, but in two different independent runs there is
        # no lineage truth.  After all locally confident matches are fixed,
        # pair the remaining minority by global nearest one-to-one position so
        # equal/different populations still produce a complete comparison.
        used_a = {x[0] for x in out}
        used_b = {x[1] for x in out}
        fallback_a = [i for i in idxs_a if i not in used_a]
        fallback_b = [j for j in idxs_b if j not in used_b]
        distances = []
        for i in fallback_a:
            for j in fallback_b:
                _, _, distance = _delta(base[i], target[j], domain, model,
                                        width, height)
                distances.append((distance, i, j))
        for distance, i, j in sorted(distances):
            if i in used_a or j in used_b:
                continue
            score = _edge_score(base[i], target[j], distance, radius,
                                domain, model, width, height)
            out.append((i, j, min(0.24, max(0.01, score)),
                        "spatial_nearest"))
            used_a.add(i)
            used_b.add(j)
    return out


def _candidate_edges(base: List[Dict[str, Any]],
                     target: List[Dict[str, Any]],
                     idxs_a: List[int], idxs_b: List[int],
                     domain: str, model: str,
                     width: float, height: float,
                     radius: float) -> Dict[int, List[Tuple[int, float]]]:
    edges: Dict[int, List[Tuple[int, float]]] = defaultdict(list)
    if radius <= 0:
        b_by_pos: Dict[Tuple[int, int], List[int]] = defaultdict(list)
        for j in idxs_b:
            b = target[j]
            b_by_pos[(int(round(_num(b.get("x")))),
                     int(round(_num(b.get("y")))))].append(j)
        for i in idxs_a:
            a = base[i]
            for j in b_by_pos.get((int(round(_num(a.get("x")))),
                                   int(round(_num(a.get("y"))))), []):
                score = _edge_score(base[i], target[j], 0.0, radius,
                                    domain, model, width, height)
                if score >= 0.25:
                    edges[i].append((j, score))
        return edges

    if domain == "traffic":
        candidates = _ring_candidates(base, target, idxs_a, idxs_b,
                                      width, radius)
    else:
        candidates = _plane_candidates(base, target, idxs_a, idxs_b,
                                       width, height, radius)
    for i, j, distance in candidates:
        score = _edge_score(base[i], target[j], distance, radius,
                            domain, model, width, height)
        if score >= 0.25:
            edges[i].append((j, score))
    return edges


def _plane_candidates(base: List[Dict[str, Any]],
                      target: List[Dict[str, Any]],
                      idxs_a: List[int], idxs_b: List[int],
                      width: float, height: float,
                      radius: float) -> List[Tuple[int, int, float]]:
    # For very small/sparse groups the quadratic scan is cheaper and avoids
    # pathological wrap bins when radius approaches the world size.
    if len(idxs_a) * len(idxs_b) <= 2500 or radius >= min(width, height) / 3:
        out = []
        for i in idxs_a:
            for j in idxs_b:
                dx, dy, d = _delta(base[i], target[j], "ecology", "abm",
                                   width, height)
                if d <= radius:
                    out.append((i, j, d))
        return out

    cell = max(radius, 1e-9)
    bins: Dict[Tuple[int, int], List[Tuple[int, float, float]]] = defaultdict(list)
    for j in idxs_b:
        b = target[j]
        x, y = _num(b.get("x")), _num(b.get("y"))
        shifted_x = [x]
        shifted_y = [y]
        if x - radius < 0:
            shifted_x.append(x + width)
        if x + radius >= width:
            shifted_x.append(x - width)
        if y - radius < 0:
            shifted_y.append(y + height)
        if y + radius >= height:
            shifted_y.append(y - height)
        for cx in shifted_x:
            for cy in shifted_y:
                bins[(int(math.floor(cx / cell)),
                      int(math.floor(cy / cell)))].append((j, cx, cy))

    out: List[Tuple[int, int, float]] = []
    seen = set()
    for i in idxs_a:
        a = base[i]
        ax, ay = _num(a.get("x")), _num(a.get("y"))
        gx, gy = int(math.floor(ax / cell)), int(math.floor(ay / cell))
        for ox in (-1, 0, 1):
            for oy in (-1, 0, 1):
                for j, bx, by in bins.get((gx + ox, gy + oy), []):
                    dx, dy, d = _delta(a, target[j], "ecology", "abm",
                                       width, height)
                    if d <= radius and (i, j) not in seen:
                        seen.add((i, j))
                        out.append((i, j, d))
    return out


def _ring_candidates(base: List[Dict[str, Any]],
                     target: List[Dict[str, Any]],
                     idxs_a: List[int], idxs_b: List[int],
                     length: float,
                     radius: float) -> List[Tuple[int, int, float]]:
    ordered = sorted(((_num(target[j].get("x")) % length, j)
                      for j in idxs_b), key=lambda x: x[0])
    positions = [x for x, _ in ordered]
    out: List[Tuple[int, int, float]] = []
    for i in idxs_a:
        ax = _num(base[i].get("x")) % length
        ranges = [(max(0.0, ax - radius), min(length, ax + radius))]
        if ax - radius < 0:
            ranges.append((length + ax - radius, length))
        if ax + radius >= length:
            ranges.append((0.0, ax + radius - length))
        for lo, hi in ranges:
            left = _bisect_left(positions, lo)
            right = _bisect_right(positions, hi)
            for k in range(left, right):
                _, j = ordered[k]
                dx = (target[j]["x"] - base[i]["x"] + length / 2) % length - length / 2
                d = abs(dx)
                if abs(dx) <= radius:
                    out.append((i, j, d))
    return out


def _assign_edges(idxs_a: List[int], idxs_b: List[int],
                  edges: Dict[int, List[Tuple[int, float]]]
                  ) -> List[Tuple[int, int, float]]:
    """One-to-one maximum-score assignment with an implicit unmatched option."""
    if not edges:
        return []
    index_a = {idx: pos for pos, idx in enumerate(idxs_a)}
    index_b = {idx: pos for pos, idx in enumerate(idxs_b)}
    simple_edges = [
        (index_a[i], index_b[j], score)
        for i, row in edges.items() for j, score in row
    ]
    n_a, n_b = len(idxs_a), len(idxs_b)
    candidates: Dict[int, List[Tuple[int, float]]] = defaultdict(list)
    for i, j, score in simple_edges:
        candidates[i].append((j, score))

    epsilon = 1.0 / (max(n_a, n_b) + 1.0)
    prices: Dict[int, float] = defaultdict(float)
    owner: Dict[int, int] = {}
    pending = deque(range(n_a))
    max_bids = max(1, 50 * len(simple_edges) + 100 * max(n_a, n_b))
    bids = 0

    while pending and bids < max_bids:
        i = pending.popleft()
        best_j, best_value = -1, 0.0
        second = 0.0  # score of remaining unmatched
        for j, score in candidates.get(i, []):
            value = score - prices[j]
            if value > best_value:
                second = best_value
                best_j, best_value = j, value
            elif value > second:
                second = value
        if best_j < 0 or best_value <= 0.0:
            continue
        increment = max(epsilon, best_value - second)
        prices[best_j] += increment
        previous = owner.get(best_j)
        owner[best_j] = i
        if previous is not None and previous != i:
            pending.append(previous)
        bids += 1

    score_lookup = {(i, j): score for i, j, score in simple_edges}
    return [(idxs_a[i], idxs_b[j], score_lookup[(i, j)])
            for j, i in owner.items() if (i, j) in score_lookup]


def _edge_score(a: Dict[str, Any], b: Dict[str, Any], distance: float,
                radius: float, domain: str, model: str,
                width: float, height: float) -> float:
    if a.get("type") != b.get("type"):
        return 0.0
    if radius > 0:
        position_cost = 0.55 * min(1.0, distance / radius)
    else:
        position_cost = 0.0
    state_cost = 0.15 if a.get("state") != b.get("state") else 0.0
    motion_cost = 0.10 * _motion_difference(a, b, domain)
    lane_cost = 0.0
    if domain == "traffic":
        lane_cost = min(0.20, 0.05 * abs(_num(b.get("y")) - _num(a.get("y"))))
    identity_bonus = 0.10 if a.get("id") == b.get("id") else 0.0
    cost = position_cost + state_cost + motion_cost + lane_cost - identity_bonus
    return max(0.0, min(1.0, 1.0 - cost))


def _motion_difference(a: Dict[str, Any], b: Dict[str, Any],
                       domain: str) -> float:
    if "v" in a and "v" in b:
        scale = max(1.0, abs(_num(a.get("v"))), abs(_num(b.get("v"))))
        return min(1.0, abs(_num(a.get("v")) - _num(b.get("v"))) / scale)
    if domain == "ecology" and "vx" in a and "vx" in b:
        avx, avy = _num(a.get("vx")), _num(a.get("vy"))
        bvx, bvy = _num(b.get("vx")), _num(b.get("vy"))
        sa, sb = math.hypot(avx, avy), math.hypot(bvx, bvy)
        scale = max(1.0, sa, sb)
        speed_part = abs(sa - sb) / scale
        if sa > 1e-9 and sb > 1e-9:
            dot = (avx * bvx + avy * bvy) / (sa * sb)
            heading_part = (1.0 - max(-1.0, min(1.0, dot))) / 2.0
        else:
            heading_part = 0.0
        return min(1.0, 0.5 * speed_part + 0.5 * heading_part)
    return 0.0


def _auto_radius(domain: str, model: str,
                 base: List[Dict[str, Any]], target: List[Dict[str, Any]],
                 width: float, height: float) -> float:
    if not base or not target:
        return 0.0
    if domain == "epidemic" and model == "ca":
        return 0.0
    if domain == "ecology" and model == "ca":
        return 1.5
    if domain == "traffic":
        types_ = ({a.get("type") for a in base}
                  | {b.get("type") for b in target})
        n = max([
            max(sum(1 for a in base if a.get("type") == typ),
                sum(1 for b in target if b.get("type") == typ), 1)
            for typ in types_
        ])
        return max(1.0, 2.0 * width / n)
    # Continuous two-dimensional worlds and the ecology/epidemic ABMs.
    radii = []
    for typ in {a.get("type") for a in base} | {b.get("type") for b in target}:
        n = max(sum(1 for a in base if a.get("type") == typ),
                sum(1 for b in target if b.get("type") == typ))
        if n:
            radii.append(1.5 * math.sqrt(width * height / n))
    return max(1.0, min(radii or [1.0]))


# --------------------------------------------------------------------------- #
# Geometry and helpers
# --------------------------------------------------------------------------- #
def _delta(a: Dict[str, Any], b: Dict[str, Any],
           domain: str, model: str,
           width: float, height: float) -> Tuple[float, float, float]:
    dx = _num(b.get("x")) - _num(a.get("x"))
    dy = _num(b.get("y")) - _num(a.get("y"))
    if domain == "traffic":
        dx = (dx + width / 2.0) % width - width / 2.0
        # Lanes do not wrap; the y coordinate is a lane index or fixed road y.
    else:
        dx = (dx + width / 2.0) % width - width / 2.0
        dy = (dy + height / 2.0) % height - height / 2.0
    return dx, dy, math.hypot(dx, dy)


def _compatible_bounds(base: Dict[str, Any], target: Dict[str, Any]) -> Dict[str, float]:
    b = base.get("bounds") or {}
    t = target.get("bounds") or {}
    width = float(b.get("width", t.get("width", 100)))
    height = float(b.get("height", t.get("height", 100)))
    tw, th = float(t.get("width", width)), float(t.get("height", height))
    if abs(width - tw) > 1e-6 or abs(height - th) > 1e-6:
        raise ValueError("两个快照的画布尺寸不同，无法逐个体对比")
    return {"width": width, "height": height}


def _model_key(base_meta: Optional[Dict[str, Any]],
               target_meta: Optional[Dict[str, Any]]) -> Tuple[str, str]:
    meta_a = base_meta or {}
    meta_b = target_meta or {}
    domain_a = meta_a.get("domain")
    domain_b = meta_b.get("domain")
    model_a = meta_a.get("model")
    model_b = meta_b.get("model")
    if domain_a and domain_b and domain_a != domain_b:
        raise ValueError("只能对比同一领域的运行")
    if model_a and model_b and model_a != model_b:
        raise ValueError("只能对比同一种模型（CA/ABM）的运行")
    return str(domain_a or domain_b or ""), str(model_a or model_b or "")


def _id_index(items: List[Dict[str, Any]]) -> Dict[Any, List[int]]:
    out: Dict[Any, List[int]] = defaultdict(list)
    for i, item in enumerate(items):
        out[item.get("id")].append(i)
    return out


def _group_by_type(items: List[Dict[str, Any]],
                   indexes: List[int]) -> Dict[Any, List[int]]:
    out: Dict[Any, List[int]] = defaultdict(list)
    for i in indexes:
        out[items[i].get("type")].append(i)
    return out


def _state_counts(items: List[Dict[str, Any]]) -> Dict[str, int]:
    out: Dict[str, int] = defaultdict(int)
    for item in items:
        out[str(item.get("state", ""))] += 1
    return dict(out)


def _displacement_stats(all_distances: List[float],
                        moved_distances: List[float]) -> Dict[str, Any]:
    def block(values: List[float]) -> Dict[str, float]:
        if not values:
            return {"count": 0, "min": 0, "max": 0, "mean": 0,
                    "median": 0, "total": 0}
        return {
            "count": len(values),
            "min": round(min(values), 4),
            "max": round(max(values), 4),
            "mean": round(sum(values) / len(values), 4),
            "median": round(median(values), 4),
            "total": round(sum(values), 4),
        }
    return {"moved": block(moved_distances),
            "matched": block(all_distances)}


def _public(item: Dict[str, Any], kind: Optional[str]) -> Dict[str, Any]:
    out = dict(item)
    if kind is None:
        out["_diff"] = None
    else:
        out["_diff"] = {"kinds": [kind], "base_id": None,
                        "distance": None, "match_method": "none",
                        "confidence": 0.0}
    return out


def _endpoint(meta: Optional[Dict[str, Any]],
              snap: Dict[str, Any]) -> Dict[str, Any]:
    meta = meta or {}
    return {
        "run_id": meta.get("id"),
        "run_name": meta.get("name"),
        "step": snap.get("step", meta.get("current_step", 0)),
    }


def _num(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _bisect_left(values: List[float], target: float) -> int:
    lo, hi = 0, len(values)
    while lo < hi:
        mid = (lo + hi) // 2
        if values[mid] < target:
            lo = mid + 1
        else:
            hi = mid
    return lo


def _bisect_right(values: List[float], target: float) -> int:
    lo, hi = 0, len(values)
    while lo < hi:
        mid = (lo + hi) // 2
        if values[mid] <= target:
            lo = mid + 1
        else:
            hi = mid
    return lo
