"""Persistence for chat history and alert events.

SQLite by default (zero-config); set DATABASE_URL to a PostgreSQL URL to run
on PostgreSQL, e.g. postgresql+psycopg://user:pass@db:5432/weathergpt.
"""
from __future__ import annotations

import logging
from contextlib import contextmanager
from datetime import datetime, timezone

from sqlalchemy import Column, DateTime, Float, Integer, String, Text, create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from .config import settings

log = logging.getLogger("weathergpt.store")


class Base(DeclarativeBase):
    pass


class ChatMessage(Base):
    __tablename__ = "chat_messages"
    id = Column(Integer, primary_key=True, autoincrement=True)
    ts = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    language = Column(String(8), default="en")
    intent = Column(String(32), default="fallback")
    question = Column(Text)
    answer = Column(Text)
    source_type = Column(String(16), default="chat")  # chat | voice
    latency_ms = Column(Float, default=0.0)


class AlertEvent(Base):
    __tablename__ = "alert_events"
    id = Column(Integer, primary_key=True, autoincrement=True)
    ts = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    source_city = Column(String(64))
    event_type = Column(String(128))
    severity = Column(String(32))
    description = Column(Text)
    lat = Column(Float)
    lon = Column(Float)
    fingerprint = Column(String(128), index=True)
    active = Column(Integer, default=1)


class WeatherSnapshot(Base):
    __tablename__ = "weather_snapshots"
    id = Column(Integer, primary_key=True, autoincrement=True)
    ts = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    lat = Column(Float)
    lon = Column(Float)
    name = Column(String(128))
    payload = Column(Text)


_engine = create_engine(settings.database_url, pool_pre_ping=True, connect_args={"check_same_thread": False} if settings.database_url.startswith("sqlite") else {})
_SessionLocal = sessionmaker(bind=_engine, autoflush=False, expire_on_commit=False)


def init_db() -> None:
    Base.metadata.create_all(_engine)


@contextmanager
def db_session() -> Session:
    session = _SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def save_chat(question: str, answer: str, language: str, intent: str, latency_ms: float, source_type: str = "chat") -> None:
    try:
        with db_session() as s:
            s.add(ChatMessage(question=question[:4000], answer=answer[:8000], language=language,
                              intent=intent, latency_ms=latency_ms, source_type=source_type))
    except Exception as exc:
        log.debug("chat save skipped: %s", exc)


def recent_chat(limit: int = 25) -> list[dict]:
    try:
        with db_session() as s:
            rows = s.query(ChatMessage).order_by(ChatMessage.id.desc()).limit(limit).all()
            return [{"ts": r.ts.isoformat(), "language": r.language, "intent": r.intent,
                     "question": r.question, "answer": r.answer[:220], "latency_ms": r.latency_ms}
                    for r in rows]
    except Exception:
        return []


def upsert_alert(fingerprint: str, city: str, event: str, severity: str, description: str,
                 lat: float, lon: float) -> bool:
    """Return True if this is a NEW alert (never seen before)."""
    try:
        with db_session() as s:
            existing = s.query(AlertEvent).filter(AlertEvent.fingerprint == fingerprint).first()
            if existing:
                existing.active = 1
                return False
            s.add(AlertEvent(fingerprint=fingerprint, source_city=city, event_type=event,
                             severity=severity, description=description[:2000], lat=lat, lon=lon))
            return True
    except Exception as exc:
        log.debug("alert upsert skipped: %s", exc)
        return False


def recent_alerts(limit: int = 30) -> list[dict]:
    try:
        with db_session() as s:
            rows = (s.query(AlertEvent).order_by(AlertEvent.id.desc()).limit(limit).all())
            return [{"ts": r.ts.isoformat(), "city": r.source_city, "event": r.event_type,
                     "severity": r.severity, "description": r.description, "lat": r.lat, "lon": r.lon}
                    for r in rows]
    except Exception:
        return []


def stats() -> dict:
    from sqlalchemy import func
    try:
        with db_session() as s:
            chat_count = s.query(func.count(ChatMessage.id)).scalar() or 0
            alert_count = s.query(func.count(AlertEvent.id)).scalar() or 0
            return {"chat_messages": chat_count, "alert_events": alert_count}
    except Exception:
        return {"chat_messages": 0, "alert_events": 0}
