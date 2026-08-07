"""Parser for terraform.tfvars to look up schema_file paths.

Terraform blocks may contain nested ``{ ... }`` (e.g. ``table_constraints``),
so a plain non-greedy regex over ``{ ... }`` is incorrect: it would match
only the innermost block and miss the outer one carrying ``table_id``/
``dataset_id``. This module walks the text and extracts brace-balanced
blocks with string-literal awareness.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Iterator


def _iter_balanced_blocks(text: str) -> Iterator[str]:
    """Yield every brace-balanced ``{ ... }`` substring in ``text``.

    Correctly handles nested braces and ignores braces that appear inside
    double-quoted string literals (with backslash escapes).
    """
    n = len(text)
    i = 0
    while i < n:
        if text[i] != "{":
            i += 1
            continue

        start = i
        depth = 0
        in_str = False
        escape = False
        j = i
        while j < n:
            ch = text[j]
            if in_str:
                if escape:
                    escape = False
                elif ch == "\\":
                    escape = True
                elif ch == '"':
                    in_str = False
            else:
                if ch == '"':
                    in_str = True
                elif ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 0:
                        yield text[start : j + 1]
                        break
            j += 1
        i = j + 1


def find_schema_file(tfvars_path: str | Path, dataset_id: str, table_id: str) -> str:
    """Locate the schema_file for a given (dataset_id, table_id) pair.

    Scans every brace-balanced block in the tfvars file and returns the
    ``schema_file`` value of the first block that contains both a matching
    ``table_id`` and ``dataset_id``.

    Raises:
        FileNotFoundError: if the tfvars file is missing.
        LookupError: if no matching block is found.
    """
    text = Path(tfvars_path).read_text(encoding="utf-8")

    table_re = re.compile(rf'table_id\s*=\s*"{re.escape(table_id)}"')
    dataset_re = re.compile(rf'dataset_id\s*=\s*"{re.escape(dataset_id)}"')
    schema_re = re.compile(r'schema_file\s*=\s*"([^"]+)"')

    for chunk in _iter_balanced_blocks(text):
        if table_re.search(chunk) and dataset_re.search(chunk):
            m = schema_re.search(chunk)
            if m:
                return m.group(1)

    raise LookupError(
        f"No tfvars block found for dataset_id={dataset_id!r}, "
        f"table_id={table_id!r} in {tfvars_path}."
    )
