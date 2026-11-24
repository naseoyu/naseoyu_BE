from alembic import context
from sqlalchemy import create_engine
from be2_backend.config import Settings
from be2_backend.db import Base


def run(connection):
    context.configure(connection=connection, target_metadata=Base.metadata)
    with context.begin_transaction():
        context.run_migrations()


connection = context.config.attributes.get('connection')
if connection is not None:
    run(connection)
elif context.is_offline_mode():
    context.configure(url=Settings().database_url, target_metadata=Base.metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()
else:
    engine = create_engine(Settings().database_url)
    with engine.connect() as connection:
        run(connection)
