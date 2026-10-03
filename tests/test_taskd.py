"""Tests for taskd.py: settings, client gating, sync lifecycle, API endpoints."""

import pytest

import taskd
from taskd import (
    get_taskd_settings,
    normalize_taskd_settings,
    sync_results,
)


def make_result(image, status, **overrides):
    result = {
        "image": image,
        "status": status,
        "local_digest": "sha256:local",
        "remote_digest": "sha256:remote",
        "checked_at": "2026-01-01T00:00:00+00:00",
        "compose_files": ["/compose/stack/compose.yaml"],
        "stacks": ["stack"],
    }
    result.update(overrides)
    return result


def make_settings(**overrides):
    settings = {
        "enabled": True,
        "url": "http://taskd:8000",
        "timeout": 5,
        "tags": ["automated", "homelab"],
        "source": "docker-update-checker",
        "host_label": "moxy",
    }
    settings.update(overrides)
    return settings


class FakeTaskdServer:
    """Minimal in-memory stand-in for the taskd REST API."""

    def __init__(self, tasks=None):
        self.tasks = tasks or {}
        self.next_id = 1

    def _new_id(self):
        tid = f"task-{self.next_id}"
        self.next_id += 1
        return tid

    def search(self, settings, params):
        needle = (params.get("search") or "").lower()
        matches = []
        for t in self.tasks.values():
            if params.get("parent") == "none" and t.get("parent_task_id"):
                continue
            if needle and needle not in (t.get("name") or "").lower():
                continue
            matches.append(t)
        return matches

    def get(self, settings, task_id):
        task = self.tasks[task_id]
        detail = dict(task)
        detail["subtasks"] = [
            dict(t) for t in self.tasks.values()
            if t.get("parent_task_id") == task_id
        ]
        return detail

    def create(self, settings, body):
        tid = self._new_id()
        task = {
            "id": tid,
            "name": body.get("name", ""),
            "description": body.get("description") or "",
            "status": body.get("status") or "todo",
            "priority": body.get("priority") or "medium",
            "parent_task_id": body.get("parent_task_id"),
            "tags": body.get("tags") or [],
            "source": body.get("source") or "api",
            "updated_at": "2026-01-01T00:00:00Z",
        }
        self.tasks[tid] = task
        return dict(task)

    def patch(self, settings, task_id, body):
        task = self.tasks[task_id]
        task.update({k: v for k, v in body.items() if v is not None})
        return dict(task)

    def complete(self, settings, task_id):
        self.tasks[task_id]["status"] = "done"
        return dict(self.tasks[task_id])

    def names(self):
        return sorted(t["name"] for t in self.tasks.values())


@pytest.fixture
def wire_server(monkeypatch):
    """Route taskd client calls to a FakeTaskdServer with fixed settings."""
    server = FakeTaskdServer()

    def install(settings=None):
        monkeypatch.setattr(taskd, "get_taskd_settings", lambda: settings or make_settings())
        monkeypatch.setattr(taskd, "search_tasks", server.search)
        monkeypatch.setattr(taskd, "get_task", server.get)
        monkeypatch.setattr(taskd, "create_task", server.create)
        monkeypatch.setattr(taskd, "patch_task", server.patch)
        monkeypatch.setattr(taskd, "complete_task", server.complete)

    install.server = server
    return install


# ── Settings ──────────────────────────────────────────────────────────────────


class TestSettings:
    def test_env_defaults_when_no_file(self):
        settings = get_taskd_settings()
        assert settings["enabled"] is False  # pinned in conftest
        assert settings["url"] == ""
        assert settings["timeout"] == 10
        assert settings["tags"] == ["automated", "homelab"]
        assert settings["source"] == "docker-update-checker"

    def test_file_overrides_env(self, monkeypatch, tmp_path):
        path = tmp_path / "taskd_settings.json"
        path.write_text('{"enabled": true, "url": "http://file:8000/"}')
        monkeypatch.setattr(taskd, "TASKD_SETTINGS_FILE", str(path))
        settings = get_taskd_settings()
        assert settings["enabled"] is True
        assert settings["url"] == "http://file:8000"  # trailing slash stripped

    def test_normalization(self):
        settings = normalize_taskd_settings({
            "url": "http://x:8000/",
            "timeout": "not-a-number",
            "tags": ["Automated", " homelab ", ""],
            "source": "  ",
            "host_label": None,
            "enabled": "true",
        })
        assert settings["url"] == "http://x:8000"
        assert settings["timeout"] == 10  # falls back to default
        assert settings["tags"] == ["automated", "homelab"]
        assert settings["source"] == "docker-update-checker"
        assert settings["host_label"] == ""
        assert settings["enabled"] is True

    def test_timeout_is_bounded(self):
        assert normalize_taskd_settings({"timeout": 999})["timeout"] == 60

    def test_string_false_does_not_enable(self):
        for value in ("false", "no", "0", "off", ""):
            assert normalize_taskd_settings({"enabled": value})["enabled"] is False
        assert normalize_taskd_settings({"enabled": "true"})["enabled"] is True
        assert normalize_taskd_settings({"enabled": True})["enabled"] is True

    def test_schemeless_url_gets_http_prefix(self):
        assert normalize_taskd_settings({"url": "taskd:8000"})["url"] == "http://taskd:8000"

    def test_unknown_keys_are_dropped(self):
        settings = normalize_taskd_settings({"enabled": True, "junk": "x", "url": "http://t:8000"})
        assert settings == {
            "enabled": True,
            "url": "http://t:8000",
            "timeout": 10,
            "tags": ["automated", "homelab"],
            "source": "docker-update-checker",
            "host_label": "",
        }


# ── Connection test ──────────────────────────────────────────────────────────


class TestConnection:
    def test_no_url_configured(self):
        result = taskd.test_taskd_connection(url="")
        assert result["status"] == "error"
        assert "No taskd URL" in result["message"]

    def test_success_reports_latency(self, monkeypatch):
        class FakeResponse:
            def raise_for_status(self):
                pass

        monkeypatch.setattr(taskd.requests, "get", lambda url, timeout=None: FakeResponse())
        result = taskd.test_taskd_connection(url="http://taskd:8000")
        assert result["status"] == "success"
        assert result["latency_ms"] >= 0

    def test_unreachable_returns_error_not_raise(self, monkeypatch):
        def boom(url, timeout=None):
            raise taskd.requests.ConnectionError("refused")

        monkeypatch.setattr(taskd.requests, "get", boom)
        result = taskd.test_taskd_connection(url="http://taskd:8000")
        assert result["status"] == "error"
        assert "refused" in result["message"]


# ── Sync ─────────────────────────────────────────────────────────────────────


class TestSyncGating:
    def test_disabled_skips_sync(self):
        # conftest pins TASKD_ENABLED=false; no client call must happen
        assert sync_results({"redis:7": make_result("redis:7", "update_available")})["status"] == "skipped"

    def test_enabled_without_url_skips_sync(self, monkeypatch):
        monkeypatch.setattr(taskd, "get_taskd_settings", lambda: make_settings(url=""))
        assert sync_results({})["status"] == "skipped"

    def test_concurrent_sync_skips(self, monkeypatch):
        monkeypatch.setattr(taskd, "get_taskd_settings", lambda: make_settings())
        assert taskd._sync_lock.acquire(blocking=False)
        try:
            result = sync_results({})
        finally:
            taskd._sync_lock.release()
        assert result == {"status": "skipped", "reason": "sync in progress"}

    def test_unreachable_taskd_returns_error_not_raise(self, monkeypatch):
        monkeypatch.setattr(taskd, "get_taskd_settings", lambda: make_settings())

        def boom(settings, params):
            raise taskd.requests.ConnectionError("refused")

        monkeypatch.setattr(taskd, "search_tasks", boom)
        result = sync_results({"redis:7": make_result("redis:7", "update_available")})
        assert result["status"] == "error"
        assert "refused" in result["message"]
        # the lock must be released so the next sync is not stuck as "in progress"
        result2 = sync_results({"redis:7": make_result("redis:7", "update_available")})
        assert result2["status"] == "error"


class TestSyncLifecycle:
    def test_creates_parent_and_subtasks_for_outdated_images(self, wire_server):
        wire_server()
        result = sync_results({
            "redis:7": make_result("redis:7", "update_available"),
            "nginx:1.25": make_result("nginx:1.25", "update_available"),
            "fresh:2": make_result("fresh:2", "up_to_date"),
        })
        assert result["status"] == "success"
        assert result["outdated"] == 2
        assert result["created"] == 2
        server = wire_server.server
        assert server.names() == [
            "Container updates: moxy",
            "Update nginx:1.25",
            "Update redis:7",
        ]
        subtasks = [t for t in server.tasks.values() if t["parent_task_id"]]
        assert all(t["source"] == "docker-update-checker" for t in subtasks)

    def test_existing_subtask_is_not_recreated(self, wire_server):
        server = wire_server.server
        parent = server.create(None, {"name": "Container updates: moxy",
                                     "source": "docker-update-checker"})
        existing = server.create(None, {
            "name": "Update redis:7",
            "parent_task_id": parent["id"],
            "source": "docker-update-checker",
            "description": "old",
        })
        wire_server()
        result = sync_results({"redis:7": make_result("redis:7", "update_available")})
        assert result["created"] == 0
        assert result["updated"] == 1  # description refreshed
        assert existing["id"] in server.tasks
        assert len([t for t in server.tasks.values() if t.get("parent_task_id")]) == 1

    def test_unchanged_subtask_is_not_patched(self, wire_server):
        server = wire_server.server
        parent = server.create(None, {"name": "Container updates: moxy",
                                      "source": "docker-update-checker"})
        server.create(None, {
            "name": "Update redis:7",
            "parent_task_id": parent["id"],
            "source": "docker-update-checker",
            "tags": ["automated", "homelab"],
            "description": taskd._subtask_description(make_result("redis:7", "update_available")),
        })
        wire_server()
        result = sync_results({"redis:7": make_result("redis:7", "update_available")})
        assert result["created"] == 0
        assert result["updated"] == 0

    def test_completed_subtask_for_outdated_image_is_reopened(self, wire_server):
        server = wire_server.server
        parent = server.create(None, {"name": "Container updates: moxy",
                                      "source": "docker-update-checker"})
        done = server.create(None, {
            "name": "Update redis:7",
            "parent_task_id": parent["id"],
            "source": "docker-update-checker",
        })
        server.patch(None, done["id"], {"status": "done"})
        wire_server()
        result = sync_results({"redis:7": make_result("redis:7", "update_available")})
        assert result["updated"] == 1
        assert server.tasks[done["id"]]["status"] == "todo"

    def test_up_to_date_and_removed_images_are_completed(self, wire_server):
        server = wire_server.server
        parent = server.create(None, {"name": "Container updates: moxy",
                                      "source": "docker-update-checker"})
        fresh = server.create(None, {
            "name": "Update redis:7", "parent_task_id": parent["id"],
            "source": "docker-update-checker",
        })
        gone = server.create(None, {
            "name": "Update old:1", "parent_task_id": parent["id"],
            "source": "docker-update-checker",
        })
        wire_server()
        result = sync_results({"redis:7": make_result("redis:7", "up_to_date")})
        assert result["completed"] == 2
        assert server.tasks[fresh["id"]]["status"] == "done"
        assert server.tasks[gone["id"]]["status"] == "done"

    def test_registry_errors_and_unknown_status_never_complete(self, wire_server):
        server = wire_server.server
        parent = server.create(None, {"name": "Container updates: moxy",
                                      "source": "docker-update-checker"})
        errored = server.create(None, {
            "name": "Update redis:7", "parent_task_id": parent["id"],
            "source": "docker-update-checker",
        })
        unknown = server.create(None, {
            "name": "Update ghost:9", "parent_task_id": parent["id"],
            "source": "docker-update-checker",
        })
        wire_server()
        result = sync_results({
            "redis:7": make_result("redis:7", "registry_error"),
            "ghost:9": make_result("ghost:9", "unknown"),
        })
        assert result["completed"] == 0
        assert server.tasks[errored["id"]]["status"] == "todo"
        assert server.tasks[unknown["id"]]["status"] == "todo"

    def test_user_created_subtasks_are_left_alone(self, wire_server):
        server = wire_server.server
        parent = server.create(None, {"name": "Container updates: moxy",
                                      "source": "docker-update-checker"})
        manual = server.create(None, {
            "name": "Update redis:7 by hand", "parent_task_id": parent["id"],
            "source": "gui",
        })
        wire_server()
        sync_results({"redis:7": make_result("redis:7", "up_to_date")})
        assert server.tasks[manual["id"]]["status"] == "todo"

    def test_archived_subtasks_are_ignored(self, wire_server):
        server = wire_server.server
        parent = server.create(None, {"name": "Container updates: moxy",
                                      "source": "docker-update-checker"})
        archived = server.create(None, {
            "name": "Update redis:7", "parent_task_id": parent["id"],
            "source": "docker-update-checker",
        })
        server.patch(None, archived["id"], {"status": "archived"})
        wire_server()
        result = sync_results({"redis:7": make_result("redis:7", "update_available")})
        # a fresh subtask is created; the archived one is neither patched nor completed
        assert result["created"] == 1
        assert server.tasks[archived["id"]]["status"] == "archived"
        assert len([t for t in server.tasks.values() if t.get("parent_task_id")]) == 2

    def test_multiple_parent_matches_prefers_marker_then_newest(self, wire_server):
        server = wire_server.server
        other = server.create(None, {
            "name": "Container updates: moxy",
            "description": "Host: moxy\nOutdated images: 5",
            "source": "docker-update-checker",
            "updated_at": "2025-01-01T00:00:00Z",
        })
        newer_unmarked = server.create(None, {
            "name": "Container updates: moxy",
            "description": "unrelated",
            "updated_at": "2026-06-01T00:00:00Z",
        })
        wire_server()
        sync_results({"redis:7": make_result("redis:7", "update_available")})
        subtasks = [t for t in server.tasks.values() if t.get("parent_task_id") == other["id"]]
        assert subtasks  # the marked parent won, not the newer unmarked one
        assert not [t for t in server.tasks.values()
                    if t.get("parent_task_id") == newer_unmarked["id"]]

    def test_no_updates_completes_stale_and_creates_nothing(self, wire_server):
        server = wire_server.server
        parent = server.create(None, {"name": "Container updates: moxy",
                                      "source": "docker-update-checker"})
        stale = server.create(None, {
            "name": "Update redis:7", "parent_task_id": parent["id"],
            "source": "docker-update-checker",
        })
        wire_server()
        result = sync_results({"redis:7": make_result("redis:7", "up_to_date")})
        assert result["status"] == "success"
        assert result["outdated"] == 0
        assert result["created"] == 0
        assert server.tasks[stale["id"]]["status"] == "done"

    def test_host_label_is_used_in_parent_title(self, wire_server):
        wire_server(make_settings(host_label="moxy-lan"))
        sync_results({})
        server = wire_server.server
        assert "Container updates: moxy-lan" in server.names()

    def test_parent_description_is_refreshed_when_count_changes(self, wire_server):
        server = wire_server.server
        parent = server.create(None, {"name": "Container updates: moxy",
                                      "source": "docker-update-checker",
                                      "description": "Outdated images: 0"})
        wire_server()
        sync_results({"redis:7": make_result("redis:7", "update_available")})
        assert "Outdated images: 1" in server.tasks[parent["id"]]["description"]

    def test_tags_change_propagates_to_existing_tasks(self, wire_server):
        server = wire_server.server
        parent = server.create(None, {"name": "Container updates: moxy",
                                      "source": "docker-update-checker",
                                      "tags": ["automated", "homelab"]})
        subtask = server.create(None, {
            "name": "Update redis:7", "parent_task_id": parent["id"],
            "source": "docker-update-checker", "tags": ["automated", "homelab"],
            "description": taskd._subtask_description(make_result("redis:7", "update_available")),
        })
        wire_server(make_settings(tags=["automated", "docker"]))
        sync_results({"redis:7": make_result("redis:7", "update_available")})
        assert server.tasks[subtask["id"]]["tags"] == ["automated", "docker"]
        assert server.tasks[parent["id"]]["tags"] == ["automated", "docker"]

    def test_degenerate_scan_never_completes(self, wire_server):
        server = wire_server.server
        parent = server.create(None, {"name": "Container updates: moxy",
                                      "source": "docker-update-checker"})
        stale = server.create(None, {
            "name": "Update redis:7", "parent_task_id": parent["id"],
            "source": "docker-update-checker",
        })
        wire_server()
        # empty compose scan: completion is unsafe and must be skipped
        result = sync_results({}, allow_completion=False)
        assert result["status"] == "success"
        assert result["completed"] == 0
        assert server.tasks[stale["id"]]["status"] == "todo"


# ── API endpoints ────────────────────────────────────────────────────────────


class TestTaskdAPI:
    @pytest.fixture(autouse=True)
    def isolate_settings_file(self, monkeypatch, tmp_path):
        """Keep API tests off the shared settings file so they cannot
        pollute other tests that rely on env defaults."""
        monkeypatch.setattr(taskd, "TASKD_SETTINGS_FILE",
                            str(tmp_path / "taskd_settings.json"))

    def test_get_config_returns_defaults(self, client):
        resp = client.get("/api/config/taskd")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["enabled"] is False
        assert data["url"] == ""
        assert data["tags"] == ["automated", "homelab"]

    def test_post_config_persists_and_round_trips(self, client):
        resp = client.post("/api/config/taskd", json={
            "enabled": True,
            "url": "http://taskd:8000/",
            "timeout": 15,
            "tags": ["Automated"],
            "source": "docker-update-checker",
            "host_label": "moxy",
        })
        assert resp.status_code == 200
        data = client.get("/api/config/taskd").get_json()
        assert data["enabled"] is True
        assert data["url"] == "http://taskd:8000"
        assert data["timeout"] == 15
        assert data["tags"] == ["automated"]

    def test_post_config_rejects_empty_body(self, client):
        resp = client.post("/api/config/taskd", json={})
        assert resp.status_code == 400

    def test_test_endpoint_reports_unreachable(self, client, monkeypatch):
        def boom(url, timeout=None):
            raise taskd.requests.ConnectionError("refused")

        monkeypatch.setattr(taskd.requests, "get", boom)
        resp = client.post("/api/taskd/test", json={"url": "http://taskd:8000"})
        assert resp.status_code == 502
        assert resp.get_json()["status"] == "error"

    def test_test_endpoint_success(self, client, monkeypatch):
        class FakeResponse:
            def raise_for_status(self):
                pass

        monkeypatch.setattr(taskd.requests, "get", lambda url, timeout=None: FakeResponse())
        resp = client.post("/api/taskd/test", json={"url": "http://taskd:8000"})
        assert resp.status_code == 200
        assert resp.get_json()["status"] == "success"


class TestFullCheckHook:
    def _fake_check(self, image_ref):
        return make_result(image_ref, "update_available" if image_ref.startswith("redis") else "up_to_date")

    def test_scan_survives_taskd_outage(self, monkeypatch, clean_state):
        import jobs
        import services

        monkeypatch.setattr(services, "check_image", self._fake_check)
        monkeypatch.setattr(services, "notify_updates_found", lambda results: None)
        monkeypatch.setattr(taskd, "get_taskd_settings", lambda: make_settings())
        monkeypatch.setattr(taskd, "search_tasks",
                            lambda s, p: (_ for _ in ()).throw(taskd.requests.ConnectionError("refused")))

        run_full_check = services.run_full_check
        run_full_check()

        finished = [j for j in jobs.jobs_state.values() if j["type"] == "full_check"]
        assert finished[0]["status"] == "success"
        assert any(entry["action"] == "taskd" and entry["status"] == "error"
                   for entry in jobs.operations_log._entries)

    def test_scan_syncs_results_when_reachable(self, monkeypatch, clean_state, wire_server):
        import jobs
        import services

        monkeypatch.setattr(services, "check_image", self._fake_check)
        monkeypatch.setattr(services, "notify_updates_found", lambda results: None)
        wire_server()

        services.run_full_check()

        server = wire_server.server
        # the fixture tree has one outdated image (redis:7-alpine)
        assert server.names() == [
            "Container updates: moxy",
            "Update redis:7-alpine",
        ]
        assert any(entry["action"] == "taskd" and entry["status"] == "success"
                   for entry in jobs.operations_log._entries)
