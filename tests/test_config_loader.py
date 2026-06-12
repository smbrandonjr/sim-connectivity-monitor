import pytest
import yaml
from pydantic import ValidationError

from sim_monitor.config.loader import (
    ConfigError,
    delete_profile,
    load_app_config,
    load_profiles,
    save_profile,
)
from sim_monitor.config.schema import AppConfig, Profile

VALID_PROFILE = {
    "name": "test-profile",
    "match": {"iccid_patterns": ["8944500*"], "priority": 10},
    "pdp_contexts": [{"cid": 1, "apn": "hologram", "bearer": True}],
}


def write_yaml(path, data):
    path.write_text(yaml.safe_dump(data), encoding="utf-8")


class TestAppConfig:
    def test_defaults_when_no_path(self):
        cfg = load_app_config(None)
        assert isinstance(cfg, AppConfig)
        assert cfg.web.port == 8080
        assert not cfg.simulate

    def test_load_valid(self, tmp_path):
        p = tmp_path / "config.yaml"
        write_yaml(p, {"web": {"port": 9000}, "simulate": True})
        cfg = load_app_config(p)
        assert cfg.web.port == 9000
        assert cfg.simulate

    def test_missing_file(self, tmp_path):
        with pytest.raises(ConfigError, match="not found"):
            load_app_config(tmp_path / "nope.yaml")

    def test_unknown_key_rejected(self, tmp_path):
        p = tmp_path / "config.yaml"
        write_yaml(p, {"webb": {}})
        with pytest.raises(ConfigError, match="invalid config"):
            load_app_config(p)

    def test_example_config_in_repo_is_valid(self):
        from pathlib import Path

        example = Path(__file__).parent.parent / "config" / "config.example.yaml"
        cfg = load_app_config(example)
        assert cfg.web.port == 8080


class TestProfileSchema:
    def test_single_context_auto_bearer(self):
        p = Profile.model_validate(
            {"name": "x", "pdp_contexts": [{"cid": 1, "apn": "hologram"}]}
        )
        assert p.pdp_contexts[0].bearer
        assert p.bearer_context.apn == "hologram"

    def test_three_contexts_allowed(self):
        p = Profile.model_validate(
            {
                "name": "x",
                "pdp_contexts": [
                    {"cid": 1, "apn": "a", "bearer": True},
                    {"cid": 2, "apn": "b"},
                    {"cid": 3, "apn": "c"},
                ],
            }
        )
        assert len(p.pdp_contexts) == 3
        assert p.bearer_context.cid == 1

    def test_four_contexts_rejected(self):
        with pytest.raises(ValidationError):
            Profile.model_validate(
                {
                    "name": "x",
                    "pdp_contexts": [
                        {"cid": i, "apn": "a", "bearer": i == 1} for i in range(1, 5)
                    ],
                }
            )

    def test_zero_contexts_rejected(self):
        with pytest.raises(ValidationError):
            Profile.model_validate({"name": "x", "pdp_contexts": []})

    def test_duplicate_cids_rejected(self):
        with pytest.raises(ValidationError, match="duplicate"):
            Profile.model_validate(
                {
                    "name": "x",
                    "pdp_contexts": [
                        {"cid": 1, "apn": "a", "bearer": True},
                        {"cid": 1, "apn": "b"},
                    ],
                }
            )

    def test_multiple_contexts_need_explicit_bearer(self):
        with pytest.raises(ValidationError, match="bearer"):
            Profile.model_validate(
                {
                    "name": "x",
                    "pdp_contexts": [{"cid": 1, "apn": "a"}, {"cid": 2, "apn": "b"}],
                }
            )

    def test_two_bearers_rejected(self):
        with pytest.raises(ValidationError, match="bearer"):
            Profile.model_validate(
                {
                    "name": "x",
                    "pdp_contexts": [
                        {"cid": 1, "apn": "a", "bearer": True},
                        {"cid": 2, "apn": "b", "bearer": True},
                    ],
                }
            )

    def test_auth_requires_username(self):
        with pytest.raises(ValidationError, match="username"):
            Profile.model_validate(
                {
                    "name": "x",
                    "pdp_contexts": [{"cid": 1, "apn": "a", "auth": "pap"}],
                }
            )

    def test_bad_iccid_pattern_rejected(self):
        with pytest.raises(ValidationError, match="pattern"):
            Profile.model_validate({**VALID_PROFILE, "match": {"iccid_patterns": ["89ab*"]}})

    def test_monitor_enabled_requires_request(self):
        with pytest.raises(ValidationError, match="request"):
            Profile.model_validate({**VALID_PROFILE, "monitor": {"enabled": True}})


class TestLoadProfiles:
    def test_missing_dir(self, tmp_path):
        profiles, errors = load_profiles(tmp_path / "nope")
        assert profiles == []
        assert len(errors) == 1

    def test_loads_sorted_and_skips_broken(self, tmp_path):
        write_yaml(tmp_path / "10-b.yaml", {**VALID_PROFILE, "name": "bbb"})
        write_yaml(tmp_path / "00-a.yaml", {**VALID_PROFILE, "name": "aaa"})
        (tmp_path / "20-broken.yaml").write_text("pdp_contexts: {{{", encoding="utf-8")
        write_yaml(tmp_path / "30-invalid.yaml", {"name": "x"})  # missing pdp_contexts
        (tmp_path / "40-skipme.yaml.example").write_text("ignored", encoding="utf-8")

        profiles, errors = load_profiles(tmp_path)
        assert [p.name for p in profiles] == ["aaa", "bbb"]
        assert len(errors) == 2

    def test_duplicate_names_keep_first(self, tmp_path):
        write_yaml(tmp_path / "00-a.yaml", VALID_PROFILE)
        write_yaml(tmp_path / "10-b.yaml", VALID_PROFILE)
        profiles, errors = load_profiles(tmp_path)
        assert len(profiles) == 1
        assert len(errors) == 1
        assert "duplicate" in errors[0].error

    def test_repo_default_profile_is_valid(self):
        from pathlib import Path

        repo_profiles = Path(__file__).parent.parent / "config" / "profiles.d"
        profiles, errors = load_profiles(repo_profiles)
        assert errors == []
        assert any(p.name == "hologram-default" for p in profiles)
        default = next(p for p in profiles if p.name == "hologram-default")
        assert default.bearer_context.apn == "hologram"
        assert default.match.iccid_patterns == ["*"]


class TestSaveDelete:
    def test_save_load_roundtrip(self, tmp_path):
        profile = Profile.model_validate(VALID_PROFILE)
        path = save_profile(tmp_path, profile)
        assert path.exists()
        profiles, errors = load_profiles(tmp_path)
        assert errors == []
        assert profiles[0] == profile

    def test_save_updates_existing_file_with_different_name(self, tmp_path):
        write_yaml(tmp_path / "99-custom.yaml", VALID_PROFILE)
        profile = Profile.model_validate({**VALID_PROFILE, "description": "updated"})
        path = save_profile(tmp_path, profile)
        assert path.name == "99-custom.yaml"
        profiles, _ = load_profiles(tmp_path)
        assert len(profiles) == 1
        assert profiles[0].description == "updated"

    def test_delete(self, tmp_path):
        profile = Profile.model_validate(VALID_PROFILE)
        save_profile(tmp_path, profile)
        assert delete_profile(tmp_path, "test-profile")
        assert not delete_profile(tmp_path, "test-profile")
        profiles, _ = load_profiles(tmp_path)
        assert profiles == []
