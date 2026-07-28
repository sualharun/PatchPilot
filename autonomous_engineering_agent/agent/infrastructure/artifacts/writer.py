import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any


class JsonArtifactWriter:
    def exists(self, path: Path) -> bool:
        return path.exists()

    def write(self, path: Path, payload: Mapping[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(dict(payload), indent=2, sort_keys=True), encoding="utf-8")
