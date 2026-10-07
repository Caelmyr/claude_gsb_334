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

from backend import models, report, state_diff, storage  # noqa: E402
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


def state_diff_identity() -> None:
    bounds = {"width": 100.0, "height": 100.0}
    base_snap = {"bounds": bounds, "individuals": [
        {"id": "a1", "type": "boid", "state": "boid", "x": 0.0, "y": 0.0},
        {"id": "a2", "type": "boid", "state": "boid", "x": 90.0, "y": 0.0},
    ]}
    # Same run: a nearby/new id must not inherit the disappeared individual;
    # a2 crossed the periodic x boundary by two cells.
    target_snap = {"bounds": bounds, "individuals": [
        {"id": "new", "type": "boid", "state": "boid", "x": 1.0, "y": 0.0},
        {"id": "a2", "type": "boid", "state": "boid", "x": 92.0, "y": 0.0},
    ]}
    diff = state_diff.compare_snapshots(
        base_snap, target_snap,
        {"domain": "ecology", "model": "abm"},
        {"domain": "ecology", "model": "abm"}, same_run=True,
        verified_ids={"a2"})
    assert diff["counts"]["added"] == 1
    assert diff["counts"]["removed"] == 1
    assert diff["counts"]["moved"] == 1
    moved = [m for m in diff["matches"] if "moved" in m["kinds"]]
    assert len(moved) == 1 and moved[0]["base_id"] == "a2"
    assert moved[0]["dx"] == 2.0 and moved[0]["dy"] == 0.0

    # Different runs: ids and counts may differ.  One-to-one spatial matching
    # must resolve the local swap rather than assigning both base agents to the
    # same target, and leave the far-away extra target as added.
    cross_base = {"bounds": bounds, "individuals": [
        {"id": "x1", "type": "boid", "state": "boid", "x": 0.0, "y": 0.0},
        {"id": "x2", "type": "boid", "state": "boid", "x": 10.0, "y": 0.0},
    ]}
    cross_target = {"bounds": bounds, "individuals": [
        {"id": "y1", "type": "boid", "state": "boid", "x": 1.0, "y": 0.0},
        {"id": "y2", "type": "boid", "state": "boid", "x": 11.0, "y": 0.0},
        {"id": "y3", "type": "boid", "state": "boid", "x": 80.0, "y": 80.0},
    ]}
    cross = state_diff.compare_snapshots(
        cross_base, cross_target,
        {"domain": "ecology", "model": "abm", "scene_id": "s1"},
        {"domain": "ecology", "model": "abm", "scene_id": "s2"},
        same_run=False)
    pairs = {(m["base_id"], m["target_id"]) for m in cross["matches"]}
    assert pairs == {("x1", "y1"), ("x2", "y2")}, pairs
    assert cross["counts"]["added"] == 1
    assert cross["counts"]["removed"] == 0


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


def main() -> None:
    check("six engines step and snapshot", engines_step)
    check("interventions apply", interventions_apply)
    check("atomic sharded storage", storage_atomic_roundtrip)
    check("state-diff identity matching", state_diff_identity)
    check("run lifecycle + report", run_lifecycle)
    print("\nall smoke tests passed")


if __name__ == "__main__":
    main()
