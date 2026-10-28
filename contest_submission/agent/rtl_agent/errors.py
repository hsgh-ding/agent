class AgentError(Exception):
    """An actionable error that the CLI may display without a traceback."""


class ContextOverflow(AgentError):
    pass


class ModelError(AgentError):
    pass


class ModelFormatError(AgentError):
    pass
