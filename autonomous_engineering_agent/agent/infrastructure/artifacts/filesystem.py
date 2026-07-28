from pathlib import Path


class FilesystemArtifactCatalog:
    def list_artifacts(self, location: str) -> list[str]:
        root = Path(location)
        if not root.exists():
            return []
        return [str(path) for path in sorted(root.glob("*.json"), reverse=True)]
