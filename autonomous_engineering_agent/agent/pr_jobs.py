"""Compatibility facade for PR job domain and Kafka adapters."""

from __future__ import annotations

import time
from typing import Any

from .domain.entities import PRAnalysisJob
from .domain.services import pr_job_from_payload
from .infrastructure.kafka import KafkaPRJobConsumer, KafkaPRJobProducer, deserialize_job, serialize_job
from .infrastructure.security import verify_github_signature


def job_from_pull_request_webhook(payload: dict[str, Any], *, delivery_id: str) -> PRAnalysisJob | None:
    return pr_job_from_payload(payload, delivery_id=delivery_id, enqueued_at=time.time())


__all__ = [
    "KafkaPRJobConsumer",
    "KafkaPRJobProducer",
    "PRAnalysisJob",
    "deserialize_job",
    "job_from_pull_request_webhook",
    "serialize_job",
    "verify_github_signature",
]
