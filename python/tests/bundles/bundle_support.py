"""Helpers shared by the pack/bundle tests (imported by module name)."""
from pathlib import Path

import yaml

FIXTURES = Path(__file__).resolve().parent / "fixtures"
CATALOG = FIXTURES / "library"


def edit_yaml(path: Path, change) -> None:
    """Load `path`, let `change(data)` mutate it, write it back."""
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    change(data)
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
