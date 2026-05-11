from __future__ import annotations

import logging

from dataclasses import replace
from app.application.reply_builder import (
    build_closed_ticket_reply,
    build_general_query_reply,
    build_no_ticket_found_reply,
    build_reviewing_ticket_reply,
    build_add_comment_ticket_reply,
)
from app.application.use_cases import classify_email
from app.domain.folder_mapper import FolderMapper
from app.domain.models import Rule, TicketStatus
from app.domain.rules_engine import (
    extract_adhoc_number,
    extract_incident_number,
    extract_ref_message_id,
    is_auto_notification_email,
    is_servicenow_cced,
    is_vip_sender,
)
from app.infrastructure.ai.base import AIClient
from app.infrastructure.mailbox.base import MailboxClient
from app.infrastructure.persistence.repository import ProcessedEmailRepository
from app.infrastructure.ticketing.base import ServiceNowTicketingClient
from app.observability.audit_logger import AuditLogger
from app.observability.metrics import Metrics

logger = logging.getLogger(__name__)

_TERMINAL_STATUSES = {TicketStatus.RESOLVED, TicketStatus.CANCELLED, TicketStatus.CLOSED}

# UC4 Step 2: categories that must be moved silently — no reply sent
_NO_REPLY_CATEGORIES: frozenset[str] = frozenset({"bot"})


class EmailSegregationPipeline:
    def __init__(
        self,
        mailbox_client: MailboxClient,
        ai_client: AIClient,
        repository: ProcessedEmailRepository,
        folder_mapper: FolderMapper,
        rules: list[Rule],
        metrics: Metrics,
        audit_logger: AuditLogger,
        system_prompt: str,
        fewshot_prompt: str,
        # ticketing_client: TicketingClient | None = None,
        ticketing_client:ServiceNowTicketingClient | None = None ,
        support_engineer_emails: list[str] | None = None,
        escalation_email: str | None = None,
        vip_titles: list[str] | None = None,
        general_categories: list[str] | None = None,
    ) -> None:
        self._mailbox_client = mailbox_client
        self._ai_client = ai_client
        self._repository = repository
        self._folder_mapper = folder_mapper
        self._rules = rules
        self._metrics = metrics
        self._audit_logger = audit_logger
        self._system_prompt = system_prompt
        self._fewshot_prompt = fewshot_prompt
        self._ticketing_client = ticketing_client
        self._support_engineer_emails = support_engineer_emails or []
        self._escalation_email = escalation_email
        self._vip_titles = vip_titles or [
            "Director", "VP", "Vice President", "Chief",
            "CTO", "CEO", "COO", "CFO", "SVP", "EVP",
        ]
        self._general_categories = set(
            general_categories or ["general", "marketing", "newsletter", "junk"]
        )

    def fetch_unread(self, limit: int = 25) -> list:
        return self._mailbox_client.fetch_unread(limit=limit)
    

    def _is_incident_number_and_recipient(self, email, incident_number) -> None:
        name_part, last_part = email.sender_name.rsplit(",", 1)
        # ref_message_id = extract_ref_message_id(email)
        status = self._ticketing_client.get_ticket_status(incident_number)
        response = {'action':None,'reason':None,'processed_count':0}
        matche_subject = f"Incident {incident_number} has been opened for you".format(incident_number=incident_number)

        if status in _TERMINAL_STATUSES: # If ticket is already closed/resolved/cancelled, reply with closure message and do not create a new ticket
            reply = build_closed_ticket_reply(incident_number, status, sender_name=name_part)
            self._metrics.increment("emails_ticket_closed_reply")
            response['action'] = "replied: For new ticket" ,
            response['reason'] = "ticket is {status}".format(status=status.value)
            response['processed_count'] += 1 

        elif status in {TicketStatus.NEW, TicketStatus.IN_PROGRESS, TicketStatus.ON_HOLD} and matche_subject in email.subject: # If ticket is open Ticket is opened, Support is working on it, and sender will be notified of updates by support.
            reply = build_reviewing_ticket_reply(sender_name=name_part,status=status, ticket_number=incident_number)
            self._mailbox_client.reply_email(email.id, reply)
            self._metrics.increment("emails_ticket_open_no_action")
            response['action'] = "replied: Ticket is {status}, support update to sender".format(status=status.value) ,
            response['reason'] = "Ticket is {status} , support team working on it.".format(status=status.value)
            response['processed_count'] += 1

        elif (status in {TicketStatus.NEW, TicketStatus.IN_PROGRESS, TicketStatus.ON_HOLD}) and not matche_subject in email.subject: # If ticket is open Ticket is opened, Support is working on it, and sender will be notified of updates by support.
            reply = build_add_comment_ticket_reply(ticket_number=incident_number,sender_name=name_part)
            self._mailbox_client.reply_email(email.id, reply)
            comment_added = self._ticketing_client.add_comment(incident_number, email.body)
            self._audit_logger.log({
                "email_id": email.id,
                "action": "support_notified_and_comment_added",
                "ticket_number": incident_number,
                "ticket_status": status.value,
                "comment_added": comment_added,
            })
            self._metrics.increment("emails_ticket_open_no_action")
            response['action'] = "replied: Ticket is {status}, support update to sender".format(status=status.value) ,
            response['reason'] = "Ticket is {status} , support team working on it.".format(status=status.value)
            response['processed_count'] += 1    

        else:
            reply = build_no_ticket_found_reply(sender_name=name_part)
            self._mailbox_client.reply_email(email.id, reply)
            self._metrics.increment("emails_ticket_missing_reply")
            response['action'] = "replied: Ticket not found." ,
            response['reason'] = "ticket is {status}".format(status=status.value)
            response['processed_count'] += 1
        return response 

    
    def _is_incident_number(self, email, incident_number) -> None:   
        name_part, last_part = email.sender_name.rsplit(",", 1)
        # ref_message_id = extract_ref_message_id(email)
        status = self._ticketing_client.get_ticket_status(incident_number)
        response = {'action':None,'reason':None,'processed_count':0}
        matche_subject = f"Incident {incident_number} has been opened for you".format(incident_number=incident_number)

        if status in _TERMINAL_STATUSES: # If ticket is already closed/resolved/cancelled, reply with closure message and do not create a new ticket
            reply = build_closed_ticket_reply(incident_number, status, sender_name=name_part)
            self._mailbox_client.reply_email(email.id, reply)
            self._metrics.increment("emails_ticket_closed_reply")
            response['action'] = "replied: For new ticket" ,
            response['reason'] = "ticket is {status}".format(status=status.value)
            response['processed_count'] += 1
            

        elif status in {TicketStatus.NEW, TicketStatus.IN_PROGRESS, TicketStatus.ON_HOLD} and matche_subject in email.subject: # If ticket is open but sender is asking to create a new one, reply with a message that ticket is already open and support will be notified. Notify support with the email content and add a comment to the existing ticket for visibility.
            reply = build_reviewing_ticket_reply(ticket_number=incident_number,status=status,sender_name=name_part)
            self._mailbox_client.reply_email(email.id, reply)
            
            support_subject = f"GSRT - [{incident_number}] - Reference message unavailable"
            support_body = (
                f"<html><body><p>Hi Support Team,</p>"
                f"<p>We received a user query for ticket <strong>{incident_number}</strong> "
                f"with status <strong>{status.value}</strong>.</p>"
                f"<p>Please review and respond at the earliest.</p>"
                f"<p>Regards,<br/>GenWizard Automation Team</p></body></html>"
            )
            attachment_body = (
                f"From: {email.sender}\n"
                f"Subject: {email.subject}\n\n"
                f"{email.body}"
            )
            self._mailbox_client.send_support_notification(
                to_addresses=self._support_engineer_emails,
                subject=support_subject,
                body=support_body,
                attachment_name=f"user-query-{email.id}.txt",
                attachment_content=attachment_body,
            )
            # comment_added = self._ticketing_client.add_comment(incident_number, email.body)
            # self._audit_logger.log({
            #     "email_id": email.id,
            #     "action": "support_notified_and_comment_added",
            #     "ticket_number": incident_number,
            #     "ticket_status": status.value,
            #     "comment_added": comment_added,
            # })
            # self._repository.save(
            #     email.id,
            #     "ticket_open_support_notified",
            #     "Inbox",
            #     f"Support notified for {incident_number}; comment_added={comment_added}",
            # )
            self._metrics.increment("emails_ticket_open_support_notified")
            response['action'] = "replied: Ticket is {status} comment added on serviceNow and support will be notified for visibility.".format(status=status.value) ,
            response['reason'] = "ticket is {status}".format(status=status.value)
            response['processed_count'] += 1

        elif (status in {TicketStatus.NEW, TicketStatus.IN_PROGRESS, TicketStatus.ON_HOLD}) and not matche_subject in email.subject: # If ticket is open but sender is asking to create a new one, reply with a message that ticket is already open and support will be notified. Notify support with the email content and add a comment to the existing ticket for visibility.
            reply = build_add_comment_ticket_reply(ticket_number=incident_number, sender_name=name_part)
            self._mailbox_client.reply_email(email.id, reply)
            
            support_subject = f"GSRT - [{incident_number}] - Reference message unavailable"
            support_body = (
                f"<html><body><p>Hi Support Team,</p>"
                f"<p>We received a user query for ticket <strong>{incident_number}</strong> "
                f"with status <strong>{status.value}</strong>.</p>"
                f"<p>Please review and respond at the earliest.</p>"
                f"<p>Regards,<br/>GenWizard Automation Team</p></body></html>"
            )
            attachment_body = (
                f"From: {email.sender}\n"
                f"Subject: {email.subject}\n\n"
                f"{email.body}"
            )
            self._mailbox_client.send_support_notification(
                to_addresses=self._support_engineer_emails,
                subject=support_subject,
                body=support_body,
                attachment_name=f"user-query-{email.id}.txt",
                attachment_content=attachment_body,
            )
            comment_added = self._ticketing_client.add_comment(incident_number, email.body)
            self._audit_logger.log({
                "email_id": email.id,
                "action": "support_notified_and_comment_added",
                "ticket_number": incident_number,
                "ticket_status": status.value,
                "comment_added": comment_added,
            })
            self._repository.save(
                email.id,
                "ticket_open_support_notified",
                "Inbox",
                f"Support notified for {incident_number}; comment_added={comment_added}",
            )
            self._metrics.increment("emails_ticket_open_support_notified")
            response['action'] = "replied: Ticket is {status} comment added on serviceNow and support will be notified for visibility.".format(status=status.value) ,
            response['reason'] = "ticket is {status}".format(status=status.value)
            response['processed_count'] += 1

        else:
            reply = build_no_ticket_found_reply(sender_name=name_part)
            self._mailbox_client.reply_email(email.id, reply)
            self._metrics.increment("emails_ticket_missing_reply")
            response['action'] = "replied: Ticket not found." ,
            response['reason'] = "ticket is {status}".format(status=status.value)
            response['processed_count'] += 1
        return response    


    def process_unread_emails(self, limit: int = 25) -> int:
        processed_ids = self._repository.list_processed_ids()
        unread = self._mailbox_client.fetch_unread(limit=limit)
        response = {'action':None,'reason':None,'processed_count':0}
       
        for email in unread:
            if email.id in processed_ids:
                continue

            vip, vip_detected_by = is_vip_sender(email, self._vip_titles)
            if vip:
                logger.warning(
                    "VIP sender detected — email_id=%s from='%s <%s>'. Flagged for manual review by escalation contact: %s",
                    email.id,
                    email.sender_name,
                    email.sender,
                    self._escalation_email or "N/A",
                )
                self._audit_logger.log({
                    "email_id": email.id,
                    "action": "vip_escalation",
                    "sender": email.sender,
                    "sender_name": email.sender_name,
                    "subject": email.subject,
                    "vip_detected_by": vip_detected_by,
                    "note": f"Requires discussion with: {self._escalation_email or 'escalation contact'}",
                })
                self._repository.save(email.id, "escalation", "Inbox", "VIP sender — flagged for manual review")
                self._metrics.increment("emails_vip_escalated")
                response['action'] = "no_replied: VIP sender detected " ,
                response['reason'] = "VIP sender — flagged for manual review"
                response['processed_count'] += 1

            is_bot, bot_reason = is_auto_notification_email(email)
            if is_bot:
                self._audit_logger.log({
                    "email_id": email.id,
                    "action": "no_action:auto_notification",
                    "reason": bot_reason,
                    "sender": email.sender,
                    "subject": email.subject,
                })
                response['action'] = "no_replied: Auto-notification detected " ,
                response['reason'] = "Auto-notification detected"
                response['processed_count'] += 1
                self._repository.save(email.id, "bot", "Inbox", f"Auto-notification detected: {bot_reason}")
                self._metrics.increment("emails_bot_skipped")
                
            
            # Check mail type By Rules and AI classification
            result = classify_email(
                email=email,
                rules=self._rules,
                ai_client=self._ai_client,
                system_prompt=self._system_prompt,
                fewshot_prompt=self._fewshot_prompt,
            )
            action = None
            # adhoc_number = extract_adhoc_number(email)
            # ref_message_id = extract_ref_message_id(email)
            servicenow_recipient_present = is_servicenow_cced(email)
            incident_number = extract_incident_number(email)
            response = {'action':None,'reason':None,'processed_count':0}

            result =  self._ticketing_client.get_customer_comment_from_servicenow(incident_number)
            match_percent = self._ticketing_client.match_accuracy_text(result,email)
            
            if ( incident_number and f"Incident {incident_number} has been opened for you" in email.subject) or servicenow_recipient_present:
                result = replace(result, category="Service-now")

            if (result.category in self._general_categories or result.category in _NO_REPLY_CATEGORIES) and not (incident_number or servicenow_recipient_present):
                action = f"no_reply:{result.category}"
                self._metrics.increment("emails_general_or_bot_skipped")
                response['action'] = action ,
                response['reason'] = "No incident number or ServiceNow recipient detected, classified as {result.category}".format(result=result)
                response['processed_count'] += 1

            else:
                if incident_number and servicenow_recipient_present:
                   response = self._is_incident_number_and_recipient(email, incident_number)

                elif incident_number:
                    response = self._is_incident_number(email, incident_number)

                else:
                    reply = build_no_ticket_found_reply(sender_name=email.sender_name)  
                    self._mailbox_client.reply_email(email.id, reply)
                    response['action'] = "replied: Ticket not found." ,
                    response['reason'] = "No incident number or ServiceNow recipient detected, classified as {result.category}".format(result=result)
                    response['processed_count'] += 1

            folder = self._folder_mapper.to_folder(result.category)
            self._mailbox_client.move_email(email.id, folder)
            reason = result.reason if result.reason else response.get('reason')
            self._repository.save(email.id, result.category, folder, reason)
            self._metrics.increment("emails_processed")
            
            self._audit_logger.log({
                "email_id": email.id,
                "category": result.category,
                "folder": folder,
                "reason": reason,
                "action": response['action'],
            })
            logger.info(
                "Processed email_id=%s category=%s folder=%s action=%s",
                email.id,
                result.category,
                folder,
                action,
            )
            continue
        return response    