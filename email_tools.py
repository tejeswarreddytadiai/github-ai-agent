"""Email intake parsing + approval email dispatch via Outlook (pywin32)."""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


# ---------------------------------------------------------------------------
# Intake parsing
# ---------------------------------------------------------------------------

# Db2 -> BigQuery type mapping. Extend as needed.
DB2_TO_BQ_TYPE = {
    "SMALLINT": "INTEGER",
    "INTEGER": "INTEGER",
    "INT": "INTEGER",
    "BIGINT": "INTEGER",
    "DECIMAL": "NUMERIC",
    "NUMERIC": "NUMERIC",
    "FLOAT": "FLOAT",
    "REAL": "FLOAT",
    "DOUBLE": "FLOAT",
    "CHAR": "STRING",
    "VARCHAR": "STRING",
    "CLOB": "STRING",
    "DATE": "DATE",
    "TIME": "TIME",
    "TIMESTAMP": "TIMESTAMP",
    "BOOLEAN": "BOOL",
}


@dataclass(frozen=True)
class ColumnRequest:
    """A single column to add, extracted from the intake email."""
    name: str
    db2_type: str
    nullable: bool

    @property
    def bq_type(self) -> str:
        return DB2_TO_BQ_TYPE.get(self.db2_type.upper(), "STRING")

    @property
    def bq_mode(self) -> str:
        return "NULLABLE" if self.nullable else "REQUIRED"

    def to_bq_field(self) -> dict:
        return {
            "name": self.name.lower(),
            "mode": self.bq_mode,
            "type": self.bq_type,
        }


@dataclass(frozen=True)
class EmailRequest:
    """Parsed email content."""
    column: ColumnRequest
    target_fqn: str  # e.g. prj-dfdl-817-tdbq-p-817.zgrps3rd.zgrt415_ccbsrdm
    project_id: str
    dataset_id: str
    table_id: str
    raw_text: str


# Matches the column-spec line, e.g.:
# "60  WORST_STAT_24MO_CD             SMALLINT 2      Y  Y        Y"
_COL_LINE_RE = re.compile(
    r"^\s*\d+\s+"                          # Num
    r"(?P<name>[A-Za-z_][A-Za-z0-9_]*)\s+"  # Column Name
    r"(?P<type>[A-Za-z]+)\s+"               # Coltype
    r"\d+\s+"                               # Length
    r"(?P<nl>[YN])\b",                      # Nl
    re.MULTILINE,
)

# Matches project.dataset.table (BQ FQN with dashes allowed in project).
_FQN_RE = re.compile(
    r"\b([A-Za-z0-9\-]+)\.([A-Za-z0-9_]+)\.([A-Za-z0-9_]+)\b"
)


def parse_email(path: str | Path) -> EmailRequest:
    """Parse the intake email file and return a structured EmailRequest.

    Raises:
        FileNotFoundError: if the email file is missing.
        ValueError: if the required fields cannot be extracted.
    """
    text = Path(path).read_text(encoding="utf-8")

    col_match = _COL_LINE_RE.search(text)
    if not col_match:
        raise ValueError("Could not extract column spec line from email.")

    col = ColumnRequest(
        name=col_match.group("name"),
        db2_type=col_match.group("type").upper(),
        nullable=col_match.group("nl").upper() == "Y",
    )

    fqn_match: Optional[re.Match[str]] = None
    for m in _FQN_RE.finditer(text):
        # Skip matches that appear on the column-spec line itself.
        if m.start() < col_match.end():
            continue
        fqn_match = m
        break

    if not fqn_match:
        raise ValueError("Could not extract target FQN (project.dataset.table).")

    project_id, dataset_id, table_id = fqn_match.group(1, 2, 3)
    return EmailRequest(
        column=col,
        target_fqn=f"{project_id}.{dataset_id}.{table_id}",
        project_id=project_id,
        dataset_id=dataset_id,
        table_id=table_id,
        raw_text=text,
    )


# ---------------------------------------------------------------------------
# Approval email via Outlook (pywin32)
# ---------------------------------------------------------------------------

def send_approval_email(
    to: str,
    subject: str,
    body_html: str,
    display_only: bool = False,
) -> str:
    """Send an approval email using the locally-installed Outlook via pywin32.

    Args:
        to: Recipient address.
        subject: Email subject.
        body_html: HTML body content.
        display_only: If True, open the draft in Outlook instead of sending
            (useful for local review).

    Returns:
        A short status string.
    """
    try:
        import win32com.client  # type: ignore
    except ImportError as exc:
        raise RuntimeError(
            "pywin32 is required to send Outlook mail. "
            "Install it with `pip install pywin32`."
        ) from exc

    outlook = win32com.client.Dispatch("Outlook.Application")
    mail = outlook.CreateItem(0)  # 0 = olMailItem
    mail.To = to
    mail.Subject = subject
    mail.HTMLBody = body_html
    if display_only:
        mail.Display(False)
        return f"Draft opened in Outlook for {to}."
    mail.Send()
    return f"Approval email sent to {to}."


def build_approval_email_body(
    pr_url: str,
    pr_number: int,
    schema_file: str,
    dataset_id: str,
    table_id: str,
    column: ColumnRequest,
) -> str:
    """Compose the HTML body for the approval email."""
    return f"""
    <html><body style="font-family:Segoe UI, Arial, sans-serif;">
    <h3>Schema Change Approval Request</h3>
    <p>A pull request has been raised to update the BigQuery schema file.</p>
    <table cellpadding="6" style="border-collapse:collapse;border:1px solid #ddd;">
      <tr><td><b>Repo path</b></td><td>{schema_file}</td></tr>
      <tr><td><b>Dataset</b></td><td>{dataset_id}</td></tr>
      <tr><td><b>Table</b></td><td>{table_id}</td></tr>
      <tr><td><b>New column</b></td><td>{column.name.lower()}</td></tr>
      <tr><td><b>Db2 type</b></td><td>{column.db2_type}</td></tr>
      <tr><td><b>BigQuery type</b></td><td>{column.bq_type}</td></tr>
      <tr><td><b>Mode</b></td><td>{column.bq_mode}</td></tr>
    </table>
    <p><b>PR #{pr_number}:</b> <a href="{pr_url}">{pr_url}</a></p>
    <p>Reply <b>APPROVE</b> to authorize the merge, or reject with reason.</p>
    </body></html>
    """.strip()
