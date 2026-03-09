"""
Camera test profiles for canary.

Each profile lives in a subdirectory (e.g. profiles/realsense/) or as a
standalone .py file. Profiles register themselves via `register()` at import.

Directory structure per profile:
    profiles/<name>/
        __init__.py      ← imports collect.py
        setup.md         ← natural language setup playbook for the agent
        collect.py       ← data collection class (subclass of BaseProfile)

The base profile lives at profiles/base.py (standalone).
"""

PROFILES = {}


def register(cls):
    """Register a profile class by its `name` attribute."""
    PROFILES[cls.name] = cls
    return cls
