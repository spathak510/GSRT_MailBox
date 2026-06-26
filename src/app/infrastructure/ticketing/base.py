from __future__ import annotations

from difflib import SequenceMatcher
from app.domain.models import TicketStatus
from typing import Any
import logging
import os, re
from urllib.parse import urlsplit

import requests

logger = logging.getLogger(__name__)

_INCIDENT_API_STATUS_MAP = {
    1: TicketStatus.NEW,
    2: TicketStatus.IN_PROGRESS,
    3: TicketStatus.ON_HOLD,
    6: TicketStatus.RESOLVED,
    7: TicketStatus.CLOSED,
    8: TicketStatus.CANCELLED,
}

_ADHOC_API_STATUS_MAP = {
    1: TicketStatus.OPEN,
    2: TicketStatus.IN_PROGRESS,
    4: TicketStatus.CANCELLED,
    5: TicketStatus.PENDING,
    6: TicketStatus.RESOLVED,
}

_DEFAULT_ADHOC_TABLE_URL = "https://ihg.service-now.com/api/now/table/u_ad_hoc_request"

STOP_WORDS = {
    "from", "reply", "sent", "to", "cc",
    "the", "a", "an", "is", "for"
}

class ServiceNowTicketingClient:
    def __init__(
        self,
        base_url: str | None = None,
        incident_table_url: str | None = None,
        adhoc_table_url: str | None = None,
        portal_url: str | None = None,
        username: str | None = None,
        password: str | None = None,
    ) -> None:
        self._base_url = (base_url or "").strip()
        self._incident_table_url = (incident_table_url or "").strip()
        self._adhoc_table_url = (adhoc_table_url or "").strip()
        self._portal_url = (portal_url or "").strip()
        self._username = (username or os.getenv("IHG_SERVICENOW_USERNAME") or "").strip()
        self._password = (password or os.getenv("IHG_SERVICENOW_PASSWORD") or "").strip()

    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        basic_auth = (os.getenv("IHG_SERVICENOW_BASIC_AUTH") or "").strip()
        cookie = (os.getenv("IHG_SERVICENOW_COOKIE") or "").strip()
        if basic_auth:
            headers["Authorization"] = basic_auth
        if cookie:
            headers["Cookie"] = cookie
        return headers

    def _auth(self) -> tuple[str, str] | None:
        if "Authorization" in self._headers():
            return None
        if self._username and self._password:
            return (self._username, self._password)
        return None

    def _incident_table_base_url(self) -> str:
        configured_url = (
            self._incident_table_url
            or self._base_url
            or self._portal_url
            or os.getenv("IHG_SERVICENOW_URL")
            or os.getenv("IHG_SERVICENOW_INCIDENT_TABLE_URL")
            or os.getenv("IHG_SERVICENOW_BASE_URL")
            or os.getenv("IHG_SERVICENOW_PORTAL_URL")
            or ""
        ).strip()
        if not configured_url:
            return ""

        parsed_url = urlsplit(configured_url)
        if parsed_url.scheme and parsed_url.netloc and "/api/now/table/incident" not in parsed_url.path:
            return f"{parsed_url.scheme}://{parsed_url.netloc}/api/now/table/incident"
        if "/api/now/table/incident" in configured_url:
            return configured_url.split("/api/now/table/incident", 1)[0] + "/api/now/table/incident"
        return configured_url.rstrip("/")

    def _adhoc_table_base_url(self) -> str:
        configured_url = (
            self._adhoc_table_url
            or os.getenv("IHG_SERVICENOW_ADHOC_TABLE_URL")
            or _DEFAULT_ADHOC_TABLE_URL
        ).strip()
        if not configured_url:
            return ""

        parsed_url = urlsplit(configured_url)
        if parsed_url.scheme and parsed_url.netloc and "/api/now/table/u_ad_hoc_request" not in parsed_url.path:
            return f"{parsed_url.scheme}://{parsed_url.netloc}/api/now/table/u_ad_hoc_request"
        if "/api/now/table/u_ad_hoc_request" in configured_url:
            return configured_url.split("/api/now/table/u_ad_hoc_request", 1)[0] + "/api/now/table/u_ad_hoc_request"
        return configured_url.rstrip("/")

    def _table_base_url(self, ticket_type: str) -> str:
        if ticket_type == "adhoc":
            return self._adhoc_table_base_url()
        return self._incident_table_base_url()

    def _status_map(self, ticket_type: str) -> dict[int, TicketStatus]:
        if ticket_type == "adhoc":
            return _ADHOC_API_STATUS_MAP
        return _INCIDENT_API_STATUS_MAP

    # def clean_recipients_from_text(self, all_addresses: list[str], result: str) -> str:
    #     for email_address in all_addresses:
    #         result = re.sub( re.escape(email_address),'',result,flags=re.IGNORECASE )
    #     return result

    def clean_recipients_from_text(self, body: str) -> str:
        if not body:
            return ""

        # Normalize line breaks
        body = body.replace("\r", "\n")

        # Remove "reply from: xxx" line if present
        body = re.sub(
            r"^reply\s+from:.*?\n+",
            "",
            body,
            flags=re.IGNORECASE | re.MULTILINE
        )

        # Convert multiline to single clean text
        body = re.sub(r"\n+", " ", body)

        # Normalize spaces
        body = re.sub(r"\s+", " ", body).strip()

        # Patterns where auto-generated/system content starts
        split_patterns = [
            r"\bfrom:\b",
            r"\bihg service desk\b",
            r"\bsent:\b",
            r"\bsubject:\b",
            r"\bincident\s+inc\d+\b",
            r"\bhas been opened for you\b",
        ]

        for pattern in split_patterns:
            match = re.search(pattern, body, flags=re.IGNORECASE)
            if match:
                body = body[:match.start()].strip()
                break

        return body.strip(" -:\n\t")


    def clean_text(self, text: str) -> str:
        if not text:
            return ""

        # Convert to lowercase
        text = text.lower()

        # Remove escape characters
        text = text.replace("\r", " ").replace("\n", " ").replace("\t", " ")

        # Remove header labels but keep the surrounding content.
        text = re.sub(r"\b(?:from|sent|subject|to|reply)\b\s*:?", " ", text)

        # Remove multiple spaces
        text = re.sub(r"\s+", " ", text)

        # Remove unwanted special characters
        text = re.sub(r"[^\w\s.,-]", "", text)

        # Remove common disclaimers
        text = re.split(r"may contain privileged.*", text, flags=re.IGNORECASE)[0]

        return text.strip()

    def get_inc_ticket_status(self, ticket_number: str) -> "TicketStatus":
        base_url = self._incident_table_base_url()
        if not base_url:
            logger.error("ServiceNow incident table URL is not configured.")
            return TicketStatus.NOT_FOUND

        try:
            response = requests.get(
                base_url,
                headers=self._headers(),
                auth=self._auth(),
                params={
                    "sysparm_query": f"number={ticket_number}",
                    "sysparm_limit": "1",
                },
                timeout=5,
            )
            response.raise_for_status()
            data = response.json()

        except requests.RequestException as exc:
            logger.error(
                "ServiceNow API error for %s: %s",
                ticket_number,
                exc
            )
            return TicketStatus.NOT_FOUND

        except ValueError:
            logger.error("Invalid JSON response")
            return TicketStatus.NOT_FOUND

        # ✅ Unified processing AFTER try block
        results = data.get("result", [])

        if not results:
            logger.warning("No ticket found: %s", ticket_number)
            return TicketStatus.NOT_FOUND

        # ✅ Extract + normalize state
        raw_state = results[0].get("state")

        try:
            state_value = abs(int(raw_state))
        except (TypeError, ValueError):
            logger.error("Invalid state value in response: %s", raw_state)
            return TicketStatus.NOT_FOUND

        return self._status_map("incident").get(state_value, TicketStatus.NOT_FOUND)

    def get_adhoc_ticket_status(self, ticket_number: str) -> "TicketStatus":
        base_url = self._adhoc_table_base_url()
        if not base_url:
            logger.error("ServiceNow adhoc table URL is not configured.")
            return TicketStatus.NOT_FOUND

        try:
            response = requests.get(
                base_url,
                headers=self._headers(),
                auth=self._auth(),
                params={
                    "sysparm_query": f"number={ticket_number}",
                    "sysparm_limit": "1",
                },
                timeout=5,
            )
            response.raise_for_status()
            data = response.json()

        except requests.RequestException as exc:
            logger.error(
                "ServiceNow API error for %s: %s",
                ticket_number,
                exc
            )
            return TicketStatus.NOT_FOUND

        except ValueError:
            logger.error("Invalid JSON response")
            return TicketStatus.NOT_FOUND

        results = data.get("result", [])

        if not results:
            logger.warning("No ticket found: %s", ticket_number)
            return TicketStatus.NOT_FOUND

        raw_state = results[0].get("state")

        try:
            state_value = abs(int(raw_state))
        except (TypeError, ValueError):
            logger.error("Invalid state value in response: %s", raw_state)
            return TicketStatus.NOT_FOUND

        return self._status_map("adhoc").get(state_value, TicketStatus.NOT_FOUND)
    
    def get_sys_id_from_servicenow(self, ticket_number: str, ticket_type: str):
        base_url = self._table_base_url(ticket_type)
        if not base_url:
            logger.error("ServiceNow %s table URL is not configured.", ticket_type)
            return False
        try:
            response = requests.get(
                base_url,
                headers=self._headers(),
                auth=self._auth(),
                params={
                    "sysparm_query": f"number={ticket_number}",
                    "sysparm_limit": "1",
                },
                timeout=5,
            )
            response.raise_for_status()
            sys_response = response.json()
            sys_id = sys_response["result"][0]["sys_id"]
            return sys_id
        except (requests.RequestException, KeyError, IndexError, TypeError, ValueError) as exc:
            logger.error("Unexpected error in add_comment for %s: %s", ticket_number, exc)
            return False


    def add_comment(self, incident_number: str, mail_body: str, ticket_type: str) -> bool:
        base_url = self._table_base_url(ticket_type)
        if not base_url:
            logger.error("ServiceNow %s table URL is not configured.", ticket_type)
            return False

        headers = {**self._headers(), "Content-Type": "application/json"}
        mail_body_rsp = self.clean_text(mail_body)
        payload = {
            "comments": mail_body_rsp,
        }
        sys_response = None
        try:
            response = requests.get(
                base_url,
                headers=headers,
                auth=self._auth(),
                params={
                    "sysparm_query": f"number={incident_number}",
                    "sysparm_limit": "1",
                },
                timeout=5,
            )
            response.raise_for_status()
            sys_response = response.json()
        except (requests.RequestException, ValueError) as exc:
            logger.error("Unexpected error in add_comment for %s: %s", incident_number, exc)
            return False

        try:
            sys_id = sys_response["result"][0]["sys_id"]
            url = f"{base_url}/{sys_id}"

            response = requests.patch(url,json=payload,headers=headers,auth=self._auth(), timeout=5)
            response.raise_for_status()
        except (requests.RequestException, KeyError, IndexError, TypeError) as exc:
            logger.error("ServiceNow add comment error for %s: %s", incident_number, exc)
            return False

        return True
    

    def get_customer_comment_from_servicenow(self, ticket_number: str, ticket_type: str):
        base_url = self._table_base_url(ticket_type)
        if not base_url:
            logger.error("ServiceNow %s table URL is not configured.", ticket_type)
            return False

        headers = {**self._headers(), "Content-Type": "application/json"}
        comments = {}
        try:
            sys_id = self.get_sys_id_from_servicenow(ticket_number, ticket_type)
            url = f"{base_url}/{sys_id}"

            response = requests.get(url, headers=headers, auth=self._auth(), timeout=5)
            response.raise_for_status()
            result = response.json().get("result", {})
            comments["u_comment_customer"] = result.get("u_comments_customer", "")
            comments["u_comments_fulfiller"] = result.get("u_comments_fulfiller", "")
            return comments
        except requests.RequestException as exc:
            logger.error("ServiceNow add comment error for %s: %s", ticket_number, exc)
            return False
        
    def extract_email_body(self, body: str) -> str:
        if not body:
            return ""

        body = body.replace("\r", " ").replace("\n", " ")

        # Normalize spaces
        body = re.sub(r"\s+", " ", body).strip()

        # Common patterns where ServiceNow/system text starts
        split_patterns = [
            r"\bfrom:\b",
            r"\bihg service desk\b",
            r"\bincident\s+inc\d+\b",
            r"\bhas been opened for you\b",
            r"\bsent:\b",
            r"\bsubject:\b",
        ]

        for pattern in split_patterns:
            match = re.search(pattern, body, flags=re.IGNORECASE)
            if match:
                body = body[:match.start()].strip()
                break

        return body.strip(" -:\n\t")    
        
    def match_accuracy_text(self, result: str, email: str) -> tuple[int, set[str]]:
    
        all_addresses = email.to_addresses + email.cc_addresses + [email.sender]

        
        # result_text_A = self.clean_recipients_from_text(all_addresses, result)
        result_text_A = self.clean_recipients_from_text(result)
        result_text_B = self.extract_email_body(email.body)
        # result_text_B = self.clean_recipients_from_text(all_addresses, email_body)

        # Remove extra spaces/newlines
        text_A = re.sub(r'\s+', ' ', result_text_A).strip()
        text_B = re.sub(r'\s+', ' ', result_text_B).strip()

        text_A_clean = self.clean_text(text_A)
        text_B_clean = self.clean_text(text_B)
        text_A_clean_set = set(text_A_clean.lower().split())
        text_B_clean_set = set(text_B_clean.lower().split())
        matched_words = text_A_clean_set.intersection(text_B_clean_set)
        # Prevent ZeroDivisionError
        if not text_A_clean_set:
            match_percent = 0
        else:
            match_percent = round(
                (len(matched_words) / len(text_A_clean_set)) * 100
            )

        print(match_percent)
        response = {
            "match_percent": match_percent,
            "matched_words": matched_words,
            "customer_comment": text_A_clean
        }
        return response

    def comment_accuracy_validation(self, ticket_number: str, email: str, ticket_type: str) -> bool:
        comments =  self.get_customer_comment_from_servicenow(ticket_number, ticket_type)
        if comments["u_comment_customer"]:
            match_response = self.match_accuracy_text(comments["u_comment_customer"], email)
            if match_response["match_percent"] < 70:
               match_response = self.match_accuracy_text(comments["u_comments_fulfiller"], email)
        else:
              match_response = self.match_accuracy_text(comments["u_comments_fulfiller"], email)         
        print("Match response----------------------------------------:", match_response)
        match_response["match"] = False
        if match_response["match_percent"] >= 70:
            logger.warning("Comment accuracy validation failed for %s: %s", ticket_number, match_response)
            match_response["match"] = True
    
        return match_response

        
