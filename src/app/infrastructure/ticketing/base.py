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

    def similarity(self, a, b):
        return SequenceMatcher(None, a, b).ratio() * 100


    def clean_text(self, text: str) -> str:
        if not text:
            return ""

        # Convert to lowercase
        text = text.lower()

        # Remove escape characters
        text = text.replace("\r", " ").replace("\n", " ").replace("\t", " ")

        # Remove email headers / quoted thread
        text = re.split(r"from:|sent:|subject:|to:", text, flags=re.IGNORECASE)[0]

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
            "work_notes": mail_body_rsp,
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
            url = f"https://ihguat.service-now.com/api/now/table/incident/{sys_id}"

            response = requests.patch(url,json=payload,headers=headers,auth=(username, password),)
            response.raise_for_status()
            print("Comment_response:", response)
        except requests.RequestException as exc:
            logger.error("ServiceNow add comment error for %s: %s", incident_number, exc)
            return False
        
        try:
            sys_id = sys_response["result"][0]["sys_id"]
            url = f"https://ihg.service-now.com/api/now/table/incident/{sys_id}"

            response = requests.get(url,json={},headers=headers,auth=(username, password),)
            response.raise_for_status()
            result = response.json().get("result", {})
            customer_comment = result.get("u_comments_customer", "")
            print("Comment_response:", customer_comment)
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
        
    def match_accuracy_text(self, result:str, email:str):
    
        all_addresses = email.to_addresses + email.cc_addresses

        for email_address in all_addresses:
            result = re.sub( re.escape(email_address),'',result,flags=re.IGNORECASE )

        # Remove extra spaces/newlines
        text_A = re.sub(r'\s+', ' ', result).strip()

        text_A_clean = self.clean_text(text_A)
        text_B_clean = self.clean_text(email.body)

        match_percent = round(self.similarity(text_A_clean,text_B_clean))

        print(match_percent) 
        return match_percent 


    # def match_accuracy_text(self, text_a, text_b):

    #     text_a = self.clean_text(text_a)
    #     text_b = self.clean_text(text_b)

    #     words1 = {
    #         w for w in text_a.split()
    #         if w not in STOP_WORDS
    #     }

    #     words2 = {
    #         w for w in text_b.split()
    #         if w not in STOP_WORDS
    #     }

    #     common_words = words1.intersection(words2)

    #     match_percent = (
    #         len(common_words) / max(len(words1), len(words2))
    #     ) * 100

    #     return round(match_percent, 2) 

