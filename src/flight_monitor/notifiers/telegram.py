"""Telegram notification plugin using Bot API."""

from datetime import datetime
from typing import Optional

import requests

from .base import FlightCheckResult, Notifier

# Spanish day and month names
DAYS_ES = ["Lun", "Mar", "Mie", "Jue", "Vie", "Sab", "Dom"]
MONTHS_ES = [
    "", "Ene", "Feb", "Mar", "Abr", "May", "Jun",
    "Jul", "Ago", "Sep", "Oct", "Nov", "Dic"
]


def format_date_spanish(date_input: str | datetime) -> str:
    """Format date as 'Lun 25 May' in Spanish (short version for Telegram)."""
    try:
        if isinstance(date_input, str):
            dt = datetime.strptime(date_input, "%Y-%m-%d")
        else:
            dt = date_input
        day_name = DAYS_ES[dt.weekday()]
        month_name = MONTHS_ES[dt.month]
        return f"{day_name} {dt.day} {month_name}"
    except (ValueError, AttributeError):
        return str(date_input)


def _to_datetime(date_input: str | datetime) -> datetime:
    """Convert string or date to datetime."""
    if isinstance(date_input, str):
        return datetime.strptime(date_input, "%Y-%m-%d")
    if isinstance(date_input, datetime):
        return date_input
    # Handle date object
    return datetime.combine(date_input, datetime.min.time())


def calculate_trip_duration(depart_date: str | datetime, return_date: str | datetime) -> int:
    """Calculate trip duration in days."""
    try:
        depart = _to_datetime(depart_date)
        ret = _to_datetime(return_date)
        return (ret - depart).days
    except (ValueError, AttributeError):
        return 0


def get_price_indicator(price_level: Optional[str], recommended: bool) -> str:
    """Get visual indicator for price level."""
    if recommended:
        return "🟢"
    if price_level == "low":
        return "🟢"
    elif price_level == "typical":
        return "🟡"
    elif price_level == "high":
        return "🔴"
    return "⚪"


class TelegramNotifier(Notifier):
    """Send notifications via Telegram Bot API."""

    def __init__(
        self,
        bot_token: Optional[str] = None,
        chat_id: Optional[str] = None,
        timeout: int = 10,
    ):
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.timeout = timeout

    def is_configured(self) -> bool:
        return bool(self.bot_token and self.chat_id)

    def _sanitize_error(self, error: Exception) -> str:
        """Remove bot token from exception messages before logging them."""
        message = str(error)
        if self.bot_token:
            return message.replace(self.bot_token, "***REDACTED***")
        return message

    def send_summary(self, results: list[FlightCheckResult]) -> bool:
        """Send daily summary via Telegram."""
        if not self.is_configured():
            return False

        assert self.bot_token is not None
        assert self.chat_id is not None

        now = datetime.now()
        today_formatted = format_date_spanish(now.strftime("%Y-%m-%d"))
        time_str = now.strftime("%H:%M")

        # Check if any flight is recommended
        any_recommended = any(r.recommended for r in results if r.succeeded)

        lines = [
            "✈️ *RESUMEN DE VUELOS*",
            f"📅 {today_formatted} • {time_str}",
            "",
        ]

        if not results:
            lines.append("No hubo vuelos para resumir.")
        else:
            for result in results:
                route = f"{result.origin} → {result.destination}"
                indicator = "⚠️"

                if result.succeeded and result.offer:
                    indicator = get_price_indicator(
                        result.offer.price_level, result.recommended
                    )

                lines.append(f"{indicator} *{route}*")

                # Dates
                depart_fmt = format_date_spanish(result.depart_date)
                if result.return_date:
                    return_fmt = format_date_spanish(result.return_date)
                    duration = calculate_trip_duration(
                        result.depart_date, result.return_date
                    )
                    lines.append(f"   🗓 {depart_fmt} → {return_fmt} ({duration}d)")
                else:
                    lines.append(f"   🗓 {depart_fmt} (solo ida)")

                if result.date_unavailable:
                    lines.append("   ℹ️ La fecha ya no esta disponible.")
                    lines.append("")
                    continue

                if not result.succeeded or result.offer is None:
                    error_detail = result.error_message or "Error al consultar"
                    lines.append(f"   ❌ {error_detail}")
                    lines.append("")
                    continue

                offer = result.offer

                # Price
                if offer.adults > 1:
                    lines.append(f"   💰 {offer.currency} {offer.price:,.0f} total")
                    lines.append(
                        f"   👤 {offer.currency} {offer.price_per_person:,.0f}/persona"
                    )
                else:
                    lines.append(f"   💰 {offer.currency} {offer.price:,.0f}")

                # Airline and duration
                stops = "directo" if offer.stops == 0 else f"{offer.stops} escala(s)"
                if offer.duration_formatted:
                    lines.append(f"   🛫 {offer.airline} • {offer.duration_formatted} • {stops}")
                else:
                    lines.append(f"   🛫 {offer.airline} ({stops})")

                # Price comparison
                if offer.typical_price_low and offer.typical_price_high:
                    if result.discount_pct > 0:
                        lines.append(f"   📉 {result.discount_pct:.0f}% bajo típico 🎉")
                    else:
                        lines.append(f"   📈 {abs(result.discount_pct):.0f}% sobre típico")

                # Recommendation
                if result.recommended:
                    lines.append("   ✅ *COMPRAR AHORA*")
                else:
                    lines.append("   ⏳ Esperar mejor precio")

                # Cheaper alternatives
                cheaper_alts = result.cheaper_alternatives
                if cheaper_alts:
                    lines.append("   _Alternativas:_")
                    for alt in cheaper_alts:
                        if alt.offer:
                            savings = offer.price - alt.offer.price
                            alt_route = f"{alt.origin}→{alt.destination}"
                            lines.append(
                                f"   • {alt_route}: "
                                f"{alt.offer.currency} {alt.offer.price:,.0f} "
                                f"(-{savings:,.0f})"
                            )

                lines.append("")

        # Footer
        if any_recommended:
            lines.append("🔔 *¡Hay vuelos recomendados para comprar!*")
            lines.append("")

        lines.append("🔍 google.com/flights")

        text = "\n".join(lines)
        url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"

        try:
            resp = requests.post(
                url,
                json={
                    "chat_id": self.chat_id,
                    "text": text,
                    "parse_mode": "Markdown",
                },
                timeout=self.timeout,
            )
            if resp.ok:
                print("[Telegram] Resumen enviado.")
                return True
            else:
                print(f"[Telegram] Error: {resp.text}")
                return False
        except Exception as e:
            print(f"[Telegram] Error de conexion: {self._sanitize_error(e)}")
            return False
