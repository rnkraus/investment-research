"""Register every module-owned SQLAlchemy mapping in shared metadata."""

from sqlalchemy import MetaData

from investment_research.infrastructure.database.base import Base
from investment_research.modules.documents import persistence as _documents_persistence
from investment_research.modules.evidence import persistence as _evidence_persistence
from investment_research.modules.identity import persistence as _identity_persistence
from investment_research.modules.operations import persistence as _operations_persistence
from investment_research.modules.processing import persistence as _processing_persistence
from investment_research.modules.research import persistence as _research_persistence
from investment_research.modules.search import persistence as _search_persistence

MAPPING_MODULES = (
    _identity_persistence,
    _documents_persistence,
    _processing_persistence,
    _search_persistence,
    _research_persistence,
    _evidence_persistence,
    _operations_persistence,
)


def get_model_metadata() -> MetaData:
    """Return metadata after importing every module-owned mapping."""

    return Base.metadata
