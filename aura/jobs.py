"""Background jobs with progress.

Downloading a model takes minutes, and the browser must stay responsive while
it happens, so anything long runs on a thread and publishes its state here. The
UI polls one route (`GET /api/jobs/{id}`) and gets a plain dict back.

Deliberately tiny: no queues, no workers, no persistence. A job that was
running when the app closed is simply gone.
"""
from __future__ import annotations

import datetime
import threading
import uuid
from typing import Callable, Dict, List, Optional

from .downloads import DownloadCancelled
from .models import AuraError

TERMINAL = ("done", "error", "cancelled")


def _now() -> str:
    return datetime.datetime.now().isoformat(timespec="seconds")


class Job:
    def __init__(self, kind: str, label: str):
        self.id = uuid.uuid4().hex[:12]
        self.kind = kind
        self.label = label
        self.state = "queued"
        self.progress = 0.0
        self.detail = ""
        self.error = ""
        self.result: dict = {}
        self.created = _now()
        self.finished = ""
        self._cancel = threading.Event()

    # ------------------------------------------------------------------ control
    def cancel(self) -> bool:
        if self.state in TERMINAL:
            return False
        self._cancel.set()
        self.detail = "cancelling ..."
        return True

    def cancelled(self) -> bool:
        return self._cancel.is_set()

    def set_progress(self, done: int, total: int, detail: str = "") -> None:
        self.progress = max(0.0, min(1.0, (done / total) if total else 0.0))
        if detail:
            self.detail = detail
        elif total:
            self.detail = "{} of {}".format(_mb(done), _mb(total))

    def as_dict(self) -> dict:
        return {
            "id": self.id, "kind": self.kind, "label": self.label, "state": self.state,
            "progress": round(self.progress, 4), "percent": int(round(self.progress * 100)),
            "detail": self.detail, "error": self.error, "result": self.result,
            "created": self.created, "finished": self.finished,
            "done": self.state in TERMINAL,
        }


def _mb(count: int) -> str:
    value = float(count or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return "{:.0f} {}".format(value, unit) if unit in ("B", "KB") else "{:.1f} {}".format(value, unit)
        value /= 1024
    return "{} B".format(count)


class Jobs:
    def __init__(self, limit: int = 30):
        self.lock = threading.RLock()
        self.order: List[str] = []
        self.items: Dict[str, Job] = {}
        self.limit = int(limit)

    def create(self, kind: str, label: str) -> Job:
        job = Job(kind, label)
        with self.lock:
            self.items[job.id] = job
            self.order.append(job.id)
            while len(self.order) > self.limit:
                self.items.pop(self.order.pop(0), None)
        return job

    def get(self, job_id: str) -> Optional[Job]:
        with self.lock:
            return self.items.get(job_id)

    def latest(self, kinds=None) -> Optional[dict]:
        with self.lock:
            for job_id in reversed(self.order):
                job = self.items.get(job_id)
                if job is None:
                    continue
                if kinds and job.kind not in kinds:
                    continue
                return job.as_dict()
        return None

    def list(self, kinds=None) -> List[dict]:
        with self.lock:
            jobs = [self.items[i].as_dict() for i in self.order if i in self.items]
        if kinds:
            jobs = [job for job in jobs if job["kind"] in kinds]
        return list(reversed(jobs))

    def run(self, kind: str, label: str, work: Callable[[Job], Optional[dict]]) -> Job:
        """Run `work(job)` on a daemon thread and keep its outcome on the job."""
        job = self.create(kind, label)

        def runner() -> None:
            job.state = "running"
            try:
                job.result = work(job) or {}
                job.state = "cancelled" if job.cancelled() else "done"
                if job.state == "done":
                    job.progress = 1.0
            except DownloadCancelled as exc:
                job.state = "cancelled"
                job.detail = str(exc) or "cancelled"
            except AuraError as exc:
                job.state = "error"
                job.error = str(exc)
            except Exception as exc:  # noqa: BLE001 - a job must never take the app down
                job.state = "error"
                job.error = "{}: {}".format(type(exc).__name__, exc)
            finally:
                job.finished = _now()

        try:
            thread = threading.Thread(target=runner, name="aura-job-" + job.id, daemon=True)
            thread.start()
        except RuntimeError:  # no thread support (e.g. a wasm build) - do it now
            runner()
        return job
