class TerminalWeaveError(Exception):
    """Base domain error."""

class ValidationError(TerminalWeaveError):
    pass

class ConflictError(TerminalWeaveError):
    pass

class CapacityError(TerminalWeaveError):
    pass

class DispatchRejectedError(CapacityError):
    """No equipment can take a task; carries a specific reason per candidate."""

    def __init__(self, message: str, reasons: dict[str, list["BlockReason"]] | None = None):
        super().__init__(message)
        self.reasons_by_equipment = reasons or {}

class StateTransitionError(TerminalWeaveError):
    pass

class NotFoundError(TerminalWeaveError):
    pass

class SafetyConstraintError(TerminalWeaveError):
    pass
