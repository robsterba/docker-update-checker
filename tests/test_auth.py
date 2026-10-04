"""Tests for auth.py, security headers, and secret masking in API responses."""

import pytest

import auth
import config
import services
from services import save_notification_settings


@pytest.fixture(autouse=True)
def reset_auth_state(monkeypatch):
    """Every test starts with auth off and a clean failure record."""
    monkeypatch.setattr(config, "API_TOKEN", "")
    auth.clear_failures()
    yield
    auth.clear_failures()


def auth_header(token):
    return {"Authorization": f"Bearer {token}"}


class TestTokenAuth:
    def test_open_when_token_unset(self, client):
        assert client.get("/api/status").status_code == 200

    def test_api_requires_token_when_configured(self, client, monkeypatch):
        monkeypatch.setattr(config, "API_TOKEN", "secret-token")
        assert client.get("/api/status").status_code == 401
        ok = client.get("/api/status", headers=auth_header("secret-token"))
        assert ok.status_code == 200

    def test_dashboard_shell_stays_open(self, client, monkeypatch):
        monkeypatch.setattr(config, "API_TOKEN", "secret-token")
        assert client.get("/").status_code == 200
        assert client.get("/api/version").status_code == 200

    def test_wrong_token_rejected(self, client, monkeypatch):
        monkeypatch.setattr(config, "API_TOKEN", "secret-token")
        assert client.get("/api/status", headers=auth_header("wrong")).status_code == 401

    def test_non_api_paths_unaffected(self, client, monkeypatch):
        monkeypatch.setattr(config, "API_TOKEN", "secret-token")
        assert client.get("/health").status_code in (200, 500)  # no 401/429

    def test_only_version_is_exempt(self, client, monkeypatch):
        monkeypatch.setattr(config, "API_TOKEN", "secret-token")
        for path in ("/api/version/", "/api/versionx", "/api/instances/local/status"):
            assert client.get(path).status_code == 401, path

    def test_proxy_paths_require_token(self, client, monkeypatch):
        monkeypatch.setattr(config, "API_TOKEN", "secret-token")
        assert client.get("/api/instances/local/status").status_code == 401
        ok = client.get("/api/instances/local/status", headers=auth_header("secret-token"))
        assert ok.status_code == 200

    def test_no_external_script_origins_in_dashboard(self):
        import re
        from pathlib import Path

        html = Path("static/index.html").read_text(encoding="utf-8")
        srcs = re.findall(r'<script[^>]+src="([^"]+)"', html)
        assert srcs, "expected at least one vendored script"
        assert all(src.startswith("/static/") for src in srcs), srcs


class TestRateLimiting:
    def test_lockout_after_repeated_failures(self, client, monkeypatch):
        monkeypatch.setattr(config, "API_TOKEN", "secret-token")
        for _ in range(auth.MAX_FAILURES):
            client.get("/api/status", headers=auth_header("wrong"))
        # blocked even with the correct token while locked out
        locked = client.get("/api/status", headers=auth_header("secret-token"))
        assert locked.status_code == 429

    def test_failures_expire(self, client, monkeypatch):
        monkeypatch.setattr(config, "API_TOKEN", "secret-token")
        monkeypatch.setattr(auth, "FAILURE_WINDOW_SECONDS", 0)
        for _ in range(auth.MAX_FAILURES):
            client.get("/api/status", headers=auth_header("wrong"))
        ok = client.get("/api/status", headers=auth_header("secret-token"))
        assert ok.status_code == 200

    def test_success_clears_failure_record(self, client, monkeypatch):
        monkeypatch.setattr(config, "API_TOKEN", "secret-token")
        client.get("/api/status", headers=auth_header("wrong"))
        assert client.get("/api/status", headers=auth_header("secret-token")).status_code == 200
        # a fresh client has a clean slate again
        assert auth._failures == {}


class TestSecurityHeaders:
    def test_headers_applied_to_api_responses(self, client):
        resp = client.get("/api/version")
        assert resp.headers["X-Content-Type-Options"] == "nosniff"
        assert resp.headers["X-Frame-Options"] == "DENY"
        assert resp.headers["Referrer-Policy"] == "no-referrer"
        assert "default-src 'self'" in resp.headers["Content-Security-Policy"]

    def test_headers_applied_to_dashboard(self, client):
        resp = client.get("/")
        assert resp.headers["X-Frame-Options"] == "DENY"


class TestNotificationSecretMasking:
    def test_secrets_are_masked_in_get(self, client):
        save_notification_settings({
            "enabled": True,
            "mqtt_password": "mqtt-secret",
            "email_password": "smtp-secret",
            "webhook_url": "https://discord.com/api/webhooks/123/abc",
        })
        data = client.get("/api/config/notification").get_json()
        assert data["mqtt_password"] == config.SECRET_MASK
        assert data["email_password"] == config.SECRET_MASK
        assert data["webhook_url"] == config.SECRET_MASK

    def test_empty_secrets_stay_empty(self, client):
        save_notification_settings({})  # reset any state from earlier tests
        data = client.get("/api/config/notification").get_json()
        assert data["mqtt_password"] == ""
        assert data["webhook_url"] == ""

    def test_masked_post_preserves_stored_secret(self, client):
        save_notification_settings({
            "enabled": True,
            "mqtt_password": "mqtt-secret",
            "email_password": "smtp-secret",
            "webhook_url": "https://discord.com/api/webhooks/123/abc",
        })
        resp = client.post("/api/config/notification", json={
            "enabled": True,
            "mqtt_password": config.SECRET_MASK,
            "email_password": config.SECRET_MASK,
            "webhook_url": config.SECRET_MASK,
        })
        assert resp.status_code == 200
        stored = services.load_notification_settings()
        assert stored["mqtt_password"] == "mqtt-secret"
        assert stored["email_password"] == "smtp-secret"
        assert stored["webhook_url"] == "https://discord.com/api/webhooks/123/abc"

    def test_partial_post_preserves_unposted_settings(self, client):
        save_notification_settings({"mqtt_password": "mqtt-secret", "backend": "mqtt"})
        client.post("/api/config/notification", json={"enabled": True})
        stored = services.load_notification_settings()
        assert stored["mqtt_password"] == "mqtt-secret"
        assert stored["backend"] == "mqtt"

    def test_new_password_overwrites(self, client):
        save_notification_settings({"mqtt_password": "old"})
        client.post("/api/config/notification", json={"mqtt_password": "new"})
        assert services.load_notification_settings()["mqtt_password"] == "new"


class TestRemoteInstanceTokens:
    def test_proxy_forwards_token_to_remote(self, client, monkeypatch):
        captured = {}

        def fake_request(method, url, **kwargs):
            captured["url"] = url
            captured["headers"] = kwargs.get("headers")
            class R:
                status_code = 200
                content = b'{"ok": true}'
                headers = {"Content-Type": "application/json"}
            return R()

        monkeypatch.setattr(services.requests, "request", fake_request)
        monkeypatch.setattr(services, "load_remote_instances", lambda: [{
            "id": "boxy", "name": "Boxy", "url": "http://boxy:5000",
            "description": "", "token": "remote-secret", "type": "remote",
        }])

        resp = client.get("/api/instances/boxy/status")
        assert resp.status_code == 200
        assert captured["headers"] == {"Authorization": "Bearer remote-secret"}

    def test_proxy_omits_header_without_token(self, client, monkeypatch):
        captured = {}

        def fake_request(method, url, **kwargs):
            captured["headers"] = kwargs.get("headers")
            class R:
                status_code = 200
                content = b'{"ok": true}'
                headers = {"Content-Type": "application/json"}
            return R()

        monkeypatch.setattr(services.requests, "request", fake_request)
        monkeypatch.setattr(services, "load_remote_instances", lambda: [{
            "id": "boxy", "name": "Boxy", "url": "http://boxy:5000",
            "description": "", "token": "", "type": "remote",
        }])

        assert client.get("/api/instances/boxy/status").status_code == 200
        assert captured["headers"] is None

    def test_tokens_masked_in_instance_lists(self, client, monkeypatch):
        import api

        def fake():
            return [{
                "id": "boxy", "name": "Boxy", "url": "http://boxy:5000",
                "description": "", "token": "remote-secret", "type": "remote",
            }]
        # api.py binds load_remote_instances directly, so patch both modules
        monkeypatch.setattr(services, "load_remote_instances", fake)
        monkeypatch.setattr(api, "load_remote_instances", fake)
        for path in ("/api/instances", "/api/instances/remote"):
            data = client.get(path).get_json()
            remote = [i for i in data if i.get("id") == "boxy"][0]
            assert remote["token"] == config.SECRET_MASK

    def test_post_preserves_masked_token(self, client, monkeypatch, tmp_path):
        import api

        path = tmp_path / "remote_instances.json"
        monkeypatch.setattr(api, "REMOTE_INSTANCES_FILE", str(path))
        monkeypatch.setattr(services, "REMOTE_INSTANCES_FILE", str(path))

        # create a host with a token
        client.post("/api/instances/remote", json=[{
            "id": "boxy", "name": "Boxy", "url": "http://boxy:5000",
            "token": "remote-secret",
        }])
        # save again with the masked token (as the UI does)
        client.post("/api/instances/remote", json=[{
            "id": "boxy", "name": "Boxy", "url": "http://boxy:5000",
            "token": config.SECRET_MASK,
        }])
        assert services.load_remote_instances()[0]["token"] == "remote-secret"

    def test_rename_preserves_token_via_original_id(self, client, monkeypatch, tmp_path):
        import api

        path = tmp_path / "remote_instances.json"
        monkeypatch.setattr(api, "REMOTE_INSTANCES_FILE", str(path))
        monkeypatch.setattr(services, "REMOTE_INSTANCES_FILE", str(path))

        client.post("/api/instances/remote", json=[{
            "id": "boxy", "name": "Boxy", "url": "http://boxy:5000",
            "token": "remote-secret",
        }])
        # rename (new derived id) but send the original id, as the UI does
        client.post("/api/instances/remote", json=[{
            "id": "boxy", "name": "Boxy Prime", "url": "http://boxy:5000",
            "token": config.SECRET_MASK,
        }])
        instances = services.load_remote_instances()
        assert instances[0]["name"] == "Boxy Prime"
        assert instances[0]["token"] == "remote-secret"

    def test_new_token_overwrites(self, client, monkeypatch, tmp_path):
        import api

        path = tmp_path / "remote_instances.json"
        monkeypatch.setattr(api, "REMOTE_INSTANCES_FILE", str(path))
        monkeypatch.setattr(services, "REMOTE_INSTANCES_FILE", str(path))

        client.post("/api/instances/remote", json=[{
            "id": "boxy", "name": "Boxy", "url": "http://boxy:5000", "token": "old",
        }])
        client.post("/api/instances/remote", json=[{
            "id": "boxy", "name": "Boxy", "url": "http://boxy:5000", "token": "new",
        }])
        assert services.load_remote_instances()[0]["token"] == "new"
