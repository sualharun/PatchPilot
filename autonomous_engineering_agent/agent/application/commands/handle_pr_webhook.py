from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from agent.application.ports.outbound import AuditLog, Clock, PRJobPublisher
from agent.domain.entities import PRAnalysisJob
from agent.domain.services import pr_job_from_payload


@dataclass(frozen=True, slots=True)
class EnqueuePullRequestAnalysisCommand:
    payload: dict[str, Any]
    delivery_id: str


class EnqueuePullRequestAnalysisHandler:
    def __init__(self, publisher: PRJobPublisher, audit_log: AuditLog, clock: Clock) -> None:
        self._publisher = publisher
        self._audit_log = audit_log
        self._clock = clock

    def execute(self, command: EnqueuePullRequestAnalysisCommand) -> PRAnalysisJob | None:
        job = pr_job_from_payload(
            command.payload,
            delivery_id=command.delivery_id,
            enqueued_at=float(self._clock.timestamp()),
        )
        if job is None:
            return None
        self._publisher.publish(job)
        self._audit_log.record_event(
            actor=job.sender_login or "github",
            event="pr_analysis.enqueued",
            target=f"{job.full_name}#{job.pr_number}",
            metadata={
                "owner": job.owner,
                "repo": job.repo,
                "pr_number": job.pr_number,
                "commit_sha": job.commit_sha,
                "action": job.action,
                "delivery_id": job.delivery_id,
                "installation_id": job.installation_id,
                "sender_login": job.sender_login,
                "enqueued_at": job.enqueued_at,
            },
        )
        return job
