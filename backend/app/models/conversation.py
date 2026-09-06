# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

import enum
from sqlalchemy import String, Text, Enum, ForeignKey, JSON
from sqlalchemy.orm import mapped_column, Mapped, relationship
from app.core.database import Base
from app.models.base import TimestampMixin, generate_uuid


class ConversationModule(str, enum.Enum):
    PROMPT_ENHANCER = "prompt_enhancer"
    RESEARCHER = "researcher"
    CANVAS = "canvas"
    BRD = "brd"
    TECH_SPEC = "tech_spec"
    XSD = "xsd"
    PRODUCT_KIT = "product_kit"


class MessageRole(str, enum.Enum):
    USER = "user"
    ASSISTANT = "assistant"


class Conversation(Base, TimestampMixin):
    __tablename__ = "conversations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=generate_uuid)
    change_request_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("change_requests.id"), nullable=False
    )
    module: Mapped[ConversationModule] = mapped_column(Enum(ConversationModule, values_callable=lambda x: [e.value for e in x]), nullable=False)
    role: Mapped[MessageRole] = mapped_column(Enum(MessageRole, values_callable=lambda x: [e.value for e in x]), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    metadata_: Mapped[dict | None] = mapped_column("metadata", JSON, nullable=True)
    created_by: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("users.id"), nullable=True
    )

    # Relationships
    change_request: Mapped["ChangeRequest"] = relationship(
        "ChangeRequest", back_populates="conversations"
    )
