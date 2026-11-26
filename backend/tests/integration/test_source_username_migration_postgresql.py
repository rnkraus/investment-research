"""Keep captured and edited metadata intact through the source-field rename."""

from uuid import uuid4

from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, MetaData, String, Table, inspect, text

from investment_research.modules.processing.domain import ProcessingState
from tests.integration.test_company_migration_postgresql import (
    company_migration_database as company_migration_database,
)
from tests.integration.test_processing_outbox_migration_postgresql import seed_legacy_run


def test_username_rename_preserves_values_types_and_allows_rollback(
    company_migration_database: tuple[Engine, Config],
) -> None:
    engine, config = company_migration_database
    source = seed_legacy_run(engine, ProcessingState.CAPTURED)
    command.upgrade(config, "0022_query_planning")
    version_id = uuid4()
    with engine.begin() as connection:
        table = Table("document_versions", MetaData(), autoload_with=connection)
        connection.execute(
            table.insert().values(
                source.version.model_dump(exclude={"username"})
                | {"id": version_id, "version_number": 2, "x_handle": "@OriginalIR"}
            )
        )
        connection.execute(
            text(
                "INSERT INTO document_source_attribution_edits "
                "(workspace_id, document_version_id, x_handle) "
                "VALUES (:workspace, :version, '@EditedIR')"
            ),
            {"workspace": source.context.workspace_id, "version": version_id},
        )

    for target, field in (("head", "username"), ("0022_query_planning", "x_handle")):
        if target == "head":
            command.upgrade(config, target)
            command.check(config)
        else:
            command.downgrade(config, target)
        with engine.connect() as connection:
            captured = connection.execute(
                text(
                    f"SELECT {field}, title, content_sha256 FROM document_versions WHERE id = :id"
                ),
                {"id": version_id},
            ).one()
            assert captured == ("@OriginalIR", source.version.title, source.version.content_sha256)
            assert (
                connection.scalar(text(f"SELECT {field} FROM document_source_attribution_edits"))
                == "@EditedIR"
            )
        for name in ("document_versions", "document_source_attribution_edits"):
            columns = {column["name"]: column for column in inspect(engine).get_columns(name)}
            assert ("x_handle" if field == "username" else "username") not in columns
            column_type = columns[field]["type"]
            assert isinstance(column_type, String)
            assert column_type.length == 16
            assert columns[field]["nullable"]

    command.upgrade(config, "head")
    command.check(config)
