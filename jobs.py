from datetime import datetime, timezone
import threading
import uuid
from typing import Any, Dict, Optional

from config import (
    OPERATION_LOG_MAX_ENTRIES,
    JOB_MAX_ENTRIES,
    JOB_EVENTS_MAX_ENTRIES,
)

# Single source of truth for the state lock
state_lock = threading.Lock()

# Shared application state
check_results: Dict[str, Dict[str, Any]] = {}
# Initialize to current time so there's always a value, even before first check completes
last_full_check: Optional[str] = datetime.now(timezone.utc).isoformat()


class OperationLog:
    def __init__(self, max_entries: int = OPERATION_LOG_MAX_ENTRIES):
        self._entries: list[dict[str, Any]] = []
        self.max_entries = max_entries

    def log(self, action: str, target: str, status: str, message: str) -> None:
        entry = {
            "time": datetime.now(timezone.utc).isoformat(),
            "action": action,
            "target": target,
            "status": status,
            "message": message,
        }
        with state_lock:
            self._entries.insert(0, entry)
            if len(self._entries) > self.max_entries:
                self._entries.pop()

    def latest(self, limit: int = 50) -> list[dict[str, Any]]:
        with state_lock:
            return self._entries[:limit]


class JobManager:
    def __init__(self, max_entries: int = JOB_MAX_ENTRIES):
        self.jobs_state: dict[str, dict[str, Any]] = {}
        self.max_entries = max_entries

    def create_job(self, job_type: str, target: str, stack: Optional[str] = None,
                   total_steps: int = 1, meta: Optional[dict[str, Any]] = None) -> str:
        job_id = str(uuid.uuid4())
        job = {
            "job_id": job_id,
            "type": job_type,
            "target": target,
            "stack": stack,
            "status": "running",
            "progress": 0,
            "total_steps": max(total_steps, 1),
            "current_step": "Starting",
            "started_at": datetime.now(timezone.utc).isoformat(),
            "finished_at": None,
            "message": "",
            "meta": meta or {},
            "events": [],
        }
        with state_lock:
            self.jobs_state[job_id] = job
            self._trim_jobs_locked()
        return job_id

    def update_job(self, job_id: str, progress: Optional[int] = None,
                   current_step: Optional[str] = None,
                   message: Optional[str] = None,
                   event: Optional[dict[str, Any]] = None,
                   status: Optional[str] = None) -> None:
        with state_lock:
            job = self.jobs_state.get(job_id)
            if not job:
                return
            if progress is not None:
                job["progress"] = max(0, min(progress, job["total_steps"]))
            if current_step is not None:
                job["current_step"] = current_step
            if message is not None:
                job["message"] = message
            if status is not None:
                job["status"] = status
            if event:
                entry = {
                    "time": datetime.now(timezone.utc).isoformat(),
                    **event,
                }
                job["events"].insert(0, entry)
                if len(job["events"]) > JOB_EVENTS_MAX_ENTRIES:
                    job["events"].pop()

    def finish_job(self, job_id: str, status: str = "success", message: str = "") -> None:
        with state_lock:
            job = self.jobs_state.get(job_id)
            if not job:
                return
            job["status"] = status
            job["progress"] = job["total_steps"]
            job["message"] = message or job.get("message", "")
            job["finished_at"] = datetime.now(timezone.utc).isoformat()
            job["events"].insert(0, {
                "time": job["finished_at"],
                "status": status,
                "message": job["message"] or f"Job finished with status: {status}",
            })
            if len(job["events"]) > JOB_EVENTS_MAX_ENTRIES:
                job["events"].pop()
            self._trim_jobs_locked()

    def _trim_jobs_locked(self) -> None:
        if len(self.jobs_state) <= self.max_entries:
            return
        ordered = sorted(
            self.jobs_state.items(),
            key=lambda kv: kv[1].get("started_at", ""),
            reverse=True,
        )
        keep_ids = {job_id for job_id, _ in ordered[: self.max_entries]}
        for job_id in list(self.jobs_state.keys()):
            if job_id not in keep_ids:
                self.jobs_state.pop(job_id, None)


operations_log = OperationLog()
job_manager = JobManager()
jobs_state = job_manager.jobs_state

# Helper function for backward compatibility
def get_jobs_state():
    """Get the jobs_state dictionary."""
    return jobs_state


def get_check_results():
    """Get the check_results dictionary."""
    return check_results


def get_last_full_check():
    """Get the last_full_check timestamp."""
    return last_full_check


def set_last_full_check(value: str):
    """Set the last_full_check timestamp."""
    global last_full_check
    last_full_check = value


def log_op(action: str, target: str, status: str, message: str) -> None:
    operations_log.log(action, target, status, message)


def create_job(job_type: str, target: str, stack: Optional[str] = None,
               total_steps: int = 1, meta: Optional[dict[str, Any]] = None) -> str:
    return job_manager.create_job(job_type, target, stack, total_steps, meta)


def update_job(job_id: str, progress: Optional[int] = None,
               current_step: Optional[str] = None,
               message: Optional[str] = None,
               event: Optional[dict[str, Any]] = None,
               status: Optional[str] = None) -> None:
    job_manager.update_job(job_id, progress, current_step, message, event, status)


def finish_job(job_id: str, status: str = "success", message: str = "") -> None:
    job_manager.finish_job(job_id, status, message)
