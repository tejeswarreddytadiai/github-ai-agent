"""PyGithub-based authenticated client factory."""
from __future__ import annotations

from functools import lru_cache

from github import Auth, Github
from github.Repository import Repository

from config import settings


@lru_cache(maxsize=1)
def get_github() -> Github:
    """Return an authenticated PyGithub client (cached)."""
    auth = Auth.Token(settings.github_token)
    return Github(auth=auth)


@lru_cache(maxsize=1)
def get_repo() -> Repository:
    """Return the target repository object."""
    return get_github().get_repo(settings.github_repo)
