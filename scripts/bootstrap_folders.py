from __future__ import annotations

from pathlib import Path
import sys

import yaml

SRC_DIR = Path(__file__).resolve().parents[1] / "src"
if str(SRC_DIR) not in sys.path:
	sys.path.insert(0, str(SRC_DIR))

from app.infrastructure.mailbox.microsoft_graph_client import MicrosoftGraphMailboxClient
from app.settings.config import load_config


def main() -> None:
	cfg = load_config()
	with cfg.mapping_path.open("r", encoding="utf-8") as f:
		data = yaml.safe_load(f) or {}

	mapping = data.get("mapping", {})
	folders = sorted(set(mapping.values()))

	client = MicrosoftGraphMailboxClient()
	client.create_folders(folders)
	print(f"Ensured {len(folders)} folder(s)")


if __name__ == "__main__":
	main()
