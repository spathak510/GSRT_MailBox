# Email Segregation Assistant

A production-ready starter template for classifying mailbox emails into folders using:

- deterministic rules
- optional AI classification fallback
- folder mapping
- persistence and audit logs

## Project Layout

The project follows a layered architecture under `src/app`:

- `domain`: pure business logic
- `application`: orchestration and use-cases
- `infrastructure`: mailbox, AI, and DB adapters
- `observability`: metrics and audit logs

## Quick Start

1. Create a virtual environment and install dependencies.
2. Update values in `.env`.
3. Run setup scripts:
   - `python scripts/bootstrap_folders.py`
   - `python scripts/seed_rules.py`
4. Run once:
   - `python scripts/run_once.py`

## Microsoft Graph Setup (Client ID + Secret)

To use a real Microsoft 365 mailbox, configure Microsoft Graph app-only auth:

1. Register an app in Azure AD (Microsoft Entra ID).
2. Create a client secret for the app.
3. Add Graph Application permissions:
   - `Mail.ReadWrite`
   - `MailboxSettings.Read`
4. Grant admin consent.
5. Set these values in `.env`:

```env
MAILBOX_PROVIDER=graph
GRAPH_TENANT_ID=<tenant-id>
GRAPH_CLIENT_ID=<client-id>
GRAPH_CLIENT_SECRET=<client-secret>
GRAPH_MAILBOX_USER=<user@yourdomain.com>
GRAPH_TIMEOUT_SECONDS=20
```

When these values are provided, the pipeline reads unread emails from Inbox and moves them to mapped folders using Microsoft Graph.

## Testing

Run:

```bash
pytest -q
```

## Notes

- The included mailbox and AI clients are safe starter implementations and can be replaced with real providers.
- SQLite is used by default (`data/email_segregation.db`).
- PostgreSQL is supported via `DATABASE_URL=postgresql://...`.

## PostgreSQL Setup

1. Install dependencies:

```bash
pip install -r requirements.txt
```

2. Set `DATABASE_URL` in `.env`:

```env
DATABASE_URL=postgresql://postgres:postgres@localhost:5432/email_segregation
```

3. Run once:

```bash
python scripts/run_once.py
```
