from contextlib import contextmanager
from pathlib import Path
import time

from sqlalchemy import (JSON, BigInteger, Float, ForeignKey, ForeignKeyConstraint, Integer, LargeBinary, String,
                        Index, create_engine, event, text)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker


class Base(DeclarativeBase):
    pass


class ScenarioHead(Base):
    __tablename__ = 'scenarios'
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    latest_version: Mapped[int] = mapped_column(Integer)
    name: Mapped[str] = mapped_column(String(100))
    created_at: Mapped[float] = mapped_column(Float, default=time.time)


class ScenarioVersion(Base):
    __tablename__ = 'scenario_versions'
    scenario_id: Mapped[str] = mapped_column(ForeignKey('scenarios.id'), primary_key=True)
    version: Mapped[int] = mapped_column(Integer, primary_key=True)
    payload: Mapped[dict] = mapped_column(JSON)
    sha256: Mapped[str] = mapped_column(String(64))
    parent_run_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    created_at: Mapped[float] = mapped_column(Float, default=time.time)


class Run(Base):
    __tablename__ = 'analysis_runs'
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    scenario_id: Mapped[str] = mapped_column(String(36))
    version: Mapped[int] = mapped_column(Integer)
    snapshot: Mapped[dict] = mapped_column(JSON)
    snapshot_hash: Mapped[str] = mapped_column(String(64))
    adapter_revision: Mapped[str] = mapped_column(String(120))
    request_key: Mapped[str] = mapped_column(String(160), unique=True)
    request_hash: Mapped[str] = mapped_column(String(64))
    state: Mapped[str] = mapped_column(String(24), default='queued', index=True)
    stage: Mapped[str] = mapped_column(String(24), default='validate')
    attempt: Mapped[int] = mapped_column(Integer, default=0)
    owner: Mapped[str | None] = mapped_column(String(36), nullable=True)
    lease_until: Mapped[float | None] = mapped_column(Float, nullable=True)
    result: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    error: Mapped[str | None] = mapped_column(String(400), nullable=True)
    created_at: Mapped[float] = mapped_column(Float, default=time.time)
    completed_at: Mapped[float | None] = mapped_column(Float, nullable=True)
    __table_args__ = (Index('ix_run_claim', 'state', 'lease_until', 'created_at'),
                     ForeignKeyConstraint(['scenario_id', 'version'], ['scenario_versions.scenario_id', 'scenario_versions.version']))


class EventRecord(Base):
    __tablename__ = 'analysis_events'
    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, 'sqlite'), primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(ForeignKey('analysis_runs.id', ondelete='CASCADE'), index=True)
    attempt: Mapped[int] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(String(32))
    payload: Mapped[dict] = mapped_column(JSON)
    __table_args__ = (Index('ix_event_cursor', 'run_id', 'id'),)


class ReportRecord(Base):
    __tablename__ = 'reports'
    run_id: Mapped[str] = mapped_column(ForeignKey('analysis_runs.id', ondelete='CASCADE'), primary_key=True)
    revision: Mapped[int] = mapped_column(Integer, primary_key=True)
    payload: Mapped[dict] = mapped_column(JSON)


class Preset(Base):
    __tablename__ = 'presets'
    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    revision: Mapped[int] = mapped_column(Integer)
    payload: Mapped[dict] = mapped_column(JSON)


class UploadRecord(Base):
    __tablename__ = 'uploads'
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    filename: Mapped[str] = mapped_column(String(200))
    encoding: Mapped[str] = mapped_column(String(20))
    size_bytes: Mapped[int] = mapped_column(Integer)
    sha256: Mapped[str] = mapped_column(String(64))
    raw: Mapped[bytes] = mapped_column(LargeBinary)
    columns: Mapped[list] = mapped_column(JSON)
    rows: Mapped[list] = mapped_column(JSON)
    row_count: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[float] = mapped_column(Float, default=time.time)


class Database:
    def __init__(self, url):
        self.url = url
        self.engine = create_engine(url, connect_args={'check_same_thread': False} if url.startswith('sqlite') else {}, pool_pre_ping=True)
        if url.startswith('sqlite'):
            @event.listens_for(self.engine, 'connect')
            def sqlite_options(connection, _):
                connection.execute('PRAGMA foreign_keys=ON')
                connection.execute('PRAGMA busy_timeout=10000')
        self.Session = sessionmaker(self.engine, expire_on_commit=False)

    def migrate(self):
        from alembic import command
        from alembic.config import Config
        cfg = Config()
        cfg.set_main_option('script_location', str(Path(__file__).resolve().parent/'migrations'))
        with self.engine.begin() as connection:
            cfg.attributes['connection'] = connection
            command.upgrade(cfg, 'head')
        from .presets import seed
        seed(self)

    def check_schema(self):
        with self.engine.connect() as connection:
            version = connection.scalar(text('SELECT version_num FROM alembic_version'))
        if version != 'be2_0002':
            raise RuntimeError('먼저 be2 init으로 DB 스키마를 갱신하세요')

    @contextmanager
    def session(self):
        with self.Session() as db:
            try:
                yield db
            except BaseException:
                db.rollback()
                raise
