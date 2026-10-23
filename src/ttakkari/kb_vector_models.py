"""Rebuildable KB projections. The immutable KB ledger remains the source of truth."""
import uuid
from datetime import datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from ttakkari.models import EMBED_DIM, Base, utcnow


class KbPassageVector(Base):
    __tablename__ = "kb_passage_vectors"
    passage_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("kb_passages.id", ondelete="RESTRICT"), primary_key=True)
    model_key: Mapped[str] = mapped_column(String(200), primary_key=True)
    text_sha256: Mapped[str] = mapped_column(String(64))
    vector: Mapped[list[float]] = mapped_column(Vector(EMBED_DIM))
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
