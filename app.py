"""Chainlit UI orchestrating the GitHub schema-update workflows.

Three entry points:

* **Email intake** — user types ``start`` (or "process the email", etc.). Reads
  ``sample_email.txt``, extracts the column + FQN, and drives the PR flow.
* **Interactive add** — user types ``add`` / "add a column". Chatbot collects
  Column Name, Coltype, Mode, Table Name; confirms with proceed/modify;
  verifies the tfvars entry, shows existing columns, previews the diff, then
  drives the PR flow.
* **Table lookup** — user asks in natural language (e.g. "show me the details
  of zgrps3rd.zgrt415_ccbsrdm"). Normalizes the FQN, locates it in
  ``terraform.tfvars``, and shows the current schema.

Run with:
    python serve.py app.py
"""
from __future__ import annotations

import asyncio
import json
import traceback
from typing import Optional

import chainlit as cl
from openai import OpenAI

import intent
from config import settings
from email_tools import (
    ColumnRequest,
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
    read_schema_fields,
    update_schema_file,
)
from tfvars_parser import (
    find_schema_file,
    find_schema_file_by_ref,
    normalize_table_ref,
)


# ---------------------------------------------------------------------------
# LLM client (used only for the PR-description summary)
# ---------------------------------------------------------------------------

_llm_client: Optional[OpenAI] = None


def get_llm() -> OpenAI:
    global _llm_client
    if _llm_client is None:
        _llm_client = OpenAI(
            api_key=settings.azure_api_key,
            base_url=settings.azure_endpoint.rstrip("/") + "/openai/v1/",
        )
    return _llm_client


def summarize_change(column: ColumnRequest, schema_file: str) -> str:
    """Ask the LLM to produce a short human-readable change summary."""
    prompt = (
        "Summarize the following BigQuery schema change in 2 short sentences "
        "suitable for a PR description. Be factual, no marketing tone.\n\n"
        f"Target schema file: {schema_file}\n"
        f"New column: {column.name} (Db2 {column.db2_type}, "
        f"nullable={column.nullable}) -> BQ {column.bq_type} "
        f"mode={column.bq_mode}\n"
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
    except Exception as exc:  # pragma: no cover
        return f"(LLM summary unavailable: {exc})"


# ---------------------------------------------------------------------------
# Chat UI helpers
# ---------------------------------------------------------------------------

WELCOME = (
    "Hi! I'm **GITHUB-AI-AGENT**. I can help with three things:\n\n"
    f"* **`start`** — process `{settings.email_file}` end-to-end and raise a "
    f"PR against `{settings.github_repo}`.\n"
    "* **`add`** — walk you through adding a column interactively "
    "(you enter the column name, type, mode, and table).\n"
    "* **Ask about a table** in plain English — e.g. *\"show me the details "
    "of `zgrps3rd.zgrt415_ccbsrdm`\"* — and I'll fetch its current schema.\n\n"
    "When a PR is open, type **`approve`** or click the *Approve & Merge* "
    "button to merge it."
)


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


def _fields_as_markdown(fields: list[dict]) -> str:
    """Render a BQ schema field list as a compact markdown code block."""
    return "```json\n" + json.dumps(fields, indent=2) + "\n```"


# ---------------------------------------------------------------------------
# Shared PR flow (branch → commit → PR → approval email → wait for merge)
# ---------------------------------------------------------------------------

async def run_pr_flow(
    column: ColumnRequest,
    schema_file: str,
    table_id: str,
    dataset_id: str,
) -> None:
    """Create the branch, commit the schema change, open a PR, email approver."""
    step = await _step("Creating feature branch on GitHub")
    try:
        branch: BranchInfo = create_feature_branch(table_id)
    except Exception as exc:
        await _fail(step, "Branch creation", exc)
        return
    await _done(step, f"Branch created: `{branch.name}`")

    step = await _step("Updating schema JSON and committing")
    try:
        update: SchemaUpdateResult = update_schema_file(schema_file, branch.name, column)
    except Exception as exc:
        await _fail(step, "Schema update", exc)
        return
    await _done(
        step,
        f"Committed `{update.added_field['name']}` "
        f"({update.added_field['type']}, {update.added_field['mode']}) "
        f"at commit `{update.commit_sha[:7]}`",
    )

    step = await _step("Raising Pull Request")
    try:
        summary = summarize_change(column, schema_file)
        pr_title = f"Add column {update.added_field['name']} to {table_id}"
        pr_body = (
            f"### Automated schema update\n\n"
            f"{summary}\n\n"
            f"**File:** `{schema_file}`\n"
            f"**Dataset:** `{dataset_id}`\n"
            f"**Table:** `{table_id}`\n"
            f"**Added field:** `{update.added_field}`\n"
        )
        pr = open_pull_request(branch.name, pr_title, pr_body)
    except Exception as exc:
        await _fail(step, "PR creation", exc)
        return
    await _done(step, f"PR #{pr.number} raised: {pr.html_url}")

    cl.user_session.set("pr_number", pr.number)
    cl.user_session.set("pr_url", pr.html_url)

    step = await _step("Sending approval email via Outlook")
    try:
        body_html = build_approval_email_body(
            pr_url=pr.html_url,
            pr_number=pr.number,
            schema_file=schema_file,
            dataset_id=dataset_id,
            table_id=table_id,
            column=column,
        )
        status = send_approval_email(
            to=settings.approver_email,
            subject=f"[Approval] Schema update PR #{pr.number} - {table_id}",
            body_html=body_html,
        )
    except Exception as exc:
        await _fail(step, "Approval email", exc)
        return
    await _done(step, status)

    await cl.Message(
        content=(
            f"PR **#{pr.number}** is ready for review: {pr.html_url}\n\n"
            "Type **`approve`** to simulate approval and merge the PR, or "
            "click the button below."
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
    cl.user_session.set("pr_number", None)
    cl.user_session.set("pr_url", None)
    await cl.Message(content="Workflow complete. Say `start`, `add`, or ask about a table to run again.").send()


# ---------------------------------------------------------------------------
# Flow 1 — email intake (existing "start" behaviour)
# ---------------------------------------------------------------------------

async def run_email_intake_flow() -> None:
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

    step = await _step("Looking up schema_file in terraform.tfvars")
    try:
        schema_file = find_schema_file(
            settings.tfvars_file, email.dataset_id, email.table_id
        )
    except Exception as exc:
        await _fail(step, "TFVARS lookup", exc)
        return
    await _done(step, f"TFVARS resolved -> `{schema_file}`")

    await run_pr_flow(
        column=email.column,
        schema_file=schema_file,
        table_id=email.table_id,
        dataset_id=email.dataset_id,
    )


# ---------------------------------------------------------------------------
# Flow 2 — interactive add
# ---------------------------------------------------------------------------

async def _ask(prompt: str) -> Optional[str]:
    reply = await cl.AskUserMessage(content=prompt, timeout=300).send()
    if reply is None:
        return None
    text = (reply.get("output") if isinstance(reply, dict) else None) or ""
    return text.strip()


def _coerce_mode(raw: str) -> Optional[bool]:
    """Return ``True`` for nullable, ``False`` for required, ``None`` on parse fail."""
    v = raw.strip().upper()
    if v in {"Y", "YES", "NULLABLE", "NULL", "TRUE"}:
        return True
    if v in {"N", "NO", "REQUIRED", "NOT NULL", "NOTNULL", "FALSE"}:
        return False
    return None


async def _collect_interactive_fields() -> Optional[dict]:
    """Ask the user for column name / coltype / mode / table. Returns dict or None."""
    col_name = await _ask("Please enter the **Column Name** (e.g. `worst_stat_24mo_cd`):")
    if not col_name:
        await cl.Message(content="Cancelled — no column name provided.").send()
        return None

    coltype = await _ask(
        "Enter the **Coltype** (Db2 type such as `SMALLINT`, `VARCHAR`, "
        "`TIMESTAMP`, or a BQ type like `STRING`, `INTEGER`):"
    )
    if not coltype:
        await cl.Message(content="Cancelled — no coltype provided.").send()
        return None

    mode_raw = await _ask(
        "Enter the **Mode** — `NULLABLE` / `REQUIRED`, or `Y` / `N` for nullable:"
    )
    if mode_raw is None:
        await cl.Message(content="Cancelled.").send()
        return None
    nullable = _coerce_mode(mode_raw)
    if nullable is None:
        await cl.Message(
            content=f"I couldn't parse `{mode_raw}` as a mode. Please start over with `add`."
        ).send()
        return None

    table_raw = await _ask(
        "Enter the **Table Name** (`dataset.table` or "
        "`project.dataset.table`):"
    )
    if not table_raw:
        await cl.Message(content="Cancelled — no table name provided.").send()
        return None

    return {
        "column_name": col_name,
        "coltype": coltype,
        "nullable": nullable,
        "mode_display": "NULLABLE" if nullable else "REQUIRED",
        "table_raw": table_raw,
    }


async def run_interactive_add_flow() -> None:
    while True:
        fields = await _collect_interactive_fields()
        if fields is None:
            return

        confirmation = (
            "Please cross-verify — these are the details you've given:\n\n"
            f"- **Column Name:** `{fields['column_name']}`\n"
            f"- **Coltype:** `{fields['coltype']}`\n"
            f"- **Mode:** `{fields['mode_display']}`\n"
            f"- **Table Name:** `{fields['table_raw']}`\n"
        )

        choice = await cl.AskActionMessage(
            content=confirmation,
            actions=[
                cl.Action(name="proceed", label="Proceed", payload={}),
                cl.Action(name="modify", label="Modify", payload={}),
                cl.Action(name="cancel", label="Cancel", payload={}),
            ],
            timeout=300,
        ).send()

        picked = (choice or {}).get("name") if choice else None
        if picked == "cancel" or picked is None:
            await cl.Message(content="Cancelled.").send()
            return
        if picked == "modify":
            await cl.Message(content="Okay — let's re-enter the details.").send()
            continue
        break  # proceed

    # ---- Validate table reference ----------------------------------------
    parsed = normalize_table_ref(fields["table_raw"])
    if not parsed:
        await cl.Message(
            content=(
                f"I couldn't parse `{fields['table_raw']}` as a table reference. "
                "Expected `dataset.table` or `project.dataset.table`. "
                "Type `add` to try again."
            )
        ).send()
        return
    dataset_id, table_id = parsed

    # ---- tfvars lookup ---------------------------------------------------
    step = await _step("Verifying table in terraform.tfvars")
    schema_file = find_schema_file_by_ref(settings.tfvars_file, dataset_id, table_id)
    if not schema_file:
        await _fail(
            step,
            "TFVARS lookup",
            LookupError(
                f"No block matching dataset_id={dataset_id!r}, table_id={table_id!r} "
                f"in {settings.tfvars_file}."
            ),
        )
        return
    await _done(
        step,
        f"Great — table `{dataset_id}.{table_id}` is present in "
        f"`{settings.tfvars_file}`",
    )

    step = await _step("Identifying schema JSON file")
    await _done(step, f"Identified schema file: `{schema_file}`")

    # ---- Show existing columns, then the diff ----------------------------
    step = await _step("Fetching existing schema from GitHub")
    try:
        existing_fields = read_schema_fields(schema_file)
    except Exception as exc:
        await _fail(step, "Fetch existing schema", exc)
        return
    await _done(step, f"Found {len(existing_fields)} existing column(s)")

    await cl.Message(
        content=(
            f"Existing columns in `{schema_file}`:\n"
            f"{_fields_as_markdown(existing_fields)}"
        )
    ).send()

    await asyncio.sleep(2)

    column = ColumnRequest(
        name=fields["column_name"],
        db2_type=fields["coltype"].upper(),
        nullable=fields["nullable"],
    )
    new_field = column.to_bq_field()
    preview = existing_fields + [new_field]

    await cl.Message(
        content=(
            f"Adding the following column to the schema:\n"
            f"{_fields_as_markdown([new_field])}\n\n"
            f"The updated schema will look like this:\n"
            f"{_fields_as_markdown(preview)}"
        )
    ).send()

    # ---- Proceed with the PR pipeline ------------------------------------
    await run_pr_flow(
        column=column,
        schema_file=schema_file,
        table_id=table_id,
        dataset_id=dataset_id,
    )


# ---------------------------------------------------------------------------
# Flow 3 — natural-language table lookup
# ---------------------------------------------------------------------------

async def run_lookup_flow(table_ref_raw: Optional[str]) -> None:
    if not table_ref_raw:
        await cl.Message(
            content=(
                "Which table would you like to look up? "
                "Please give me the reference as `dataset.table` or "
                "`project.dataset.table`."
            )
        ).send()
        return

    parsed = normalize_table_ref(table_ref_raw)
    if not parsed:
        await cl.Message(
            content=(
                f"I couldn't parse `{table_ref_raw}` as a BigQuery table "
                "reference. Please give me `dataset.table` or "
                "`project.dataset.table`."
            )
        ).send()
        return
    dataset_id, table_id = parsed

    step = await _step(f"Searching `{dataset_id}.{table_id}` in `{settings.tfvars_file}`")
    schema_file = find_schema_file_by_ref(settings.tfvars_file, dataset_id, table_id)
    if not schema_file:
        await _fail(
            step,
            "TFVARS lookup",
            LookupError(
                f"No entry for {dataset_id}.{table_id} in {settings.tfvars_file}."
            ),
        )
        return
    await _done(
        step,
        f"Table `{dataset_id}.{table_id}` was found in `{settings.tfvars_file}`",
    )

    step = await _step("Locating the schema JSON file")
    await _done(step, f"Schema file: `{schema_file}`")

    step = await _step("Fetching existing schema from GitHub")
    try:
        fields = read_schema_fields(schema_file)
    except Exception as exc:
        await _fail(step, "Fetch existing schema", exc)
        return
    await _done(step, f"Found {len(fields)} column(s)")

    await cl.Message(
        content=(
            f"**Existing columns in `{dataset_id}.{table_id}`**\n"
            f"{_fields_as_markdown(fields)}"
        )
    ).send()


# ---------------------------------------------------------------------------
# Chainlit event handlers
# ---------------------------------------------------------------------------

@cl.on_chat_start
async def on_chat_start() -> None:
    cl.user_session.set("pr_number", None)
    cl.user_session.set("pr_url", None)
    await cl.Message(content=WELCOME).send()


@cl.action_callback("approve_merge")
async def on_approve_action(action: cl.Action) -> None:
    pr_number = int(action.payload.get("pr_number", 0)) or cl.user_session.get("pr_number")
    if not pr_number:
        await cl.Message(content="No PR in this session to merge.").send()
        return
    await do_merge(pr_number)


# Fast-path keyword routing before we call the LLM classifier. Keeps the UX
# snappy for the common commands and avoids an LLM round-trip when the user
# is answering a "yes/no" prompt with something ambiguous.
_START_WORDS = {"start", "run", "go", "begin"}
_ADD_WORDS = {"add", "add column", "add a column", "new column", "interactive"}
_APPROVE_WORDS = {"approve", "approved", "merge", "lgtm"}
_HELP_WORDS = {"help", "?", "commands"}


def _keyword_intent(text: str) -> Optional[intent.ParsedIntent]:
    lower = text.strip().lower()
    if lower in _START_WORDS or "start the schema update" in lower:
        return intent.ParsedIntent(intent="start", reason="keyword")
    if lower in _ADD_WORDS:
        return intent.ParsedIntent(intent="interactive", reason="keyword")
    if lower in _APPROVE_WORDS:
        return intent.ParsedIntent(intent="approve", reason="keyword")
    if lower in _HELP_WORDS:
        return intent.ParsedIntent(intent="help", reason="keyword")
    return None


@cl.on_message
async def on_message(msg: cl.Message) -> None:
    text = (msg.content or "").strip()
    if not text:
        return

    parsed = _keyword_intent(text) or intent.classify(text)

    try:
        if parsed.intent == "start":
            await run_email_intake_flow()
            return

        if parsed.intent == "interactive":
            await run_interactive_add_flow()
            return

        if parsed.intent == "lookup":
            await run_lookup_flow(parsed.table_ref)
            return

        if parsed.intent == "approve":
            pr_number = cl.user_session.get("pr_number")
            if not pr_number:
                await cl.Message(
                    content="No PR pending in this session. Type `start` or `add` first."
                ).send()
                return
            await do_merge(pr_number)
            return

        if parsed.intent == "cancel":
            await cl.Message(content="Nothing active to cancel.").send()
            return

        if parsed.intent == "help":
            await cl.Message(content=WELCOME).send()
            return

        # Unknown — ask for clarification rather than guessing.
        await cl.Message(
            content=(
                "I'm not sure what you meant. I can:\n"
                "* Run the full workflow from `sample_email.txt` — say **`start`**.\n"
                "* Walk you through adding a column interactively — say **`add`**.\n"
                "* Look up a table's schema — try *\"show me the details of "
                "`dataset.table`\"*.\n"
                "* Merge a pending PR — say **`approve`**.\n\n"
                "What would you like to do?"
            )
        ).send()

    except Exception:
        await cl.Message(
            content=f"Unexpected error:\n```\n{traceback.format_exc()}\n```"
        ).send()
