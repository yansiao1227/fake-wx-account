"""Canonical filesystem paths for project-owned skills."""

import os


def get_project_root() -> str:
    """Return the repository root containing the agent package."""
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def get_project_skills_dir() -> str:
    """Return the single directory used to load and manage skills."""
    return os.path.join(get_project_root(), "skills")
