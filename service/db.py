import datetime as dt
import uuid
from functools import lru_cache

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Integer, String, create_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker

from service.settings import database_url


def now():
    return dt.datetime.now(dt.timezone.utc)


def uid():
    return str(uuid.uuid4())


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = 'users'
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    username: Mapped[str] = mapped_column(String(64), unique=True)
    email: Mapped[str] = mapped_column(String(254), unique=True)
    status: Mapped[str] = mapped_column(String(24), default='pending')
    email_verified: Mapped[bool] = mapped_column(Boolean, default=False)
    admin: Mapped[bool] = mapped_column(Boolean, default=False)
    totp_secret: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=now)


class Identity(Base):
    __tablename__ = 'identities'
    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey('users.id'))
    provider: Mapped[str] = mapped_column(String(64), default='local')
    subject: Mapped[str] = mapped_column(String(128))


class LocalCredential(Base):
    __tablename__ = 'local_credentials'
    user_id: Mapped[str] = mapped_column(ForeignKey('users.id'), primary_key=True)
    password_hash: Mapped[str] = mapped_column(String(512))


class Space(Base):
    __tablename__ = 'spaces'
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    user_id: Mapped[str] = mapped_column(ForeignKey('users.id'), unique=True)
    owner_kind: Mapped[str] = mapped_column(String(16), default='user')
    runtime_status: Mapped[str] = mapped_column(String(24), default='stopped')


class LoginSession(Base):
    __tablename__ = 'login_sessions'
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    user_id: Mapped[str] = mapped_column(ForeignKey('users.id'), index=True)
    access_hash: Mapped[str] = mapped_column(String(64), unique=True)
    refresh_hash: Mapped[str] = mapped_column(String(64), unique=True)
    csrf_hash: Mapped[str] = mapped_column(String(64))
    access_until: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True))
    refresh_until: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True))
    revoked: Mapped[bool] = mapped_column(Boolean, default=False)
    device: Mapped[str] = mapped_column(String(256), default='')
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=now)


class ActionToken(Base):
    __tablename__ = 'action_tokens'
    digest: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey('users.id'))
    kind: Mapped[str] = mapped_column(String(32))
    expires_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True))
    consumed: Mapped[bool] = mapped_column(Boolean, default=False)
    session_id: Mapped[str | None] = mapped_column(String(36))


class NativeFlow(Base):
    __tablename__ = 'native_flows'
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    challenge: Mapped[str] = mapped_column(String(64))
    redirect_uri: Mapped[str] = mapped_column(String(512))
    state: Mapped[str] = mapped_column(String(256))
    user_id: Mapped[str | None] = mapped_column(ForeignKey('users.id'))
    code_hash: Mapped[str | None] = mapped_column(String(64), unique=True)
    expires_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True))
    consumed: Mapped[bool] = mapped_column(Boolean, default=False)


class Usage(Base):
    __tablename__ = 'usage'
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey('users.id'))
    day: Mapped[str] = mapped_column(String(10))
    tokens: Mapped[int] = mapped_column(Integer, default=0)
    inflight: Mapped[int] = mapped_column(Integer, default=0)


class ModelCall(Base):
    __tablename__ = 'model_calls'
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    user_id: Mapped[str] = mapped_column(ForeignKey('users.id'))
    usage_id: Mapped[str] = mapped_column(ForeignKey('usage.id'))
    reserved: Mapped[int] = mapped_column(Integer)
    actual: Mapped[int | None] = mapped_column(Integer)
    state: Mapped[str] = mapped_column(String(24), default='running')
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=now)


class Audit(Base):
    __tablename__ = 'audit_events'
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    actor: Mapped[str] = mapped_column(String(64))
    event: Mapped[str] = mapped_column(String(64))
    target: Mapped[str] = mapped_column(String(64))
    detail: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=now)


class Attempt(Base):
    __tablename__ = 'auth_attempts'
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    count: Mapped[int] = mapped_column(Integer, default=0)
    until: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True))


@lru_cache
def factory():
    return sessionmaker(create_engine(database_url(), pool_pre_ping=True), expire_on_commit=False)


def audit(db, actor, event, target='', **detail):
    db.add(Audit(actor=actor, event=event, target=target, detail=detail))
