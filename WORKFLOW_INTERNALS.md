# GITHUB-AI-AGENT — Workflow Internals

> Everything is done via **GitHub REST API** (never local Git commands).
> PyGithub wraps those API calls. No `git clone`, no `git push`, no shell.

---

## Short Answer: API or Git Commands?

**100% GitHub REST API.** The app never runs any `git` shell commands.
All branch creation, file reads, file commits, and PR operations are HTTP calls
to `api.github.com` made by the **PyGithub** library.

---

## Libraries & Packages Used Per Step

| Step | Library / Package | What it does |
|------|------------------|--------------|
| Load config | `python-dotenv` | Reads `.env` into `os.environ` |
| Read email file | Python stdlib `pathlib` + `re` | Reads `sample_email.txt`, regex-extracts column spec & FQN |
| Parse tfvars | Custom `tfvars_parser.py` (stdlib only) | Character-walk parser finds the right block in `terraform.tfvars` |
| GitHub auth | `PyGithub` → `github.Auth.Token` | Wraps PAT into a `Token` auth object |
| GitHub client | `PyGithub` → `github.Github` | Authenticated REST API client |
| Get repo object | `PyGithub` → `repo.get_repo()` | `GET /repos/{owner}/{repo}` |
| Read schema from GitHub | `PyGithub` → `repo.get_contents()` | `GET /repos/{owner}/{repo}/contents/{path}` |
| Create feature branch | `PyGithub` → `repo.create_git_ref()` | `POST /repos/{owner}/{repo}/git/refs` |
| Commit schema change | `PyGithub` → `repo.update_file()` | `PUT /repos/{owner}/{repo}/contents/{path}` |
| Open Pull Request | `PyGithub` → `repo.create_pull()` | `POST /repos/{owner}/{repo}/pulls` |
| Merge Pull Request | `PyGithub` → `pr.merge()` | `PUT /repos/{owner}/{repo}/pulls/{number}/merge` |
| Intent classification | `openai` SDK → Azure OpenAI | `POST /openai/v1/chat/completions` (JSON mode) |
| PR description summary | `openai` SDK → Azure OpenAI | Same endpoint, free-text response |
| Send approval email | `pywin32` → `win32com.client` | Calls locally-installed Outlook via Windows COM |
| Chat UI | `chainlit` | WebSocket + HTTP server; renders messages & action buttons |

---

## Step-by-Step: What Happens When You Type `start`

### Step 0 — User types `start`

**File:** `app.py` → `on_message()`

1. Message arrives in `on_message()` via Chainlit's WebSocket.
2. `_keyword_intent("start")` matches the word in `_START_WORDS` — returns
   `ParsedIntent(intent="start")` immediately. **No LLM call needed.**
3. Calls `run_email_intake_flow()`.

---

### Step 1 — Parse the intake email

**File:** `email_tools.py` → `parse_email()`  
**Library:** Python stdlib `pathlib`, `re`  
**API:** None — local file read only

```
sample_email.txt  →  parse_email()  →  EmailRequest(column, dataset_id, table_id, ...)
```

- Reads `sample_email.txt` from disk using `Path.read_text()`.
- Two regex patterns extract the data:
  - `_COL_LINE_RE` — matches the column-spec line:
    `60  WORST_STAT_24MO_CD  SMALLINT  2  Y  Y  Y`
    → extracts `name=WORST_STAT_24MO_CD`, `db2_type=SMALLINT`, `nullable=Y`
  - `_FQN_RE` — matches `project.dataset.table`:
    `prj-dfdl-817-tdbq-p-817.zgrps3rd.zgrt415_ccbsrdm`
    → extracts `dataset_id=zgrps3rd`, `table_id=zgrt415_ccbsrdm`
- Returns an `EmailRequest` dataclass (frozen, immutable).

---

### Step 2 — Type conversion (Db2 → BigQuery)

**File:** `email_tools.py` → `ColumnRequest` properties  
**Library:** None — pure Python dict lookup

```
SMALLINT  →  DB2_TO_BQ_TYPE dict  →  INTEGER
nullable=Y  →  bq_mode  →  "NULLABLE"
```

A hardcoded mapping dict (`DB2_TO_BQ_TYPE`) converts Db2 types to BQ types.
The `to_bq_field()` method produces the JSON dict that gets appended to the
schema file:
```json
{ "name": "worst_stat_24mo_cd", "mode": "NULLABLE", "type": "INTEGER" }
```

---

### Step 3 — Look up the schema file path in terraform.tfvars

**File:** `tfvars_parser.py` → `find_schema_file()`  
**Library:** Python stdlib `re`, `pathlib`  
**API:** None — local file read only

```
terraform.tfvars  →  _iter_balanced_blocks()  →  first block where
                       table_id="zgrt415_ccbsrdm" AND dataset_id="zgrps3rd"
                     →  schema_file = "schema_files/zgrps3rd.zgrt415_ccbsrdm.json"
```

- `terraform.tfvars` is parsed by `_iter_balanced_blocks()` — a custom
  O(N) character-walk that correctly handles nested `{ }` (like
  `table_constraints`) that would break a simple regex.
- String-literal awareness: braces inside `"..."` are ignored.
- Returns the `schema_file` value from the matching block.

**Why not a real Terraform parser?**
`terraform.tfvars` uses HCL syntax. A full HCL parser is a large dependency.
The brace-walk is sufficient because we only need three specific string values
(`table_id`, `dataset_id`, `schema_file`) from one block.

---

### Step 4 — Authenticate to GitHub

**File:** `github_client.py` → `get_github()`, `get_repo()`  
**Library:** `PyGithub` (package: `PyGithub>=2.4.0`)  
**API:** `GET https://api.github.com/repos/tejeswarreddytadiai/github-ai-agent`

```
GITHUB_TOKEN (.env)  →  Auth.Token(token)  →  Github(auth=auth)  →  get_repo()
```

- `Auth.Token` wraps the Personal Access Token (PAT) from `.env`.
- `Github(auth=auth)` creates an authenticated client that will include
  `Authorization: token <PAT>` on every API request.
- `get_repo("tejeswarreddytadiai/github-ai-agent")` makes one HTTP GET to
  fetch the repo metadata (name, default branch, etc.).
- Both `get_github()` and `get_repo()` are decorated with `@lru_cache(maxsize=1)`
  — the client and repo objects are created once and reused across all steps.

---

### Step 5 — Create a feature branch on GitHub

**File:** `github_tools.py` → `create_feature_branch()`  
**Library:** `PyGithub`  
**GitHub API calls:**
1. `GET /repos/{owner}/{repo}/branches/main` — get the current HEAD SHA of main
2. `POST /repos/{owner}/{repo}/git/refs` — create the new branch pointing at that SHA

```
table_id="zgrt415_ccbsrdm"  →  branch name = "schema-update/zgrt415_ccbsrdm-20260808-112620"
base branch HEAD SHA  →  new ref created pointing at same commit
```

- Branch naming pattern: `schema-update/<table_id>-<UTC-timestamp>`
- The timestamp ensures uniqueness even if you run the workflow twice for the
  same table.
- `repo.get_branch("main")` → fetches the branch object, reads `.commit.sha`.
- `repo.create_git_ref(ref="refs/heads/schema-update/...", sha=<sha>)` → creates
  the branch. This is equivalent to `git branch schema-update/... <sha>` + push,
  but done entirely via API.

---

### Step 6 — Read the existing schema from GitHub

**File:** `github_tools.py` → `read_schema_fields()` (called inside `update_schema_file`)  
**Library:** `PyGithub`  
**GitHub API call:** `GET /repos/{owner}/{repo}/contents/{schema_path}?ref={branch}`

```
schema_files/zgrps3rd.zgrt415_ccbsrdm.json  on branch "schema-update/..."
  →  contents.decoded_content  →  json.loads()  →  list of existing field dicts
```

- `repo.get_contents(path, ref=branch)` returns a `ContentFile` object.
- `.decoded_content` is the base64-decoded bytes of the file; we decode to UTF-8.
- `json.loads()` parses it into a Python list.
- The `.sha` (blob SHA) of the ContentFile is saved — it is required by the
  GitHub API for the subsequent update call (optimistic concurrency).

---

### Step 7 — Duplicate check

**File:** `github_tools.py` → `update_schema_file()`  
**Library:** None — pure Python set operation

```
existing_names = {f["name"].lower() for f in fields}
if new_field["name"].lower() in existing_names:  raise ValueError
```

Before writing anything, the new column name is checked against every existing
field name (case-insensitive). If it already exists, the workflow aborts with
a clear error rather than creating a duplicate.

---

### Step 8 — Commit the updated schema to the feature branch

**File:** `github_tools.py` → `update_schema_file()`  
**Library:** `PyGithub`  
**GitHub API call:** `PUT /repos/{owner}/{repo}/contents/{schema_path}`

```
existing fields + [new_field]  →  json.dumps(indent=2)  →  base64-encode
  →  PUT /contents/{path}  {message, content, sha, branch}
  →  GitHub writes the commit on the feature branch
  →  returns commit SHA
```

- The new field dict is appended to the list, re-serialized with 2-space indent.
- `repo.update_file(path, message, content, sha, branch)`:
  - `sha` = the blob SHA from Step 6 (GitHub requires this to detect conflicts)
  - `branch` = the feature branch name
  - `message` = auto-generated commit message:
    `"schema: add column worst_stat_24mo_cd (INTEGER, NULLABLE) to schema_files/..."`
- This is equivalent to: edit file → `git add` → `git commit` → `git push` —
  but all in one API call.

---

### Step 9 — Generate PR description with LLM

**File:** `app.py` → `summarize_change()`  
**Library:** `openai` SDK  
**API call:** `POST https://<azure-endpoint>/openai/v1/chat/completions`

```
ColumnRequest details  →  prompt  →  Azure OpenAI gpt-5-mini
  →  2-sentence human-readable summary  →  used as PR body text
```

- Uses the same Azure OpenAI deployment (`gpt-5-mini`) as the intent classifier.
- If the LLM call fails (network error, quota), the PR body falls back to a
  static template — the workflow is never blocked by LLM unavailability.

---

### Step 10 — Open the Pull Request

**File:** `github_tools.py` → `open_pull_request()`  
**Library:** `PyGithub`  
**GitHub API call:** `POST /repos/{owner}/{repo}/pulls`

```
head="schema-update/..."  base="main"  title="Add column ..."  body="..."
  →  GitHub creates the PR
  →  returns PullRequest object with .number and .html_url
```

- `repo.create_pull(title, body, head=branch, base="main")` opens the PR.
- The PR number and URL are saved in `cl.user_session` so the `approve` command
  can find them later without the user having to type the number.

---

### Step 11 — Send approval email via Outlook

**File:** `email_tools.py` → `send_approval_email()`  
**Library:** `pywin32` (`win32com.client`)  
**Protocol:** Windows COM (not HTTP — Outlook API)

```
win32com.client.Dispatch("Outlook.Application")
  →  CreateItem(0)  →  set .To, .Subject, .HTMLBody
  →  .Send()
  →  Outlook sends the email using the locally signed-in account
```

- `pywin32` uses Windows Component Object Model (COM) to automate the locally
  installed Outlook application.
- This is not Outlook REST API or Graph API — it talks directly to the Outlook
  process running on the machine.
- The HTML body includes a table with all column details and a link to the PR.
- If Outlook is not installed or pywin32 is unavailable, the step fails with
  a clear error (the PR still exists on GitHub).

---

### Step 12 — User types `approve` (or clicks the button)

**File:** `app.py` → `do_merge()` → `github_tools.py` → `merge_pull_request()`  
**Library:** `PyGithub`  
**GitHub API calls:**
1. `GET /repos/{owner}/{repo}/pulls/{number}` — check if already merged / check mergeability
2. `PUT /repos/{owner}/{repo}/pulls/{number}/merge` — perform the merge

```
pr_number (from user_session)  →  repo.get_pull(number)
  →  pr.mergeable check  →  pr.merge(commit_title, merge_method="squash")
  →  returns merge commit SHA
```

- `merge_method="squash"` squash-merges all commits on the feature branch into
  one commit on main.
- If `pr.mergeable` is `None` (GitHub is still computing it), the code calls
  `pr.update()` once and checks again before failing.
- After merge, `pr_number` is cleared from the session — the workflow is complete.

---

## Flow 2: Interactive Add (`add`)

Same Steps 4–12 above, but Steps 1–3 are replaced by a chat-based data
collection loop:

| Replaced step | What happens instead |
|--------------|---------------------|
| Step 1 (parse email) | `cl.AskUserMessage` prompts for Column Name, Coltype, Mode, Table |
| Step 2 (type convert) | Same `ColumnRequest` / `to_bq_field()` logic |
| Step 3 (tfvars lookup) | `cl.AskActionMessage` confirmation → `normalize_table_ref()` → `find_schema_file_by_ref()` |

The user's table reference (`dataset.table` or `project.dataset.table`) is
normalized by `normalize_table_ref()` in `tfvars_parser.py` — it strips
backticks, quotes, whitespace, and drops the project prefix if present.

---

## Flow 3: Table Lookup (`show me the details of ...`)

| Step | What happens |
|------|-------------|
| Intent classification | LLM (`intent.py`) returns `intent="lookup"`, `table_ref="zgrps3rd.zgrt415_ccbsrdm"` |
| Normalize ref | `normalize_table_ref()` → `("zgrps3rd", "zgrt415_ccbsrdm")` |
| tfvars lookup | `find_schema_file_by_ref()` → `"schema_files/zgrps3rd.zgrt415_ccbsrdm.json"` |
| Read schema from GitHub | `read_schema_fields()` → `GET /repos/.../contents/schema_files/...` |
| Display | Chainlit renders the JSON field list in a code block |

No branch, no commit, no PR — read-only.

---

## Intent Routing

**File:** `app.py` → `on_message()` + `_keyword_intent()` + `intent.py` → `classify()`

```
user message
  │
  ├── _keyword_intent()   ← fast path, no LLM, O(1)
  │     matches: start / add / approve / help
  │
  └── intent.classify()   ← LLM path (Azure OpenAI JSON mode)
        returns: { intent, table_ref, reason }
        never raises: returns "unknown" on error
```

The keyword fast-path avoids an LLM round-trip for the most common single-word
commands, keeping the UI snappy. The LLM handles natural language like
*"can you show me the schema of this table: zgrps3rd.zgrt415_ccbsrdm"*.

---

## Security

- All secrets live in `.env` only, loaded by `python-dotenv` at startup.
- `.env` is in `.gitignore` — never committed.
- The GitHub PAT is sent only to `api.github.com` via HTTPS.
- The Azure API key is sent only to the Azure OpenAI endpoint via HTTPS.
- `pywin32` COM calls are local-machine only — no network involved.

---

## Summary Table: GitHub API Calls Made

| Operation | HTTP Method | GitHub REST Endpoint |
|-----------|------------|---------------------|
| Get repo metadata | GET | `/repos/{owner}/{repo}` |
| Get branch HEAD SHA | GET | `/repos/{owner}/{repo}/branches/{branch}` |
| Create feature branch | POST | `/repos/{owner}/{repo}/git/refs` |
| Read schema file | GET | `/repos/{owner}/{repo}/contents/{path}?ref={branch}` |
| Commit updated schema | PUT | `/repos/{owner}/{repo}/contents/{path}` |
| Open pull request | POST | `/repos/{owner}/{repo}/pulls` |
| Get PR status | GET | `/repos/{owner}/{repo}/pulls/{number}` |
| Merge pull request | PUT | `/repos/{owner}/{repo}/pulls/{number}/merge` |
