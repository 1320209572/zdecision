"""Dependency-free bounded errors shared by optional Recall Demo bridges."""


class RecallDemoPublicationError(RuntimeError):
    """A deliberately non-sensitive publication failure."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)
