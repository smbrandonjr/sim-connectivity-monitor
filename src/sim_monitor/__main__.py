"""CLI entry point: python -m sim_monitor [--config FILE] [--simulate] [--log-level LVL]"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from sim_monitor import __version__
from sim_monitor.config.loader import ConfigError, load_app_config, load_profiles


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="sim-monitor")
    parser.add_argument("--config", type=Path, default=None, help="path to config.yaml")
    parser.add_argument(
        "--simulate", action="store_true", help="run against a fake modem (no hardware)"
    )
    parser.add_argument("--log-level", default=None, help="override config log level")
    parser.add_argument("--version", action="version", version=f"sim-monitor {__version__}")
    args = parser.parse_args(argv)

    try:
        config = load_app_config(args.config)
    except ConfigError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    if args.simulate:
        config.simulate = True

    logging.basicConfig(
        level=args.log_level or config.log_level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    log = logging.getLogger("sim_monitor")
    log.info("sim-monitor %s starting (simulate=%s)", __version__, config.simulate)

    profiles, errors = load_profiles(config.profiles_dir)
    for err in errors:
        log.warning("profile problem: %s: %s", err.path, err.error)
    log.info("loaded %d profile(s): %s", len(profiles), ", ".join(p.name for p in profiles))

    if not config.simulate:
        log.error("hardware mode is not implemented yet -- run with --simulate")
        return 1

    from sim_monitor import app

    return app.run(config, profiles)


if __name__ == "__main__":
    sys.exit(main())
