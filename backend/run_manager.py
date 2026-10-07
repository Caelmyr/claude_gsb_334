"""Run lifecycle management: real-time stepping, batch runs, interventions.

A :class:`RunManager` owns the in-memory engines for the current server
process.  Every mutation funnels through :mod:`backend.storage` so the
atomic-write / time-step-sharding guarantees hold whether a step comes from the
UI or a background batch.  Engines are kept in memory while a run exists in
this process so a run can be stepped interactively; batch runs that belong to a
comparison experiment can drop their engine on completion to bound memory.

Concurrency: each run has its own re-entrant lock, so long batch runs on one
scene never block interactive stepping on another.
"""

from __future__ import annotations

import threading
from typing import Any, Dict, List, Optional

from . import models, state_diff, storage, util
from .engine import make_engine
from .engine.base import Engine


class RunManager:
    def __init__(self) -> None:
        self._engines: Dict[str, Engine] = {}
        self._locks: Dict[str, threading.RLock] = {}
        self._abort: set = set()
        self._lock = threading.RLock()

    # ------------------------------------------------------------------ #
    # Internals
    # ------------------------------------------------------------------ #
    def _lock_for(self, run_id: str) -> threading.RLock:
        with self._lock:
            return self._locks.setdefault(run_id, threading.RLock())

    def _engine(self, run_id: str) -> Optional[Engine]:
        with self._lock:
            return self._engines.get(run_id)

    def _require(self, run_id: str) -> tuple:
        eng = self._engine(run_id)
        meta = storage.load_run_meta(run_id)
        if meta is None:
            raise KeyError(f"run not found: {run_id}")
        return eng, meta

    # ------------------------------------------------------------------ #
    # Create
    # ------------------------------------------------------------------ #
    def create_run(self, scene: models.Scene, name: Optional[str] = None,
                   seed: Optional[int] = None,
                   snapshot_interval: int = 1) -> Dict[str, Any]:
        config = models.resolve_config(scene)
        if seed is None:
            seed = int(config.get("seed", 0))
        engine = make_engine(scene.domain, scene.model, config=config, seed=seed)
        run_id = util.new_id("run")
        now = util.now_iso()
        meta: Dict[str, Any] = {
            "id": run_id,
            "name": name or f"{scene.name} · 运行",
            "scene_id": scene.id,
            "scene_name": scene.name,
            "domain": scene.domain,
            "model": scene.model,
            "config": config,
            "interventions": [{**i, "applied": False}
                              for i in scene.interventions],
            "snapshot_interval": max(1, int(snapshot_interval)),
            "status": "ready",
            "current_step": 0,
            "total_steps": 0,
            "seed": seed,
            "created_at": now,
            "updated_at": now,
        }
        storage.create_run_dir(run_id)
        storage.save_step(run_id, 0, engine.snapshot())
        storage.save_series(run_id, [{"step": 0, **engine.stats()}])
        storage.save_events(run_id, [])
        storage.save_run_meta(meta)
        with self._lock:
            self._engines[run_id] = engine
        return meta

    # ------------------------------------------------------------------ #
    # Stepping
    # ------------------------------------------------------------------ #
    def _apply_due(self, run_id: str, engine: Engine,
                   meta: Dict[str, Any], step: int) -> None:
        """Apply any scheduled intervention whose ``at_step`` has been reached."""
        events = storage.load_events(run_id)
        changed = False
        for itv in meta["interventions"]:
            if itv.get("applied"):
                continue
            if int(itv.get("at_step", 0)) <= step:
                result = engine.apply_intervention(itv)
                itv["applied"] = True
                events.append({"step": step, "type": itv["type"],
                               "params": itv.get("params", {}),
                               "scheduled": True, "result": result})
                changed = True
        if changed:
            storage.save_events(run_id, events)
            storage.save_run_meta(meta)

    def step(self, run_id: str, n: int = 1) -> Dict[str, Any]:
        """Advance ``n`` steps and return the current snapshot + stats."""
        with self._lock_for(run_id):
            engine, meta = self._require(run_id)
            if engine is None:
                raise RuntimeError("该运行未载入内存（服务器重启后不可续跑），请重开新运行")
            if meta["status"] in ("finished", "stopped"):
                meta["status"] = "ready"
            for _ in range(int(n)):
                self._apply_due(run_id, engine, meta, engine.step_count)
                engine.step()
                meta["current_step"] = engine.step_count
                meta["updated_at"] = util.now_iso()
                storage.append_series(run_id, {"step": engine.step_count,
                                               **engine.stats()})
                if engine.step_count % meta["snapshot_interval"] == 0:
                    storage.save_step(run_id, engine.step_count, engine.snapshot())
            storage.save_run_meta(meta)
            return {"step": engine.step_count, "stats": engine.stats(),
                    "snapshot": engine.snapshot()}

    def run_batch(self, run_id: str, steps: int,
                  snapshot_interval: Optional[int] = None,
                  keep_engine: bool = True) -> Dict[str, Any]:
        """Run ``steps`` steps to completion, returning final stats.

        Series rows are accumulated in memory and flushed periodically (and at
        the end) so the per-step write cost stays O(1) amortised even for very
        long runs.
        """
        with self._lock_for(run_id):
            engine, meta = self._require(run_id)
            if engine is None:
                raise RuntimeError("该运行未载入内存，请重开新运行")
            if snapshot_interval is not None:
                meta["snapshot_interval"] = max(1, int(snapshot_interval))
            meta["status"] = "running"
            storage.save_run_meta(meta)

            series = storage.load_series(run_id)
            self._abort.discard(run_id)
            for _ in range(int(steps)):
                if run_id in self._abort:
                    break
                self._apply_due(run_id, engine, meta, engine.step_count)
                engine.step()
                meta["current_step"] = engine.step_count
                series.append({"step": engine.step_count, **engine.stats()})
                if engine.step_count % meta["snapshot_interval"] == 0:
                    storage.save_step(run_id, engine.step_count, engine.snapshot())
                if engine.step_count % 50 == 0:
                    storage.save_series(run_id, series)
                    meta["updated_at"] = util.now_iso()
                    storage.save_run_meta(meta)

            meta["status"] = "stopped" if run_id in self._abort else "finished"
            meta["updated_at"] = util.now_iso()
            storage.save_series(run_id, series)
            storage.save_run_meta(meta)
            self._abort.discard(run_id)

            final = engine.snapshot()
            if not keep_engine:
                with self._lock:
                    self._engines.pop(run_id, None)
            return {"step": engine.step_count, "stats": engine.stats(),
                    "snapshot": final}

    # ------------------------------------------------------------------ #
    # Control
    # ------------------------------------------------------------------ #
    def pause(self, run_id: str) -> Dict[str, Any]:
        with self._lock_for(run_id):
            _, meta = self._require(run_id)
            meta["status"] = "paused"
            meta["updated_at"] = util.now_iso()
            storage.save_run_meta(meta)
            return meta

    def resume(self, run_id: str) -> Dict[str, Any]:
        with self._lock_for(run_id):
            _, meta = self._require(run_id)
            meta["status"] = "ready"
            meta["updated_at"] = util.now_iso()
            storage.save_run_meta(meta)
            return meta

    def stop(self, run_id: str) -> Dict[str, Any]:
        with self._lock_for(run_id):
            _, meta = self._require(run_id)
            with self._lock:
                self._abort.add(run_id)
            meta["status"] = "stopped"
            meta["updated_at"] = util.now_iso()
            storage.save_run_meta(meta)
            return meta

    def reset(self, run_id: str) -> Dict[str, Any]:
        with self._lock_for(run_id):
            _, meta = self._require(run_id)
            seed = meta.get("seed", 0)
            engine = make_engine(meta["domain"], meta["model"],
                                 config=meta["config"], seed=seed)
            with self._lock:
                self._engines[run_id] = engine
            for itv in meta["interventions"]:
                itv["applied"] = False
            meta["current_step"] = 0
            meta["status"] = "ready"
            meta["updated_at"] = util.now_iso()
            storage.clear_steps(run_id)
            storage.save_step(run_id, 0, engine.snapshot())
            storage.save_series(run_id, [{"step": 0, **engine.stats()}])
            storage.save_events(run_id, [])
            storage.save_run_meta(meta)
            return meta

    def delete_run(self, run_id: str) -> bool:
        with self._lock_for(run_id):
            with self._lock:
                self._engines.pop(run_id, None)
                self._locks.pop(run_id, None)
                self._abort.discard(run_id)
            return storage.delete_run(run_id)

    # ------------------------------------------------------------------ #
    # Interventions
    # ------------------------------------------------------------------ #
    def apply_intervention(self, run_id: str,
                           itv: Dict[str, Any]) -> Dict[str, Any]:
        with self._lock_for(run_id):
            engine, meta = self._require(run_id)
            if engine is None:
                raise RuntimeError("该运行未载入内存，请重开新运行")
            result = engine.apply_intervention(itv)
            events = storage.load_events(run_id)
            events.append({"step": engine.step_count, "type": itv["type"],
                           "params": itv.get("params", {}),
                           "scheduled": False, "result": result})
            storage.save_events(run_id, events)
            meta["updated_at"] = util.now_iso()
            storage.save_run_meta(meta)
            return result

    # ------------------------------------------------------------------ #
    # Reads
    # ------------------------------------------------------------------ #
    def status(self, run_id: str) -> Dict[str, Any]:
        with self._lock_for(run_id):
            _, meta = self._require(run_id)
            out = dict(meta)
            engine = self._engine(run_id)
            if engine is not None:
                out["stats"] = engine.stats()
            return out

    def get_snapshot(self, run_id: str, step: Optional[int] = None) -> Dict[str, Any]:
        with self._lock_for(run_id):
            engine, meta = self._require(run_id)
            if step is None:
                step = meta["current_step"]
            if engine is not None and int(step) == engine.step_count:
                return engine.snapshot()
            snap = storage.load_step(run_id, int(step))
            if snap is None:
                raise KeyError(f"snapshot not found: step {step}")
            return snap

    def get_series(self, run_id: str) -> List[Dict[str, Any]]:
        with self._lock_for(run_id):
            self._require(run_id)
            return storage.load_series(run_id)

    def get_events(self, run_id: str) -> List[Dict[str, Any]]:
        with self._lock_for(run_id):
            self._require(run_id)
            return storage.load_events(run_id)

    def get_individuals(self, run_id: str, step: Optional[int] = None) -> List[Dict[str, Any]]:
        return self.get_snapshot(run_id, step).get("individuals", [])

    def get_state_diff(self,
                       base_run_id: str,
                       base_step: int,
                       target_run_id: str,
                       target_step: int,
                       move_threshold: Optional[float] = None,
                       match_radius: Optional[float] = None) -> Dict[str, Any]:
        """Compare two snapshots, either within one run or across two runs."""
        same_run = base_run_id == target_run_id
        lock_ids = sorted({base_run_id, target_run_id})
        if same_run:
            locks = [self._lock_for(base_run_id)]
        else:
            locks = [self._lock_for(rid) for rid in lock_ids]
        for lock in locks:
            lock.acquire()
        try:
            base_meta = storage.load_run_meta(base_run_id)
            if base_meta is None:
                raise KeyError(f"run not found: {base_run_id}")
            target_meta = (base_meta if same_run
                           else storage.load_run_meta(target_run_id))
            if target_meta is None:
                raise KeyError(f"run not found: {target_run_id}")
            base_snap = self.get_snapshot(base_run_id, int(base_step))
            target_snap = self.get_snapshot(target_run_id, int(target_step))
            verified = None
            if same_run:
                verified = self._verified_lineage_ids(
                    base_run_id, int(base_step), int(target_step))
            return state_diff.compare_snapshots(
                base_snap, target_snap, base_meta, target_meta,
                same_run=same_run, move_threshold=move_threshold,
                match_radius=match_radius, verified_ids=verified)
        finally:
            for lock in reversed(locks):
                lock.release()

    def _verified_lineage_ids(self, run_id: str,
                              base_step: int, target_step: int) -> set:
        """Find shared ids that never recur/duplicate in persisted snapshots.

        Ecology predator ids from older data could be reused after an
        intervention.  Such an id can be unique at both endpoints but refer to
        two different animals, so the complete persisted interval must be
        checked before treating it as a lineage identifier.
        """
        lo, hi = sorted((int(base_step), int(target_step)))
        candidates = set()
        first = storage.load_step(run_id, lo)
        last = storage.load_step(run_id, hi)
        if first and last:
            first_counts: Dict[Any, int] = {}
            last_counts: Dict[Any, int] = {}
            for individual in first.get("individuals", []):
                raw_id = individual.get("id")
                first_counts[raw_id] = first_counts.get(raw_id, 0) + 1
            for individual in last.get("individuals", []):
                raw_id = individual.get("id")
                last_counts[raw_id] = last_counts.get(raw_id, 0) + 1
            first_once = {raw_id for raw_id, count in first_counts.items()
                          if count == 1}
            last_once = {raw_id for raw_id, count in last_counts.items()
                         if count == 1}
            candidates = first_once & last_once
        if not candidates:
            return set()

        verified = set(candidates)
        for step in storage.list_steps(run_id):
            if not lo <= step <= hi:
                continue
            snap = storage.load_step(run_id, step)
            if snap is None:
                continue
            seen: Dict[Any, int] = {}
            for individual in snap.get("individuals", []):
                raw_id = individual.get("id")
                if raw_id in candidates:
                    seen[raw_id] = seen.get(raw_id, 0) + 1
            for raw_id, count in seen.items():
                # More than one occurrence at a step is a duplicated id; zero at
                # an intermediate step means the lineage died before returning.
                if count != 1:
                    verified.discard(raw_id)
            if step not in (lo, hi):
                verified.intersection_update(seen)
        return verified


# Global singleton used by the Flask app.
manager = RunManager()
