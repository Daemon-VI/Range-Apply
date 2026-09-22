"""Durable AI accounting: one row per gateway decision (including DISABLED
and cache hits), never the prompt or the output."""

import uuid

from sqlalchemy import Boolean, Column, DateTime, Float, Index, Integer, String

from app.core.timeutils import db_now
from app.jobs.database.models import Base


def new_id() -> str:
    return str(uuid.uuid4())


class AIUsageRow(Base):
    __tablename__ = "ai_usage"

    id = Column(String(36), primary_key=True, default=new_id)
    tenant_id = Column(String(64), nullable=False, index=True)
    operation = Column(String(48), nullable=False)
    scope = Column(String(16), nullable=False, default="job")
    provider = Column(String(32), nullable=True)
    model = Column(String(128), nullable=True)
    status = Column(String(24), nullable=False)
    cache_hit = Column(Boolean, nullable=False, default=False)
    latency_ms = Column(Integer, nullable=False, default=0)
    input_tokens = Column(Integer, nullable=True)
    output_tokens = Column(Integer, nullable=True)
    estimated_cost_usd = Column(Float, nullable=True)
    fallback_reason = Column(String(256), nullable=True)
    prompt_version = Column(String(32), nullable=False, default="v1")
    gateway_version = Column(String(32), nullable=False)
    reference = Column(String(128), nullable=True)
    created_at = Column(DateTime, nullable=False, default=db_now)

    __table_args__ = (
        Index("ix_ai_usage_tenant_created", "tenant_id", "created_at"),
        Index("ix_ai_usage_tenant_operation", "tenant_id", "operation"),
    )
