"""Compatibility facade for the FastAPI inbound adapter."""

from .interfaces.http import dashboard as _dashboard

KafkaPRJobProducer = _dashboard.KafkaPRJobProducer


def create_app(database_url: str | None = None):
    # Preserve the original monkeypatch seam used by integrations and tests.
    _dashboard.KafkaPRJobProducer = KafkaPRJobProducer  # type: ignore[misc]
    return _dashboard.create_app(database_url)


def serve_dashboard(host: str, port: int, database_url: str | None = None) -> None:
    _dashboard.KafkaPRJobProducer = KafkaPRJobProducer  # type: ignore[misc]
    _dashboard.serve_dashboard(host, port, database_url)


__all__ = ["KafkaPRJobProducer", "create_app", "serve_dashboard"]
