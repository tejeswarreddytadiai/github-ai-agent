"""Centralized configuration loaded from .env."""
from __future__ import annotations

import os
from dataclasses import dataclass
from dotenv import load_dotenv

load_dotenv()


def _require(name: str) -> str:
    val = os.getenv(name)
    if not val:
        raise RuntimeError(f"Missing required env var: {name}")
    return val


@dataclass(frozen=True)
class Settings:
    # Azure OpenAI
    azure_endpoint: str
    azure_api_key: str
    azure_deployment: str

    # GitHub
    github_token: str
    github_repo: str
    github_base_branch: str

    # Local files
    email_file: str
    tfvars_file: str

    # Approval
    approver_email: str


def load_settings() -> Settings:
    return Settings(
        azure_endpoint=_require("AZURE_ENDPOINT"),
        azure_api_key=_require("AZURE_API_KEY"),
        azure_deployment=_require("AZURE_DEPLOYMENT"),
        github_token=_require("GITHUB_TOKEN"),
        github_repo=_require("GITHUB_REPO"),
        github_base_branch=os.getenv("GITHUB_BASE_BRANCH", "main"),
        email_file=os.getenv("EMAIL_FILE", "sample_email.txt"),
        tfvars_file=os.getenv("TFVARS_FILE", "terraform.tfvars"),
        approver_email=_require("APPROVER_EMAIL"),
    )


settings = load_settings()
