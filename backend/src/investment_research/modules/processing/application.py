"""Application services for creating and advancing document processing."""

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from investment_research.modules.identity.domain import WorkspaceContext
from investment_research.modules.processing.domain import (
    ConfigurationVersion,
    ProcessingRun,
    ProcessingState,
)


@dataclass(frozen=True, slots=True)
class ProcessingProfile:
    """Version identifiers that make one processing attempt reproducible."""

    parser_version: ConfigurationVersion
    parser_config_version: ConfigurationVersion
    chunker_version: ConfigurationVersion
    chunking_config_version: ConfigurationVersion
    lexical_config_version: ConfigurationVersion


M0_0_PROCESSING_PROFILE = ProcessingProfile(
    parser_version="pypdf-6.16.2",
    parser_config_version="m0.0-pdf-bounds-v1",
    chunker_version="m0.0-structure-block-v1",
    chunking_config_version="m0.0-structure-block-config-v1",
    lexical_config_version="m0.0-english-lexical-v1",
)


def create_captured_processing_run(
    *,
    context: WorkspaceContext,
    document_version_id: UUID,
    processing_run_id: UUID,
    created_at: datetime,
    profile: ProcessingProfile = M0_0_PROCESSING_PROFILE,
) -> ProcessingRun:
    """Create the durable processing intent associated with a captured original."""

    return ProcessingRun(
        id=processing_run_id,
        workspace_id=context.workspace_id,
        document_version_id=document_version_id,
        state=ProcessingState.CAPTURED,
        parser_version=profile.parser_version,
        parser_config_version=profile.parser_config_version,
        chunker_version=profile.chunker_version,
        chunking_config_version=profile.chunking_config_version,
        lexical_config_version=profile.lexical_config_version,
        created_at=created_at,
    )
