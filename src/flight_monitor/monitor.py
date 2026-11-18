"""Flight price monitor with support for multiple flights."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timezone
from typing import Any, Optional, Protocol

from .config import AppConfig, FlightConfig
from .notifiers.base import (
    FlightCheckResult,
    FlightOffer,
    Notifier,
    country_summary,
    flexible_summary,
)
from .storage.sqlite import SQLiteStorage


class FlightClient(Protocol):
    """Protocol for flight API clients."""

    def fetch_cheapest_offer(self, flight: FlightConfig) -> Optional[FlightOffer]:
        """Fetch the cheapest flight offer for the given configuration."""
        ...


def country_destinations(config: AppConfig, flight: FlightConfig) -> list[str]:
    """Return the other airports of the destination country, capped by configuration.

    Empty when the flight opted out or the destination is not in the country catalog.
    """
    if not flight.country_alternatives:
        return []

    country_code = config.airport_country.get(flight.destination)
    if country_code is None:
        return []

    country = config.countries.get(country_code)
    if country is None:
        return []

    # The origin can sit in the same country (BOG -> SMR): never route back to it.
    excluded = {flight.destination, flight.origin}
    others = [airport for airport in country.airports if airport not in excluded]
    return others[: flight.country_alternatives_max]


class FlightMonitor:
    """Monitor flight prices and send notifications on price drops."""

    def __init__(
        self,
        config: AppConfig,
        client: FlightClient,
        storage: SQLiteStorage,
        notifiers: list[Notifier],
        country_alternatives_enabled: bool = True,
    ):
        """
        Initialize the flight monitor.

        Args:
            config: Application configuration
            client: Flight API client (SerpApi, Amadeus, etc.)
            storage: Price history storage
            notifiers: List of notification plugins
            country_alternatives_enabled: False drops the optional country searches
                (used when the SerpApi quota only covers the core checks)
        """
        self.config = config
        self.client = client
        self.storage = storage
        self.notifiers = [n for n in notifiers if n.is_configured()]
        self.country_alternatives_enabled = country_alternatives_enabled

    def calculate_discount(self, current_price: float, typical_low: float) -> float:
        """
        Calculate the percentage discount from Google's typical low price.

        Args:
            current_price: Current flight price
            typical_low: Google's typical price range lower bound

        Returns:
            Discount percentage (positive means cheaper than typical)
        """
        if typical_low <= 0:
            return 0.0
        return ((typical_low - current_price) / typical_low) * 100

    def should_recommend(self, offer: FlightOffer) -> tuple[bool, float]:
        """
        Determine if purchase should be recommended based on Google's typical price range.

        Recommend if price is below the typical price range lower bound.

        Args:
            offer: Flight offer with price insights from Google

        Returns:
            Tuple of (should_recommend, discount_percentage)
        """
        if offer.typical_price_low is None:
            return False, 0.0

        discount_pct = self.calculate_discount(offer.price, offer.typical_price_low)

        # Recommend if price is below typical low (discount_pct > 0)
        should_buy = discount_pct > 0

        return should_buy, discount_pct

    def get_destination_alternatives(self, destination: str) -> list[str]:
        """Get alternative destination airports for a given airport code."""
        return self.config.airport_alternatives.get(destination, [])

    def country_destinations(self, flight: FlightConfig) -> list[str]:
        """Get other cities of the destination country to compare against."""
        return country_destinations(self.config, flight)

    def is_departure_date_unavailable(self, flight: FlightConfig) -> bool:
        """Return whether the departure date has already passed."""
        depart = flight.depart_date
        # Handle both date objects (from YAML auto-parsing) and strings
        if isinstance(depart, date):
            depart_date = depart
        else:
            try:
                depart_date = datetime.strptime(depart, "%Y-%m-%d").date()
            except (ValueError, TypeError):
                return False

        return depart_date < datetime.now().date()

    def expand_with_alternatives(self, flight: FlightConfig) -> list[FlightConfig]:
        """
        Expand a flight into primary + destination alternatives.

        Only expands if check_alternatives is True for the flight.
        Origin stays fixed, only destination varies.

        Args:
            flight: Primary flight configuration

        Returns:
            List with primary flight first, followed by alternative destinations
        """
        flights = [flight]

        if not flight.check_alternatives:
            return flights

        alt_destinations = self.get_destination_alternatives(flight.destination)
        for alt_dest in alt_destinations:
            alt_flight = FlightConfig(
                origin=flight.origin,
                destination=alt_dest,
                depart_date=flight.depart_date,
                return_date=flight.return_date,
                adults=flight.adults,
                currency=flight.currency,
                check_alternatives=False,  # Don't recurse
            )
            flights.append(alt_flight)

        if alt_destinations:
            print(
                f"[Alternatives] {flight.origin}->{flight.destination}: "
                f"chequeando {len(alt_destinations)} alternativa(s): {', '.join(alt_destinations)}"
            )

        return flights

    def check_flight(self, flight: FlightConfig, is_alternative: bool = False) -> FlightCheckResult:
        """
        Check price for a single flight.

        Args:
            flight: Flight configuration to check
            is_alternative: Whether this is an alternative airport check

        Returns:
            FlightCheckResult with offer details or failure metadata
        """
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        route = f"{flight.origin} -> {flight.destination}"
        alt_label = " [ALT]" if is_alternative else ""
        print(f"\n{'='*50}")
        print(f"[{now}] Chequeando {route} ({flight.depart_date}){alt_label}")

        if self.is_departure_date_unavailable(flight):
            message = "La fecha ya no esta disponible."
            print(f"[Monitor] {message} {route} ({flight.depart_date}).")
            return FlightCheckResult(
                origin=flight.origin,
                destination=flight.destination,
                depart_date=flight.depart_date,
                return_date=flight.return_date,
                date_unavailable=True,
                is_alternative=is_alternative,
            )

        # 1. Fetch current price with Google's price insights
        offer = self.client.fetch_cheapest_offer(flight)
        if offer is None:
            print(f"[Monitor] No se pudo obtener precio para {route}.")
            return FlightCheckResult(
                origin=flight.origin,
                destination=flight.destination,
                depart_date=flight.depart_date,
                return_date=flight.return_date,
                error_message="No se pudo obtener precio desde SerpApi.",
                is_alternative=is_alternative,
            )

        category_label = "LOW" if offer.price_category == "best" else "OTHER"
        if offer.adults > 1:
            total = f"{offer.currency} {offer.price:,.0f}"
            per_person = f"{offer.currency} {offer.price_per_person:,.0f}"
            print(
                f"[Monitor] Precio: {total} ({per_person}/persona, {offer.adults} pax) "
                f"[{offer.airline}, {offer.stops} escala(s), {category_label}]"
            )
        else:
            print(
                f"[Monitor] Precio encontrado: {offer.currency} {offer.price:,.0f} "
                f"({offer.airline}, {offer.stops} escala(s)) [{category_label}]"
            )

        # 2. Save to history
        self.storage.insert_price(offer)

        # 3. Compare with Google's typical price range
        should_buy, discount_pct = self.should_recommend(offer)

        if offer.typical_price_low:
            if discount_pct > 0:
                print(f"[Monitor] Precio {discount_pct:.1f}% POR DEBAJO del rango tipico")
            else:
                print(f"[Monitor] Precio {abs(discount_pct):.1f}% por encima del rango tipico")

            if should_buy:
                print("[Monitor] *** RECOMENDADO COMPRAR ***")
            else:
                print("[Monitor] Esperar mejor precio")
        else:
            print("[Monitor] Google no proporciono rango tipico para esta ruta")

        # Return result for summary
        return FlightCheckResult(
            origin=flight.origin,
            destination=flight.destination,
            depart_date=flight.depart_date,
            return_date=flight.return_date,
            offer=offer,
            discount_pct=discount_pct,
            recommended=should_buy,
            is_alternative=is_alternative,
        )

    async def check_all_flights_async(self) -> list[FlightCheckResult]:
        """Check all configured flights concurrently, including alternatives."""
        if not self.config.flights:
            print("[Monitor] No hay vuelos configurados.")
            return []

        # Build list of all flights to check (primary + alternatives)
        # Track which primary flight each expanded flight belongs to
        all_flights: list[tuple[int, FlightConfig, bool]] = []  # (primary_idx, flight, is_alt)

        for idx, primary_flight in enumerate(self.config.flights):
            expanded = self.expand_with_alternatives(primary_flight)
            for i, flight in enumerate(expanded):
                is_alt = i > 0  # First one is primary, rest are alternatives
                all_flights.append((idx, flight, is_alt))

        # Run all checks in parallel
        loop = asyncio.get_event_loop()
        max_workers = max(len(all_flights), 1)
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            tasks = [
                loop.run_in_executor(
                    executor, self.check_flight, flight, is_alt
                )
                for _, flight, is_alt in all_flights
            ]
            all_results = await asyncio.gather(*tasks)

        # Group results: attach alternatives to their primary flights
        primary_results: dict[int, FlightCheckResult] = {}
        alternatives_by_primary: dict[int, list[FlightCheckResult]] = {}

        for (primary_idx, _, is_alt), result in zip(all_flights, all_results):
            if not is_alt:
                primary_results[primary_idx] = result
            else:
                if primary_idx not in alternatives_by_primary:
                    alternatives_by_primary[primary_idx] = []
                alternatives_by_primary[primary_idx].append(result)

        # Attach alternatives to primary results
        for primary_idx, alternatives in alternatives_by_primary.items():
            if primary_idx in primary_results:
                primary_results[primary_idx].alternatives = alternatives

        # Exact queries finish first; flexible discovery uses at most two extra searches per route.
        for idx, flight in enumerate(self.config.flights):
            result = primary_results[idx]
            if not flight.flexible_days or result.date_unavailable:
                continue
            result.flexible_days = flight.flexible_days
            fetch = getattr(self.client, "fetch_flexible_offer", None)
            if fetch is None:
                result.flexible_message = "El proveedor no admite fechas flexibles."
                continue
            offer, message = await asyncio.to_thread(fetch, flight)
            result.flexible_offer = offer
            result.flexible_message = message
            if offer:
                self.storage.insert_price(offer)
            for line in flexible_summary(result):
                print(f"[Flexible] {flight.origin}->{flight.destination}: {line}")

        await self._attach_country_alternatives(primary_results)

        # Return in original order
        return [primary_results[i] for i in range(len(self.config.flights))]

    async def _attach_country_alternatives(
        self, primary_results: dict[int, FlightCheckResult]
    ) -> None:
        """Price other cities of the destination country: one extra search per flight.

        Informative only: these offers carry no typical price range, so they never
        drive a purchase recommendation.
        """
        pending: list[tuple[FlightCheckResult, FlightConfig, list[str], Any]] = []

        for idx, flight in enumerate(self.config.flights):
            result = primary_results[idx]
            destinations = self.country_destinations(flight)
            if not destinations or result.date_unavailable:
                continue

            country_code = self.config.airport_country.get(flight.destination)
            country = self.config.countries.get(country_code or "")
            result.country_name = country.name if country else None

            if not self.country_alternatives_enabled:
                result.country_message = (
                    "Alternativas del pais omitidas para preservar cuota de SerpApi."
                )
                continue

            fetch = getattr(self.client, "fetch_country_offers", None)
            if fetch is None:
                result.country_message = "El proveedor no admite alternativas por pais."
                continue

            pending.append((result, flight, destinations, fetch))

        if not pending:
            return

        outcomes = await asyncio.gather(
            *(
                asyncio.to_thread(fetch, flight, destinations)
                for _, flight, destinations, fetch in pending
            )
        )

        for (result, flight, _, _), (alternatives, missing, message) in zip(pending, outcomes):
            result.country_alternatives = alternatives
            result.country_missing = missing
            result.country_message = message
            for alternative in alternatives:
                self.storage.insert_price(alternative.offer)
            for line in country_summary(result):
                print(f"[Pais] {flight.origin}->{flight.destination}: {line}")

    def check_all_flights(self) -> list[FlightCheckResult]:
        """Check all configured flights (sync wrapper)."""
        return asyncio.run(self.check_all_flights_async())

    def print_history(self) -> None:
        """Print recent price history for all flights."""
        for flight in self.config.flights:
            route = f"{flight.origin} -> {flight.destination}"
            rows = self.storage.get_price_history(
                flight.origin, flight.destination, flight.depart_date, limit=10
            )

            if not rows:
                print(f"\n[{route}] Sin historial previo.")
                continue

            print(f"\n--- {route} ({flight.depart_date}) ---")
            print(f"    Ultimos {len(rows)} registros:")
            for record in rows:
                cat = "LOW" if record.price_category == "best" else "   "
                print(
                    f"      {record.checked_at[:16]}  {record.currency} {record.price:,.0f}  "
                    f"({record.airline}) [{cat}]"
                )

    def _send_summary(self, results: list[FlightCheckResult]) -> bool:
        """Send a summary through all configured notifiers."""
        summary_sent = True
        for notifier in self.notifiers:
            summary_sent = notifier.send_summary(results) and summary_sent
        return summary_sent

    async def run_async(self) -> None:
        """Main async loop that checks prices at configured intervals."""
        print("=" * 50)
        print("  Flight Monitor (SerpApi)")
        print(f"  Monitoreando {len(self.config.flights)} vuelo(s)")
        print("  Alerta: cuando precio < rango tipico de Google")
        print(f"  Intervalo: cada {self.config.check_interval_minutes} minutos")
        print("=" * 50)

        # Show previous history
        self.print_history()

        last_summary_date: Optional[str] = None

        # Initial check
        results = await self.check_all_flights_async()
        today = datetime.now().date().isoformat()
        self._send_summary(results)
        last_summary_date = today

        print(
            f"\n[Monitor] Corriendo. Proximo chequeo en "
            f"{self.config.check_interval_minutes} min. Ctrl+C para detener.\n"
        )

        # Periodic checks
        while True:
            await asyncio.sleep(self.config.check_interval_minutes * 60)
            results = await self.check_all_flights_async()
            today = datetime.now().date().isoformat()
            if today != last_summary_date:
                self._send_summary(results)
                last_summary_date = today

    def run(self) -> None:
        """Main entry point (sync wrapper) - continuous mode."""
        try:
            asyncio.run(self.run_async())
        except KeyboardInterrupt:
            print("\n[Monitor] Detenido por el usuario.")

    def run_once(self) -> bool:
        """Run a single check and exit (for cron jobs)."""
        print("=" * 50)
        print("  Flight Monitor (SerpApi) - Modo unico")
        print(f"  Chequeando {len(self.config.flights)} vuelo(s)")
        print("  Alerta: cuando precio < rango tipico de Google")
        print("=" * 50)

        # Run single check
        results = self.check_all_flights()

        # Send daily summary
        summary_sent = self._send_summary(results)

        print("\n[Monitor] Chequeo completado.")
        checks_ok = all(result.succeeded for result in results)

        # Primary success: flight checks worked
        # Notification failures are warnings, not fatal errors
        if not checks_ok or not results:
            print("[Monitor] Ejecucion marcada para reintento (fallos en chequeo).")
            return False

        if not summary_sent:
            print("[Monitor] AVISO: Notificaciones fallaron pero el chequeo fue exitoso.")

        return True
