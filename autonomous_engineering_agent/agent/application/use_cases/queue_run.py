"""Compatibility facade for the original queue-run use-case path."""

import time

from agent.application.commands.queue_run import QueueRunCommand, QueueRunHandler, QueueRunResult


class _SystemClock:
    def timestamp(self) -> int:
        return int(time.time())


class QueueRunUseCase(QueueRunHandler):
    def __init__(self, runs, audit_log, clock=None) -> None:
        super().__init__(runs, audit_log, clock or _SystemClock())


__all__ = ["QueueRunCommand", "QueueRunResult", "QueueRunUseCase"]
