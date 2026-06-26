from __future__ import annotations

import logging

from dataclasses import replace
from app.application.reply_builder import (
    build_closed_ticket_reply,
    build_multi_incident_clarification_reply,
    build_multi_incident_reply,
    build_no_ticket_found_reply,
    build_no_ticket_found_into_mail_reply,
    build_reviewing_ticket_reply,
    build_add_comment_ticket_reply,
)
from app.application.use_cases import classify_email
from app.domain.folder_mapper import FolderMapper
from app.domain.models import Rule, TicketStatus
from app.domain.rules_engine import (
    extract_ticket_numbers,
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
from app.settings.config import load_config

logger = logging.getLogger(__name__)

_TERMINAL_STATUSES = {TicketStatus.RESOLVED, TicketStatus.CANCELLED, TicketStatus.CLOSED}
_OPEN_STATUSES = {TicketStatus.NEW, TicketStatus.OPEN, TicketStatus.IN_PROGRESS, TicketStatus.ON_HOLD, TicketStatus.PENDING}

# UC4 Step 2: categories that must be moved silently — no reply sent
_NO_REPLY_CATEGORIES: frozenset[str] = frozenset({"bot"})

cfg = load_config()
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
        self._vip_titles = vip_titles or []
        self._general_categories = set(
            general_categories or ["marketing", "newsletter", "junk"]
        )

    def fetch_unread(self, limit: int = 25) -> list:
        if cfg.is_unread_mail:
            try:
                return self._mailbox_client.fetch_unread(limit=limit, is_unread_mail=cfg.is_unread_mail)
            except TypeError:
                return self._mailbox_client.fetch_unread(limit=limit)
        else:
            return self._mailbox_client.fetch_unread(limit=limit)
        

    def _reply_sender_name(self, email) -> str:
        sender_name = (email.sender_name or "").strip()
        if not sender_name:
            return ""
        return sender_name.split(",", 1)[0].strip()

    def _extract_ticket_numbers(self, email) -> list[dict[str, list[str]]]:
        grouped_tickets: dict[str, list[str]] = {"incident": [], "adhoc": []}

        for ticket in extract_ticket_numbers(email):
            ticket_type = ticket.get("ticket_type", "incident")
            ticket_number = ticket.get("ticket_number")
            if ticket_number:
                grouped_tickets.setdefault(ticket_type, []).append(ticket_number)

        return [
            {ticket_type: numbers}
            for ticket_type, numbers in grouped_tickets.items()
            if numbers
        ]

    def _get_ticket_status(self, ticket_number: str, ticket_type: str) -> TicketStatus:
        if self._ticketing_client is None:
            return TicketStatus.NOT_FOUND

        if ticket_type == "adhoc":
            adhoc_status_lookup = getattr(self._ticketing_client, "get_adhoc_ticket_status", None)
            if callable(adhoc_status_lookup):
                return adhoc_status_lookup(ticket_number)

        return self._ticketing_client.get_inc_ticket_status(ticket_number)

    def _finalize_processed_email(self, email, classification_result, action: str, reason: str) -> dict:
        folder = self._folder_mapper.to_folder(classification_result.category)
        self._mailbox_client.move_email(email.id, folder)
        self._repository.save(email.id, classification_result.category, folder, reason)
        self._metrics.increment("emails_processed")
        self._audit_logger.log({
            "email_id": email.id,
            "category": classification_result.category,
            "folder": folder,
            "reason": reason,
            "action": action,
        })
        logger.info(
            "Processed email_id=%s category=%s folder=%s action=%s",
            email.id,
            classification_result.category,
            folder,
            action,
        )
        logger.info("Completed processing for email_id=%s", email.id)
        return {"action": action, "reason": reason, "processed_count": 1}

    def _collect_multi_ticket_result(
        self,
        email,
        ticket_number: str,
        ticket_type: str,
        allow_comment_update: bool = True,
    ) -> tuple[str, TicketStatus, str]:
        if self._ticketing_client is None:
            return ticket_number, TicketStatus.NOT_FOUND, "Incident was not found in ServiceNow."

        logger.info(
            "Agent started processing to check the status for incident number %s ................",
            ticket_number,
        )
        status = self._get_ticket_status(ticket_number, ticket_type)
        logger.info(
            "Completed agent to check the status for incident number %s and status is %s ................",
            ticket_number,
            status.value,
        )

        if status is TicketStatus.NOT_FOUND:
            return ticket_number, status, "Ticket was not found in ServiceNow."

        if status in _TERMINAL_STATUSES:
            return ticket_number, status, f"Ticket is currently {status.value.replace('_', ' ')}."

        if not allow_comment_update:
            return ticket_number, status, f"Ticket is currently {status.value.replace('_', ' ')}."

        comment_accuracy = getattr(self._ticketing_client, "comment_accuracy_validation", None)
        extract_email_body = getattr(self._ticketing_client, "extract_email_body", lambda body: body)
        match_percent = 0
        if callable(comment_accuracy):
            comment_result = comment_accuracy(ticket_number, email, ticket_type)
            match_percent = comment_result.get("match_percent", 0)

        if match_percent < 70:
            logger.info(
                "Agent started processing to add comment to the incident number %s ................",
                ticket_number,
            )
            comment_added = self._ticketing_client.add_comment(
                ticket_number,
                extract_email_body(email.body),
                ticket_type,
            )
            logger.info(
                "Completed agent to add comment into the incident number : %s from email id: %s",
                ticket_number,
                email.id,
            )
            if comment_added:
                self._metrics.increment("emails_ticket_open_support_notified")
                return (
                    ticket_number,
                    status,
                    f"Ticket is currently {status.value.replace('_', ' ')}. Your latest update was added to the ticket.",
                )
            return (
                ticket_number,
                status,
                f"Ticket is currently {status.value.replace('_', ' ')}. We could not add your latest update to the ticket automatically.",
            )

        return (
            ticket_number,
            status,
            f"Ticket is currently {status.value.replace('_', ' ')}. The latest ServiceNow comment already matches your email.",
        )

    def _handle_multi_ticket_email(self, email, classification_result, ticket_groups: list[dict[str, list[str]]]) -> dict:
        ticket_summaries: list[tuple[str, TicketStatus, str]] = []
        for ticket_group in ticket_groups:
            for ticket_type, ticket_numbers in ticket_group.items():
                for ticket_number in ticket_numbers:
                    ticket_reference, status, summary = self._collect_multi_ticket_result(
                        email,
                        ticket_number,
                        ticket_type,
                        allow_comment_update=False,
                    )
                    ticket_summaries.append((ticket_reference, status, summary))

        statuses = [status for _, status, _ in ticket_summaries]
        active_ticket_numbers = [
            ticket_reference
            for ticket_reference, status, _ in ticket_summaries
            if status in _OPEN_STATUSES
        ]

        if statuses and all(status in _TERMINAL_STATUSES for status in statuses):
            reply = build_multi_incident_reply(
                sender_name=self._reply_sender_name(email),
                incident_summaries=ticket_summaries,
            )
            self._mailbox_client.reply_email(email.id, reply)
            action = "replied: consolidated multi-incident summary"
            reason = "All referenced incidents are already closed, cancelled, or resolved."
        elif statuses and all(status is TicketStatus.NOT_FOUND for status in statuses):
            reply = build_multi_incident_reply(
                sender_name=self._reply_sender_name(email),
                incident_summaries=ticket_summaries,
            )
            self._mailbox_client.reply_email(email.id, reply)
            action = "replied: consolidated multi-incident summary"
            reason = "All referenced incidents were not found in ServiceNow."
        elif len(active_ticket_numbers) >= 2:
            reply = build_multi_incident_clarification_reply(
                sender_name=self._reply_sender_name(email),
                incident_numbers=active_ticket_numbers,
            )
            self._mailbox_client.reply_email(email.id, reply)
            action = "replied: multi-incident clarification"
            reason = "Two or more active incidents were found; clarification requested from the user."
        else:
            refreshed_summaries: list[tuple[str, TicketStatus, str]] = []
            for ticket_group in ticket_groups:
                for ticket_type, ticket_numbers in ticket_group.items():
                    for ticket_number in ticket_numbers:
                        if ticket_number in active_ticket_numbers:
                            refreshed_summaries.append(
                                self._collect_multi_ticket_result(
                                    email,
                                    ticket_number,
                                    ticket_type,
                                    allow_comment_update=True,
                                )
                            )
                        else:
                            refreshed_summaries.append(
                                next(
                                    summary_entry
                                    for summary_entry in ticket_summaries
                                    if summary_entry[0] == ticket_number
                                )
                            )
            ticket_summaries = refreshed_summaries
            action = "no_reply: multi-incident comments updated"
            reason = "Processed multiple incidents and updated active tickets without replying to the user."

        response = self._finalize_processed_email(
            email,
            classification_result,
            action,
            reason,
        )
        self._audit_logger.log({
            "email_id": email.id,
            "action": "multi_incident_summary",
            "incidents": [
                {"ticket_number": ticket_reference, "ticket_status": status.value, "summary": summary}
                for ticket_reference, status, summary in ticket_summaries
            ],
        })
        return response

    
    def _is_ticket_number(self, email, ticket_number: str, ticket_type: str) -> None:   
        name_part = self._reply_sender_name(email)

        logger.info("Agent started processing to check the status for ticket number %s ................", ticket_number)
        status = self._get_ticket_status(ticket_number, ticket_type)
        logger.info("Completed agent to check the status for ticket number %s and status is %s ................", ticket_number, status.value)

        response = {'action':None,'reason':None,'processed_count':0}

        if status is TicketStatus.NOT_FOUND: # If ticket number is not valid or ticket not found, reply with ticket not found message
            logger.info("Agent started processing to reply ticket-not-found into the system ................")
            reply = build_no_ticket_found_reply(sender_name=name_part,incident_number=ticket_number)
            self._mailbox_client.reply_email(email.id, reply)
            self._metrics.increment("emails_ticket_missing_reply")
            response['action'] = "replied: Ticket not found." ,
            response['reason'] = "Invalid ticket number or ticket not found for {incident_number}".format(incident_number=ticket_number)
            response['processed_count'] += 1
            logger.info("Completed agent to reply ticket-not-found into the system : %s", email.id)
        
        elif status in _TERMINAL_STATUSES: # If ticket is already closed/resolved/cancelled, reply with closure message and do not create a new ticket
            logger.info("Agent started processing to reply closed ticket emails ................")
            reply = build_closed_ticket_reply(ticket_number, status, sender_name=name_part)
            self._mailbox_client.reply_email(email.id, reply)
            self._metrics.increment("emails_ticket_closed_reply")
            response['action'] = "replied: For new ticket" ,
            response['reason'] = "ticket is {status}".format(status=status.value)
            response['processed_count'] += 1
            logger.info("Completed agent to reply closed ticket emails : %s", email.id)

        elif status in _OPEN_STATUSES: # If ticket is open but sender is asking to create a new one, reply with a message that ticket is already open and support will be notified. Notify support with the email content and add a comment to the existing ticket for visibility.
            logger.info("Agent started processing to reply open ticket emails ................")
            logger.info("Agent started processing to check comment accuracy for incident number %s ................", ticket_number)
            comment_accuracy = self._ticketing_client.comment_accuracy_validation(ticket_number, email, ticket_type)
            logger.info("Completed agent to check comment accuracy for incident number %s and accuracy is %s ................", ticket_number, comment_accuracy["match_percent"])
            if comment_accuracy["match_percent"] < 70:
                logger.info("Agent started processing to add comment to the ticket number %s ................", ticket_number)
                body = self._ticketing_client.extract_email_body(email.body)
                comment_added = self._ticketing_client.add_comment(ticket_number, body, ticket_type)
                self._audit_logger.log({
                    "email_id": email.id,
                    "action": "comment added support will be notified automatically",
                    "ticket_number": ticket_number,
                    "ticket_status": status.value,
                    "comment_added": comment_added,
                })
                self._repository.save(
                    email.id,
                    "ticket_open_support_notified",
                    "Inbox",
                    f"Support notified for {ticket_number}; comment_added={comment_added}",
                )
                self._metrics.increment("emails_ticket_open_support_notified")
                response['action'] = "replied: Ticket is {status} comment added on serviceNow and support will be notified for visibility.".format(status=status.value) ,
                response['reason'] = "ticket is {status}".format(status=status.value)
                response['processed_count'] += 1
                logger.info("Completed agent to add comment into the incident number : %s from email id: %s", ticket_number, email.id)

        else:
            logger.info("Agent started processing to reply ticket-not-found emails ................")
            reply = build_no_ticket_found_into_mail_reply(sender_name=name_part)
            self._mailbox_client.reply_email(email.id, reply)
            self._metrics.increment("emails_ticket_missing_reply")
            response['action'] = "replied: Ticket not found." ,
            response['reason'] = "ticket is {status}".format(status=status.value)
            response['processed_count'] += 1
            logger.info("Completed agent to reply ticket-not-found emails : %s", email.id)
        return response    


    def process_unread_emails(self, limit: int = 25) -> int:
        logger.info("Starting processing --------------------------------")

        processed_ids = self._repository.list_processed_ids()
        # Step 1: Fetch unread emails from the mailbox by Agent

        logger.info( "Starting email agent to fetch unread emails from the mailbox .............................")
        unread = self.fetch_unread(limit=limit)
        logger.info("Completed email agent to fetch unread emails from the mailbox : %d", len(unread))
        response = {'action':None,'reason':None,'processed_count':0}
       
        for email in unread:
            if email.id in processed_ids:
                logger.debug("Skipping already processed email_id=%s", email.id)
                continue
            
            logger.info("Agent started processing to check for VIP mails ................")
            vip, vip_detected_by = is_vip_sender(email, self._vip_titles)
            if vip:
                vip_folder = self._folder_mapper.to_folder("escalation")
                logger.warning(
                    "VIP sender detected — email_id=%s from='%s <%s>'. Flagged for manual review by escalation contact: %s",
                    email.id,
                    email.sender_name,
                    email.sender,
                    self._escalation_email or "N/A",
                )
                support_subject = f"Urgent: Support Required for Leadership Email Response"
                support_body = (
                    f"<html><body><p>Hi Support Team,</p>"
                    f"<p>We have received an emai from Leadership team in GSRT Inbox, Please review and take appropriate</p> "
                    f"action on the email received <strong> from {email.sender}</strong>.</p>"
                    f"<p>I have attached the email for your reference. Kindly ensure that a response is sent back at the earliest.</p>"
                    f"<p>Please treat this as a priority.</p>"
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
                self._audit_logger.log({
                    "email_id": email.id,
                    "action": "vip_escalation",
                    "sender": email.sender,
                    "sender_name": email.sender_name,
                    "subject": email.subject,
                    "vip_detected_by": vip_detected_by,
                    "note": f"Requires discussion with: {self._escalation_email or 'escalation contact'}",
                })
                self._repository.save(email.id, "escalation", vip_folder, "VIP sender — flagged for manual review")
                self._metrics.increment("emails_vip_escalated")
                self._mailbox_client.move_email(email.id, vip_folder)
                response['action'] = "no_replied: VIP sender detected " ,
                response['reason'] = "VIP sender — flagged for manual review"
                response['processed_count'] += 1
                logger.info("VIP mail detected by agent and support team will notified for this mails : %s", email.id)
                continue
            logger.info("Completed agent to check for VIP mails ................") 

            logger.info("Agent started processing to check for auto-notification mails ................")
            is_bot, bot_reason = is_auto_notification_email(email)
            if is_bot:
                logger.info("Skipping auto-notification email_id=%s sender=%s reason=%s",email.id,email.sender,bot_reason,)
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
                logger.info("Completed agent to check for auto-notification mails : %s", email.id)
                continue
                
            
            # Check mail type By Rules and AI classification
            logger.info("Agent started processing to classify the mails ................")
            result = classify_email(
                email=email,
                rules=self._rules,
                ai_client=self._ai_client,
                system_prompt=self._system_prompt,
                fewshot_prompt=self._fewshot_prompt,
            )
            logger.info("Completed agent to classify the mails and category is %s ................", result.category)
            action = None

            logger.info("Agent started processing to extract incident number from mails ................")
            ticket_groups = self._extract_ticket_numbers(email)
            logger.info(
                "Completed agent to extract ticket numbers from email_id=%s tickets=%s",
                email.id,
                ticket_groups,
            )
            incident_numbers = next((group["incident"] for group in ticket_groups if "incident" in group), [])
            adhoc_numbers = next((group["adhoc"] for group in ticket_groups if "adhoc" in group), [])

            all_tickets: list[tuple[str, str]] = [
                *[("incident", ticket_number) for ticket_number in incident_numbers],
                *[("adhoc", ticket_number) for ticket_number in adhoc_numbers],
            ]

            if len(all_tickets) > 1:
                response = self._handle_multi_ticket_email(email, result, ticket_groups)
            elif len(all_tickets) == 1:
                ticket_type, ticket_number = all_tickets[0]
                response = self.core_process_email(
                    ticket_number,
                    ticket_type,
                    result,
                    email,
                    finalize_email=True,
                )
            else:
                logger.info("Agent started processing to reply ticket-not-found emails ................")
                logger.info(
                    "Replying ticket-not-found email_id=%s category=%s",
                    email.id,
                    result.category,
                )
                reply = build_no_ticket_found_into_mail_reply(sender_name=email.sender_name)  
                self._mailbox_client.reply_email(email.id, reply)
                response['action'] = "replied: Ticket not found." ,
                response['reason'] = "No incident number or ServiceNow recipient detected, classified as {result.category}".format(result=result)
                response['processed_count'] += 1
                logger.info("Completed agent to reply ticket-not-found emails : %s", email.id)
    
        return response 
    
    def core_process_email(self, ticket_number: str, ticket_type: str, AI_result, email, finalize_email: bool = True):
        # This method can be used to core process email and can be called from process_unread_emails or can be used independently for processing single email

        logger.info("Agent started processing to check for ServiceNow emails ................")
        servicenow_recipient_present = is_servicenow_cced(email)
        logger.info("Completed agent to check for ServiceNow :%s emails : %s", servicenow_recipient_present, email.id)
        response = {'action':None,'reason':None,'processed_count':0}
            
        if ( ticket_number and f"Incident {ticket_number} has been opened for you" in email.subject) or servicenow_recipient_present:
            AI_result = replace(AI_result, category="Service-now")

        if (AI_result.category in self._general_categories or AI_result.category in _NO_REPLY_CATEGORIES) and not (ticket_number or servicenow_recipient_present):
            action = f"no_reply:{AI_result.category}"
            logger.info(
                "Suppressing reply email_id=%s category=%s no_incident_or_servicenow=true",
                email.id,
                AI_result.category,
            )
            self._metrics.increment("emails_general_or_bot_skipped")
            response['action'] = action ,
            response['reason'] = "No incident number or ServiceNow recipient detected, classified as {AI_result.category}".format(AI_result=AI_result)
            response['processed_count'] += 1

        try:
            response = self._is_ticket_number(email, ticket_number, ticket_type)
        except Exception as e:
            logger.error("Error processing ticket number %s for email_id=%s: %s", ticket_number, email.id, str(e))
            response['action'] = "error_processing_ticket_number" ,
            response['reason'] = f"Error processing ticket number {ticket_number}: {str(e)}"
            response['processed_count'] += 1    
        if finalize_email:
            folder = self._folder_mapper.to_folder(AI_result.category)
            self._mailbox_client.move_email(email.id, folder)
            reason = AI_result.reason if AI_result.reason else response.get('reason')
            self._repository.save(email.id, AI_result.category, folder, reason)
            self._metrics.increment("emails_processed")

            response['action'] = response['action'] ,
            response['reason'] = reason
            response['processed_count'] += 1
            
            self._audit_logger.log({
                "email_id": email.id,
                "category": AI_result.category,
                "folder": folder,
                "reason": reason,
                "action": response['action'],
            })
            logger.info(
                "Processed email_id=%s category=%s folder=%s action=%s",
                email.id,
                AI_result.category,
                folder,
                response['action'],
            )
            logger.info("Completed processing for email_id=%s", email.id)
        else:
            logger.info(
                "Processed ticket number %s for email_id=%s. Deferring email finalization until remaining ticket numbers are handled.",
                ticket_number,
                email.id,
            )
        return response

       