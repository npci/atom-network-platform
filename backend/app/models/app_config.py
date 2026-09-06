# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""AppConfig model — stores platform configuration key-value pairs in the database."""
from sqlalchemy import String, Text
from sqlalchemy.orm import mapped_column, Mapped
from app.core.database import Base
from app.models.base import TimestampMixin


class AppConfig(Base, TimestampMixin):
    __tablename__ = "app_configs"

    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[str] = mapped_column(Text, nullable=False, default="")
    category: Mapped[str] = mapped_column(String(50), nullable=False, default="general")
    is_secret: Mapped[bool] = mapped_column(default=False, nullable=False)
