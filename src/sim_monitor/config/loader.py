"""Load and save the app config and profile files.

Profiles live one-per-file in a profiles.d directory and are loaded in sorted
filename order. A broken profile file is reported, skipped, and must never take
the daemon down.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

import yaml
from pydantic import ValidationError

from sim_monitor.config.schema import AppConfig, Profile

log = logging.getLogger(__name__)


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class ProfileLoadError:
    path: Path
    error: str


def load_app_config(path: Path | None) -> AppConfig:
    """Load config.yaml; with no path, return defaults (useful for --simulate)."""
    if path is None:
        return AppConfig()
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except FileNotFoundError:
        raise ConfigError(f"config file not found: {path}") from None
    except yaml.YAMLError as e:
        raise ConfigError(f"invalid YAML in {path}: {e}") from e
    try:
        return AppConfig.model_validate(raw)
    except ValidationError as e:
        raise ConfigError(f"invalid config {path}:\n{e}") from e


def load_profiles(profiles_dir: Path) -> tuple[list[Profile], list[ProfileLoadError]]:
    """Load every *.yaml profile (sorted by filename), skipping broken files."""
    profiles: list[Profile] = []
    errors: list[ProfileLoadError] = []
    if not profiles_dir.is_dir():
        errors.append(ProfileLoadError(profiles_dir, "profiles directory does not exist"))
        return profiles, errors

    seen_names: dict[str, Path] = {}
    for path in sorted(profiles_dir.glob("*.yaml")):
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raise ConfigError("file does not contain a YAML mapping")
            profile = Profile.model_validate(raw)
        except (yaml.YAMLError, ValidationError, ConfigError, OSError) as e:
            errors.append(ProfileLoadError(path, str(e)))
            log.warning("skipping invalid profile %s: %s", path, e)
            continue
        if profile.name in seen_names:
            errors.append(
                ProfileLoadError(
                    path,
                    f"duplicate profile name {profile.name!r}"
                    f" (already defined in {seen_names[profile.name].name})",
                )
            )
            continue
        seen_names[profile.name] = path
        profiles.append(profile)
    return profiles, errors


def profile_path(profiles_dir: Path, name: str) -> Path:
    return profiles_dir / f"{name}.yaml"


def save_profile(profiles_dir: Path, profile: Profile) -> Path:
    """Write a profile as YAML (used by the web UI). Returns the file path."""
    profiles_dir.mkdir(parents=True, exist_ok=True)
    path = find_profile_file(profiles_dir, profile.name) or profile_path(
        profiles_dir, profile.name
    )
    data = profile.model_dump(mode="json")
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return path


def delete_profile(profiles_dir: Path, name: str) -> bool:
    path = find_profile_file(profiles_dir, name)
    if path is None:
        return False
    path.unlink()
    return True


def find_profile_file(profiles_dir: Path, name: str) -> Path | None:
    """Locate the file holding a profile by name (filename may not equal name)."""
    if not profiles_dir.is_dir():
        return None
    for path in sorted(profiles_dir.glob("*.yaml")):
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (yaml.YAMLError, OSError):
            continue
        if isinstance(raw, dict) and raw.get("name") == name:
            return path
    return None
