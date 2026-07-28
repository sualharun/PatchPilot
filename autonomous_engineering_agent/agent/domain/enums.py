from enum import StrEnum


class RunStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED_TESTS = "failed_tests"
    SETUP_FAILED = "setup_failed"
    AGENT_ERROR = "agent_error"
    PR_OPENED = "pr_opened"

    @property
    def is_success(self) -> bool:
        return self in {self.SUCCESS, self.PR_OPENED}

    @property
    def is_terminal(self) -> bool:
        return self not in {self.QUEUED, self.RUNNING}


class JobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    DEAD_LETTER = "dead_letter"
