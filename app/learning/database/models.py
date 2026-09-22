"""Learning persistence (tenant-scoped, append-only).

``learning_snapshots``       what the engine believed at ``as_of`` under one
                             set of versions and settings; never updated.
``learning_metrics``         every group × metric of a snapshot with counts,
                             smoothed rate, interval, confidence, evidence mix.
``learning_recommendations`` the phrased findings of a snapshot with their
                             supporting metric, sample size and window.
"""

from sqlalchemy import Column, DateTime, Float, ForeignKey, Index, Integer, String, Text
from sqlalchemy.types import JSON

from app.core.ids import new_id
from app.core.timeutils import db_now
from app.jobs.database.models import Base


class LearningSnapshotRow(Base):
    __tablename__ = "learning_snapshots"

    id = Column(String(36), primary_key=True, default=new_id)
    tenant_id = Column(String(64), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False)
    learning_version = Column(String(32), nullable=False)
    feature_version = Column(String(32), nullable=False)
    smoothing_method = Column(String(48), nullable=False)
    outcome_rules_version = Column(String(32), nullable=False)
    attribution_version = Column(String(32), nullable=False)
    as_of = Column(DateTime, nullable=False)
    window_days = Column(Integer, nullable=True)
    window_start = Column(DateTime, nullable=True)
    generated_at = Column(DateTime, nullable=False, default=db_now)
    dataset_size = Column(Integer, nullable=False, default=0)
    metric_count = Column(Integer, nullable=False, default=0)
    recommendation_count = Column(Integer, nullable=False, default=0)
    settings = Column(JSON, nullable=False, default=dict)
    baseline = Column(JSON, nullable=False, default=dict)
    summary = Column(JSON, nullable=False, default=dict)
    source_discovery = Column(JSON, nullable=False, default=list)
    actor = Column(String(128), nullable=False, default="learning")

    __table_args__ = (Index("ix_learning_snapshots_tenant_generated", "tenant_id", "generated_at"),)


class LearningMetricRow(Base):
    __tablename__ = "learning_metrics"

    id = Column(String(36), primary_key=True, default=new_id)
    tenant_id = Column(String(64), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False)
    snapshot_id = Column(String(36), ForeignKey("learning_snapshots.id", ondelete="CASCADE"), nullable=False, index=True)
    dimension = Column(String(24), nullable=False)
    group_key = Column(String(256), nullable=False)
    group_label = Column(String(256), nullable=False, default="")
    metric = Column(String(32), nullable=False)
    n = Column(Integer, nullable=False, default=0)
    positives = Column(Integer, nullable=True)
    negatives = Column(Integer, nullable=True)
    raw_rate = Column(Float, nullable=True)
    smoothed_rate = Column(Float, nullable=True)
    ci_low = Column(Float, nullable=True)
    ci_high = Column(Float, nullable=True)
    median_value = Column(Float, nullable=True)
    confidence = Column(String(8), nullable=False, default="NONE")
    baseline_rate = Column(Float, nullable=True)
    evidence = Column(JSON, nullable=False, default=dict)
    learning_version = Column(String(32), nullable=False)
    feature_version = Column(String(32), nullable=False)
    smoothing_method = Column(String(48), nullable=False)
    created_at = Column(DateTime, nullable=False, default=db_now)

    __table_args__ = (Index("ix_learning_metrics_snapshot_dimension", "snapshot_id", "dimension", "metric"),)


class LearningRecommendationRow(Base):
    __tablename__ = "learning_recommendations"

    id = Column(String(36), primary_key=True, default=new_id)
    tenant_id = Column(String(64), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False)
    snapshot_id = Column(String(36), ForeignKey("learning_snapshots.id", ondelete="CASCADE"), nullable=False, index=True)
    kind = Column(String(48), nullable=False)
    text = Column(Text, nullable=False)
    dimension = Column(String(24), nullable=False)
    group_key = Column(String(256), nullable=False)
    group_label = Column(String(256), nullable=False, default="")
    metric = Column(String(32), nullable=False)
    n = Column(Integer, nullable=False, default=0)
    positives = Column(Integer, nullable=False, default=0)
    observed_rate = Column(Float, nullable=True)
    baseline_rate = Column(Float, nullable=True)
    confidence = Column(String(8), nullable=False, default="NONE")
    evidence = Column(JSON, nullable=False, default=dict)
    learning_version = Column(String(32), nullable=False)
    window_days = Column(Integer, nullable=True)
    as_of = Column(DateTime, nullable=True)
    caveat = Column(String(256), nullable=False, default="")
    created_at = Column(DateTime, nullable=False, default=db_now)
