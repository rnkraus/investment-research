"""Application-level research failures safe to expose through API translation."""

from uuid import UUID


class ResearchProviderUnavailableError(RuntimeError):
    """Safe application failure for one persisted request's provider operation."""

    def __init__(self, research_request_id: UUID, *, retriable: bool) -> None:
        super().__init__("research provider is unavailable")
        self.research_request_id = research_request_id
        self.retriable = retriable
