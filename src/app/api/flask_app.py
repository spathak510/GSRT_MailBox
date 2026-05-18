from __future__ import annotations

import logging
import threading
import time
import pythoncom
import win32com.client
from datetime import datetime, timezone

from flask import Flask, jsonify, request

from app.infrastructure.mailbox.microsoft_graph_client import MicrosoftGraphMailboxClient
from app.main import build_pipeline
from app.settings.config import load_config

logger = logging.getLogger(__name__)


def create_app() -> Flask:
    app = Flask(__name__)
    cfg = load_config()
    pipeline = build_pipeline()

    # ------------------------------------------------------------------ #
    # Background Poller (Option A)                                        #
    # Polls the mailbox every WORKER_INTERVAL_SECONDS seconds.            #
    # Enabled whenever WORKER_INTERVAL_SECONDS > 0 (default: 60).        #
    # ------------------------------------------------------------------ #

    _poller_state: dict = {
        "running": False,
        "last_run": None,
        "last_processed": 0,
        "run_count": 0,
        "thread": None,
    }

    def _poll_loop(interval: int) -> None:
        logger.info("Background poller started — interval=%ds", interval)
        _poller_state["running"] = True
        while _poller_state["running"]:
            try:
                count = pipeline.process_unread_emails()
                _poller_state["last_run"] = datetime.now(timezone.utc).isoformat()
                _poller_state["last_processed"] = count
                _poller_state["run_count"] += 1
                if count:
                    logger.info("Poller: processed %d email(s)", count.get('processed_count',0))
            except Exception as exc:
                logger.error("Poller error: %s", exc)
            time.sleep(interval)

    if cfg.worker_interval_seconds > 0:
        t = threading.Thread(
            target=_poll_loop,
            args=(cfg.worker_interval_seconds,),
            daemon=True,  # dies when Flask process exits
            name="mailbox-poller",
        )
        t.start()
        _poller_state["thread"] = t
        logger.info(
            "Mailbox auto-processing enabled — polling every %ds",
            cfg.worker_interval_seconds,
        )

    # ------------------------------------------------------------------ #
    # Graph Webhook Subscription (Option B)                               #
    # Registered on startup when WEBHOOK_BASE_URL is configured.          #
    # Graph will POST to /api/v1/notifications when new mail arrives.     #
    # ------------------------------------------------------------------ #

    _webhook_state: dict = {
        "subscription_id": None,
        "expires_at": None,
        "registered": False,
        "error": None,
    }

    if cfg.webhook_base_url and isinstance(pipeline._mailbox_client, MicrosoftGraphMailboxClient):
        try:
            notification_url = f"{cfg.webhook_base_url.rstrip('/')}/api/v1/notifications"
            sub = pipeline._mailbox_client.register_webhook_subscription(
                notification_url=notification_url,
                client_state=cfg.webhook_client_state,
            )
            _webhook_state.update({
                "subscription_id": sub.get("id"),
                "expires_at": sub.get("expirationDateTime"),
                "registered": True,
                "error": None,
            })
            logger.info(
                "Graph webhook registered — subscription_id=%s notification_url=%s",
                sub.get("id"),
                notification_url,
            )
        except Exception as exc:
            _webhook_state["error"] = str(exc)
            logger.error("Graph webhook registration failed: %s", exc)

    # ------------------------------------------------------------------ #
    # Health                                                               #
    # ------------------------------------------------------------------ #

    @app.get("/health")
    def health():
        return jsonify({
            "status": "ok",
            "env": cfg.app_env,
            "poller": {
                "enabled": cfg.worker_interval_seconds > 0,
                "interval_seconds": cfg.worker_interval_seconds,
                "last_run": _poller_state["last_run"],
                "run_count": _poller_state["run_count"],
            },
            "webhook": {
                "enabled": _webhook_state["registered"],
                "subscription_id": _webhook_state["subscription_id"],
                "expires_at": _webhook_state["expires_at"],
                "error": _webhook_state["error"],
            },
        })

    # ------------------------------------------------------------------ #
    # Emails                                                               #
    # ------------------------------------------------------------------ #

    @app.get("/api/v1/emails")
    def list_emails():
        """Return unread emails from the mailbox (or local fallback)."""
        print("hello")
        limit = request.args.get("limit", 25, type=int)
        emails = pipeline.fetch_unread(limit=limit)
        return jsonify(
            [
                {
                    "id": e.id,
                    "subject": e.subject,
                    "sender": e.sender,
                    "received_at": e.received_at.isoformat(),
                    "body": e.body,
                }
                for e in emails
            ]
        )

    # ------------------------------------------------------------------ #
    # Manual Processing                                                    #
    # ------------------------------------------------------------------ #

    @app.post("/api/v1/process")
    def process_emails():
        """Trigger an immediate pipeline run (independent of poller/webhook)."""
        limit = request.args.get("limit", 25, type=int)
        processed = pipeline.process_unread_emails(limit=limit)
        return jsonify({"processed": processed}), 200
    


    # ------------------------------------------------------------------ #
    # Read  Senders Title                                                  #
    # ------------------------------------------------------------------ #
    @app.get("/api/v1/sender-titles")
    def get_user_details():
        FullName = ""
        JobTitle = ""
        Department = ""
        Company = ""

        try:
            email_id = request.args.get("email_id", type=str)
            pythoncom.CoInitialize()
            outlookApp = win32com.client.Dispatch("Outlook.Application")
            session = outlookApp.Session

            recipient = session.CreateRecipient(email_id)

            if recipient.Resolve():
                try:
                    address_entry = recipient.AddressEntry
                    user = address_entry.GetExchangeUser()

                    if user is not None:
                        FullName = user.Name
                        JobTitle = user.JobTitle
                        Department = user.Department
                        Company = user.CompanyName

                except Exception:
                    # Equivalent to "On Error Resume Next"
                    pass

        except Exception as e:
            print("Error:", e)

        return jsonify({
            "FullName": FullName,
            "JobTitle": JobTitle,
            "Department": Department,
            "Company": Company
        })

    # ------------------------------------------------------------------ #
    # Graph Webhook — Notification Receiver (Option B)                    #
    # ------------------------------------------------------------------ #

    @app.route("/api/v1/notifications", methods=["GET", "POST"])
    def graph_notifications():
        """
        Endpoint called by Microsoft Graph when new inbox messages arrive.

        GET  — Graph webhook validation: echo back the validationToken.
        POST — Graph notification payload: trigger the pipeline immediately.

        Security: validates the `clientState` field in each notification
        against WEBHOOK_CLIENT_STATE to reject spoofed requests.
        """
        # Validation handshake (Graph sends GET with ?validationToken=...)
        validation_token = request.args.get("validationToken")
        if validation_token:
            logger.info("Graph webhook validation handshake received")
            return validation_token, 200, {"Content-Type": "text/plain"}

        # Notification payload
        data = request.get_json(silent=True) or {}
        notifications = data.get("value", [])

        for notification in notifications:
            # Validate clientState to reject spoofed/third-party requests
            if notification.get("clientState") != cfg.webhook_client_state:
                logger.warning(
                    "Webhook notification rejected — clientState mismatch"
                )
                continue

            resource = notification.get("resource", "")
            logger.info("Graph webhook notification received — resource=%s", resource)

            # Trigger pipeline immediately in a background thread so Graph
            # receives 202 within its 30-second timeout window
            threading.Thread(
                target=_run_pipeline_safe,
                daemon=True,
                name="webhook-trigger",
            ).start()
            break  # one trigger per batch is enough (pipeline processes all unread)

        return "", 202

    def _run_pipeline_safe() -> None:
        try:
            count = pipeline.process_unread_emails()
            logger.info("Webhook-triggered pipeline run: processed %d email(s)", count)
        except Exception as exc:
            logger.error("Webhook-triggered pipeline error: %s", exc)

    # ------------------------------------------------------------------ #
    # Webhook Management                                                   #
    # ------------------------------------------------------------------ #

    @app.post("/api/v1/webhook/register")
    def register_webhook():
        """Manually register or re-register the Graph webhook subscription."""
        if not cfg.webhook_base_url:
            return jsonify({"error": "WEBHOOK_BASE_URL is not configured in .env"}), 400
        if not isinstance(pipeline._mailbox_client, MicrosoftGraphMailboxClient):
            return jsonify({"error": "Webhook requires MAILBOX_PROVIDER=graph"}), 400
        try:
            notification_url = f"{cfg.webhook_base_url.rstrip('/')}/api/v1/notifications"
            sub = pipeline._mailbox_client.register_webhook_subscription(
                notification_url=notification_url,
                client_state=cfg.webhook_client_state,
            )
            _webhook_state.update({
                "subscription_id": sub.get("id"),
                "expires_at": sub.get("expirationDateTime"),
                "registered": True,
                "error": None,
            })
            return jsonify(sub), 201
        except Exception as exc:
            return jsonify({"error": str(exc)}), 500

    @app.post("/api/v1/webhook/renew")
    def renew_webhook():
        """Renew the active Graph webhook subscription before it expires."""
        sub_id = _webhook_state.get("subscription_id")
        if not sub_id:
            return jsonify({"error": "No active subscription. POST /api/v1/webhook/register first."}), 400
        if not isinstance(pipeline._mailbox_client, MicrosoftGraphMailboxClient):
            return jsonify({"error": "Webhook requires MAILBOX_PROVIDER=graph"}), 400
        try:
            sub = pipeline._mailbox_client.renew_webhook_subscription(
                subscription_id=sub_id,
                client_state=cfg.webhook_client_state,
            )
            _webhook_state["expires_at"] = sub.get("expirationDateTime")
            return jsonify(sub), 200
        except Exception as exc:
            return jsonify({"error": str(exc)}), 500

    @app.get("/api/v1/webhook/status")
    def webhook_status():
        """Return current webhook subscription status."""
        return jsonify(_webhook_state), 200

    return app

