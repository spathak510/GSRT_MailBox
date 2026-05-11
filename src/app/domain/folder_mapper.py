from __future__ import annotations


class FolderMapper:
    def __init__(self, mapping: dict[str, str], default_folder: str = "General") -> None:
        self._mapping = {k.strip().lower(): v.strip() for k, v in mapping.items()}
        self._default_folder = default_folder

    def to_folder(self, category: str) -> str:
        return self._mapping.get(category.strip().lower(), self._default_folder)
