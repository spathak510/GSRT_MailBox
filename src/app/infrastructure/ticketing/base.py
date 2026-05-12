from __future__ import annotations

from difflib import SequenceMatcher
from app.domain.models import TicketStatus
from typing import Any
import logging
import os, re

import requests

from dotenv import load_dotenv
load_dotenv()

logger = logging.getLogger(__name__)

_API_STATUS_MAP = {
    1: TicketStatus.NEW,
    2: TicketStatus.IN_PROGRESS,
    3: TicketStatus.ON_HOLD,
    6: TicketStatus.RESOLVED,
    7: TicketStatus.CLOSED,
    8: TicketStatus.CANCELLED,
}

STOP_WORDS = {
    "from", "reply", "sent", "to", "cc",
    "the", "a", "an", "is", "for"
}

class ServiceNowTicketingClient:
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

    def get_ticket_status(self, ticket_number: str) -> "TicketStatus":
        url = os.getenv("IHG_SERVICENOW_URL")
        resolved_username = os.getenv("IHG_SERVICENOW_USERNAME")
        resolved_password = os.getenv("IHG_SERVICENOW_PASSWORD")

        headers = {"Content-Type": "application/json"}

        # ✅ safer URL construction
        resolved_url = f"{url}{ticket_number}"

        try:
            response = requests.get(
                resolved_url,
                json={},
                headers=headers,
                auth=(resolved_username, resolved_password),
            )
            response.raise_for_status()

            data = response.json()

            if data.get("result"):
                state = data["result"][0].get("state")
                print("State:", state)
            else:
                print("Ticket not found")

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
            state_value = int(raw_state)
        except (TypeError, ValueError):
            logger.error("Invalid state value in response: %s", raw_state)
            return TicketStatus.NOT_FOUND

        # ✅ Map to enum
        return _API_STATUS_MAP.get(state_value, TicketStatus.NOT_FOUND)
    
    def get_sys_id_from_servicenow(self, incident_number: str):
        username = os.getenv("IHG_SERVICENOW_USERNAME")
        password = os.getenv("IHG_SERVICENOW_PASSWORD")
        headers = {"Content-Type": "application/json"}
        try:
            url = f"https://ihg.service-now.com/api/now/table/incident?sysparm_query=number={incident_number}&sysparm_limit=1"
            response = requests.get(url,json={},headers=headers,auth=(username, password),)
            sys_response = response.json()
            print("sys_response:", sys_response)
            sys_id = sys_response["result"][0]["sys_id"]
            return sys_id
        except Exception as exc:
            logger.error("Unexpected error in add_comment for %s: %s", incident_number, exc)
            return False


    def add_comment(self, incident_number: str, mail_body: str) -> bool:
        username = os.getenv("IHG_SERVICENOW_USERNAME")
        password = os.getenv("IHG_SERVICENOW_PASSWORD")
        headers = {"Content-Type": "application/json"}
        mail_body_rsp = self.clean_text(mail_body)
        payload = {
            "comments": mail_body_rsp,
        }
        sys_response = None
        try:
            url = f"https://ihg.service-now.com/api/now/table/incident?sysparm_query=number={incident_number}&sysparm_limit=1"
            response = requests.get(url,json={},headers=headers,auth=(username, password),)
            sys_response = response.json()
            print("sys_response:", sys_response)
        except Exception as exc:
            logger.error("Unexpected error in add_comment for %s: %s", incident_number, exc)
            return False

        try:
            sys_id = sys_response["result"][0]["sys_id"]
            url = f"https://ihg.service-now.com/api/now/table/incident/{sys_id}"

            response = requests.patch(url,json=payload,headers=headers,auth=(username, password),)
            response.raise_for_status()
            print("Comment_response:", response)
        except requests.RequestException as exc:
            logger.error("ServiceNow add comment error for %s: %s", incident_number, exc)
            return False

        return True
    

    def get_customer_comment_from_servicenow(self, incident_number: str):
        username = os.getenv("IHG_SERVICENOW_USERNAME")
        password = os.getenv("IHG_SERVICENOW_PASSWORD")
        headers = {"Content-Type": "application/json"}
        try:
            sys_id = self.get_sys_id_from_servicenow(incident_number)
            url = f"https://ihg.service-now.com/api/now/table/incident/{sys_id}"

            response = requests.get(url,json={},headers=headers,auth=(username, password),)
            response.raise_for_status()
            result = response.json().get("result", {})
            customer_comment = result.get("u_comments_customer", "")
            print("Comment_response:", customer_comment)
            return customer_comment
        except requests.RequestException as exc:
            logger.error("ServiceNow add comment error for %s: %s", incident_number, exc)
            return False
        
    def extract_latest_comment(self, body: str) -> str:
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
        result_text_B = self.extract_latest_comment(email.body)
        # result_text_B = self.clean_recipients_from_text(all_addresses, email_body)

        # Remove extra spaces/newlines
        text_A = re.sub(r'\s+', ' ', result_text_A).strip()
        text_B = re.sub(r'\s+', ' ', result_text_B).strip()

        text_A_clean = self.clean_text(text_A)
        text_B_clean = self.clean_text(text_B)
        text_A_clean_set = set(text_A_clean.lower().split())
        text_B_clean_set = set(text_B_clean.lower().split())
        matched_words = text_A_clean_set.intersection(text_B_clean_set)
        match_percent = round((len(matched_words) / len(text_A_clean_set)) * 100)

        # match_percent = round(self.similarity(text_A_clean,text_B_clean))

        print(match_percent)
        response = {
            "match_percent": match_percent,
            "matched_words": matched_words,
            "customer_comment": text_A_clean
        }
        return response

    def comment_accuracy_validation(self, incident_number: str, email: str) -> bool:
        customer_comment =  self.get_customer_comment_from_servicenow(incident_number)
        match_response = self.match_accuracy_text(customer_comment, email)
        print("Match response:", match_response)
        match_response["match"] = False
        if match_response["match_percent"] >= 70:
            logger.warning("Comment accuracy validation failed for %s: %s", incident_number, match_response)
            match_response["match"] = True
    
        return match_response

        
