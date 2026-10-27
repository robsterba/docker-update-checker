"""Tests for registry token/digest flow with all HTTP traffic mocked."""
import base64
import json
import time

import pytest

import docker_utils
from config import REGISTRY_TOKEN_CACHE
from docker_utils import check_image, get_registry_token, get_remote_digest, load_registry_credentials

HUB_TOKEN_URL = "https://auth.docker.io/token"
HUB_MANIFEST_URL = "https://registry-1.docker.io/v2/library/redis/manifests/7"
GHCR_TOKEN_URL = "https://ghcr.io/token"
GHCR_MANIFEST_URL = "https://ghcr.io/v2/owner/app/manifests/v1"


class TestGetRegistryToken:
    def test_docker_hub_token(self, requests_mock):
        requests_mock.get(HUB_TOKEN_URL, json={"token": "hub-token"})
        assert get_registry_token("registry-1.docker.io", "library/redis") == "hub-token"

    def test_docker_hub_token_401_returns_none(self, requests_mock):
        requests_mock.get(HUB_TOKEN_URL, status_code=401)
        assert get_registry_token("registry-1.docker.io", "library/redis") is None

    def test_token_is_cached(self, requests_mock):
        mock = requests_mock.get(HUB_TOKEN_URL, json={"token": "hub-token"})
        get_registry_token("registry-1.docker.io", "library/redis")
        get_registry_token("registry-1.docker.io", "library/redis")
        assert mock.call_count == 1

    def test_expired_cache_entry_is_refetched(self, requests_mock):
        REGISTRY_TOKEN_CACHE["registry-1.docker.io:library/redis"] = {
            "token": "stale",
            "expires_at": time.time() - 10,
        }
        mock = requests_mock.get(HUB_TOKEN_URL, json={"token": "fresh"})
        assert get_registry_token("registry-1.docker.io", "library/redis") == "fresh"
        assert mock.call_count == 1

    def test_ghcr_token(self, requests_mock):
        requests_mock.get(GHCR_TOKEN_URL, json={"token": "ghcr-token"})
        assert get_registry_token("ghcr.io", "owner/app") == "ghcr-token"

    def test_unsupported_registry_returns_none(self):
        assert get_registry_token("quay.io", "team/app") is None


def _write_docker_config(config_dir, auths: dict[str, tuple[str, str]]):
    """Write a Docker config.json with base64 auths into config_dir."""
    payload = {
        key: {"auth": base64.b64encode(f"{user}:{password}".encode()).decode()}
        for key, (user, password) in auths.items()
    }
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "config.json").write_text(json.dumps({"auths": payload}))


class TestRegistryCredentials:
    def test_hub_credentials_used_for_token_request(self, tmp_path, monkeypatch, requests_mock):
        _write_docker_config(tmp_path, {"https://index.docker.io/v1/": ("me", "secret")})
        monkeypatch.setenv("DOCKER_CONFIG", str(tmp_path))
        mock = requests_mock.get(HUB_TOKEN_URL, json={"token": "authed-token"})

        assert get_registry_token("registry-1.docker.io", "library/redis") == "authed-token"
        expected = "Basic " + base64.b64encode(b"me:secret").decode()
        assert mock.last_request.headers["Authorization"] == expected

    def test_ghcr_credentials_used_for_token_request(self, tmp_path, monkeypatch, requests_mock):
        _write_docker_config(tmp_path, {"ghcr.io": ("user", "ghp-token")})
        monkeypatch.setenv("DOCKER_CONFIG", str(tmp_path))
        mock = requests_mock.get(GHCR_TOKEN_URL, json={"token": "ghcr-token"})

        assert get_registry_token("ghcr.io", "owner/app") == "ghcr-token"
        expected = "Basic " + base64.b64encode(b"user:ghp-token").decode()
        assert mock.last_request.headers["Authorization"] == expected

    def test_missing_config_is_anonymous(self, tmp_path, monkeypatch, requests_mock):
        monkeypatch.setenv("DOCKER_CONFIG", str(tmp_path / "nonexistent"))
        mock = requests_mock.get(HUB_TOKEN_URL, json={"token": "anon-token"})

        assert get_registry_token("registry-1.docker.io", "library/redis") == "anon-token"
        assert "Authorization" not in mock.last_request.headers

    def test_hub_credentials_rejected_returns_none(self, tmp_path, monkeypatch, requests_mock):
        _write_docker_config(tmp_path, {"https://index.docker.io/v1/": ("me", "bad")})
        monkeypatch.setenv("DOCKER_CONFIG", str(tmp_path))
        requests_mock.get(HUB_TOKEN_URL, status_code=401, json={"error": "incorrect authentication credentials"})

        assert get_registry_token("registry-1.docker.io", "library/redis") is None

    def test_rejected_credentials_fall_back_to_anonymous(self, tmp_path, monkeypatch, requests_mock):
        _write_docker_config(tmp_path, {"https://index.docker.io/v1/": ("me", "bad")})
        monkeypatch.setenv("DOCKER_CONFIG", str(tmp_path))
        mock = requests_mock.get(HUB_TOKEN_URL, [
            {"status_code": 401, "json": {"error": "incorrect authentication credentials"}},
            {"json": {"token": "anon-fallback"}},
        ])

        assert get_registry_token("registry-1.docker.io", "library/redis") == "anon-fallback"
        assert mock.call_count == 2
        assert "Authorization" not in mock.request_history[1].headers

    def test_quota_exhausted_401_returns_none(self, requests_mock):
        requests_mock.get(
            HUB_TOKEN_URL,
            status_code=401,
            json={"error": "toomanyrequests: You have reached your pull rate limit."},
        )
        assert get_registry_token("registry-1.docker.io", "library/redis") is None

    def test_malformed_config_is_ignored(self, tmp_path, monkeypatch):
        config_dir = tmp_path / "docker"
        config_dir.mkdir()
        (config_dir / "config.json").write_text("{not json")
        monkeypatch.setenv("DOCKER_CONFIG", str(config_dir))

        assert load_registry_credentials() == {}

    def test_config_directory_is_ignored_with_single_warning(self, tmp_path, monkeypatch, caplog):
        # Docker creates a directory at the mount point when the host file is
        # missing at container creation; read_text then raises IsADirectoryError
        monkeypatch.setattr(docker_utils, "_docker_config_warning_shown", False)
        (tmp_path / "config.json").mkdir()
        monkeypatch.setenv("DOCKER_CONFIG", str(tmp_path))

        import logging
        with caplog.at_level(logging.WARNING, logger="docker_utils"):
            assert load_registry_credentials() == {}
            assert load_registry_credentials() == {}

        directory_warnings = [r for r in caplog.records if "is a directory" in r.message]
        assert len(directory_warnings) == 1

    def test_token_429_returns_none(self, requests_mock):
        requests_mock.get(HUB_TOKEN_URL, status_code=429)
        assert get_registry_token("registry-1.docker.io", "library/redis") is None

    def test_failed_token_skips_hub_manifest_request(self, requests_mock):
        head = requests_mock.head(HUB_MANIFEST_URL, status_code=401)
        requests_mock.get(HUB_TOKEN_URL, status_code=401)

        assert get_remote_digest("redis:7") is None
        assert head.call_count == 0


class TestGetRemoteDigest:
    def test_docker_hub_digest_with_token(self, requests_mock):
        requests_mock.get(HUB_TOKEN_URL, json={"token": "hub-token"})
        head = requests_mock.head(
            HUB_MANIFEST_URL,
            headers={"Docker-Content-Digest": "sha256:remote"},
        )
        assert get_remote_digest("redis:7") == "sha256:remote"
        assert head.last_request.headers["Authorization"] == "Bearer hub-token"

    def test_digest_falls_back_to_etag(self, requests_mock):
        requests_mock.get(HUB_TOKEN_URL, json={"token": "t"})
        requests_mock.head(
            HUB_MANIFEST_URL,
            headers={"Etag": '"sha256:etag-digest"'},
        )
        assert get_remote_digest("redis:7") == "sha256:etag-digest"

    def test_manifest_401_returns_none(self, requests_mock):
        requests_mock.get(HUB_TOKEN_URL, json={"token": "t"})
        requests_mock.head(HUB_MANIFEST_URL, status_code=401)
        assert get_remote_digest("redis:7") is None

    def test_connection_error_returns_none(self, requests_mock):
        requests_mock.get(
            HUB_TOKEN_URL,
            exc=ConnectionError("registry unreachable"),
        )
        assert get_remote_digest("redis:7") is None

    def test_ghcr_digest(self, requests_mock):
        requests_mock.get(GHCR_TOKEN_URL, json={"token": "ghcr-token"})
        requests_mock.head(
            GHCR_MANIFEST_URL,
            headers={"Docker-Content-Digest": "sha256:ghcr"},
        )
        assert get_remote_digest("ghcr.io/owner/app:v1") == "sha256:ghcr"


class TestCheckImage:
    def test_up_to_date_when_digests_match(self, requests_mock, monkeypatch):
        requests_mock.get(HUB_TOKEN_URL, json={"token": "t"})
        requests_mock.head(
            "https://registry-1.docker.io/v2/library/redis/manifests/7",
            headers={"Docker-Content-Digest": "sha256:same"},
        )
        monkeypatch.setattr(docker_utils, "get_local_digest", lambda ref: "sha256:same")

        result = check_image("redis:7")
        assert result["status"] == "up_to_date"
        assert result["local_digest"] == result["remote_digest"] == "sha256:same"

    def test_update_available_when_digests_differ(self, requests_mock, monkeypatch):
        requests_mock.get(HUB_TOKEN_URL, json={"token": "t"})
        requests_mock.head(
            "https://registry-1.docker.io/v2/library/redis/manifests/7",
            headers={"Docker-Content-Digest": "sha256:new"},
        )
        monkeypatch.setattr(docker_utils, "get_local_digest", lambda ref: "sha256:old")

        assert check_image("redis:7")["status"] == "update_available"

    def test_registry_error_when_remote_unreachable(self, monkeypatch):
        monkeypatch.setattr(docker_utils, "get_local_digest", lambda ref: "sha256:local")
        # No requests_mock: real HTTP is blocked from the test sandbox, so
        # get_remote_digest returns None without network access. To make this
        # deterministic, mock it directly.
        monkeypatch.setattr(docker_utils, "get_remote_digest", lambda ref: None)
        assert check_image("redis:7")["status"] == "registry_error"

    def test_not_pulled_when_local_missing(self, requests_mock, monkeypatch):
        requests_mock.get(HUB_TOKEN_URL, json={"token": "t"})
        requests_mock.head(
            "https://registry-1.docker.io/v2/library/redis/manifests/7",
            headers={"Docker-Content-Digest": "sha256:remote"},
        )
        monkeypatch.setattr(docker_utils, "get_local_digest", lambda ref: None)
        assert check_image("redis:7")["status"] == "not_pulled"

    def test_unknown_when_both_digests_missing(self, monkeypatch):
        monkeypatch.setattr(docker_utils, "get_local_digest", lambda ref: None)
        monkeypatch.setattr(docker_utils, "get_remote_digest", lambda ref: None)
        assert check_image("redis:7")["status"] == "unknown"


@pytest.mark.parametrize("image_ref", ["redis:7", "nginx:latest"])
def test_check_image_result_shape(image_ref, monkeypatch):
    monkeypatch.setattr(docker_utils, "get_local_digest", lambda ref: None)
    monkeypatch.setattr(docker_utils, "get_remote_digest", lambda ref: None)
    result = check_image(image_ref)
    assert set(result) == {"image", "status", "local_digest", "remote_digest", "checked_at"}
    assert result["image"] == image_ref
