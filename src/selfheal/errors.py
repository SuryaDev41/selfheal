"""Domain errors shared by execution and selector healing."""


class UnsupportedStepError(RuntimeError):
    """A spreadsheet instruction cannot be executed safely or deterministically."""


class ActionFailedError(RuntimeError):
    """A browser action or verification failed."""


class HealingApprovalRequiredError(UnsupportedStepError):
    """A new AI locator was found but automatic healing is disabled."""
