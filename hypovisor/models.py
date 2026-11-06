"""SQLAlchemy models: the canonical event store plus the food knowledge base.

All timestamps (`ts`) are naive UTC. `utc_offset_min` is the local offset that was in
effect, so local hour-of-day can be reconstructed exactly.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class GlucoseReading(Base):
    __tablename__ = "glucose_readings"
    __table_args__ = (
        UniqueConstraint("source", "ts", name="uq_glucose_source_ts"),
        Index("ix_glucose_ts", "ts"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    utc_offset_min: Mapped[int] = mapped_column(Integer, default=0)
    value_mgdl: Mapped[float | None] = mapped_column(Float)  # None when the sensor reported Low/High text
    raw_label: Mapped[str | None] = mapped_column(String(32))  # "Low" / "High" / localized label
    trend_rate: Mapped[float | None] = mapped_column(Float)  # mg/dL per minute if available
    source: Mapped[str] = mapped_column(
        String(32), nullable=False
    )  # dexcom_clarity, tandem_source, nightscout...
    device: Mapped[str | None] = mapped_column(String(64))
    external_id: Mapped[str | None] = mapped_column(String(64))


class Bolus(Base):
    __tablename__ = "boluses"
    __table_args__ = (
        UniqueConstraint("source", "ts", "units_total", name="uq_bolus_source_ts_units"),
        Index("ix_bolus_ts", "ts"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime, nullable=False)  # delivery start (requested) time if known
    utc_offset_min: Mapped[int] = mapped_column(Integer, default=0)
    units_total: Mapped[float] = mapped_column(Float, nullable=False)
    units_normal: Mapped[float] = mapped_column(Float, default=0.0)
    units_extended: Mapped[float] = mapped_column(Float, default=0.0)
    extended_minutes: Mapped[int] = mapped_column(Integer, default=0)
    kind: Mapped[str] = mapped_column(String(16), default="normal")  # normal | extended | dual | automated
    reason: Mapped[str] = mapped_column(
        String(24), default="unknown"
    )  # food | correction | food_correction | auto
    carbs_entered: Mapped[float | None] = mapped_column(Float)
    bg_entered: Mapped[float | None] = mapped_column(Float)
    food_units: Mapped[float | None] = mapped_column(Float)
    correction_units: Mapped[float | None] = mapped_column(Float)
    target_bg: Mapped[float | None] = mapped_column(Float)
    isf: Mapped[float | None] = mapped_column(Float)
    icr: Mapped[float | None] = mapped_column(Float)
    status: Mapped[str | None] = mapped_column(String(32))
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    device: Mapped[str | None] = mapped_column(String(64))
    external_id: Mapped[str | None] = mapped_column(String(64))


class BasalDose(Base):
    """Basal delivery as discrete doses (Tandem Source exports commanded doses every ~5 minutes)."""

    __tablename__ = "basal_doses"
    __table_args__ = (
        UniqueConstraint("source", "ts", name="uq_basal_source_ts"),
        Index("ix_basal_ts", "ts"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    utc_offset_min: Mapped[int] = mapped_column(Integer, default=0)
    units: Mapped[float] = mapped_column(Float, nullable=False)
    duration_min: Mapped[float] = mapped_column(Float, default=5.0)
    rate_uph: Mapped[float | None] = mapped_column(Float)
    is_temp: Mapped[bool] = mapped_column(Boolean, default=False)
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    device: Mapped[str | None] = mapped_column(String(64))


class PumpEvent(Base):
    """Site changes, cartridge changes, sensor starts, suspends, alarms, exercise, notes."""

    __tablename__ = "pump_events"
    __table_args__ = (
        UniqueConstraint("source", "ts", "kind", name="uq_event_source_ts_kind"),
        Index("ix_event_ts", "ts"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    utc_offset_min: Mapped[int] = mapped_column(Integer, default=0)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    # site_change | cartridge_change | sensor_start | suspend | resume | alarm | exercise | illness | note | bg_meter
    value: Mapped[float | None] = mapped_column(Float)
    duration_min: Mapped[int | None] = mapped_column(Integer)
    notes: Mapped[str | None] = mapped_column(Text)
    source: Mapped[str] = mapped_column(String(32), nullable=False)


class Food(Base):
    """Personal food knowledge base entry. Values are per the stated serving."""

    __tablename__ = "foods"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False, unique=True)
    aliases: Mapped[list | None] = mapped_column(JSON, default=list)
    category: Mapped[str | None] = mapped_column(String(64))
    serving_desc: Mapped[str] = mapped_column(String(120), default="1 serving")
    serving_grams: Mapped[float | None] = mapped_column(Float)
    carbs_g: Mapped[float] = mapped_column(Float, default=0.0)
    fat_g: Mapped[float] = mapped_column(Float, default=0.0)
    protein_g: Mapped[float] = mapped_column(Float, default=0.0)
    fiber_g: Mapped[float] = mapped_column(Float, default=0.0)
    kcal: Mapped[float | None] = mapped_column(Float)
    gi_band: Mapped[str | None] = mapped_column(String(8))  # low | medium | high
    provenance: Mapped[str] = mapped_column(
        String(24), default="user"
    )  # user | model | usda | openfoodfacts | corrected
    confidence: Mapped[float | None] = mapped_column(Float)
    reference_id: Mapped[str | None] = mapped_column(String(64))  # FDC id or barcode
    notes: Mapped[str | None] = mapped_column(Text)
    times_eaten: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    items: Mapped[list[MealItem]] = relationship(back_populates="food")


class Meal(Base):
    __tablename__ = "meals"
    __table_args__ = (Index("ix_meal_ts", "ts"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    utc_offset_min: Mapped[int] = mapped_column(Integer, default=0)
    description: Mapped[str | None] = mapped_column(Text)  # user text
    photo_path: Mapped[str | None] = mapped_column(String(300))
    notes: Mapped[str | None] = mapped_column(Text)
    tags: Mapped[list | None] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(16), default="draft")  # draft | confirmed
    estimate_json: Mapped[dict | None] = mapped_column(JSON)  # raw structured model output
    values_source: Mapped[str] = mapped_column(String(16), default="model")  # model | user | mixed
    carbs_g: Mapped[float | None] = mapped_column(Float)
    carbs_low_g: Mapped[float | None] = mapped_column(Float)
    carbs_high_g: Mapped[float | None] = mapped_column(Float)
    fat_g: Mapped[float | None] = mapped_column(Float)
    protein_g: Mapped[float | None] = mapped_column(Float)
    fiber_g: Mapped[float | None] = mapped_column(Float)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    items: Mapped[list[MealItem]] = relationship(back_populates="meal", cascade="all, delete-orphan")
    response: Mapped[MealResponse | None] = relationship(
        back_populates="meal", uselist=False, cascade="all, delete-orphan"
    )

    @property
    def fpu(self) -> float | None:
        """Fat-protein units: (9*fat + 4*protein) / 100 kcal."""
        if self.fat_g is None and self.protein_g is None:
            return None
        return round((9 * (self.fat_g or 0) + 4 * (self.protein_g or 0)) / 100, 2)


class MealItem(Base):
    __tablename__ = "meal_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    meal_id: Mapped[int] = mapped_column(ForeignKey("meals.id", ondelete="CASCADE"), nullable=False)
    food_id: Mapped[int | None] = mapped_column(ForeignKey("foods.id", ondelete="SET NULL"))
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    portion_desc: Mapped[str | None] = mapped_column(String(120))
    grams: Mapped[float | None] = mapped_column(Float)
    carbs_g: Mapped[float | None] = mapped_column(Float)
    carbs_low_g: Mapped[float | None] = mapped_column(Float)
    carbs_high_g: Mapped[float | None] = mapped_column(Float)
    fat_g: Mapped[float | None] = mapped_column(Float)
    protein_g: Mapped[float | None] = mapped_column(Float)
    fiber_g: Mapped[float | None] = mapped_column(Float)
    confidence: Mapped[float | None] = mapped_column(Float)
    values_source: Mapped[str] = mapped_column(String(16), default="model")

    meal: Mapped[Meal] = relationship(back_populates="items")
    food: Mapped[Food | None] = relationship(back_populates="items")


class MealResponse(Base):
    """Computed glucose response fingerprint for a meal (recomputable)."""

    __tablename__ = "meal_responses"

    meal_id: Mapped[int] = mapped_column(ForeignKey("meals.id", ondelete="CASCADE"), primary_key=True)
    computed_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    n_readings: Mapped[int] = mapped_column(Integer, default=0)
    coverage: Mapped[float] = mapped_column(
        Float, default=0.0
    )  # fraction of expected 5-min readings in 0..6h
    baseline_mgdl: Mapped[float | None] = mapped_column(Float)
    baseline_trend: Mapped[float | None] = mapped_column(Float)  # mg/dL/min over the 15 min before the meal
    peak_mgdl: Mapped[float | None] = mapped_column(Float)
    time_to_peak_min: Mapped[float | None] = mapped_column(Float)
    min_mgdl: Mapped[float | None] = mapped_column(Float)
    delta_1h: Mapped[float | None] = mapped_column(Float)
    delta_2h: Mapped[float | None] = mapped_column(Float)
    delta_3h: Mapped[float | None] = mapped_column(Float)
    delta_4h: Mapped[float | None] = mapped_column(Float)
    iauc_2h: Mapped[float | None] = mapped_column(Float)  # mg/dL * min above baseline
    iauc_4h: Mapped[float | None] = mapped_column(Float)
    tir_0_2h: Mapped[float | None] = mapped_column(Float)
    tir_2_6h: Mapped[float | None] = mapped_column(Float)
    tir_0_6h: Mapped[float | None] = mapped_column(Float)
    low_event: Mapped[bool] = mapped_column(Boolean, default=False)
    late_rise: Mapped[bool] = mapped_column(Boolean, default=False)  # rise > 40 mg/dL between 2h and 5h
    bolus_units: Mapped[float | None] = mapped_column(Float)  # sum of boluses in [-60, +30] min
    prebolus_min: Mapped[float | None] = mapped_column(Float)  # minutes the first bolus preceded the meal
    carbs_entered_on_pump: Mapped[float | None] = mapped_column(Float)
    iob_at_meal: Mapped[float | None] = mapped_column(Float)
    basal_rate_at_meal: Mapped[float | None] = mapped_column(Float)
    insulin_6h: Mapped[float | None] = mapped_column(Float)  # all bolus insulin in [-60, +360]
    score: Mapped[str | None] = mapped_column(String(8))  # green | amber | red
    score_reason: Mapped[str | None] = mapped_column(String(200))
    local_hour: Mapped[int | None] = mapped_column(Integer)

    meal: Mapped[Meal] = relationship(back_populates="response")


class Setting(Base):
    """Key/value store for user-adjustable settings and sync state (tokens, last sync)."""

    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[dict | list | str | None] = mapped_column(JSON)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class ImportLog(Base):
    __tablename__ = "import_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    source: Mapped[str] = mapped_column(String(32))
    filename: Mapped[str | None] = mapped_column(String(300))
    summary: Mapped[dict | None] = mapped_column(JSON)
