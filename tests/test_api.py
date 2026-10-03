"""Tests for the Flask API surface, using the test client with Docker mocked."""
import json

import pytest

import jobs
import services
import version
from config import APP_SETTINGS_FILE
from pathlib import Path


@pytest.fixture(autouse=True)
def no_docker_side_effects(monkeypatch, clean_state):
    """Prevent tests from touching Docker or registries."""
    monkeypatch.setattr(services, "run_prune_job", lambda *a, **k: None)
    monkeypatch.setattr(services, "run_bulk_pull", lambda *a, **k: None)
    monkeypatch.setattr(services, "run_stack_recreate", lambda *a, **k: None)
    monkeypatch.setattr(
        services, "docker_client", lambda: None
    )
    yield


class TestBasicEndpoints:
    def test_root_serves_dashboard(self, client):
        response = client.get("/")
        assert response.status_code == 200
        assert b"Docker Update Checker" in response.data

    def test_favicon(self, client):
        assert client.get("/favicon.ico").status_code == 200

    def test_health(self, client):
        response = client.get("/health")
        assert response.status_code == 200
        assert response.get_json()["status"] == "ok"

    def test_version(self, client):
        response = client.get("/api/version")
        assert response.status_code == 200
        assert response.get_json() == {"version": version.VERSION}

    def test_status_shape(self, client):
        data = client.get("/api/status").get_json()
        assert set(data) == {
            "last_check", "total", "up_to_date", "updates_available", "unknown",
            "check_interval_minutes", "auto_recreate_after_pull", "notify_enabled",
            "notify_backend", "version",
        }
        assert data["version"] == version.VERSION
        assert data["notify_enabled"] is False

    def test_instances_includes_local(self, client):
        data = client.get("/api/instances").get_json()
        assert data[0]["id"] == "local"

    def test_images_jobs_operations_empty_initially(self, client):
        assert client.get("/api/images").get_json() == []
        assert client.get("/api/jobs").get_json() == []
        assert client.get("/api/operations").get_json() == []


class TestConfigEndpoint:
    def teardown_method(self):
        Path(APP_SETTINGS_FILE).unlink(missing_ok=True)

    def test_get_returns_defaults(self, client):
        data = client.get("/api/config").get_json()
        assert data["auto_recreate_after_pull"] is False
        assert data["check_interval_minutes"] == 60

    def test_post_auto_recreate_from_string(self, client):
        data = client.post("/api/config", json={"auto_recreate": "true"}).get_json()
        assert data["auto_recreate_after_pull"] is True
        # local proxy reads the same live value
        proxied = client.get("/api/instances/local/config").get_json()
        assert proxied["auto_recreate_after_pull"] is True

    def test_post_invalid_auto_recreate_rejected(self, client):
        response = client.post("/api/config", json={"auto_recreate": {"weird": []}})
        assert response.status_code == 400

    def test_post_interval_must_be_integer(self, client):
        response = client.post("/api/config", json={"check_interval_minutes": "ten"})
        assert response.status_code == 400

    def test_post_interval_bounds(self, client):
        assert client.post("/api/config", json={"check_interval_minutes": 0}).status_code == 400
        assert client.post("/api/config", json={"check_interval_minutes": 1441}).status_code == 400

    def test_post_valid_interval_persists(self, client):
        # the scheduler must know the "full_check" job before it can reschedule
        import services

        services.scheduler.add_job(lambda: None, "interval", minutes=60, id="full_check")
        try:
            response = client.post("/api/config", json={"check_interval_minutes": 30})
            assert response.status_code == 200
            assert response.get_json()["check_interval_minutes"] == 30
            stored = json.loads(Path(APP_SETTINGS_FILE).read_text())
            assert stored["check_interval_minutes"] == 30
        finally:
            if services.scheduler.get_job("full_check"):
                services.scheduler.remove_job("full_check")


class TestProxyLocal:
    def test_status_path(self, client):
        response = client.get("/api/instances/local/status")
        assert response.status_code == 200
        assert response.get_json()["total"] == 0

    def test_unsupported_path_400(self, client):
        assert client.get("/api/instances/local/nonsense").status_code == 400

    def test_unsupported_prune_type_400(self, client):
        assert client.post("/api/instances/local/prune/nonsense").status_code == 400

    def test_images_path_reflects_state(self, client, clean_state):
        jobs.check_results["redis:7"] = {
            "image": "redis:7", "status": "up_to_date", "compose_files": [], "stacks": [],
        }
        data = client.get("/api/instances/local/images").get_json()
        assert [i["image"] for i in data] == ["redis:7"]

    def test_stacks_all_path(self, client, monkeypatch):
        monkeypatch.setattr(
            services, "get_all_stacks", lambda: {"media": {"status": "running"}}
        )
        data = client.get("/api/instances/local/stacks/all").get_json()
        assert data == {"media": {"status": "running"}}

    def test_notify_test_succeeds_when_disabled(self, client):
        response = client.post("/api/notify/test")
        assert response.status_code == 200
        assert response.get_json()["status"] == "success"


class TestBackgroundJobs:
    def test_check_endpoint_starts_job(self, client, monkeypatch):
        started = []
        monkeypatch.setattr(
            "threading.Thread.start", lambda self: started.append(self._target)
        )
        response = client.post("/api/check")
        assert response.status_code == 200
        assert response.get_json()["status"] == "started"
        assert len(jobs.jobs_state) == 1
        assert started  # the worker thread was created (but not started)

    def test_job_detail_and_list(self, client, monkeypatch):
        monkeypatch.setattr("threading.Thread.start", lambda self: None)
        job_id = client.post("/api/check").get_json()["job_id"]

        detail = client.get(f"/api/jobs/{job_id}").get_json()
        assert detail["job_id"] == job_id
        assert detail["type"] == "full_check"

        listed = client.get("/api/jobs").get_json()
        assert [j["job_id"] for j in listed] == [job_id]

    def test_unknown_job_404(self, client):
        assert client.get("/api/jobs/nope").status_code == 404
