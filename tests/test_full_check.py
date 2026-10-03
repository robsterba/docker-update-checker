"""Integration test: run_full_check over the fixture compose tree.

check_image is mocked, so no Docker daemon or network access is needed.
"""
import jobs
import services
from jobs import get_last_full_check
from services import run_full_check, summarize_stacks


def _fake_check(image_ref):
    return {
        "image": image_ref,
        "status": "update_available" if image_ref.startswith("redis") else "up_to_date",
        "local_digest": "sha256:local",
        "remote_digest": "sha256:remote",
        "checked_at": "2026-01-01T00:00:00+00:00",
    }


def test_run_full_check_populates_results_and_finishes_job(monkeypatch, clean_state):
    notified = []
    monkeypatch.setattr(services, "check_image", _fake_check)
    monkeypatch.setattr(services, "notify_updates_found", lambda results: notified.append(results))

    run_full_check()

    # every image in the fixture compose files is checked exactly once
    assert sorted(jobs.check_results) == [
        "example.invalid/site",
        "jellyfin/jellyfin:latest",
        "nginx:1.27",
        "redis:7-alpine",
    ]

    # compose file and stack metadata is attached
    redis = jobs.check_results["redis:7-alpine"]
    assert [p.replace("\\", "/") for p in redis["compose_files"]] == ["media/compose.yaml"]
    assert redis["stacks"] == ["media"]

    # the job completes successfully
    finished = [j for j in jobs.jobs_state.values() if j["type"] == "full_check"]
    assert len(finished) == 1
    assert finished[0]["status"] == "success"

    # a last-check timestamp was recorded
    assert get_last_full_check()

    # one update was found and a notification was triggered
    assert len(notified) == 1
    assert "redis:7-alpine" in notified[0]

    # stack summaries reflect the new state
    stacks = {s["stack"]: s for s in summarize_stacks()}
    assert stacks["media"]["updates_available"] == 1
    assert stacks["web"]["total_images"] == 2


def test_run_full_check_with_job_id_uses_provided_job(monkeypatch, clean_state):
    monkeypatch.setattr(services, "check_image", _fake_check)
    monkeypatch.setattr(services, "notify_updates_found", lambda results: None)

    from jobs import create_job

    job_id = create_job("full_check", "custom", total_steps=4)
    run_full_check(job_id=job_id)

    job = jobs.jobs_state[job_id]
    assert job["status"] == "success"
    assert job["target"] == "custom"
    assert len(jobs.jobs_state) == 1
