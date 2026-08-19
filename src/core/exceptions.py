"""Domain-specific relay exceptions."""


class RelayError(Exception):
    """Base exception safe to map at transport boundaries."""


class ConfigurationError(RelayError):
    pass


class WorkspaceDeniedError(RelayError):
    pass


class ConversationNotFoundError(RelayError):
    pass


class RunNotFoundError(RelayError):
    pass


class RunConflictError(RelayError):
    pass


class InvalidStateError(RelayError):
    pass


class ApprovalExpiredError(RelayError):
    pass


class AgentUnavailableError(RelayError):
    pass
