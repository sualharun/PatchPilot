import hashlib
import hmac

from agent.pr_jobs import PRAnalysisJob, job_from_pull_request_webhook, verify_github_signature


def test_verify_github_signature():
    body = b'{"ok": true}'
    secret = "webhook-secret"
    signature = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()

    assert verify_github_signature(secret=secret, body=body, signature_header=signature) is True
    assert verify_github_signature(secret=secret, body=body, signature_header="sha256=bad") is False


def test_job_from_pull_request_webhook():
    payload = {
        "action": "synchronize",
        "repository": {"name": "example", "owner": {"login": "octo"}},
        "pull_request": {"number": 42, "head": {"sha": "abc123"}},
        "installation": {"id": 123},
        "sender": {"login": "mona"},
    }

    job = job_from_pull_request_webhook(payload, delivery_id="delivery-1")

    assert job == PRAnalysisJob(
        owner="octo",
        repo="example",
        pr_number=42,
        commit_sha="abc123",
        action="synchronize",
        delivery_id="delivery-1",
        installation_id="123",
        sender_login="mona",
        enqueued_at=job.enqueued_at,
    )


def test_job_from_pull_request_webhook_ignores_uninteresting_action():
    payload = {
        "action": "closed",
        "repository": {"name": "example", "owner": {"login": "octo"}},
        "pull_request": {"number": 42, "head": {"sha": "abc123"}},
    }

    assert job_from_pull_request_webhook(payload, delivery_id="delivery-1") is None
