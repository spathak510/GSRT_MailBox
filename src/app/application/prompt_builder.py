from __future__ import annotations


def build_classifier_prompt(system_prompt: str, fewshot_prompt: str, subject: str, body: str) -> str:
    return (
        f"{system_prompt.strip()}\n\n"
        f"{fewshot_prompt.strip()}\n\n"
        "Classify the email into one category.\n"
        "Return JSON: {\"category\": \"...\", \"reason\": \"...\"}.\n\n"
        f"Subject: {subject}\n"
        f"Body: {body}\n"
    )
