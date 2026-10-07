"""Smoke tests for the simulation engines, storage and run lifecycle.

Run directly::

    python3 tests/smoke.py

Each check is independent and prints PASS / FAIL; the script exits non-zero on
the first failure so it can be wired into CI or a pre-commit hook.
"""

from __future__ import annotations

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend import models, report, storage  # noqa: E402
from backend import diff  # noqa: E402
from backend.engine import make_engine  # noqa: E402
from backend.run_manager import manager  # noqa: E402

_ENGINES = ["traffic/ca", "traffic/abm", "ecology/ca", "ecology/abm",
            "epidemic/ca", "epidemic/abm"]


def check(name: str, fn) -> None:
    try:
        fn()
        print(f"PASS  {name}")
    except AssertionError as exc:
        print(f"FAIL  {name}: {exc}")
        sys.exit(1)
    except Exception as exc:  # noqa: BLE001
        print(f"FAIL  {name}: {exc}")
        sys.exit(1)


def engines_step() -> None:
    for key in _ENGINES:
        d, m = key.split("/")
        eng = make_engine(d, m, seed=42)
        for _ in range(5):
            eng.step()
        assert eng.step_count == 5, key
        assert len(eng.individuals()) > 0, f"{key} has no individuals"
        stats = eng.stats()
        assert stats, f"{key} produced empty stats"
        snap = eng.snapshot()
        assert snap["step"] == 5
        assert snap["stats"] == stats


def interventions_apply() -> None:
    eng = make_engine("epidemic", "abm", seed=1)
    before = eng.stats()["susceptible"]
    res = eng.apply_intervention({"type": "vaccinate", "params": {"fraction": 1.0}})
    assert res["applied"], res
    assert eng.stats()["susceptible"] == 0
    assert before > 0


def storage_atomic_roundtrip() -> None:
    with tempfile.TemporaryDirectory() as td:
        # Redirect the module's DATA_DIR for this isolated check.
        old = storage.DATA_DIR
        storage.DATA_DIR = td
        try:
            storage.ensure_dirs()
            storage.save_scene({"id": "x", "name": "t", "updated_at": "z"})
            assert storage.load_scene("x")["name"] == "t"
            storage.save_step("r1", 0, {"step": 0, "v": 1})
            storage.save_step("r1", 7, {"step": 7, "v": 2})
            assert storage.load_step("r1", 7)["v"] == 2
            assert storage.list_steps("r1") == [0, 7]
        finally:
            storage.DATA_DIR = old


def run_lifecycle() -> None:
    scene = models.Scene(domain="epidemic", model="abm",
                         config={"n": 200, "width": 300, "height": 300,
                                 "initial_infected": 5})
    meta = manager.create_run(scene, seed=1, snapshot_interval=2)
    rid = meta["id"]
    try:
        r = manager.step(rid, 6)
        assert r["step"] == 6
        series = manager.get_series(rid)
        assert series[0]["step"] == 0 and series[-1]["step"] == 6
        assert len(manager.get_individuals(rid, 6)) == 200
        # snapshot_interval=2 -> full snapshots persisted at 0,2,4,6
        steps = storage.list_steps(rid)
        assert steps == [0, 2, 4, 6], steps
        rpt = report.generate_report(rid)
        assert rpt["steps"] == 7
    finally:
        manager.delete_run(rid)


def _snap(inds, run_id="r", w=100.0, h=100.0, wrap=None):
    if wrap is None:
        wrap = {"x": True, "y": True}
    return {"individuals": inds, "bounds": {"width": w, "height": h},
            "wrap": wrap, "_run_id": run_id}


def diff_same_run_identity() -> None:
    """Within a run, (type,id) tracks identity even through swaps/deaths."""
    # Two close, same-state agents swap cells: nearest-position matching would
    # swap identities; id matching must not.
    a = [{"id": "p1", "type": "person", "state": "S", "x": 0.0, "y": 0.0},
         {"id": "p2", "type": "person", "state": "S", "x": 1.0, "y": 0.0}]
    b = [dict(a[0], x=1.0), dict(a[1], x=0.0)]
    r = diff.compute_diff(_snap(a, "r"), _snap(b, "r"), "epidemic", "abm",
                          move_threshold=0.5)
    pairs = {x["a_id"]: x for x in r["details"] if x["kind"] == "matched"}
    assert pairs["p1"]["b_id"] == "p1" and pairs["p2"]["b_id"] == "p2"
    assert r["counts"]["moved"] == 2
    assert r["counts"]["appeared"] == r["counts"]["disappeared"] == 0

    # A fox vanishes and a rabbit is born on a nearby cell: the disappearance
    # must stay attributed to the fox, the birth to the rabbit.
    a = [{"id": "rb1", "type": "rabbit", "state": "rabbit", "x": 3, "y": 3},
         {"id": "fx2", "type": "fox", "state": "fox", "x": 10, "y": 10}]
    b = [{"id": "rb1", "type": "rabbit", "state": "rabbit", "x": 4, "y": 3},
         {"id": "rb3", "type": "rabbit", "state": "rabbit", "x": 3, "y": 3}]
    r = diff.compute_diff(_snap(a, "r", 60, 60), _snap(b, "r", 60, 60),
                          "ecology", "ca", move_threshold=0.5)
    assert r["counts"] == {"a": 2, "b": 2, "matched": 1, "appeared": 1,
                           "disappeared": 1, "state_changed": 0, "moved": 1,
                           "unchanged": 0}, r["counts"]
    gone = [x for x in r["details"] if x["kind"] == "disappeared"]
    assert len(gone) == 1 and gone[0]["a_id"] == "fx2" and gone[0]["type"] == "fox"

    # State transition on the same identity.
    a = [{"id": "p1", "type": "person", "state": "susceptible", "x": 1, "y": 1}]
    b = [{"id": "p1", "type": "person", "state": "infected", "x": 1, "y": 1}]
    r = diff.compute_diff(_snap(a, "r", 50, 50), _snap(b, "r", 50, 50),
                          "epidemic", "ca", move_threshold=0.5)
    assert r["counts"]["state_changed"] == 1
    assert r["transitions"][0]["from"] == "susceptible"
    assert r["transitions"][0]["to"] == "infected"


def diff_torus_displacement() -> None:
    """Ring/torus wrap: 990 -> 10 on a 1000 road is 20, not 980."""
    a = [{"id": "v1", "type": "vehicle", "state": "moving", "x": 990.0, "y": 0.0}]
    b = [{"id": "v1", "type": "vehicle", "state": "stopped", "x": 10.0, "y": 0.0}]
    wrap = {"x": True, "y": False}
    r = diff.compute_diff(_snap(a, "r", 1000.0, 60.0, wrap),
                          _snap(b, "r", 1000.0, 60.0, wrap),
                          "traffic", "abm", move_threshold=5)
    m = [x for x in r["details"] if x["kind"] == "matched"][0]
    assert m["distance"] == 20.0 and m["moved"] and m["state_changed"]

    # A lane change on traffic CA (y moves, x does not) counts as 1-cell move.
    a = [{"id": "v1", "type": "vehicle", "state": "moving", "x": 5, "y": 0}]
    b = [{"id": "v1", "type": "vehicle", "state": "stopped", "x": 5, "y": 1}]
    r = diff.compute_diff(_snap(a, "r", 80.0, 3.0, wrap),
                          _snap(b, "r", 80.0, 3.0, wrap),
                          "traffic", "ca", move_threshold=0.5)
    m = [x for x in r["details"] if x["kind"] == "matched"][0]
    assert m["distance"] == 1.0 and m["moved"]


def diff_cross_run_spatial() -> None:
    """Different runs / ids / population sizes resolve via global-greedy space."""
    # Agents interleave at distance 1; global greedy must pair the closest and
    # leave the genuinely isolated one unmatched (appeared), with no id reuse.
    a = [{"id": "X1", "type": "person", "state": "S", "x": 10.0, "y": 10.0},
         {"id": "X2", "type": "person", "state": "I", "x": 50.0, "y": 50.0}]
    b = [{"id": "Q7", "type": "person", "state": "S", "x": 10.0, "y": 11.0},
         {"id": "Q9", "type": "person", "state": "I", "x": 51.0, "y": 50.0},
         {"id": "Q5", "type": "person", "state": "S", "x": 90.0, "y": 90.0}]
    r = diff.compute_diff(_snap(a, "rA"), _snap(b, "rB"),
                          "epidemic", "abm", method="spatial",
                          move_threshold=0.5)
    assert r["method"] == "spatial"
    pairs = {x["a_id"]: x for x in r["details"] if x["kind"] == "matched"}
    assert pairs["X1"]["b_id"] == "Q7" and pairs["X2"]["b_id"] == "Q9"
    assert r["counts"]["appeared"] == 1 and r["counts"]["disappeared"] == 0
    assert r["match_radius"] is not None

    # Swap-avoidance: two A agents and two B agents arranged so each A's
    # individually-nearest B is the same agent.  A1-B1 and A2-B2 are both
    # distance 1; A1-B2 and A2-B1 are ~30. Naive greedy per-A could pair
    # A1-B1, A2-B1 (collision -> A2 unmatched); global greedy pairs both.
    a = [{"id": "X1", "type": "person", "state": "S", "x": 40.0, "y": 50.0},
         {"id": "X2", "type": "person", "state": "S", "x": 60.0, "y": 50.0}]
    b = [{"id": "Q1", "type": "person", "state": "S", "x": 41.0, "y": 50.0},
         {"id": "Q2", "type": "person", "state": "S", "x": 61.0, "y": 50.0}]
    r = diff.compute_diff(_snap(a, "rA"), _snap(b, "rB"),
                          "epidemic", "abm", method="spatial",
                          move_threshold=0.5, match_radius=10)
    assert r["counts"]["matched"] == 2, r["counts"]
    assert r["counts"]["appeared"] == r["counts"]["disappeared"] == 0

    # Different types never pair across even at the same position.
    a = [{"id": "rb1", "type": "rabbit", "state": "rabbit", "x": 5, "y": 5}]
    b = [{"id": "fx9", "type": "fox", "state": "fox", "x": 5, "y": 5}]
    r = diff.compute_diff(_snap(a, "rA", 60, 60), _snap(b, "rB", 60, 60),
                          "ecology", "ca", method="spatial")
    assert r["counts"]["matched"] == 0
    assert r["counts"]["appeared"] == r["counts"]["disappeared"] == 1


def diff_manager_end_to_end() -> None:
    scene = models.Scene(domain="ecology", model="ca",
                         config={"width": 20, "height": 20,
                                 "n_rabbits": 60, "n_foxes": 10})
    m = manager.create_run(scene, seed=3, snapshot_interval=1)
    rid = m["id"]
    try:
        manager.step(rid, 4)
        r = manager.get_diff(rid, 0, rid, 4)
        assert r["method"] == "id"
        # Population is conserved: births = b - matched, deaths = a - matched.
        c = r["counts"]
        assert c["appeared"] + c["matched"] == c["b"]
        assert c["disappeared"] + c["matched"] == c["a"]
        assert c["moved"] + c["state_changed"] + c["unchanged"] >= c["matched"]

        # Cross-run comparison with different seeds and sizes must work.
        scene2 = models.Scene(domain="ecology", model="ca",
                              config={"width": 20, "height": 20,
                                      "n_rabbits": 40, "n_foxes": 8})
        m2 = manager.create_run(scene2, seed=99)
        rid2 = m2["id"]
        try:
            r2 = manager.get_diff(rid, 2, rid2, 0)
            assert r2["method"] == "spatial"
            assert r2["counts"]["matched"] <= min(c["a"], r2["counts"]["b"])
            assert all(x["kind"] in ("matched", "appeared", "disappeared")
                       for x in r2["details"])
        finally:
            manager.delete_run(rid2)

        # Incompatible models are rejected.
        scene3 = models.Scene(domain="traffic", model="ca")
        m3 = manager.create_run(scene3, seed=1)
        rid3 = m3["id"]
        try:
            try:
                manager.get_diff(rid, 0, rid3, 0)
                raise AssertionError("expected ValueError for cross-model diff")
            except ValueError:
                pass
        finally:
            manager.delete_run(rid3)
    finally:
        manager.delete_run(rid)


def main() -> None:
    check("six engines step and snapshot", engines_step)
    check("interventions apply", interventions_apply)
    check("atomic sharded storage", storage_atomic_roundtrip)
    check("run lifecycle + report", run_lifecycle)
    check("diff: same-run identity tracking", diff_same_run_identity)
    check("diff: torus / ring displacement", diff_torus_displacement)
    check("diff: cross-run spatial matching", diff_cross_run_spatial)
    check("diff: manager end-to-end", diff_manager_end_to_end)
    print("\nall smoke tests passed")


if __name__ == "__main__":
    main()
