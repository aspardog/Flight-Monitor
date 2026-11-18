"""Base protocol and utilities for notification plugins."""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class FlightOffer:
    """Represents a flight offer."""

    price: float
    currency: str
    airline: str
    segments: list[str]
    stops: int
    origin: str
    destination: str
    depart_date: str
    return_date: Optional[str] = None
    adults: int = 1  # Number of passengers (for per-person price calculation)
    price_category: str = "other"  # "best" (LOW) or "other"
    # Flight duration in minutes
    total_duration: Optional[int] = None  # Total flight time in minutes
    # Price insights from Google Flights
    typical_price_low: Optional[float] = None  # Lower bound of typical range
    typical_price_high: Optional[float] = None  # Upper bound of typical range
    price_level: Optional[str] = None  # "low", "typical", or "high"

    @property
    def price_per_person(self) -> float:
        """Calculate price per person."""
        return self.price / self.adults if self.adults > 0 else self.price

    @property
    def duration_formatted(self) -> Optional[str]:
        """Format duration as 'Xh Ym'."""
        if self.total_duration is None:
            return None
        hours = self.total_duration // 60
        minutes = self.total_duration % 60
        if hours > 0 and minutes > 0:
            return f"{hours}h {minutes}m"
        elif hours > 0:
            return f"{hours}h"
        else:
            return f"{minutes}m"


@dataclass
class PriceRecord:
    """Represents a historical price record."""

    price: float
    currency: str
    airline: str
    checked_at: str
    price_category: str = "other"


@dataclass
class CountryAlternative:
    """Another city in the same country, priced for the exact same dates."""

    destination: str  # IATA code
    city: str  # City name as returned by Google
    offer: FlightOffer

    @property
    def price(self) -> float:
        """Total price of the alternative."""
        return self.offer.price

    def saving_vs(self, reference: Optional[FlightOffer]) -> Optional[float]:
        """Return how much cheaper this is than the monitored route (None if unknown)."""
        if reference is None:
            return None
        return reference.price - self.offer.price

    def saving_pct_vs(self, reference: Optional[FlightOffer]) -> Optional[float]:
        """Return the saving as a percentage of the monitored route price."""
        saving = self.saving_vs(reference)
        if saving is None or reference is None or reference.price <= 0:
            return None
        return saving / reference.price * 100


@dataclass
class FlightCheckResult:
    """Result of a flight price check."""

    origin: str
    destination: str
    depart_date: str
    return_date: Optional[str] = None
    offer: Optional[FlightOffer] = None
    discount_pct: float = 0.0  # Percentage below typical_price_low
    recommended: bool = False  # True if price is below typical_price_low
    error_message: Optional[str] = None
    date_unavailable: bool = False
    # Alternative airport results (only for primary flights with check_alternatives)
    alternatives: list["FlightCheckResult"] = field(default_factory=list)
    is_alternative: bool = False  # True if this is an alternative route check
    flexible_days: int = 0
    flexible_offer: Optional[FlightOffer] = None
    flexible_message: Optional[str] = None
    # Other cities in the destination country (only for primary flights)
    country_name: Optional[str] = None
    country_alternatives: list[CountryAlternative] = field(default_factory=list)
    country_missing: list[str] = field(default_factory=list)
    country_message: Optional[str] = None

    @property
    def succeeded(self) -> bool:
        """Return whether the flight check produced a valid offer."""
        return self.date_unavailable or (self.offer is not None and self.error_message is None)

    @property
    def cheaper_alternatives(self) -> list["FlightCheckResult"]:
        """Return alternatives that are cheaper than this flight."""
        if not self.offer:
            return []
        return [
            alt for alt in self.alternatives if alt.offer and alt.offer.price < self.offer.price
        ]


class Notifier(ABC):
    """Abstract base class for notification plugins."""

    @abstractmethod
    def is_configured(self) -> bool:
        """Check if this notifier has all required configuration."""
        pass

    @abstractmethod
    def send_summary(self, results: list["FlightCheckResult"]) -> bool:
        """
        Send a daily summary of all flight checks.

        Args:
            results: List of flight check results

        Returns:
            True if summary was sent successfully, False otherwise
        """
        pass


def country_summary(result: FlightCheckResult, include_notes: bool = True) -> list[str]:
    """Shared plain-text summary for same-country destination alternatives."""
    if not result.country_alternatives and not result.country_message:
        return []

    reference = result.offer
    lines = []
    for alt in result.country_alternatives:
        saving = alt.saving_vs(reference)
        saving_pct = alt.saving_pct_vs(reference)
        detail = f"{alt.offer.currency} {alt.price:,.0f}"
        if saving is not None and saving_pct is not None:
            if saving > 0:
                detail += f" · {saving_pct:.0f}% mas barato (ahorras {saving:,.0f})"
            else:
                detail += f" · {abs(saving_pct):.0f}% mas caro"
        lines.append(f"{result.origin} → {alt.destination} ({alt.city}): {detail}")

    if include_notes:
        if result.country_missing:
            lines.append(f"Sin itinerarios: {', '.join(result.country_missing)}")
        if result.country_message:
            lines.append(result.country_message)
    elif not lines and result.country_message:
        lines.append(result.country_message)
    return lines


def flexible_summary(result: FlightCheckResult) -> list[str]:
    """Shared plain-text summary for independently flexible travel dates."""
    if not result.flexible_days:
        return []
    lines = [f"Fechas flexibles: ±{result.flexible_days} dias en salida y regreso"]
    if result.flexible_message:
        lines.append(result.flexible_message)
    alt = result.flexible_offer
    if alt:
        from datetime import date

        lines.append(
            f"Mejor alternativa encontrada: {alt.depart_date} → {alt.return_date or 'solo ida'}"
        )
        if alt.return_date:
            days = (date.fromisoformat(alt.return_date) - date.fromisoformat(alt.depart_date)).days
            lines.append(f"Duracion del viaje: {days} dias")
        lines.append(f"{alt.currency} {alt.price:,.0f} · {alt.airline} · {alt.stops} escala(s)")
        if result.offer:
            saving = result.offer.price - alt.price
            if saving > 0:
                lines.append(
                    f"Ahorro vs fechas exactas: {alt.currency} {saving:,.0f} "
                    f"({saving / result.offer.price * 100:.1f}%)"
                )
            else:
                lines.append("La alternativa encontrada no mejora el precio de las fechas exactas.")
    return lines
