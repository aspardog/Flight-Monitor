"""Flight price monitor with support for multiple flights."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Optional, Protocol

from .config import AppConfig, FlightConfig
from .notifiers.base import FlightCheckResult, FlightOffer, Notifier
from .storage.sqlite import SQLiteStorage


class FlightClient(Protocol):
    """Protocol for flight API clients."""

    def fetch_cheapest_offer(self, flight: FlightConfig) -> Optional[FlightOffer]:
        """Fetch the cheapest flight offer for the given configuration."""
        ...


class FlightMonitor:
    """Monitor flight prices and send notifications on price drops."""

    def __init__(
        self,
        config: AppConfig,
        client: FlightClient,
        storage: SQLiteStorage,
        notifiers: list[Notifier],
    ):
        """
        Initialize the flight monitor.

        Args:
            config: Application configuration
            client: Flight API client (SerpApi, Amadeus, etc.)
            storage: Price history storage
            notifiers: List of notification plugins
        """
        self.config = config
        self.client = client
        self.storage = storage
        self.notifiers = [n for n in notifiers if n.is_configured()]

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

    def check_flight(self, flight: FlightConfig) -> FlightCheckResult:
        """
        Check price for a single flight.

        Args:
            flight: Flight configuration to check

        Returns:
            FlightCheckResult with offer details or failure metadata
        """
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        route = f"{flight.origin} -> {flight.destination}"
        print(f"\n{'='*50}")
        print(f"[{now}] Chequeando {route} ({flight.depart_date})")

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
        )

    async def check_all_flights_async(self) -> list[FlightCheckResult]:
        """Check all configured flights concurrently."""
        if not self.config.flights:
            print("[Monitor] No hay vuelos configurados.")
            return []

        loop = asyncio.get_event_loop()
        with ThreadPoolExecutor(max_workers=len(self.config.flights)) as executor:
            tasks = [
                loop.run_in_executor(executor, self.check_flight, flight)
                for flight in self.config.flights
            ]
            results = await asyncio.gather(*tasks)

        return results

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
