from logging.config import fileConfig

from sqlalchemy import engine_from_config, pool

import app.ai.database.models  # noqa: F401
import app.application.database.models  # noqa: F401
import app.application.killswitch  # noqa: F401
import app.career.database.models  # noqa: F401
import app.documents.database.models  # noqa: F401
import app.execution.database.models  # noqa: F401
import app.intelligence.database.models  # noqa: F401
import app.learning.database.models  # noqa: F401
import app.pipeline.database.models  # noqa: F401
import app.preparation.database.models  # noqa: F401
import app.scheduler.database.models  # noqa: F401
import app.signals.database.models  # noqa: F401
import app.tailoring.database.models  # noqa: F401
from alembic import context
from app.config import settings
from app.jobs.database.models import Base

# this is the Alembic Config object, which provides
# access to the values within the .ini file in use.
config = context.config

# Interpret the config file for Python logging.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Set database URL from app settings
config.set_main_option("sqlalchemy.url", settings.database_url)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode."""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode."""
    # Ensure connectable uses app settings URL
    configuration = config.get_section(config.config_ini_section, {})
    configuration["sqlalchemy.url"] = settings.database_url

    connectable = engine_from_config(
        configuration,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection, target_metadata=target_metadata
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
