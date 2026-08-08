"""GitHub operations: branch, read/update JSON schema, commit, PR, merge."""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from github import GithubException
from github.PullRequest import PullRequest

from config import settings
from email_tools import ColumnRequest
from github_client import get_repo


# ---------------------------------------------------------------------------
# Data containers
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class BranchInfo:
    name: str
    sha: str


@dataclass(frozen=True)
class SchemaUpdateResult:
    schema_file: str
    commit_sha: str
    added_field: dict


# ---------------------------------------------------------------------------
# Branching
# ---------------------------------------------------------------------------

def read_schema_fields(
    schema_path: str, ref: Optional[str] = None
) -> list[dict]:
    """Return the current list of BigQuery field dicts from ``schema_path``.

    Reads from ``ref`` (a branch/tag/SHA). If ``ref`` is ``None``, uses the
    configured base branch.

    Raises:
        ValueError: if the file is missing, not JSON, or not a JSON array.
    """
    repo = get_repo()
    contents = repo.get_contents(schema_path, ref=ref or settings.github_base_branch)
    if isinstance(contents, list):
        raise ValueError(f"Expected a file at {schema_path}, got a directory.")

    try:
        fields = json.loads(contents.decoded_content.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"Schema at {schema_path} is not valid JSON.") from exc

    if not isinstance(fields, list):
        raise ValueError(f"Schema at {schema_path} must be a JSON array.")
    return fields


def create_feature_branch(table_id: str, base: Optional[str] = None) -> BranchInfo:
    """Create a feature branch off the base branch.

    Branch name pattern: ``schema-update/<table_id>-<UTC-timestamp>``.

    Raises:
        GithubException: on any GitHub-side error other than "already exists".
    """
    repo = get_repo()
    base_branch = base or settings.github_base_branch
    base_ref = repo.get_branch(base_branch)

    ts = datetime.utcnow().strftime("%Y%m%d-%H%M%S")
    branch_name = f"schema-update/{table_id}-{ts}"

    repo.create_git_ref(ref=f"refs/heads/{branch_name}", sha=base_ref.commit.sha)
    return BranchInfo(name=branch_name, sha=base_ref.commit.sha)


# ---------------------------------------------------------------------------
# JSON schema edit + commit
# ---------------------------------------------------------------------------

def update_schema_file(
    schema_path: str,
    branch: str,
    column: ColumnRequest,
) -> SchemaUpdateResult:
    """Read the schema JSON on ``branch``, append the new column, and commit.

    If a field with the same (case-insensitive) name already exists, the
    function raises ``ValueError`` so the caller can decide what to do.
    """
    repo = get_repo()

    contents = repo.get_contents(schema_path, ref=branch)
    if isinstance(contents, list):
        raise ValueError(f"Expected a file at {schema_path}, got a directory.")

    current_text = contents.decoded_content.decode("utf-8")
    try:
        fields = json.loads(current_text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Existing schema at {schema_path} is not valid JSON.") from exc

    if not isinstance(fields, list):
        raise ValueError(f"Schema at {schema_path} must be a JSON array.")

    new_field = column.to_bq_field()
    existing_names = {str(f.get("name", "")).lower() for f in fields if isinstance(f, dict)}
    if new_field["name"].lower() in existing_names:
        raise ValueError(
            f"Column {new_field['name']!r} already exists in {schema_path}."
        )

    fields.append(new_field)
    updated_text = json.dumps(fields, indent=2) + "\n"

    commit_msg = (
        f"schema: add column {new_field['name']} "
        f"({new_field['type']}, {new_field['mode']}) to {schema_path}"
    )
    result = repo.update_file(
        path=schema_path,
        message=commit_msg,
        content=updated_text,
        sha=contents.sha,
        branch=branch,
    )
    commit_sha = result["commit"].sha
    return SchemaUpdateResult(
        schema_file=schema_path,
        commit_sha=commit_sha,
        added_field=new_field,
    )


# ---------------------------------------------------------------------------
# Pull request lifecycle
# ---------------------------------------------------------------------------

def open_pull_request(
    branch: str,
    title: str,
    body: str,
    base: Optional[str] = None,
) -> PullRequest:
    """Open a PR from ``branch`` to the configured base branch."""
    repo = get_repo()
    base_branch = base or settings.github_base_branch
    return repo.create_pull(title=title, body=body, head=branch, base=base_branch)


def merge_pull_request(
    pr_number: int,
    commit_title: Optional[str] = None,
    merge_method: str = "squash",
) -> str:
    """Merge a PR by number. Returns the merge commit SHA."""
    repo = get_repo()
    pr = repo.get_pull(pr_number)
    if pr.merged:
        return pr.merge_commit_sha or ""
    if not pr.mergeable:
        # PyGithub may return None while GitHub is still computing mergeability.
        # Give it one refresh before failing.
        pr.update()
        if pr.mergeable is False:
            raise GithubException(
                status=409,
                data={"message": f"PR #{pr_number} is not mergeable."},
                headers=None,
            )

    result = pr.merge(
        commit_title=commit_title or pr.title,
        merge_method=merge_method,
    )
    if not result.merged:
        raise RuntimeError(f"Merge of PR #{pr_number} failed: {result.message}")
    return result.sha
