from __future__ import annotations

import json
from collections.abc import Iterable
from typing import Any

from agent.domain.entities import PRAnalysisJob
from agent.domain.value_objects import RepositoryRef
from agent.infrastructure.config.settings import AgentConfig


def serialize_job(job: PRAnalysisJob) -> dict[str, Any]:
    return {
        "owner": job.owner,
        "repo": job.repo,
        "pr_number": job.pr_number,
        "commit_sha": job.commit_sha,
        "action": job.action,
        "delivery_id": job.delivery_id,
        "installation_id": job.installation_id,
        "sender_login": job.sender_login,
        "enqueued_at": job.enqueued_at,
    }


def deserialize_job(data: dict[str, Any]) -> PRAnalysisJob:
    return PRAnalysisJob(
        repository=RepositoryRef(str(data["owner"]), str(data["repo"])),
        pr_number=int(data["pr_number"]),
        commit_sha=str(data["commit_sha"]),
        action=str(data["action"]),
        delivery_id=str(data.get("delivery_id") or ""),
        installation_id=str(data["installation_id"]) if data.get("installation_id") is not None else None,
        sender_login=str(data["sender_login"]) if data.get("sender_login") is not None else None,
        enqueued_at=float(data.get("enqueued_at") or 0.0),
    )


class KafkaPRJobProducer:
    def __init__(self, config: AgentConfig) -> None:
        self.bootstrap_servers = config.kafka_bootstrap_servers
        self.topic = config.kafka_pr_analysis_topic
        self._producer = None

    def publish(self, job: PRAnalysisJob) -> None:
        producer = self._get_producer()
        producer.send(self.topic, serialize_job(job)).get(timeout=10)
        producer.flush(timeout=10)

    def _get_producer(self):
        if self._producer is None:
            from kafka import KafkaProducer

            self._producer = KafkaProducer(
                bootstrap_servers=self.bootstrap_servers,
                value_serializer=lambda value: json.dumps(value, sort_keys=True).encode("utf-8"),
                key_serializer=lambda value: value.encode("utf-8"),
                retries=3,
                acks="all",
            )
        return self._producer


class KafkaPRJobConsumer:
    def __init__(self, config: AgentConfig) -> None:
        self.bootstrap_servers = config.kafka_bootstrap_servers
        self.topic = config.kafka_pr_analysis_topic
        self.group_id = config.kafka_consumer_group
        self._consumer = None

    def jobs(self) -> Iterable[PRAnalysisJob]:
        for message in self._get_consumer():
            yield deserialize_job(message.value)

    def commit(self) -> None:
        self._get_consumer().commit()

    def _get_consumer(self):
        if self._consumer is None:
            from kafka import KafkaConsumer

            self._consumer = KafkaConsumer(
                self.topic,
                bootstrap_servers=self.bootstrap_servers,
                group_id=self.group_id,
                enable_auto_commit=False,
                auto_offset_reset="earliest",
                value_deserializer=lambda raw: json.loads(raw.decode("utf-8")),
            )
        return self._consumer
