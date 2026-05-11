from __future__ import annotations

from app.domain.folder_mapper import FolderMapper


def test_folder_mapper_uses_mapping_case_insensitive() -> None:
    mapper = FolderMapper({"finance": "Finance"}, default_folder="General")

    assert mapper.to_folder("FINANCE") == "Finance"


def test_folder_mapper_fallback_to_default() -> None:
    mapper = FolderMapper({"finance": "Finance"}, default_folder="General")

    assert mapper.to_folder("unknown") == "General"
