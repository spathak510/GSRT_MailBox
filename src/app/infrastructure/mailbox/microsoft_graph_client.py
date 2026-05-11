from __future__ import annotations

import base64
import json
import logging
from datetime import datetime, timedelta, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

logger = logging.getLogger(__name__)

from app.domain.models import EmailMessage
from app.infrastructure.mailbox.base import MailboxClient


class MicrosoftGraphMailboxClient(MailboxClient):
    """Mailbox client backed by Microsoft Graph with safe local fallback."""

    def __init__(
        self,
        tenant_id: str | None = None,
        client_id: str | None = None,
        client_secret: str | None = None,
        mailbox_user: str | None = None,
        mailbox_password: str | None = None,
        timeout_seconds: int = 20,
    ) -> None:
        self._tenant_id = tenant_id
        self._client_id = client_id
        self._client_secret = client_secret
        self._mailbox_user = mailbox_user
        self._mailbox_password = mailbox_password
        self._timeout_seconds = timeout_seconds
        self._token: str | None = None
        self._token_expires_at: datetime | None = None

        self._graph_enabled = all(
            [self._tenant_id, self._client_id, self._client_secret, self._mailbox_user, self._mailbox_password]
        )
        # self._graph_enabled = False  # Temporarily disabled — using local fallback
        self._job_title_cache: dict[str, str | None] = {}  # sender_email → jobTitle (per-run cache)
        self._job_title_lookup_available = True  # set False on first 403 (missing User.ReadBasic.All)

        self._emails = [
            # ── Rule-matched emails (keywords: invoice/payment/reimbursement/billing,
            #    meeting/lunch/standup, offer/sale/discount) ──────────────────────────
            # EmailMessage(
            #     id="1",
            #     subject="Invoice for March",
            #     body="Please process payment for invoice INV-3021 by end of month.",
            #     sender="billing@vendor.com",
            #     received_at=datetime.now(timezone.utc),
            # ),
            # EmailMessage(
            #     id="2",
            #     subject="Team lunch tomorrow",
            #     body="Hey team, let us meet in the cafeteria at 1 PM tomorrow for our monthly lunch.",
            #     sender="teammate@company.com",
            #     received_at=datetime.now(timezone.utc),
            # ),
            # EmailMessage(
            #     id="3",
            #     subject="Reimbursement request approved",
            #     body="Your reimbursement request for $128 (travel expenses) has been approved and will be processed this week.",
            #     sender="finance@company.com",
            #     received_at=datetime.now(timezone.utc),
            # ),
            # EmailMessage(
            #     id="4",
            #     subject="Flash sale — 40% off all plans",
            #     body="Don't miss our biggest sale of the year. Get 40% off on all subscription plans this weekend only.",
            #     sender="promo@saas-vendor.com",
            #     received_at=datetime.now(timezone.utc),
            # ),
            # EmailMessage(
            #     id="5",
            #     subject="Daily standup reminder",
            #     body="This is your automated reminder for the daily standup at 9:30 AM. Please join the call on time.",
            #     sender="bot@company.com",
            #     received_at=datetime.now(timezone.utc),
            # ),
            # EmailMessage(
            #     id="6",
            #     subject="Special offer for enterprise customers",
            #     body="As a valued enterprise customer, we are extending an exclusive offer for a 20% discount on your next renewal.",
            #     sender="sales@vendor.com",
            #     received_at=datetime.now(timezone.utc),
            # ),
            # EmailMessage(
            #     id="7",
            #     subject="Payment confirmation #TXN-9910",
            #     body="We have received your payment of $1,200 for order #TXN-9910. A receipt has been sent to your registered email.",
            #     sender="noreply@payments.com",
            #     received_at=datetime.now(timezone.utc),
            # ),
            # EmailMessage(
            #     id="8",
            #     subject="Quarterly business review meeting",
            #     body="Please join the QBR meeting on March 28th at 3 PM. Agenda and dial-in details are attached.",
            #     sender="pm@company.com",
            #     received_at=datetime.now(timezone.utc),
            # ),
            # EmailMessage(
            #     id="9",
            #     subject="Vendor invoice INV-7742",
            #     body="Attached is invoice INV-7742 for consulting services rendered in February. Payment terms: net 30.",
            #     sender="accounts@consultancy.com",
            #     received_at=datetime.now(timezone.utc),
            # ),
            # EmailMessage(
            #     id="10",
            #     subject="Limited time discount on training",
            #     body="Enroll now and receive a 30% discount on all professional certification courses until March 31st.",
            #     sender="training@edtech.com",
            #     received_at=datetime.now(timezone.utc),
            # ),
            # EmailMessage(
            #     id="11",
            #     subject="Project kickoff meeting scheduled",
            #     body="The project kickoff meeting for Project Orion is scheduled for March 20th at 10 AM in Conference Room B.",
            #     sender="manager@company.com",
            #     received_at=datetime.now(timezone.utc),
            # ),
            # EmailMessage(
            #     id="12",
            #     subject="Expense reimbursement — March batch",
            #     body="The March batch of expense reimbursements has been processed. Please check your bank account within 3 business days.",
            #     sender="payroll@company.com",
            #     received_at=datetime.now(timezone.utc),
            # ),
            # EmailMessage(
            #     id="13",
            #     subject="Summer sale preview",
            #     body="Get an early look at our summer sale catalogue. Massive discounts across electronics, apparel, and more.",
            #     sender="newsletter@retailer.com",
            #     received_at=datetime.now(timezone.utc),
            # ),
            # EmailMessage(
            #     id="14",
            #     subject="Invoice dispute — INV-4410",
            #     body="We are writing to dispute invoice INV-4410. The billed amount does not match the agreed contract price.",
            #     sender="procurement@client.com",
            #     received_at=datetime.now(timezone.utc),
            # ),
            # EmailMessage(
            #     id="15",
            #     subject="Lunch & learn: Python best practices",
            #     body="Join us for a lunch & learn session on Python best practices this Friday at noon. Food will be provided.",
            #     sender="devrel@company.com",
            #     received_at=datetime.now(timezone.utc),
            # ),

            # ── OpenAI-only emails (no keywords or billing sender match) ────────────
            # EmailMessage(
            #     id="16",
            #     subject="Urgent: Server downtime alert",
            #     body="Production server APP-01 is unreachable. Immediate action required by the on-call engineer.",
            #     sender="alerts@monitoring.internal",
            #     received_at=datetime.now(timezone.utc),
            # ),
            # EmailMessage(
            #     id="17",
            #     subject="Q1 Performance Review Schedule noreply",
            #     body="Please find attached the schedule for Q1 performance reviews. Your slot is on March 22nd at 2 PM. noreply",
            #     sender="fsprod_appsrv_noreply@ihg.com",
            #     received_at=datetime.now(timezone.utc),
            # ),
            # EmailMessage(
            #     id="18",
            #     subject="New feature request: Dark mode FSPROD",
            #     body="Customer #4821 has requested a dark mode option for the dashboard. Logging this as a feature request for the product backlog. FSPROD",
            #     sender="ihg@service.com",
            #     received_at=datetime.now(timezone.utc),
            # ),
            # EmailMessage(
            #     id="19",
            #     subject="Contract renewal reminder UNX",
            #     body="The current contract with Vendor XYZ expires on April 1st. Please initiate renewal and approvals. UNX",
            #     sender="contracts@vendor-xyz.com",
            #     received_at=datetime.now(timezone.utc),
            # ),
            # EmailMessage(
            #     id="20",
            #     subject="Weekly sprint summary Appsrv",
            #     body="Sprint 42 completed. Velocity: 34 points. 12 stories closed and 2 carried forward. Appsrv",
            #     sender="scrum-bot@company.com",
            #     received_at=datetime.now(timezone.utc),
            # ),
            # EmailMessage(
            #     id="21",
            #     subject="Security patch required Websrv",
            #     body="CVE-2026-1234 affects the current OpenSSL version. Apply the patch on all nodes before Friday. Websrv",
            #     sender="security@company.com",
            #     received_at=datetime.now(timezone.utc),
            # ),
            # EmailMessage(
            #     id="22",
            #     subject="Office closure on March 25th Service",
            #     body="The office will be closed on March 25th for a public holiday. Remote work is permitted. Service",
            #     sender="admin@company.com",
            #     received_at=datetime.now(timezone.utc),
            # ),
            # EmailMessage(
            #     id="23",
            #     subject="Purchase order PO-8801 approved noreply",
            #     body="Purchase order PO-8801 for office supplies worth $340 has been approved by finance. noreply",
            #     sender="finance@company.com",
            #     received_at=datetime.now(timezone.utc),
            # ),
            # EmailMessage(
            #     id="24",
            #     subject="Candidate interview feedback requested FSPROD",
            #     body="Please submit structured feedback for candidate Jane Doe via the ATS portal by EOD Thursday. FSPROD",
            #     sender="recruiting@company.com",
            #     received_at=datetime.now(timezone.utc),
            # ),
            # EmailMessage(
            #     id="25",
            #     subject="Onboarding checklist for new joiner UNX",
            #     body="Welcome to the team. Complete onboarding tasks including IT setup, badge activation, and compliance training. UNX",
            #     sender="hr@company.com",
            #     received_at=datetime.now(timezone.utc),
            # ),
            # EmailMessage(
            #     id="26",
            #     subject="Data backup verification report Appsrv",
            #     body="Nightly backup for March 17th completed successfully with all 14 databases verified. Appsrv",
            #     sender="ops@company.com",
            #     received_at=datetime.now(timezone.utc),
            # ),
            # EmailMessage(
            #     id="27",
            #     subject="You have been auto-checked out DDC4A.02.0017 - 3/17/2026 from 09:00 to 19:00",
            #     body="Hi Sono ,Looks like you have been auto-checked out from your seat booking, as your booked time slot has ended. You can rebook the seat if you want to continue using it.n\nBest, n\nWorkplace Experience Team",
            #     sender="mailer@spaceplanning.org.com",
            #     received_at=datetime.now(timezone.utc),
            # ),
        ]

    def fetch_unread(self, limit: int = 25) -> list[EmailMessage]:
        if not self._graph_enabled:
            return self._emails[:limit]

        mailbox = quote(self._mailbox_user or "", safe="@.-_")
        endpoint = (
            f"/users/{mailbox}/mailFolders/inbox/messages"
            f"?$filter=isRead%20eq%20false"
            "&$select=id,subject,bodyPreview,from,receivedDateTime,toRecipients,ccRecipients"
            "&$orderby=receivedDateTime%20desc"
        )
        try:
            payload = self._graph_get(endpoint)
        except RuntimeError as exc:
            self._log_graph_fallback("fetch unread emails", exc)
            return self._emails[:limit]
        values = payload.get("value", [])
        emails: list[EmailMessage] = []
        for item in values:
            sender = (
                item.get("from", {})
                .get("emailAddress", {})
                .get("address", "unknown@unknown")
            )
            sender_name = (
                item.get("from", {})
                .get("emailAddress", {})
                .get("name", "")
            )
            # Extract to and cc addresses
            to_addresses = [
                r.get("emailAddress", {}).get("address", "")
                for r in item.get("toRecipients", [])
                if r.get("emailAddress", {}).get("address")
            ]
            cc_addresses = [
                r.get("emailAddress", {}).get("address", "")
                for r in item.get("ccRecipients", [])
                if r.get("emailAddress", {}).get("address")
            ]
            # Enrich sender_name with Azure AD job title so VIP detection
            # can match titles like "VP Engineering" even when the display
            # name in the email header doesn't include a title.
            job_title = None
            try:
                request = Request(
                    f"http://127.0.0.1:5000/api/v1/sender-titles?{urlencode({'email_id': sender})}",
                    headers={"Accept": "application/json"},
                    method="GET",
                )
                with urlopen(request, timeout=self._timeout_seconds) as response:
                    data = json.loads(response.read().decode("utf-8"))
                    job_title = (data.get("JobTitle") or "").strip() or None
            except Exception:
                job_title = None
            if job_title and job_title.lower() not in sender_name.lower():
                sender_name = f"{sender_name}, {job_title}" if sender_name else job_title

            emails.append(
                EmailMessage(
                    id=item.get("id", ""),
                    subject=item.get("subject") or "(no subject)",
                    body=item.get("bodyPreview") or "",
                    sender=sender,
                    received_at=self._parse_graph_datetime(item.get("receivedDateTime")),
                    sender_name=sender_name,
                    to_addresses=to_addresses,
                    cc_addresses=cc_addresses,
                )
            )
        return emails

    def move_email(self, email_id: str, folder_name: str) -> None:
        if not self._graph_enabled:
            print(f"[mailbox] moved email {email_id} to '{folder_name}'")
            return

        try:
            mailbox = quote(self._mailbox_user or "", safe="@.-_")
            folder_id = self._ensure_folder(folder_name)
            endpoint = f"/users/{mailbox}/messages/{quote(email_id, safe='')}/move"
            self._graph_post(endpoint, {"destinationId": folder_id})
        except RuntimeError as exc:
            self._log_graph_fallback(f"move email {email_id}", exc)
            print(f"[mailbox] moved email {email_id} to '{folder_name}'")

    def reply_email(
        self,
        email_id: str,
        body: str,
        cc_addresses: list[str] | None = None,
    ) -> None:
        if not self._graph_enabled:
            cc_display = ", ".join(cc_addresses or []) or "(none)"
            print(f"[mailbox] reply to email {email_id} | cc: {cc_display}")
            return

        try:
            mailbox = quote(self._mailbox_user or "", safe="@.-_")
            endpoint = f"/users/{mailbox}/messages/{quote(email_id, safe='')}/reply"
            payload: dict = {
                "message": {
                    "body": {
                        "contentType": "HTML",
                        "content": body,
                    },
                    "ccRecipients": [
                        {"emailAddress": {"address": addr}}
                        for addr in (cc_addresses or [])
                    ]
                }
            }
            self._graph_post(endpoint, payload)
        except RuntimeError as exc:
            self._log_graph_fallback(f"reply to email {email_id}", exc)
            cc_display = ", ".join(cc_addresses or []) or "(none)"
            print(f"[mailbox] reply to email {email_id} | cc: {cc_display}")

    def create_folders(self, folders: list[str]) -> None:
        if not self._graph_enabled:
            for folder in folders:
                print(f"[mailbox] ensured folder '{folder}' exists")
            return

        for folder in folders:
            try:
                self._ensure_folder(folder)
            except RuntimeError as exc:
                self._log_graph_fallback(f"ensure folder '{folder}'", exc)
                print(f"[mailbox] ensured folder '{folder}' exists")

    def send_support_notification(
        self,
        to_addresses: list[str],
        subject: str,
        body: str,
        attachment_name: str | None = None,
        attachment_content: str | None = None,
    ) -> None:
        if not to_addresses:
            return

        if not self._graph_enabled:
            display_to = ", ".join(to_addresses)
            print(f"[mailbox] support email to: {display_to} | subject: {subject}")
            return

        mailbox = quote(self._mailbox_user or "", safe="@.-_")
        endpoint = f"/users/{mailbox}/sendMail"
        message: dict = {
            "subject": subject,
            "body": {
                "contentType": "HTML",
                "content": body,
            },
            "toRecipients": [
                {"emailAddress": {"address": addr}}
                for addr in to_addresses
            ],
        }

        if attachment_name and attachment_content:
            encoded = base64.b64encode(attachment_content.encode("utf-8")).decode("ascii")
            message["attachments"] = [
                {
                    "@odata.type": "#microsoft.graph.fileAttachment",
                    "name": attachment_name,
                    "contentType": "text/plain",
                    "contentBytes": encoded,
                }
            ]

        payload = {
            "message": message,
            "saveToSentItems": "false",
        }
        try:
            self._graph_post(endpoint, payload)
        except RuntimeError as exc:
            self._log_graph_fallback("send support notification", exc)
            display_to = ", ".join(to_addresses)
            print(f"[mailbox] support email to: {display_to} | subject: {subject}")

    def _ensure_folder(self, folder_name: str) -> str:
        mailbox = quote(self._mailbox_user or "", safe="@.-_")
        escaped_name = folder_name.replace("'", "''")
        query_params = urlencode({
            "$filter": f"displayName eq '{escaped_name}'",
            "$top": "1"
        })
        lookup = self._graph_get(
            f"/users/{mailbox}/mailFolders?{query_params}"
        )
        existing = lookup.get("value", [])
        if existing:
            return existing[0]["id"]

        created = self._graph_post(
            f"/users/{mailbox}/mailFolders", {"displayName": folder_name}
        )
        return created["id"]

    # ------------------------------------------------------------------ #
    # Graph Change Notifications (Webhook subscription management)        #
    # ------------------------------------------------------------------ #

    def register_webhook_subscription(
        self,
        notification_url: str,
        client_state: str,
    ) -> dict:
        """Create a Graph change-notification subscription for new inbox messages.

        Returns the subscription dict from Graph (contains 'id' and 'expirationDateTime').
        Raises RuntimeError if Graph is not enabled or the call fails.

        Required permissions: Mail.Read (delegated) — already in your token scope.
        Max subscription lifetime for mail: 4230 minutes (~2.9 days). Renew before expiry.
        """
        if not self._graph_enabled:
            raise RuntimeError("Graph client is not configured.")

        mailbox = quote(self._mailbox_user or "", safe="@.-_")
        from datetime import timedelta

        expiry = (
            datetime.now(timezone.utc) + timedelta(minutes=4230)
        ).strftime("%Y-%m-%dT%H:%M:%S.0000000Z")

        payload = {
            "changeType": "created",
            "notificationUrl": notification_url,
            "resource": f"/users/{mailbox}/mailFolders/inbox/messages",
            "expirationDateTime": expiry,
            "clientState": client_state,
        }
        subscription = self._graph_post("/subscriptions", payload)
        logger.info(
            "Graph webhook subscription created: id=%s expires=%s",
            subscription.get("id"),
            subscription.get("expirationDateTime"),
        )
        return subscription

    def renew_webhook_subscription(
        self,
        subscription_id: str,
        client_state: str,
    ) -> dict:
        """Extend the expiry of an existing subscription to avoid it expiring."""
        if not self._graph_enabled:
            raise RuntimeError("Graph client is not configured.")

        from datetime import timedelta

        expiry = (
            datetime.now(timezone.utc) + timedelta(minutes=4230)
        ).strftime("%Y-%m-%dT%H:%M:%S.0000000Z")

        token = self._get_access_token()
        url = f"https://graph.microsoft.com/v1.0/subscriptions/{subscription_id}"
        body = json.dumps({"expirationDateTime": expiry}).encode("utf-8")
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        request = Request(url, data=body, headers=headers, method="PATCH")
        try:
            with urlopen(request, timeout=self._timeout_seconds) as response:
                result = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"Subscription renewal failed: {detail}") from exc
        logger.info("Graph webhook subscription renewed: id=%s", subscription_id)
        return result

    def _get_access_token(self) -> str:
        if not self._graph_enabled:
            raise RuntimeError("Graph client is not configured.")

        now = datetime.now(timezone.utc)
        if self._token and self._token_expires_at and now < self._token_expires_at:
            return self._token

        token_url = (
            f"https://login.microsoftonline.com/{self._tenant_id}/oauth2/v2.0/token"
        )
        body = urlencode(
            {   
                "username": self._mailbox_user,
                "password": self._mailbox_password,
                "client_id": self._client_id,
                "client_secret": self._client_secret,
                "scope": "https://graph.microsoft.com/.default",
                "grant_type": "password",
            }
        ).encode("utf-8")
        request = Request(
            token_url,
            data=body,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )
        try:
            with urlopen(request, timeout=self._timeout_seconds) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"Token request failed: {detail}") from exc
        except URLError as exc:
            raise RuntimeError(
                f"Network error while requesting access token: {exc.reason}. "
                "Check network connectivity and firewall settings."
            ) from exc

        token = payload.get("access_token")
        expires_in = int(payload.get("expires_in", 3600))
        if not token:
            raise RuntimeError("Token response did not include access_token.")

        self._token = token
        self._token_expires_at = now + timedelta(seconds=max(60, expires_in - 60))
        return token

    def _get_sender_job_title(self, sender_email: str) -> str | None:
        """Look up the Azure AD job title for an internal sender.

        Returns None silently when:
        - Graph is disabled (local fallback mode)
        - The token lacks User.ReadBasic.All (403) — disables feature for the run
        - Sender is external to the tenant (404)

        Requires: User.ReadBasic.All or User.Read.All delegated/application permission.
        Current tokens with only User.Read can only read the signed-in user's own profile.
        """
        if not self._graph_enabled or not self._job_title_lookup_available:
            return None
        if sender_email in self._job_title_cache:
            return self._job_title_cache[sender_email]
        try:
            encoded = quote(sender_email, safe="@.-_")
            data = self._graph_get(f"/users/{encoded}?$select=jobTitle")
            title = data.get("jobTitle") or None
        except RuntimeError as exc:
            cause = exc.__cause__
            if hasattr(cause, "code") and cause.code == 403:
                # Token has User.Read only — cannot look up other users.
                # Disable for the rest of this run to avoid 403s on every email.
                self._job_title_lookup_available = False
                logger.warning(
                    "Graph job title lookup disabled: token is missing "
                    "'User.ReadBasic.All' permission. "
                    "Grant this delegated permission in Azure AD to enable VIP "
                    "detection via Azure AD job titles. "
                    "VIP detection via sender display name and email body signature "
                    "will continue to work."
                )
                return None
            # 404 = external sender not in the tenant directory — skip silently
            title = None
        self._job_title_cache[sender_email] = title
        return title

    def _graph_get(self, endpoint: str) -> dict:
        return self._graph_request("GET", endpoint)

    def _graph_post(self, endpoint: str, payload: dict) -> dict:
        return self._graph_request("POST", endpoint, payload)

    def _graph_request(self, method: str, endpoint: str, payload: dict | None = None) -> dict:
        token = self._get_access_token()
        url = f"https://graph.microsoft.com/v1.0{endpoint}"
        body = json.dumps(payload).encode("utf-8") if payload is not None else None
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
        }
        if payload is not None:
            headers["Content-Type"] = "application/json"

        request = Request(url, data=body, headers=headers, method=method)
        try:
            with urlopen(request, timeout=self._timeout_seconds) as response:
                raw = response.read()
                if not raw:
                    return {}
                return json.loads(raw.decode("utf-8"))
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"Graph API request failed ({method} {endpoint}): {detail}") from exc
        except URLError as exc:
            raise RuntimeError(
                f"Network error while calling Graph API ({method} {endpoint}): {exc.reason}. "
                "Check network connectivity, firewall settings, and DNS resolution."
            ) from exc

    def _log_graph_fallback(self, action: str, exc: RuntimeError) -> None:
        logger.warning(
            "Graph mailbox action failed while trying to %s. Falling back to local behavior. error=%s",
            action,
            exc,
        )

    @staticmethod
    def _parse_graph_datetime(value: str | None) -> datetime:
        if not value:
            return datetime.now(timezone.utc)
        normalized = value.replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(normalized)
            if parsed.tzinfo is None:
                return parsed.replace(tzinfo=timezone.utc)
            return parsed
        except ValueError:
            return datetime.now(timezone.utc)
