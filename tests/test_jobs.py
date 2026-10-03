"""Tests for jobs.py: operation log and job manager."""
import jobs
from jobs import (
    JobManager,
    OperationLog,
    create_job,
    finish_job,
    get_last_full_check,
    set_last_full_check,
    update_job,
)


class TestOperationLog:
    def test_entries_are_newest_first(self):
        log = OperationLog(max_entries=10)
        log.log("check", "a", "success", "first")
        log.log("check", "b", "success", "second")
        entries = log.latest()
        assert len(entries) == 2
        assert entries[0]["message"] == "second"
        assert entries[1]["message"] == "first"

    def test_limit_returns_requested_count(self):
        log = OperationLog(max_entries=10)
        for i in range(5):
            log.log("check", f"img{i}", "success", str(i))
        assert len(log.latest(limit=2)) == 2

    def test_max_entries_trims_oldest(self):
        log = OperationLog(max_entries=3)
        for i in range(10):
            log.log("check", f"img{i}", "success", str(i))
        entries = log.latest(limit=100)
        assert len(entries) == 3
        assert entries[0]["message"] == "9"
        assert entries[-1]["message"] == "7"

    def test_entry_shape(self):
        log = OperationLog()
        log.log("pull", "redis:7", "success", "Pulled")
        entry = log.latest()[0]
        assert set(entry) == {"time", "action", "target", "status", "message"}
        assert entry["action"] == "pull"
        assert entry["target"] == "redis:7"


class TestJobManager:
    def test_create_job_returns_id_and_defaults(self):
        manager = JobManager()
        job_id = manager.create_job("full_check", "all", total_steps=4)
        job = manager.jobs_state[job_id]
        assert job["type"] == "full_check"
        assert job["status"] == "running"
        assert job["total_steps"] == 4
        assert job["progress"] == 0
        assert job["events"] == []

    def test_update_job_progress_is_clamped(self):
        manager = JobManager()
        job_id = manager.create_job("full_check", "all", total_steps=2)
        manager.update_job(job_id, progress=99)
        assert manager.jobs_state[job_id]["progress"] == 2
        manager.update_job(job_id, progress=-5)
        assert manager.jobs_state[job_id]["progress"] == 0

    def test_update_job_unknown_id_is_noop(self):
        manager = JobManager()
        manager.update_job("missing", progress=1)  # must not raise
        manager.finish_job("missing", "error", "boom")

    def test_update_job_records_events_newest_first(self):
        manager = JobManager()
        job_id = manager.create_job("full_check", "all")
        manager.update_job(job_id, event={"status": "info", "message": "one"})
        manager.update_job(job_id, event={"status": "info", "message": "two"})
        events = manager.jobs_state[job_id]["events"]
        assert [e["message"] for e in events] == ["two", "one"]

    def test_finish_job_sets_status_and_full_progress(self):
        manager = JobManager()
        job_id = manager.create_job("bulk_pull", "all", total_steps=3)
        manager.finish_job(job_id, "success", "done")
        job = manager.jobs_state[job_id]
        assert job["status"] == "success"
        assert job["progress"] == 3
        assert job["finished_at"] is not None
        assert job["message"] == "done"

    def test_finish_job_keeps_existing_message_when_empty(self):
        manager = JobManager()
        job_id = manager.create_job("bulk_pull", "all")
        manager.update_job(job_id, message="in flight")
        manager.finish_job(job_id, "error", "")
        assert manager.jobs_state[job_id]["message"] == "in flight"

    def test_old_jobs_are_trimmed_to_max(self):
        manager = JobManager(max_entries=3)
        ids = [manager.create_job("full_check", "all") for _ in range(6)]
        assert len(manager.jobs_state) == 3
        # the most recently started jobs survive
        for job_id in ids[3:]:
            assert job_id in manager.jobs_state
        for job_id in ids[:3]:
            assert job_id not in manager.jobs_state


class TestModuleLevelHelpers:
    def test_create_update_finish_via_module_functions(self, clean_state):
        job_id = create_job("full_check", "all", total_steps=2)
        assert job_id in jobs.jobs_state
        update_job(job_id, progress=1, current_step="halfway")
        assert jobs.jobs_state[job_id]["current_step"] == "halfway"
        finish_job(job_id, "success", "complete")
        assert jobs.jobs_state[job_id]["status"] == "success"

    def test_last_full_check_roundtrip(self):
        original = get_last_full_check()
        try:
            set_last_full_check("2026-01-01T00:00:00+00:00")
            assert get_last_full_check() == "2026-01-01T00:00:00+00:00"
        finally:
            set_last_full_check(original)
