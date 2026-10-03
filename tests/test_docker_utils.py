"""Tests for the pure-logic parts of docker_utils.py."""
from docker_utils import (
    find_compose_files,
    get_compose_file_content,
    get_services_for_image,
    parse_image_ref,
    parse_images_from_compose,
    read_dotenv,
    resolve_env_vars,
    validate_compose_content,
    write_compose_file,
)


class TestParseImageRef:
    def test_official_image_gets_library_namespace(self):
        assert parse_image_ref("redis") == ("registry-1.docker.io", "library/redis", "latest")

    def test_official_image_with_tag(self):
        assert parse_image_ref("redis:7") == ("registry-1.docker.io", "library/redis", "7")

    def test_dockerhub_user_image(self):
        assert parse_image_ref("jellyfin/jellyfin:latest") == (
            "registry-1.docker.io", "jellyfin/jellyfin", "latest",
        )

    def test_ghcr_image(self):
        assert parse_image_ref("ghcr.io/owner/app:v1") == ("ghcr.io", "owner/app", "v1")

    def test_custom_registry_with_port(self):
        assert parse_image_ref("registry.example.com:5000/team/app:1.2") == (
            "registry.example.com:5000", "team/app", "1.2",
        )

    def test_port_only_registry_prefix_is_not_a_tag(self):
        registry, repo, tag = parse_image_ref("registry.example.com:5000/team/app")
        assert tag == "latest"
        assert registry == "registry.example.com:5000"


class TestReadDotenv:
    def test_parses_keys_values_and_quotes(self, tmp_path):
        env_file = tmp_path / ".env"
        env_file.write_text(
            "# comment\n"
            "PLAIN=value\n"
            'QUOTED="quoted value"\n'
            "SINGLE='single value'\n"
            "MALFORMED_LINE\n"
        )
        env = read_dotenv(env_file)
        assert env == {
            "PLAIN": "value",
            "QUOTED": "quoted value",
            "SINGLE": "single value",
        }

    def test_missing_file_returns_empty(self, tmp_path):
        assert read_dotenv(tmp_path / ".env") == {}


class TestResolveEnvVars:
    def test_resolves_known_variable(self):
        assert resolve_env_vars("nginx:${TAG}", {"TAG": "1.27"}) == "nginx:1.27"

    def test_uses_default_when_missing(self):
        assert resolve_env_vars("nginx:${TAG:-latest}", {}) == "nginx:latest"

    def test_known_variable_wins_over_default(self):
        assert resolve_env_vars("nginx:${TAG:-latest}", {"TAG": "1.27"}) == "nginx:1.27"

    def test_unresolved_without_default_left_intact(self):
        assert resolve_env_vars("nginx:${TAG}", {}) == "nginx:${TAG}"


class TestParseImagesFromCompose:
    def test_media_fixture_images(self):
        images = parse_images_from_compose("media/compose.yaml")
        assert sorted(images) == ["jellyfin/jellyfin:latest", "redis:7-alpine"]

    def test_web_fixture_resolves_env_and_strips_digest(self):
        # nginx:${TAG} resolves via web/.env, the sha256-pinned ref is stripped,
        # and the build-only service (no image:) is ignored.
        assert sorted(parse_images_from_compose("web/docker-compose.yml")) == [
            "example.invalid/site",
            "nginx:1.27",
        ]

    def test_missing_file_returns_empty(self):
        assert parse_images_from_compose("nope/missing.yaml") == []

    def test_skips_unresolvable_variable(self, tmp_path, monkeypatch):
        compose = tmp_path / "compose.yaml"
        compose.write_text("services:\n  app:\n    image: example/${MISSING}\n")
        # no .env next to it, so ${MISSING} stays unresolved and is skipped
        assert parse_images_from_compose(str(compose)) == []


class TestFindComposeFiles:
    def test_finds_all_fixture_compose_files(self):
        files = find_compose_files()
        by_path = {f["path"].replace("\\", "/"): f["project"] for f in files}
        assert by_path == {
            "media/compose.yaml": "media",
            "web/docker-compose.yml": "web",
        }


class TestGetServicesForImage:
    def test_maps_image_to_service_name(self):
        assert get_services_for_image("web/docker-compose.yml", "nginx:1.27") == ["nginx"]

    def test_no_match_returns_empty(self):
        assert get_services_for_image("web/docker-compose.yml", "other:1") == []


class TestValidateComposeContent:
    def test_valid_content(self):
        valid, message, errors = validate_compose_content(
            {"services": {"app": {"image": "app:1"}}}
        )
        assert valid is True
        assert errors == []

    def test_none_content(self):
        valid, _, errors = validate_compose_content(None)
        assert valid is False
        assert errors

    def test_non_dict_root(self):
        valid, _, errors = validate_compose_content(["not", "a", "dict"])
        assert valid is False
        assert errors

    def test_unknown_top_level_key(self):
        valid, _, errors = validate_compose_content({"bogus_key": {}, "services": {}})
        assert valid is False
        assert any("bogus_key" in e for e in errors)

    def test_services_not_a_dict(self):
        valid, _, errors = validate_compose_content({"services": ["a"]})
        assert valid is False
        assert any("services must be a dictionary" in e for e in errors)

    def test_service_config_not_a_dict(self):
        valid, _, errors = validate_compose_content({"services": {"app": "not-a-dict"}})
        assert valid is False
        assert any("Service 'app'" in e for e in errors)


class TestComposeFileContent:
    def test_read_relative_to_compose_root(self):
        content = get_compose_file_content("media/compose.yaml")
        assert content["services"]["jellyfin"]["image"] == "jellyfin/jellyfin:latest"

    def test_missing_file_returns_none(self):
        assert get_compose_file_content("missing/compose.yaml") is None

    def test_invalid_yaml_returns_none(self, tmp_path):
        bad = tmp_path / "compose.yaml"
        bad.write_text("services: [unclosed\n")
        assert get_compose_file_content(str(bad)) is None

    def test_write_compose_file_roundtrip_and_backup(self, tmp_path):
        target = tmp_path / "compose.yaml"
        target.write_text("services: {}\n")

        ok, message = write_compose_file(
            str(target), {"services": {"app": {"image": "app:1"}}}, backup=True
        )
        assert ok is True, message
        assert (tmp_path / "compose.yaml.bak").exists()
        content = get_compose_file_content(str(target))
        assert content["services"]["app"]["image"] == "app:1"

    def test_write_compose_file_without_backup(self, tmp_path):
        target = tmp_path / "compose.yaml"
        target.write_text("services: {}\n")

        ok, _ = write_compose_file(str(target), {"services": {}}, backup=False)
        assert ok is True
        assert not (tmp_path / "compose.yaml.bak").exists()


def test_derive_stack_name_uses_parent_directory():
    from services import derive_stack_name

    assert derive_stack_name("/opt/docker/media/compose.yaml") == "media"
    # A bare filename has no parent directory, so it falls back to "default"
    assert derive_stack_name("compose.yaml") == "default"
