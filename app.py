"""Chainlit UI orchestrating the GitHub schema-update workflow.

Run locally with:
    chainlit run app.py -w
"""
from __future__ import annotations

import traceback
from typing import Optional

import chainlit as cl
from openai import OpenAI

from config import settings
from email_tools import (
    EmailRequest,
    build_approval_email_body,
    parse_email,
    send_approval_email,
)
from github_tools import (
    BranchInfo,
    SchemaUpdateResult,
    create_feature_branch,
    merge_pull_request,
    open_pull_request,
    update_schema_file,
)
from tfvars_parser import find_schema_file


# ---------------------------------------------------------------------------
# LLM client
# ---------------------------------------------------------------------------

_llm_client: Optional[OpenAI] = None


def get_llm() -> OpenAI:
    """Lazy-init the Azure-backed OpenAI client."""
    global _llm_client
    if _llm_client is None:
        _llm_client = OpenAI(
            api_key=settings.azure_api_key,
            base_url=settings.azure_endpoint.rstrip("/") + "/openai/v1/",
        )
    return _llm_client


def summarize_change(email: EmailRequest, schema_file: str) -> str:
    """Ask the LLM to produce a short human-readable change summary."""
    prompt = (
        "Summarize the following BigQuery schema change in 2 short sentences "
        "suitable for a PR description. Be factual, no marketing tone.\n\n"
        f"Target schema file: {schema_file}\n"
        f"Dataset: {email.dataset_id}\n"
        f"Table: {email.table_id}\n"
        f"New column: {email.column.name} (Db2 {email.column.db2_type}, "
        f"nullable={email.column.nullable}) -> BQ {email.column.bq_type} "
        f"mode={email.column.bq_mode}\n"
    )
    try:
        resp = get_llm().chat.completions.create(
            model=settings.azure_deployment,
            messages=[
                {"role": "system", "content": "You write concise engineering PR summaries."},
                {"role": "user", "content": prompt},
            ],
            max_completion_tokens=250,
        )
        return (resp.choices[0].message.content or "").strip()
    except Exception as exc:  # pragma: no cover - LLM is optional flavor
        return f"(LLM summary unavailable: {exc})"


# ---------------------------------------------------------------------------
# Chainlit lifecycle
# ---------------------------------------------------------------------------

WELCOME = (
    "Hi! I'm **GITHUB-AI-AGENT**. Say **`start`** to process "
    f"`{settings.email_file}` and raise a PR against "
    f"`{settings.github_repo}`.\n\n"
    "After the PR is raised I'll send an approval email to "
    f"`{settings.approver_email}`. When you're ready to merge, "
    "type **`approve`** (or click the *Approve & Merge* action) to simulate "
    "the approval and complete the merge."
)


@cl.on_chat_start
async def on_chat_start() -> None:
    cl.user_session.set("pr_number", None)
    cl.user_session.set("pr_url", None)
    await cl.Message(content=WELCOME).send()


async def _step(label: str) -> cl.Message:
    msg = cl.Message(content=f"* {label}...")
    await msg.send()
    return msg


async def _done(msg: cl.Message, label: str) -> None:
    msg.content = f"* {label} ✓"
    await msg.update()


async def _fail(msg: cl.Message, label: str, err: Exception) -> None:
    msg.content = f"* {label} ✗\n```\n{err}\n```"
    await msg.update()


async def run_workflow() -> None:
    """Execute the full intake -> PR -> approval-email workflow."""

    # 1. Intake ---------------------------------------------------------------
    step = await _step("Reading and parsing intake email")
    try:
        email: EmailRequest = parse_email(settings.email_file)
    except Exception as exc:
        await _fail(step, "Reading and parsing intake email", exc)
        return
    await _done(step, "Intake parsed")
    await cl.Message(
        content=(
            f"**Extracted request**\n"
            f"- Column: `{email.column.name}` "
            f"({email.column.db2_type}, nullable={email.column.nullable})\n"
            f"- Target: `{email.target_fqn}`"
        )
    ).send()

    # 2. TFVARS lookup --------------------------------------------------------
    step = await _step("Looking up schema_file in terraform.tfvars")
    try:
        schema_file = find_schema_file(
            settings.tfvars_file, email.dataset_id, email.table_id
        )
    except Exception as exc:
        await _fail(step, "TFVARS lookup", exc)
        return
    await _done(step, f"TFVARS resolved -> `{schema_file}`")

    # 3. Branch ---------------------------------------------------------------
    step = await _step("Creating feature branch on GitHub")
    try:
        branch: BranchInfo = create_feature_branch(email.table_id)
    except Exception as exc:
        await _fail(step, "Branch creation", exc)
        return
    await _done(step, f"Branch created: `{branch.name}`")

    # 4. JSON modify + commit -------------------------------------------------
    step = await _step("Updating schema JSON and committing")
    try:
        update: SchemaUpdateResult = update_schema_file(
            schema_file, branch.name, email.column
        )
    except Exception as exc:
        await _fail(step, "Schema update", exc)
        return
    await _done(
        step,
        f"Committed `{update.added_field['name']}` "
        f"({update.added_field['type']}, {update.added_field['mode']}) "
        f"at commit `{update.commit_sha[:7]}`",
    )

    # 5. Pull request ---------------------------------------------------------
    step = await _step("Raising Pull Request")
    try:
        summary = summarize_change(email, schema_file)
        pr_title = f"Add column {update.added_field['name']} to {email.table_id}"
        pr_body = (
            f"### Automated schema update\n\n"
            f"{summary}\n\n"
            f"**File:** `{schema_file}`\n"
            f"**Dataset:** `{email.dataset_id}`\n"
            f"**Table:** `{email.table_id}`\n"
            f"**Added field:** `{update.added_field}`\n"
        )
        pr = open_pull_request(branch.name, pr_title, pr_body)
    except Exception as exc:
        await _fail(step, "PR creation", exc)
        return
    await _done(step, f"PR #{pr.number} raised: {pr.html_url}")

    cl.user_session.set("pr_number", pr.number)
    cl.user_session.set("pr_url", pr.html_url)

    # 6. Approval email -------------------------------------------------------
    step = await _step("Sending approval email via Outlook")
    try:
        body_html = build_approval_email_body(
            pr_url=pr.html_url,
            pr_number=pr.number,
            schema_file=schema_file,
            dataset_id=email.dataset_id,
            table_id=email.table_id,
            column=email.column,
        )
        status = send_approval_email(
            to=settings.approver_email,
            subject=f"[Approval] Schema update PR #{pr.number} - {email.table_id}",
            body_html=body_html,
        )
    except Exception as exc:
        await _fail(step, "Approval email", exc)
        return
    await _done(step, status)

    # 7. Await approval -------------------------------------------------------
    await cl.Message(
        content=(
            f"PR **#{pr.number}** is ready for review: {pr.html_url}\n\n"
            "Type **`approve`** to simulate approval and merge the PR, "
            "or click the button below."
        ),
        actions=[
            cl.Action(
                name="approve_merge",
                label="Approve & Merge",
                payload={"pr_number": pr.number},
            )
        ],
    ).send()


async def do_merge(pr_number: int) -> None:
    step = await _step(f"Merging PR #{pr_number}")
    try:
        sha = merge_pull_request(pr_number)
    except Exception as exc:
        await _fail(step, "Merge", exc)
        return
    await _done(step, f"Merged as `{sha[:7]}`")
    await cl.Message(content="Workflow complete. Say `start` to run again.").send()


# ---------------------------------------------------------------------------
# Message + action handlers
# ---------------------------------------------------------------------------

@cl.action_callback("approve_merge")
async def on_approve_action(action: cl.Action) -> None:
    pr_number = int(action.payload.get("pr_number", 0)) or cl.user_session.get("pr_number")
    if not pr_number:
        await cl.Message(content="No PR in this session to merge.").send()
        return
    await do_merge(pr_number)


@cl.on_message
async def on_message(msg: cl.Message) -> None:
    text = msg.content.strip().lower()

    if text in {"start", "run", "go", "begin"} or "start the schema update" in text:
        try:
            await run_workflow()
        except Exception:
            await cl.Message(
                content=f"Unexpected error:\n```\n{traceback.format_exc()}\n```"
            ).send()
        return

    if text in {"approve", "approved", "merge"}:
        pr_number = cl.user_session.get("pr_number")
        if not pr_number:
            await cl.Message(
                content="No PR pending in this session. Type `start` first."
            ).send()
            return
        await do_merge(pr_number)
        return

    if text in {"help", "?"}:
        await cl.Message(content=WELCOME).send()
        return

    await cl.Message(
        content=(
            "I understand: `start` (run the workflow), `approve` (merge the PR), "
            "or `help`."
        )
    ).send()
