"""Email notification plugin using Gmail SMTP."""

import smtplib
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Optional

from .base import FlightCheckResult, Notifier

# Spanish day and month names
DAYS_ES = ["Lun", "Mar", "Mie", "Jue", "Vie", "Sab", "Dom"]
MONTHS_ES = [
    "", "Ene", "Feb", "Mar", "Abr", "May", "Jun",
    "Jul", "Ago", "Sep", "Oct", "Nov", "Dic"
]


def format_date_spanish(date_input: str | datetime) -> str:
    """Format date as 'Lun 25 May 2026' in Spanish."""
    try:
        if isinstance(date_input, str):
            dt = datetime.strptime(date_input, "%Y-%m-%d")
        else:
            dt = date_input
        day_name = DAYS_ES[dt.weekday()]
        month_name = MONTHS_ES[dt.month]
        return f"{day_name} {dt.day:02d} {month_name} {dt.year}"
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


def build_google_flights_url(
    origin: str,
    destination: str,
    depart_date: str,
    return_date: Optional[str] = None,
) -> str:
    """Build a Google Flights search URL."""
    if return_date:
        url = (
            f"https://www.google.com/travel/flights?q=Flights%20to%20{destination}"
            f"%20from%20{origin}%20on%20{depart_date}%20through%20{return_date}"
        )
    else:
        url = (
            f"https://www.google.com/travel/flights?q=Flights%20to%20{destination}"
            f"%20from%20{origin}%20on%20{depart_date}%20oneway"
        )

    return url


def get_price_indicator(price_level: Optional[str], recommended: bool) -> str:
    """Get visual indicator for price level."""
    if recommended:
        return "🟢"  # Green - buy now
    if price_level == "low":
        return "🟢"
    elif price_level == "typical":
        return "🟡"
    elif price_level == "high":
        return "🔴"
    return "⚪"  # Unknown


def get_recommendation_text(recommended: bool, price_level: Optional[str]) -> str:
    """Get recommendation text with indicator."""
    if recommended:
        return "🟢 COMPRAR AHORA"
    elif price_level == "high":
        return "🔴 Esperar (precio alto)"
    elif price_level == "typical":
        return "🟡 Esperar (precio tipico)"
    else:
        return "⚪ Esperar mejor precio"


class EmailNotifier(Notifier):
    """Send notifications via Gmail SMTP."""

    def __init__(
        self,
        sender: Optional[str] = None,
        password: Optional[str] = None,
        receiver: Optional[str] = None,
        smtp_host: str = "smtp.gmail.com",
        smtp_port: int = 465,
    ):
        self.sender = sender
        self.password = password
        # Support multiple receivers separated by comma
        self.receivers: list[str] = []
        if receiver:
            self.receivers = [r.strip() for r in receiver.split(",") if r.strip()]
        self.smtp_host = smtp_host
        self.smtp_port = smtp_port

    def is_configured(self) -> bool:
        return bool(self.sender and self.password and self.receivers)

    def _build_quick_summary(self, results: list[FlightCheckResult]) -> list[str]:
        """Build quick summary table at the top."""
        lines = [
            "╔══════════════════════════════════════════════════════════╗",
            "║                    RESUMEN RAPIDO                        ║",
            "╠══════════════════════════════════════════════════════════╣",
        ]

        for result in results:
            route = f"{result.origin} → {result.destination}"
            if result.succeeded and result.offer:
                price = f"{result.offer.currency} {result.offer.price:,.0f}"
                indicator = get_price_indicator(result.offer.price_level, result.recommended)
                status = "COMPRAR" if result.recommended else "Esperar"
                lines.append(f"║  {route:<12} {price:<18} {indicator} {status:<10} ║")
            else:
                lines.append(f"║  {route:<12} {'ERROR':<18} ⚠️  {'---':<10} ║")

        lines.append("╚══════════════════════════════════════════════════════════╝")
        lines.append("")
        return lines

    def _build_flight_detail(self, result: FlightCheckResult) -> list[str]:
        """Build detailed section for one flight."""
        lines = []
        route = f"{result.origin} → {result.destination}"

        # Header
        if result.return_date:
            trip_type = "ida y vuelta"
        else:
            trip_type = "solo ida"

        lines.append("─" * 58)
        lines.append(f"✈️  VUELO: {route} ({trip_type})")
        lines.append("─" * 58)

        # Dates section
        depart_formatted = format_date_spanish(result.depart_date)
        lines.append(f"  📅 Ida:        {depart_formatted}")

        if result.return_date:
            return_formatted = format_date_spanish(result.return_date)
            duration = calculate_trip_duration(result.depart_date, result.return_date)
            lines.append(f"  📅 Vuelta:     {return_formatted}")
            lines.append(f"  ⏱️  Duracion:   {duration} dias")

        lines.append("")

        if result.date_unavailable:
            lines.append("  ℹ️  Estado:     Fecha no disponible")
            lines.append("  📋 Detalle:    La fecha ya no esta disponible.")
            lines.append("")
            return lines

        # Error case
        if not result.succeeded or result.offer is None:
            error_detail = result.error_message or "No se pudo consultar el vuelo"
            lines.append("  ⚠️  Estado:     ERROR")
            lines.append(f"  📋 Detalle:    {error_detail}")
            lines.append("")
            return lines

        offer = result.offer

        # Price section
        lines.append(f"  💰 Precio:     {offer.currency} {offer.price:,.0f}")
        if offer.adults > 1:
            lines.append(f"  👤 Por persona: {offer.currency} {offer.price_per_person:,.0f}")
            lines.append(f"  👥 Pasajeros:  {offer.adults}")

        # Flight details
        stops_text = "Directo" if offer.stops == 0 else f"{offer.stops} escala(s)"
        lines.append(f"  🛫 Aerolinea:  {offer.airline} ({stops_text})")
        if offer.duration_formatted:
            lines.append(f"  ⏱️  Vuelo:      {offer.duration_formatted}")
        lines.append("")

        # Price analysis
        if offer.typical_price_low and offer.typical_price_high:
            low = f"{offer.typical_price_low:,.0f}"
            high = f"{offer.typical_price_high:,.0f}"
            lines.append(f"  📊 Rango tipico Google: {offer.currency} {low} - {high}")

            if result.discount_pct > 0:
                lines.append(f"  📉 vs Tipico:  {result.discount_pct:.1f}% MAS BARATO 🎉")
            else:
                lines.append(f"  📈 vs Tipico:  {abs(result.discount_pct):.1f}% mas caro")

        # Price level
        if offer.price_level:
            level_map = {"low": "BAJO 🟢", "typical": "TIPICO 🟡", "high": "ALTO 🔴"}
            level_es = level_map.get(offer.price_level, offer.price_level.upper())
            lines.append(f"  🏷️  Nivel:      {level_es}")

        lines.append("")

        # Recommendation
        rec_text = get_recommendation_text(result.recommended, offer.price_level)
        lines.append(f"  👉 {rec_text}")

        # Google Flights link
        gf_url = build_google_flights_url(
            result.origin,
            result.destination,
            result.depart_date,
            result.return_date,
        )
        lines.append("")
        lines.append("  🔗 Ver en Google Flights:")
        lines.append(f"     {gf_url}")
        lines.append("")

        # Cheaper alternatives section
        cheaper_alts = result.cheaper_alternatives
        if cheaper_alts:
            lines.append("  ─── Alternativas mas baratas ───")
            for alt in cheaper_alts:
                if alt.offer:
                    savings = offer.price - alt.offer.price
                    alt_route = f"{alt.origin}→{alt.destination}"
                    alt_price = f"{alt.offer.currency} {alt.offer.price:,.0f}"
                    savings_str = f"({offer.currency} {savings:,.0f} menos)"
                    lines.append(f"    • {alt_route}: {alt_price} {savings_str}")
            lines.append("")

        return lines

    def send_summary(self, results: list[FlightCheckResult]) -> bool:
        """Send daily summary email with all flight check results."""
        if not self.is_configured():
            return False

        assert self.sender is not None
        assert self.password is not None

        now = datetime.now()
        today_formatted = format_date_spanish(now.strftime("%Y-%m-%d"))
        time_str = now.strftime("%H:%M")

        lines = [
            "═" * 58,
            "          ✈️  RESUMEN DIARIO DE VUELOS  ✈️",
            "═" * 58,
            f"  📅 Fecha: {today_formatted}",
            f"  🕐 Hora:  {time_str}",
            "═" * 58,
            "",
        ]

        any_recommended = False

        if not results:
            lines.append("No hubo vuelos para resumir en esta ejecucion.")
            lines.append("")
        else:
            # Quick summary table
            lines.extend(self._build_quick_summary(results))

            # Detailed sections
            for result in results:
                lines.extend(self._build_flight_detail(result))
                if result.recommended:
                    any_recommended = True

        # Footer
        lines.extend([
            "═" * 58,
            "  🔍 Buscar vuelos: https://www.google.com/flights",
            "",
            "  ---",
            "  Flight Monitor - Resumen automatico",
            "═" * 58,
        ])

        body = "\n".join(lines)

        # Subject line
        date_short = now.strftime("%d/%m/%Y")
        if any_recommended:
            subject = f"[COMPRAR] Resumen diario de vuelos - {date_short}"
        else:
            subject = f"Resumen diario de vuelos - {date_short}"

        msg = MIMEMultipart()
        msg["From"] = self.sender
        msg["To"] = ", ".join(self.receivers)
        msg["Subject"] = subject
        msg.attach(MIMEText(body, "plain", "utf-8"))

        try:
            with smtplib.SMTP_SSL(self.smtp_host, self.smtp_port) as server:
                server.login(self.sender, self.password)
                server.sendmail(self.sender, self.receivers, msg.as_string())
            print(f"[Email] Resumen diario enviado a {', '.join(self.receivers)}")
            return True
        except Exception as e:
            print(f"[Email] Error al enviar resumen: {e}")
            return False
