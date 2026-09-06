"""Alembic environment for the canonical PostgreSQL schema."""

from logging.config import fileConfig

from alembic import context
from sqlalchemy import MetaData, create_engine, pool

from investment_research.infrastructure.database.metadata import get_model_metadata
from investment_research.settings import get_settings

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata: MetaData = get_model_metadata()


def run_migrations_offline() -> None:
    """Render migrations without opening a database connection."""

    context.configure(
        url=get_settings().require_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations through one short-lived PostgreSQL connection."""

    engine = create_engine(
        get_settings().require_database_url(),
        poolclass=pool.NullPool,
        hide_parameters=True,
    )
    try:
        with engine.connect() as connection:
            context.configure(
                connection=connection,
                target_metadata=target_metadata,
                compare_type=True,
                compare_server_default=True,
            )

            with context.begin_transaction():
                context.run_migrations()
    finally:
        engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
