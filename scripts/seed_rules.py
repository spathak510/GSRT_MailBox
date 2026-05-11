from __future__ import annotations

from pathlib import Path


DEFAULT_RULES = """rules:
	- category: finance
		keywords:
			- invoice
			- payment
			- reimbursement
		sender_contains: billing

	- category: internal
		keywords:
			- meeting
			- standup
			- sync

	- category: marketing
		keywords:
			- offer
			- discount
			- sale
"""


def main() -> None:
	repo_root = Path(__file__).resolve().parents[1]
	path = repo_root / "data/rules/classification_rules.yaml"
	path.parent.mkdir(parents=True, exist_ok=True)

	if path.exists() and path.read_text(encoding="utf-8").strip():
		print(f"Rules already present: {path}")
		return

	path.write_text(DEFAULT_RULES, encoding="utf-8")
	print(f"Seeded rules: {path}")


if __name__ == "__main__":
		main()
