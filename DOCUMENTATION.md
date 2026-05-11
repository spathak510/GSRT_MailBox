# Mailbox Auto Assistant — Full Technical Documentation

> **Version:** 0.1.0 | **Python:** ≥ 3.10 | **Last Updated:** March 2026

---

## Table of Contents

1. [Overview](#1-overview)
2. [Architecture](#2-architecture)
3. [Email Processing Decision Flow](#3-email-processing-decision-flow)
4. [Project Structure](#4-project-structure)
5. [Domain Layer](#5-domain-layer)
6. [Application Layer](#6-application-layer)
7. [Infrastructure Layer](#7-infrastructure-layer)
8. [Observability](#8-observability)
9. [Settings & Configuration](#9-settings--configuration)
10. [API Layer (Flask)](#10-api-layer-flask)
11. [Data Files](#11-data-files)
12. [Database](#12-database)
13. [Scripts](#13-scripts)
14. [Tests](#14-tests)
15. [Setup & Quick Start](#15-setup--quick-start)
16. [Environment Variables Reference](#16-environment-variables-reference)
17. [Extending the Application](#17-extending-the-application)

---

## 1. Overview

**Mailbox Auto Assistant** is a production-ready Python application that automatically processes incoming emails from a Microsoft 365 mailbox. It classifies emails into categories, routes them to the appropriate mailbox folders, sends context-aware auto-replies, and tracks every action in a persistent audit log.

### Core capabilities

| Capability | Description |
|---|---|
| Email fetching | Reads unread emails from Microsoft 365 inbox via Microsoft Graph API |
| Classification | Deterministic keyword/sender rules → AI fallback (OpenAI GPT) |
| Folder routing | Moves emails to mapped folders based on category |
| Smart auto-reply | Sends different replies based on email type and ticket status |
| Ticket detection | Extracts ticket/reference numbers (e.g. `INC-12345`) and checks their status |
| VIP escalation | Detects Director/VP senders and flags for manual review |
| Deduplication | Tracks processed email IDs in SQLite or PostgreSQL |
| Audit logging | Appends every action as a JSON line to an audit log file |
| REST API | Optional Flask server to trigger processing and inspect emails |

---

## 2. Architecture

The application follows a strict **layered / clean architecture**:

```
┌─────────────────────────────────────────────────────┐
│                     Scripts / API                    │  Entry points
├─────────────────────────────────────────────────────┤
│                  Application Layer                   │  Orchestration
│          pipeline.py  ·  use_cases.py               │
│     reply_builder.py  ·  prompt_builder.py           │
├─────────────────────────────────────────────────────┤
│                    Domain Layer                      │  Business logic
│        models.py  ·  rules_engine.py                │
│              folder_mapper.py                       │
├─────────────────────────────────────────────────────┤
│                Infrastructure Layer                  │  I/O adapters
│   mailbox/  ·  ai/  ·  ticketing/  ·  persistence/ │
├─────────────────────────────────────────────────────┤
│               Observability & Settings               │  Cross-cutting
│       audit_logger.py  ·  metrics.py  ·  config.py  │
└─────────────────────────────────────────────────────┘
```

**Rule:** inner layers never import from outer layers. All adapters implement abstract base classes defined in the infrastructure layer, making them fully swappable.

---

## 3. Email Processing Decision Flow

Every unread email passes through the following decision tree in `EmailSegregationPipeline.process_unread_emails()`:

```
Unread email arrives
        │
        ▼
Already processed? ──YES──► Skip
        │ NO
        ▼
Sender is Director/VP/Chief/CTO/…?
  ──YES──► Log WARNING
           Audit: action = "vip_escalation"
           Save to DB: category = "escalation"
           ⚠️  Stays in Inbox — requires manual discussion with ESCALATION_EMAIL contact
           ──► Next email
        │ NO
        ▼
Ticket/reference number in subject or body? (e.g. INC-12345, REF-7890)
  ──YES──► Check ticket status via TicketingClient
           │
           ├─► RESOLVED / CANCELLED / CLOSED
           │       Reply: "Ticket {N} is {STATUS}, please raise a new ticket"
           │       CC: Support Engineers
           │       Save to DB: category = "ticket_closed"
           │       ──► Next email
           │
           └─► OPEN ──► Continue to classify (no auto-reply for open tickets)
        │ NO ticket found
        ▼
Classify email
  1. Try deterministic rules (keyword + sender match)
  2. If no rule matches → call OpenAI GPT for AI classification
        │
        ▼
Move email to mapped folder
        │
        ▼
Business category? (not in GENERAL_CATEGORIES)
  ──YES──► Reply: "Please raise a support ticket"
           CC: Support Engineers
  ──NO───► Reply: "Thanks for reaching out, team will respond shortly"
           CC: Support Engineers
        │
        ▼
Save to DB · Increment metrics · Append to audit log
```

---

## 4. Project Structure

```
mailbox_auto_assistant/
│
├── .env                          # Secret configuration (not committed)
├── alembic.ini                   # Alembic configuration for DB migrations
├── pyproject.toml                # Build system & pytest config
├── requirements.txt              # Python dependencies
│
├── data/
│   ├── audit_log.jsonl           # Append-only audit log (auto-created)
│   ├── mappings/
│   │   └── category_folder_map.yaml   # category → mailbox folder name
│   ├── prompts/
│   │   ├── classifier_system.txt      # OpenAI system prompt
│   │   └── classifier_fewshot.txt     # OpenAI few-shot examples
│   └── rules/
│       └── classification_rules.yaml  # Deterministic classification rules
│
├── migrations/
│   ├── env.py
│   └── versions/
│       └── 20260316_0001_create_processed_emails.py
│
├── scripts/
│   ├── bootstrap_folders.py      # Create mailbox folders from mapping
│   ├── run_once.py               # Run the pipeline once
│   ├── run_api.py                # Start the Flask dev server
│   ├── seed_rules.py             # Seed default classification rules
│   ├── check_graph_connectivity.ps1
│   └── check_graph_user_lookup.ps1
│
├── src/
│   └── app/
│       ├── main.py               # Pipeline factory & run_once entry point
│       │
│       ├── api/
│       │   └── flask_app.py      # Flask REST API
│       │
│       ├── application/
│       │   ├── pipeline.py       # Core orchestration — EmailSegregationPipeline
│       │   ├── use_cases.py      # classify_email use case
│       │   ├── prompt_builder.py # Builds the OpenAI prompt string
│       │   └── reply_builder.py  # Auto-reply message templates
│       │
│       ├── domain/
│       │   ├── models.py         # EmailMessage, Rule, ClassificationResult, TicketStatus
│       │   ├── rules_engine.py   # classify_with_rules, extract_ticket_number, is_vip_sender
│       │   └── folder_mapper.py  # category → folder name mapping
│       │
│       ├── infrastructure/
│       │   ├── ai/
│       │   │   ├── base.py           # AIClient ABC
│       │   │   └── openai_client.py  # OpenAI GPT implementation
│       │   ├── mailbox/
│       │   │   ├── base.py           # MailboxClient ABC
│       │   │   └── microsoft_graph_client.py  # MS Graph implementation
│       │   ├── ticketing/
│       │   │   ├── base.py           # TicketingClient ABC
│       │   │   └── stub_client.py    # Stub (always returns OPEN)
│       │   └── persistence/
│       │       ├── db.py             # SQLite / PostgreSQL connection & schema init
│       │       ├── models.py         # ProcessedEmailRecord dataclass
│       │       └── repository.py     # ProcessedEmailRepository (save / query)
│       │
│       ├── observability/
│       │   ├── audit_logger.py   # Append-only JSONL audit logger
│       │   └── metrics.py        # In-memory counter metrics
│       │
│       └── settings/
│           ├── config.py         # AppConfig dataclass + load_config()
│           └── logging.py        # Logging setup
│
└── tests/
    ├── conftest.py
    ├── unit/
    │   ├── test_folder_mapper.py
    │   ├── test_prompt_builder.py
    │   └── test_rules_engine.py
    └── integration/
        └── test_pipeline_flow.py
```

---

## 5. Domain Layer

### `src/app/domain/models.py`

All core data structures. All are **frozen dataclasses** (immutable).

```python
class TicketStatus(str, Enum):
    OPEN       = "open"
    RESOLVED   = "resolved"
    CANCELLED  = "cancelled"
    CLOSED     = "closed"
    NOT_FOUND  = "not_found"

@dataclass(frozen=True)
class EmailMessage:
    id: str
    subject: str
    body: str
    sender: str            # email address
    received_at: datetime
    sender_name: str = ""  # display name (populated from Graph API)

@dataclass(frozen=True)
class Rule:
    category: str
    keywords: list[str]
    sender_contains: str | None = None  # optional sender filter

@dataclass(frozen=True)
class ClassificationResult:
    email_id: str
    category: str
    reason: str
```

---

### `src/app/domain/rules_engine.py`

Pure functions — no external dependencies.

| Function | Signature | Description |
|---|---|---|
| `classify_with_rules` | `(email, rules) → (category \| None, reason)` | Iterates rules; checks sender + keywords in subject+body |
| `extract_ticket_number` | `(email) → str \| None` | Regex `\b[A-Z]{2,8}[-_]?\d{4,10}\b` — matches `INC-12345`, `REF7890`, etc. |
| `is_vip_sender` | `(email, vip_titles) → bool` | Checks if `sender_name` OR email `body` contains any VIP title (Director, VP, Chief, CEO, etc.) |

**Ticket number pattern examples that match:**
- `INC-12345`, `REF-7890`, `TICKET12345`, `SR-100001`, `JIRA-9999`

---

### `src/app/domain/folder_mapper.py`

```python
class FolderMapper:
    def __init__(self, mapping: dict[str, str], default_folder: str = "General")
    def to_folder(self, category: str) -> str  # case-insensitive lookup → default on miss
```

---

## 6. Application Layer

### `src/app/application/pipeline.py` — `EmailSegregationPipeline`

The central orchestrator. Injected with all infrastructure clients at construction time.

**Constructor parameters:**

| Parameter | Type | Description |
|---|---|---|
| `mailbox_client` | `MailboxClient` | Reads/moves/replies to emails |
| `ai_client` | `AIClient` | AI classification fallback |
| `repository` | `ProcessedEmailRepository` | Deduplication & persistence |
| `folder_mapper` | `FolderMapper` | Category → folder |
| `rules` | `list[Rule]` | Deterministic classification rules |
| `metrics` | `Metrics` | In-memory counters |
| `audit_logger` | `AuditLogger` | JSONL audit trail |
| `system_prompt` | `str` | OpenAI system message |
| `fewshot_prompt` | `str` | OpenAI few-shot examples |
| `ticketing_client` | `TicketingClient \| None` | Ticket status lookups |
| `support_engineer_emails` | `list[str] \| None` | CC recipients on auto-replies |
| `escalation_email` | `str \| None` | Contact for VIP escalation review |
| `vip_titles` | `list[str] \| None` | Title strings that trigger VIP path |
| `general_categories` | `list[str] \| None` | Categories treated as non-business |

**Public methods:**

```python
def fetch_unread(self, limit: int = 25) -> list[EmailMessage]
def process_unread_emails(self, limit: int = 25) -> int  # returns count processed
```

**Metrics tracked:**

| Key | Incremented when |
|---|---|
| `emails_processed` | Email classified and moved normally |
| `emails_vip_escalated` | Sender is a VIP title |
| `emails_ticket_closed_reply` | Referenced ticket is Resolved/Cancelled/Closed |

---

### `src/app/application/use_cases.py`

```python
def classify_email(email, rules, ai_client, system_prompt, fewshot_prompt) -> ClassificationResult
```

1. Try `classify_with_rules()` — if match, return immediately (no AI call)
2. Build prompt via `build_classifier_prompt()`
3. Call `ai_client.classify_email()` → parse JSON `{category, reason}`

---

### `src/app/application/prompt_builder.py`

```python
def build_classifier_prompt(system_prompt, fewshot_prompt, subject, body) -> str
```

Concatenates the system prompt, few-shot examples, and the email content into a single string sent to OpenAI. Expected JSON response: `{"category": "...", "reason": "..."}`.

---

### `src/app/application/reply_builder.py`

Three template functions — no external dependencies.

| Function | Triggered when | CC Support? |
|---|---|---|
| `build_open_ticket_reply(subject)` | Business email with no ticket number | Yes |
| `build_general_query_reply()` | Non-business / general email | Yes |
| `build_closed_ticket_reply(ticket_number, status)` | Ticket is Resolved/Cancelled/Closed | Yes |

---

## 7. Infrastructure Layer

### Mailbox — `src/app/infrastructure/mailbox/`

#### `base.py` — `MailboxClient` (ABC)

```python
class MailboxClient(ABC):
    def fetch_unread(self, limit: int = 25) -> list[EmailMessage]: ...
    def move_email(self, email_id: str, folder_name: str) -> None: ...
    def reply_email(self, email_id: str, body: str, cc_addresses: list[str] | None = None) -> None: ...
    def create_folders(self, folders: list[str]) -> None: ...
```

#### `microsoft_graph_client.py` — `MicrosoftGraphMailboxClient`

Communicates with **Microsoft Graph API v1.0** using Resource Owner Password Credentials (ROPC) OAuth2 flow.

**Key behaviours:**

- Token is cached and auto-refreshed 60 seconds before expiry
- When `GRAPH_*` env vars are **not** all set → operates in **local fallback mode** (prints actions, returns in-memory email list)
- `fetch_unread` — calls `/users/{user}/mailFolders/inbox/messages?$filter=isRead eq false`
- `move_email` — calls `/users/{user}/messages/{id}/move`
- `reply_email` — calls `/users/{user}/messages/{id}/reply` with CC recipients
- `create_folders` / `_ensure_folder` — creates a folder if it doesn't already exist

**Graph permissions required:**
- `Mail.ReadWrite`
- `MailboxSettings.Read`

---

### AI — `src/app/infrastructure/ai/`

#### `base.py` — `AIClient` (ABC)

```python
class AIClient(ABC):
    def classify_email(self, email: EmailMessage, prompt: str) -> tuple[str, str]: ...
    # Returns: (category, reason)
```

#### `openai_client.py` — `OpenAIClient`

- Model: `gpt-4o-mini` (configurable via constructor)
- Uses `response_format={"type": "json_object"}` for structured output
- `temperature=0`, `max_tokens=100`
- Falls back to `("general", "AI fallback: ...")` on any exception

---

### Ticketing — `src/app/infrastructure/ticketing/`

#### `base.py` — `TicketingClient` (ABC)

```python
class TicketingClient(ABC):
    def get_ticket_status(self, ticket_number: str) -> TicketStatus: ...
```

#### `stub_client.py` — `StubTicketingClient`

Always returns `TicketStatus.OPEN`. **Replace** with a real ServiceNow / Jira / Zendesk implementation by subclassing `TicketingClient` and wiring it in `main.py`.

---

### Persistence — `src/app/infrastructure/persistence/`

#### `db.py`

```python
def get_connection(database_url: str) -> Any  # SQLite or PostgreSQL connection
def init_schema(conn: Any) -> None            # Creates processed_emails table if not exists
```

Supports:
- `sqlite:///path/to/file.db` — default, zero-config
- `postgresql://user:pass@host:port/dbname`

#### `repository.py` — `ProcessedEmailRepository`

```python
def save(email_id, category, folder, reason) -> None    # UPSERT
def list_processed_ids() -> set[str]                    # Used for deduplication
def all() -> list[ProcessedEmailRecord]                 # Full history
```

**Database table: `processed_emails`**

| Column | Type | Description |
|---|---|---|
| `email_id` | TEXT PK | Microsoft Graph message ID |
| `category` | TEXT | Assigned category |
| `folder` | TEXT | Destination folder name |
| `reason` | TEXT | Classification reason |
| `processed_at` | TEXT | UTC ISO-8601 timestamp |

---

## 8. Observability

### `src/app/observability/audit_logger.py` — `AuditLogger`

Appends one JSON line per event to `data/audit_log.jsonl`.

**Event fields vary by action type:**

```jsonc
// Normal email
{"email_id": "...", "category": "finance", "folder": "Finance", "reason": "Matched keyword 'invoice'", "action": "replied:open_ticket_request"}

// VIP escalation
{"email_id": "...", "action": "vip_escalation", "sender": "...", "sender_name": "John Director", "subject": "...", "note": "Requires discussion with: mahes@company.com"}

// Closed ticket
{"email_id": "...", "action": "replied:ticket_INC-123_is_resolved", "ticket_number": "INC-123", "ticket_status": "resolved"}
```

---

### `src/app/observability/metrics.py` — `Metrics`

In-memory `Counter`. Call `metrics.snapshot()` to read current counts.

```python
metrics.increment("emails_processed")
metrics.snapshot()  # → {"emails_processed": 5, ...}
```

---

## 9. Settings & Configuration

### `src/app/settings/config.py`

All configuration is loaded from environment variables (or `.env` file at the project root).

```python
@dataclass(frozen=True)
class AppConfig:
    app_env: str
    log_level: str
    database_url: str
    audit_log_path: Path
    prompts_dir: Path
    rules_path: Path
    mapping_path: Path
    openai_api_key: str | None
    graph_tenant_id: str | None
    graph_client_id: str | None
    graph_client_secret: str | None
    graph_mailbox_user: str | None
    graph_mailbox_password: str | None
    graph_timeout_seconds: int
    support_engineer_emails: list[str]
    escalation_email: str | None
    vip_titles: list[str]
    general_categories: list[str]
```

---

## 10. API Layer (Flask)

Start with: `python scripts/run_api.py` → listens on `http://0.0.0.0:5000`

### Endpoints

#### `GET /health`
```json
{"status": "ok", "env": "dev"}
```

#### `GET /api/v1/emails?limit=25`
Returns unread emails from the mailbox.
```json
[
  {
    "id": "AAMk...",
    "subject": "Invoice pending",
    "sender": "billing@vendor.com",
    "received_at": "2026-03-24T10:00:00+00:00",
    "body": "Please pay this invoice..."
  }
]
```

#### `POST /api/v1/process?limit=25`
Triggers a full pipeline run.
```json
{"processed": 3}
```

---

## 11. Data Files

### `data/rules/classification_rules.yaml`

```yaml
rules:
  - category: finance
    keywords: [invoice, payment, reimbursement]
    sender_contains: billing      # optional — sender address must contain this

  - category: internal
    keywords: [meeting, lunch, standup]

  - category: marketing
    keywords: [offer, sale, discount]

  - category: bot
    keywords: [noreply, fsprod, unx, appsrv, websrv, service]
```

- Rules are evaluated **in order**; the first match wins.
- `sender_contains` is checked against the sender's email address (case-insensitive).
- Keywords are matched against `subject + body` (case-insensitive).

---

### `data/mappings/category_folder_map.yaml`

```yaml
mapping:
  finance: Finance
  internal: Internal
  marketing: Promotions
  bot: Alerts
  general: General
```

Categories not listed here fall back to the `default_folder` (hardcoded as `"General"` in `main.py`).

---

### `data/prompts/classifier_system.txt`

The OpenAI system prompt. Defines the classification task and available categories.

### `data/prompts/classifier_fewshot.txt`

Few-shot examples provided to OpenAI to guide consistent JSON responses.

---

## 12. Database

### PostgreSQL

1. Set `DATABASE_URL=postgresql://user:pass@host:5432/dbname` in `.env`
2. Run migrations: `alembic upgrade head`

### Alembic migration: `migrations/versions/20260316_0001_create_processed_emails.py`

Creates the `processed_emails` table. Schema matches what `init_schema()` also creates directly (both paths are safe to use).

---

## 13. Scripts

| Script | Command | Description |
|---|---|---|
| `run_once.py` | `python scripts/run_once.py` | Run the pipeline once and exit |
| `run_api.py` | `python scripts/run_api.py` | Start Flask dev server on port 5000 |
| `bootstrap_folders.py` | `python scripts/bootstrap_folders.py` | Create all folders in mailbox from mapping file |
| `seed_rules.py` | `python scripts/seed_rules.py` | Write default `classification_rules.yaml` if empty |
| `check_graph_connectivity.ps1` | PowerShell | Test Microsoft Graph token acquisition |
| `check_graph_user_lookup.ps1` | PowerShell | Verify mailbox user can be resolved via Graph |

---

## 14. Tests

Run all tests:
```bash
pytest -q
```

### Test files

| File | What it tests |
|---|---|
| `tests/unit/test_rules_engine.py` | Keyword + sender matching; no-match path |
| `tests/unit/test_folder_mapper.py` | Case-insensitive lookup; default fallback |
| `tests/unit/test_prompt_builder.py` | Prompt string construction |
| `tests/integration/test_pipeline_flow.py` | Full pipeline run with stubs; DB persistence; folder move |

### Stubs used in tests

```python
class StubAIClient(AIClient):
    def classify_email(self, email, prompt) -> tuple[str, str]:
        return "general", "stub ai"

class StubMailboxClient(MailboxClient):
    def fetch_unread(self, limit=25) -> list[EmailMessage]: ...
    def move_email(self, email_id, folder_name) -> None: ...
    def reply_email(self, email_id, body, cc_addresses=None) -> None: ...
    def create_folders(self, folders) -> None: ...
```

---

## 15. Setup & Quick Start

### 1. Create virtual environment

```bash
python -m venv venv
venv\Scripts\activate        # Windows
# source venv/bin/activate   # Linux/macOS
pip install -r requirements.txt
```

### 2. Create `.env` file

```env
# App
APP_ENV=dev
LOG_LEVEL=INFO

# Database (SQLite default — no setup needed)
DATABASE_URL=sqlite:///data/email_segregation.db

# Microsoft Graph (leave blank to use local fallback mode)
MAILBOX_PROVIDER=graph
GRAPH_TENANT_ID=<your-tenant-id>
GRAPH_CLIENT_ID=<your-client-id>
GRAPH_CLIENT_SECRET=<your-client-secret>
GRAPH_MAILBOX_USER=support@yourdomain.com
GRAPH_MAILBOX_PASSWORD=<mailbox-password>
GRAPH_TIMEOUT_SECONDS=20

# OpenAI
OPENAI_API_KEY=sk-...

# Smart reply configuration
SUPPORT_ENGINEER_EMAILS=eng1@company.com,eng2@company.com
ESCALATION_EMAIL=mahes@company.com
VIP_TITLES=Director,VP,Vice President,Chief,CTO,CEO,COO,CFO,SVP,EVP
GENERAL_CATEGORIES=general,marketing,newsletter,junk
```

### 3. Seed rules & bootstrap folders

```bash
python scripts/seed_rules.py       # creates data/rules/classification_rules.yaml
python scripts/bootstrap_folders.py  # creates mailbox folders
```

### 4. Run the pipeline

```bash
# One-shot run
python scripts/run_once.py

# Or as a REST API
python scripts/run_api.py
```

---

## 16. Environment Variables Reference

| Variable | Default | Description |
|---|---|---|
| `APP_ENV` | `dev` | Environment name (dev/staging/prod) |
| `LOG_LEVEL` | `INFO` | Python logging level |
| `DATABASE_URL` | `sqlite:///data/email_segregation.db` | DB connection string |
| `AUDIT_LOG_PATH` | `data/audit_log.jsonl` | Path to audit log file |
| `PROMPTS_DIR` | `data/prompts` | Directory containing prompt text files |
| `RULES_PATH` | `data/rules/classification_rules.yaml` | Classification rules file |
| `MAPPING_PATH` | `data/mappings/category_folder_map.yaml` | Category → folder map |
| `OPENAI_API_KEY` | — | OpenAI API key |
| `GRAPH_TENANT_ID` | — | Azure AD tenant ID |
| `GRAPH_CLIENT_ID` | — | Azure app registration client ID |
| `GRAPH_CLIENT_SECRET` | — | Azure app registration client secret |
| `GRAPH_MAILBOX_USER` | — | Mailbox UPN (e.g. `support@contoso.com`) |
| `GRAPH_MAILBOX_PASSWORD` | — | Mailbox account password (ROPC flow) |
| `GRAPH_TIMEOUT_SECONDS` | `20` | HTTP timeout for Graph API calls |
| `SUPPORT_ENGINEER_EMAILS` | _(empty)_ | Comma-separated CC list for auto-replies |
| `ESCALATION_EMAIL` | — | Contact email shown in VIP escalation logs |
| `VIP_TITLES` | `Director,VP,Vice President,Chief,CTO,CEO,COO,CFO,SVP,EVP` | Comma-separated VIP title keywords |
| `GENERAL_CATEGORIES` | `general,marketing,newsletter,junk` | Categories treated as non-business |

---

## 17. Extending the Application

### Plug in a real ticketing system (ServiceNow, Jira, Zendesk)

```python
# src/app/infrastructure/ticketing/servicenow_client.py
from app.domain.models import TicketStatus
from app.infrastructure.ticketing.base import TicketingClient

class ServiceNowClient(TicketingClient):
    def __init__(self, base_url: str, username: str, password: str) -> None:
        ...

    def get_ticket_status(self, ticket_number: str) -> TicketStatus:
        # Call ServiceNow REST API
        ...
```

Then in `src/app/main.py` replace `StubTicketingClient()` with `ServiceNowClient(...)`.

---

### Add a new mailbox provider

Subclass `MailboxClient` and implement `fetch_unread`, `move_email`, `reply_email`, and `create_folders`.

---

### Add new classification rules

Edit `data/rules/classification_rules.yaml`:

```yaml
- category: hr
  keywords:
    - onboarding
    - leave request
    - performance review
```

Add the folder mapping in `data/mappings/category_folder_map.yaml`:

```yaml
hr: HR
```

---

### Customize auto-reply templates

Edit the functions in `src/app/application/reply_builder.py`. The three functions are:
- `build_open_ticket_reply(subject)` — business query, no ticket
- `build_general_query_reply()` — non-business query
- `build_closed_ticket_reply(ticket_number, status)` — ticket is terminal


### Test Coverage Summary
All tests are located in test_pipeline_flow.py and all 6 tests pass:

Bot/auto-notification by sender pattern
Sender: no-reply@monitoring.internal
To: gsrt@ihg.com
CC:
Subject: Daily infrastructure health report
Body: Automated alert digest for last 24 hours.
Expected: No response, no move action branch, audit action no_action:auto_notification

Bot/auto-notification by keyword in content
Sender: ops.team@ihg.com
To: gsrt@ihg.com
CC:
Subject: Nightly Appsrv status update
Body: Appsrv checks completed successfully for all nodes.
Expected: No response, no action, marked bot path

VIP escalation by sender_name
Sender: jane.doe@ihg.com
To: gsrt@ihg.com
CC:
Subject: Urgent review needed for client escalation
Body: Please prioritize this request.
Sender_name: Jane Doe, VP Engineering
Expected: VIP escalation path, no auto-reply, no folder move

VIP escalation by signature in body
Sender: michael.lee@ihg.com
To: gsrt@ihg.com
CC:
Subject: Request for billing exception
Body: Please review this request. Regards, Michael Lee, Director Finance
Expected: VIP escalation path, no auto-reply, no folder move

ServiceNow-threaded email, missing both INC and ADH
Sender: user.one@client.com
To: gsrt@ihg.com
CC: ihg@servicenow.com
Subject: Need support on previous issue
Body: Please help with my request urgently.
Expected: Reply No Ticket Found Please Create One, then end

ServiceNow-threaded email, INC present but ADH missing
Sender: user.two@client.com
To: ihg@servicenow.com
CC: gsrt@ihg.com
Subject: Follow-up for INC7050808
Body: Kindly review this case.
Expected: Reply No Ticket Found Please Create One, then end

ServiceNow-threaded email, ADH present but INC missing
Sender: user.three@client.com
To: gsrt@ihg.com
CC: ihg@servicenow.com
Subject: Query for ADH123456
Body: Please check the latest update.
Expected: Reply No Ticket Found Please Create One, then end

ServiceNow-threaded email, INC and ADH present, terminal incident state
Sender: user.four@client.com
To: ihg@servicenow.com
CC: gsrt@ihg.com
Subject: Re: INC7050808 and ADH123456
Body: Please reopen this request.
Expected: If ServiceNow returns resolved/cancelled/closed, reply Ticket already resolved please raise new request, then end

ServiceNow-threaded email, INC and ADH present, active incident state
Sender: user.five@client.com
To: gsrt@ihg.com
CC: ihg@servicenow.com
Subject: Re: INC7050808 with ADH123456
Body: Additional details attached for investigation.
Expected: If ServiceNow returns new/in_progress/on_hold, reply We are reviewing your query and CC support engineer emails

ServiceNow-threaded email, INC and ADH present, incident not found
Sender: user.six@client.com
To: gsrt@ihg.com
CC: ihg@servicenow.com
Subject: Check INC9999999 and ADH123456
Body: Could you verify this ticket status?
Expected: Reply No Ticket Found Please Create One

Not a ServiceNow-threaded email, normal user mail
Sender: employee@ihg.com
To: gsrt@ihg.com
CC:
Subject: Invoice mismatch for training booking
Body: Hotel says this training should not be billed.
Expected: Goes through classification flow and existing non-ServiceNow handling

ServiceNow address present in To list (your recent requirement validation)
Sender: akshaya.rajkumar@accenture.com
To: ihg@servicenow.com, gsrt@ihg.com
CC:
Subject: BKKLS Invoice P600929886 - INC7050808 - ADH123456
Body: Please verify billing and update case.
Expected: ServiceNow-threaded branch should trigger because ihg@servicenow.com is in recipients

Quick note for realistic testing:

INC format must be like INC followed by 5 to 15 digits.
ADH format must be like ADH followed by 5 to 15 digits.
Your bot keywords currently include fsprod, unx, fsprd, appsrv, websrvservice.
If you want, I can generate these same 12 as ready Python EmailMessage objects so you can paste directly into microsoft_graph_client.py test fallback list.
GPT-5.3-Codex • 0.9x

### **What Happens**

Run:
```bash
python scripts/run_once.py
```

**Expected Results:**

1. ✅ Email is **NOT** auto-replied
2. ✅ Email stays in **Inbox** (not moved)
3. ✅ Audit log shows: `"action": "vip_escalation"`
4. ✅ Escalation contact (`sono.pathak@ihg.com`) is notified
5. ✅ Database entry: `category = "escalation"`

**Check audit log:**
```bash
tail -f data/audit_log.jsonl
```

Output:
```json
{
  "email_id": "AAMk...",
  "action": "vip_escalation",
  "sender": "robert.johnson@yourcompany.com",
  "sender_name": "Robert Johnson",
  "subject": "Strategic Decision Required",
  "note": "Requires discussion with: sono.pathak@ihg.com"
}
```

### **VIP Titles Configured**

By default (configured in `.env`):
```
VIP_TITLES=Director,VP,Vice President,Chief,CTO,CEO,COO,CFO,SVP,EVP
```

Any email body or sender name containing these keywords (case-insensitive) triggers VIP escalation.

### **Test Cases for VIP Detection**

Run unit tests:
```bash
pytest tests/unit/test_rules_engine.py -v -k vip
```

Tests verify VIP detection in:
- ✅ Sender display names
- ✅ Email body signatures
- ✅ Case-insensitive matching
- ✅ Multiple VIP title patterns
- ✅ Non-VIP emails don't trigger

**Example test scenarios:**
- VIP sender with "VP of Operations" in display name → Escalated
- Regular email with "VP Engineering" in signature → Escalated  
- Email with "director" in body (lowercase) → Escalated
- Regular support email with no VIP title → NOT escalated




What is now implemented

Recipient present + Ref Msg present + INC in New/WIP/On-Hold -> No action required
Implemented in pipeline.py:171.

Recipient present + Ref Msg missing + INC in New/WIP/On-Hold -> Send support notification + add comment to incident
Implemented in pipeline.py:201 and pipeline.py:209.

Recipient missing + Ref Msg present + INC in New/WIP/On-Hold -> Send support notification + add comment to incident
Implemented by the same matrix branch in pipeline.py:163 with condition checks from pipeline.py:126.

Recipient missing + Ref Msg missing + INC in New/WIP/On-Hold -> Send support notification + add comment to incident
Implemented by the same matrix branch in pipeline.py:163.

ServiceNow add comment API implementation

Added PATCH-based comment update method in base.py:76.
Body sent exactly as requested:
comments = mail body
work_notes = mail body
Ref Msg extraction

Added Ref Msg extractor in rules_engine.py:72.
Support notification with attachment

Added mailbox abstraction method in base.py:32.
Implemented Graph sendMail + optional text attachment in microsoft_graph_client.py:355.
Attachment currently contains sender, subject, and body of the user query as a text file.
Tests added/updated