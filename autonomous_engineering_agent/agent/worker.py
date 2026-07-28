"""Compatibility imports for the queued-run worker adapter."""

from .application.services.run_worker import WorkerResult
from .interfaces.workers.run_worker import process_queued_runs

__all__ = ["WorkerResult", "process_queued_runs"]
