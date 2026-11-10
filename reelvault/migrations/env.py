"""Alembic environment for ReelVault Members (WP1). The schema source of truth is reelvault/server/db.py."""
from alembic import context
from sqlalchemy import engine_from_config, pool

from reelvault.server import db

config = context.config
target_metadata = db.metadata


def run_offline() -> None:
    context.configure(url=config.get_main_option("sqlalchemy.url"), target_metadata=target_metadata,
                      literal_binds=True, render_as_batch=True)
    with context.begin_transaction():
        context.run_migrations()


def run_online() -> None:
    conn = config.attributes.get("connection")
    if conn is None:
        url = config.get_main_option("sqlalchemy.url") or db.get_engine().url.render_as_string(hide_password=False)
        engine = engine_from_config({"sqlalchemy.url": url}, prefix="sqlalchemy.", poolclass=pool.NullPool)
        with engine.connect() as c:
            context.configure(connection=c, target_metadata=target_metadata, render_as_batch=True)
            with context.begin_transaction():
                context.run_migrations()
        return
    context.configure(connection=conn, target_metadata=target_metadata, render_as_batch=True)
    with context.begin_transaction():
        context.run_migrations()


if context.is_offline_mode():
    run_offline()
else:
    run_online()
