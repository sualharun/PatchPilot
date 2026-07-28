class DomainError(ValueError):
    """Base error for a violated business invariant."""


class InvalidIssueReference(DomainError):
    pass


class InvalidRunTransition(DomainError):
    pass


class InvalidPullRequestEvent(DomainError):
    pass
