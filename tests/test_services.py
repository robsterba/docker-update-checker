"""Tests for services.py: instances, settings, stack summaries, and proxying."""
import json

import pytest
import requests

import jobs
import services
from services import (
    derive_stack_name,
    get_all_instances,
    get_check_interval_minutes,
    get_instance,
    get_images_for_stack,
    get_outdated_images,
    load_app_settings,
    load_remote_instances,
    normalize_remote_instance,
    save_app_settings,
    summarize_stacks,
)


@pytest.fixture
def remote_instances_env(monkeypatch, tmp_path):
    """Point the remote instance loader at a temporary file."""
    instances_file = tmp_path / "remote_instances.json"
    monkeypatch.setattr(services, "REMOTE_INSTANCES_FILE", str(instances_file))
    monkeypatch.setattr(services, "REMOTE_INSTANCES_CONFIG", "")
    return instances_file


class TestNormalizeRemoteInstance:
    def test_dict_entry(self):
        entry = normalize_remote_instance(
            {"name": "Node 1", "url": "http://192.168.1.10:5000", "description": "attic"}
        )
        assert entry == {
            "id": "node-1",
            "name": "Node 1",
            "url": "http://192.168.1.10:5000",
            "description": "attic",
            "token": "",
            "type": "remote",
        }

    def test_pipe_delimited_string(self):
        entry = normalize_remote_instance("basement|192.168.1.11:5000")
        assert entry["id"] == "basement"
        assert entry["url"] == "http://192.168.1.11:5000"

    def test_bare_url_string(self):
        entry = normalize_remote_instance("http://192.168.1.12:5000")
        assert entry["url"] == "http://192.168.1.12:5000"
        assert entry["name"] == "http://192.168.1.12:5000"

    def test_https_scheme_preserved(self):
        entry = normalize_remote_instance("https://node.example.com")
        assert entry["url"] == "https://node.example.com"

    def test_trailing_slash_stripped(self):
        entry = normalize_remote_instance({"url": "http://node:5000/"})
        assert entry["url"] == "http://node:5000"

    def test_missing_url_returns_none(self):
        assert normalize_remote_instance({"name": "no url"}) is None
        assert normalize_remote_instance(12345) is None

    def test_id_is_slugified(self):
        entry = normalize_remote_instance({"id": "My Node!!!", "url": "http://x"})
        assert entry["id"] == "my-node"


class TestLoadRemoteInstances:
    def test_loads_from_file(self, remote_instances_env):
        remote_instances_env.write_text(json.dumps([
            {"name": "Node 1", "url": "http://192.168.1.10:5000"},
            "basement|http://192.168.1.11:5000",
        ]))
        instances = load_remote_instances()
        assert [i["id"] for i in instances] == ["node-1", "basement"]

    def test_missing_file_returns_empty(self, remote_instances_env):
        assert load_remote_instances() == []

    def test_json_env_var(self, monkeypatch, remote_instances_env):
        monkeypatch.setattr(
            services,
            "REMOTE_INSTANCES_CONFIG",
            json.dumps([{"name": "Env Node", "url": "http://env:5000"}]),
        )
        instances = load_remote_instances()
        assert [i["id"] for i in instances] == ["env-node"]

    def test_pipe_delimited_env_var(self, monkeypatch, remote_instances_env):
        monkeypatch.setattr(
            services, "REMOTE_INSTANCES_CONFIG", "a|http://a:5000\nb|http://b:5000"
        )
        assert [i["id"] for i in load_remote_instances()] == ["a", "b"]

    def test_duplicate_ids_are_deduplicated(self, monkeypatch, remote_instances_env):
        monkeypatch.setattr(
            services,
            "REMOTE_INSTANCES_CONFIG",
            json.dumps([
                {"name": "Same", "url": "http://one:5000"},
                {"name": "Same", "url": "http://two:5000"},
            ]),
        )
        instances = load_remote_instances()
        assert len(instances) == 1
        assert instances[0]["url"] == "http://two:5000"


class TestInstances:
    def test_local_instance_always_present(self):
        instances = get_all_instances()
        assert instances[0]["id"] == "local"
        assert instances[0]["type"] == "local"

    def test_get_instance_local(self):
        assert get_instance("local")["type"] == "local"

    def test_get_instance_unknown_returns_none(self, remote_instances_env):
        assert get_instance("nope") is None

    def test_get_instance_remote(self, remote_instances_env):
        remote_instances_env.write_text(
            json.dumps([{"name": "Node 1", "url": "http://192.168.1.10:5000"}])
        )
        assert get_instance("node-1")["url"] == "http://192.168.1.10:5000"


class TestSettings:
    def test_app_settings_roundtrip(self, monkeypatch, tmp_path):
        settings_file = tmp_path / "settings.json"
        monkeypatch.setattr(services, "APP_SETTINGS_FILE", str(settings_file))
        assert load_app_settings() == {}
        assert save_app_settings({"auto_recreate": True}) is True
        assert load_app_settings() == {"auto_recreate": True}

    def test_check_interval_uses_stored_setting(self, monkeypatch, tmp_path):
        settings_file = tmp_path / "settings.json"
        settings_file.write_text(json.dumps({"check_interval_minutes": 15}))
        monkeypatch.setattr(services, "APP_SETTINGS_FILE", str(settings_file))
        assert get_check_interval_minutes() == 15

    def test_check_interval_falls_back_to_env_default(self, monkeypatch, tmp_path):
        monkeypatch.setattr(services, "APP_SETTINGS_FILE", str(tmp_path / "missing.json"))
        assert get_check_interval_minutes() == 60  # from test env CHECK_INTERVAL_MINUTES

    def test_check_interval_ignores_invalid_stored_value(self, monkeypatch, tmp_path):
        settings_file = tmp_path / "settings.json"
        settings_file.write_text(json.dumps({"check_interval_minutes": 0}))
        monkeypatch.setattr(services, "APP_SETTINGS_FILE", str(settings_file))
        assert get_check_interval_minutes() == 60


class TestStackSummaries:
    @pytest.fixture(autouse=True)
    def seeded_results(self, clean_state):
        jobs.check_results.clear()
        jobs.check_results.update({
            "redis:7-alpine": {
                "image": "redis:7-alpine",
                "status": "update_available",
                "checked_at": "2026-01-02T00:00:00+00:00",
                "compose_files": ["media/compose.yaml"],
                "stacks": ["media"],
            },
            "jellyfin/jellyfin:latest": {
                "image": "jellyfin/jellyfin:latest",
                "status": "up_to_date",
                "checked_at": "2026-01-01T00:00:00+00:00",
                "compose_files": ["media/compose.yaml"],
                "stacks": ["media"],
            },
            "orphan:1": {
                "image": "orphan:1",
                "status": "unknown",
                "checked_at": None,
                "compose_files": [],
                "stacks": [],
            },
        })
        yield
        jobs.check_results.clear()

    def test_summarize_stacks_groups_by_stack(self):
        stacks = {s["stack"]: s for s in summarize_stacks()}
        assert set(stacks) == {"media", "unassigned"}
        media = stacks["media"]
        assert media["total_images"] == 2
        assert media["updates_available"] == 1
        assert media["up_to_date"] == 1
        assert media["last_checked"] == "2026-01-02T00:00:00+00:00"

    def test_summarize_stacks_sorted_by_updates_desc(self):
        stacks = summarize_stacks()
        assert stacks[0]["stack"] == "media"  # has updates; unassigned has none

    def test_get_outdated_images_filters_by_status_and_stack(self):
        assert get_outdated_images() == ["redis:7-alpine"]
        assert get_outdated_images(stack_name="media") == ["redis:7-alpine"]
        assert get_outdated_images(stack_name="web") == []

    def test_get_images_for_stack(self):
        assert sorted(get_images_for_stack("media")) == [
            "jellyfin/jellyfin:latest",
            "redis:7-alpine",
        ]
        assert get_images_for_stack("nope") == []


class TestProxyRemoteRequest:
    def test_unknown_instance_404(self, client):
        response = client.get("/api/instances/ghost/status")
        assert response.status_code == 404

    def test_disallowed_path_400(self, remote_instances_env, client):
        remote_instances_env.write_text(
            json.dumps([{"name": "Node 1", "url": "http://192.168.1.10:5000"}])
        )
        response = client.get("/api/instances/node-1/admin/anything")
        assert response.status_code == 400

    def test_proxies_to_remote(self, remote_instances_env, requests_mock, client):
        remote_instances_env.write_text(
            json.dumps([{"name": "Node 1", "url": "http://192.168.1.10:5000"}])
        )
        mock = requests_mock.get(
            "http://192.168.1.10:5000/api/status", json={"remote": True}
        )
        response = client.get("/api/instances/node-1/status")
        assert response.status_code == 200
        assert response.get_json() == {"remote": True}
        assert mock.call_count == 1

    def test_unreachable_remote_502(self, remote_instances_env, requests_mock, client):
        remote_instances_env.write_text(
            json.dumps([{"name": "Node 1", "url": "http://192.168.1.10:5000"}])
        )
        requests_mock.get(
            "http://192.168.1.10:5000/api/status",
            exc=requests.exceptions.ConnectTimeout("host down"),
        )
        response = client.get("/api/instances/node-1/status")
        assert response.status_code == 502

    def test_post_forwards_json_body(self, remote_instances_env, requests_mock, client):
        remote_instances_env.write_text(
            json.dumps([{"name": "Node 1", "url": "http://192.168.1.10:5000"}])
        )
        mock = requests_mock.post(
            "http://192.168.1.10:5000/api/prune/containers", json={"status": "started"}
        )
        response = client.post(
            "/api/instances/node-1/prune/containers", json={"force": True}
        )
        assert response.status_code == 200
        assert mock.last_request.json() == {"force": True}


def test_derive_stack_name():
    assert derive_stack_name("/opt/docker/media/compose.yaml") == "media"
    assert derive_stack_name("compose.yaml") == "default"
