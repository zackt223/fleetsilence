"""Loads settings.toml. All thresholds live in that file, not in the code."""
import os
import tomllib

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_PATH = os.path.join(ROOT, "settings.toml")


class Settings(dict):
    """A dict whose sections can also be read as attributes: cfg.tracker["drive_quiet_min"]."""

    def __getattr__(self, name):
        try:
            return self[name]
        except KeyError as exc:
            raise AttributeError(name) from exc


def load(path=None):
    path = path or os.environ.get("QF_SETTINGS") or DEFAULT_PATH
    with open(path, "rb") as f:
        return Settings(tomllib.load(f))
