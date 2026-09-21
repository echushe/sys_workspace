"""Config loading. The only thing configured so far is the mount list."""

import os

import yaml

from . import mounts, paths


def load():
    """Return the config dict, or an empty one if no config file exists."""
    if not os.path.exists(paths.CONFIG_FILE):
        return {}
    with open(paths.CONFIG_FILE) as f:
        return yaml.safe_load(f) or {}


def save(cfg):
    paths.ensure_dirs()
    with open(paths.CONFIG_FILE, "w") as f:
        yaml.safe_dump(cfg, f, default_flow_style=False, sort_keys=False)


def mount_entries():
    """Mount entries from config, auto-detected if the config has none.

    Detection keeps the tool usable before `setup` has ever run, and keeps the
    machine-specific UUID out of the source tree.
    """
    cfg = load()
    entries = cfg.get("mounts")
    if entries:
        return entries
    return mounts.detect()
