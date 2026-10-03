"""Tests for registry token/digest flow with all HTTP traffic mocked."""
import time

import pytest

import docker_utils
from config import REGISTRY_TOKEN_CACHE
from docker_utils import check_image, get_registry_token, get_remote_digest

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
