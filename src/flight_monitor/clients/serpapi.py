"""SerpApi client for Google Flights searches."""

import math
from dataclasses import dataclass, replace
from datetime import date, timedelta
from typing import Any, Optional

import requests

from ..config import FlightConfig
from ..notifiers.base import CountryAlternative, FlightOffer


@dataclass(frozen=True)
class SerpApiAccountStatus:
    """Current SerpApi quota and usage summary."""

    plan_name: Optional[str]
    plan_monthly_price: Optional[float]
    searches_per_month: Optional[int]
    plan_searches_left: Optional[int]
    extra_credits: Optional[int]
    total_searches_left: Optional[int]
    this_month_usage: Optional[int]
    last_hour_searches: Optional[int]
    account_rate_limit_per_hour: Optional[int]

    @property
    def remaining_searches(self) -> Optional[int]:
        """Return the best available remaining-search count."""
        if self.total_searches_left is not None:
            return self.total_searches_left
        return self.plan_searches_left


class SerpApiClient:
    """Client for querying flight prices from Google Flights via SerpApi."""

    BASE_URL = "https://serpapi.com/search"
    ACCOUNT_URL = "https://serpapi.com/account.json"

    def __init__(self, api_key: str):
        """
        Initialize the SerpApi client.

        Args:
            api_key: SerpApi API key
        """
        self.api_key = api_key

    def _sanitize_error(self, error: Exception) -> str:
        """Remove secrets from exception messages before logging them."""
        return str(error).replace(self.api_key, "***REDACTED***")

    def fetch_account_status(self) -> Optional[SerpApiAccountStatus]:
        """Fetch the current SerpApi account usage and remaining quota."""
        try:
            response = requests.get(
                self.ACCOUNT_URL,
                params={"api_key": self.api_key},
                timeout=15,
            )
            response.raise_for_status()
            data = response.json()

            return SerpApiAccountStatus(
                plan_name=data.get("plan_name"),
                plan_monthly_price=(
                    float(data["plan_monthly_price"])
                    if data.get("plan_monthly_price") is not None
                    else None
                ),
                searches_per_month=(
                    int(data["searches_per_month"])
                    if data.get("searches_per_month") is not None
                    else None
                ),
                plan_searches_left=(
                    int(data["plan_searches_left"])
                    if data.get("plan_searches_left") is not None
                    else None
                ),
                extra_credits=(
                    int(data["extra_credits"]) if data.get("extra_credits") is not None else None
                ),
                total_searches_left=(
                    int(data["total_searches_left"])
                    if data.get("total_searches_left") is not None
                    else None
                ),
                this_month_usage=(
                    int(data["this_month_usage"])
                    if data.get("this_month_usage") is not None
                    else None
                ),
                last_hour_searches=(
                    int(data["last_hour_searches"])
                    if data.get("last_hour_searches") is not None
                    else None
                ),
                account_rate_limit_per_hour=(
                    int(data["account_rate_limit_per_hour"])
                    if data.get("account_rate_limit_per_hour") is not None
                    else None
                ),
            )
        except requests.exceptions.RequestException as e:
            print(f"[SerpApi] Error consultando cuota: {self._sanitize_error(e)}")
            return None
        except (KeyError, ValueError, TypeError) as e:
            print(f"[SerpApi] Error procesando cuota: {e}")
            return None

    def fetch_cheapest_offer(self, flight: FlightConfig) -> Optional[FlightOffer]:
        """
        Query Google Flights via SerpApi and return the cheapest flight found.

        Args:
            flight: Flight configuration with search parameters

        Returns:
            FlightOffer with price details and category, or None if error/not found
        """
        params: dict[str, str | int] = {
            "engine": "google_flights",
            "api_key": self.api_key,
            "departure_id": flight.origin,
            "arrival_id": flight.destination,
            "outbound_date": flight.depart_date,
            "adults": flight.adults,
            "currency": flight.currency,
            "type": "1" if flight.return_date else "2",  # 1=round trip, 2=one way
            "hl": "es",  # Spanish
        }

        if flight.return_date:
            params["return_date"] = flight.return_date

        try:
            response = requests.get(self.BASE_URL, params=params, timeout=30)
            response.raise_for_status()
            data = response.json()

            # Check for errors in response
            if "error" in data:
                print(f"[SerpApi] Error: {data['error']}")
                return None

            # Get best flights (LOW category) and other flights
            best_flights = data.get("best_flights", [])
            other_flights = data.get("other_flights", [])

            if not best_flights and not other_flights:
                print(f"[SerpApi] No hay vuelos {flight.origin}->{flight.destination}")
                return None

            # Find cheapest from best_flights first (these are LOW category)
            cheapest = None
            price_category = "other"

            if best_flights:
                cheapest = min(best_flights, key=lambda f: f.get("price", float("inf")))
                price_category = "best"  # LOW category

            # Check if there's a cheaper one in other_flights
            if other_flights:
                cheapest_other = min(other_flights, key=lambda f: f.get("price", float("inf")))
                other_price = cheapest_other.get("price", float("inf"))
                current_price = cheapest.get("price", float("inf")) if cheapest else float("inf")
                if cheapest is None or other_price < current_price:
                    cheapest = cheapest_other
                    price_category = "other"

            if cheapest is None:
                print("[SerpApi] No se encontraron vuelos validos")
                return None

            offer = self._build_offer(cheapest, flight, price_category=price_category)
            if offer is None:
                print("[SerpApi] Respuesta sin detalles de vuelo")
                return None

            category_label = "LOW" if price_category == "best" else "OTHER"
            print(f"[SerpApi] Categoria de precio: {category_label}")

            # Extract price insights from Google
            price_insights = data.get("price_insights", {})
            typical_range = price_insights.get("typical_price_range", [None, None])
            typical_low = typical_range[0] if len(typical_range) > 0 else None
            typical_high = typical_range[1] if len(typical_range) > 1 else None
            price_level = price_insights.get("price_level")

            if typical_low and typical_high:
                low_fmt, high_fmt = f"{typical_low:,.0f}", f"{typical_high:,.0f}"
                print(f"[SerpApi] Rango tipico Google: {flight.currency} {low_fmt} - {high_fmt}")
            if price_level:
                print(f"[SerpApi] Nivel de precio Google: {price_level.upper()}")

            return replace(
                offer,
                typical_price_low=float(typical_low) if typical_low else None,
                typical_price_high=float(typical_high) if typical_high else None,
                price_level=price_level,
            )

        except requests.exceptions.RequestException as e:
            print(f"[SerpApi] Error de conexion: {self._sanitize_error(e)}")
            return None
        except (KeyError, ValueError, TypeError) as e:
            print(f"[SerpApi] Error procesando respuesta: {e}")
            return None
        except Exception as e:
            print(f"[SerpApi] Error inesperado: {e}")
            return None

    def _build_offer(
        self,
        itinerary: dict[str, Any],
        flight: FlightConfig,
        price_category: str = "other",
        destination: Optional[str] = None,
    ) -> Optional[FlightOffer]:
        """Build a FlightOffer from one itinerary, without price insights."""
        legs = itinerary.get("flights") or []
        if not legs:
            return None

        raw_price = itinerary.get("price")
        if raw_price is None:
            return None
        try:
            price = float(raw_price)
        except (TypeError, ValueError):
            return None
        if not math.isfinite(price) or price <= 0:
            return None

        segments = []
        for seg in legs:
            departure = seg.get("departure_airport", {})
            arrival = seg.get("arrival_airport", {})
            dep_time = departure.get("time", "")
            segments.append(
                f"{departure.get('id', '?')} -> {arrival.get('id', '?')} ({dep_time})"
            )

        total_duration = itinerary.get("total_duration")

        return FlightOffer(
            price=price,
            currency=flight.currency,
            airline=legs[0].get("airline", "Unknown"),
            segments=segments,
            stops=len(legs) - 1,
            origin=flight.origin,
            destination=destination or flight.destination,
            depart_date=flight.depart_date,
            return_date=flight.return_date,
            adults=flight.adults,
            price_category=price_category,
            total_duration=int(total_duration) if total_duration else None,
        )

    @staticmethod
    def _arrival_cities(data: dict[str, Any]) -> dict[str, str]:
        """Map arrival airport code -> city name from the response metadata."""
        cities: dict[str, str] = {}
        for group in data.get("airports", []):
            for entry in group.get("arrival", []):
                code = entry.get("airport", {}).get("id")
                city = entry.get("city")
                if code and city:
                    cities.setdefault(code, city)
        return cities

    def fetch_country_offers(
        self, flight: FlightConfig, destinations: list[str]
    ) -> tuple[list[CountryAlternative], list[str], str]:
        """Price other cities of the destination country with a single search.

        Google Flights accepts several arrival airports in one query, so the whole
        country comparison costs one search. Such responses carry no price_insights
        and no best_flights, so every alternative is category "other" and never
        drives a purchase recommendation.

        Returns:
            Tuple of (alternatives sorted by price, destinations without itineraries, message)
        """
        if not destinations:
            return [], [], ""

        params: dict[str, str | int] = {
            "engine": "google_flights",
            "api_key": self.api_key,
            "departure_id": flight.origin,
            "arrival_id": ",".join(destinations),
            "outbound_date": flight.depart_date,
            "adults": flight.adults,
            "currency": flight.currency,
            "type": "1" if flight.return_date else "2",
            "sort_by": "2",  # cheapest first, so each airport surfaces its best price
            "hl": "es",
        }
        if flight.return_date:
            params["return_date"] = flight.return_date

        try:
            response = requests.get(self.BASE_URL, params=params, timeout=60)
            response.raise_for_status()
            data = response.json()
            if "error" in data:
                return [], list(destinations), "El proveedor no devolvio alternativas del pais."
        except (requests.exceptions.RequestException, ValueError, TypeError, KeyError) as e:
            print(f"[SerpApi] Error en alternativas por pais: {self._sanitize_error(e)}")
            return (
                [],
                list(destinations),
                "No se pudo completar la busqueda de alternativas del pais.",
            )

        wanted = set(destinations)
        cities = self._arrival_cities(data)
        cheapest: dict[str, FlightOffer] = {}

        itineraries = list(data.get("best_flights", [])) + list(data.get("other_flights", []))
        for itinerary in itineraries:
            legs = itinerary.get("flights") or []
            if not legs:
                continue
            # On round trips the response only carries the outbound legs.
            arrival = legs[-1].get("arrival_airport", {}).get("id")
            if arrival not in wanted:
                continue
            offer = self._build_offer(itinerary, flight, destination=arrival)
            if offer is None:
                continue
            current = cheapest.get(arrival)
            if current is None or offer.price < current.price:
                cheapest[arrival] = offer

        alternatives = [
            CountryAlternative(destination=code, city=cities.get(code, code), offer=offer)
            for code, offer in cheapest.items()
        ]
        alternatives.sort(key=lambda alt: alt.price)
        missing = [code for code in destinations if code not in cheapest]

        if not alternatives:
            return (
                [],
                missing,
                "Google no devolvio itinerarios para los destinos alternativos.",
            )

        return (
            alternatives,
            missing,
            "Mismas fechas exactas, una sola consulta a Google Flights.",
        )

    def fetch_flexible_offer(self, flight: FlightConfig) -> tuple[Optional[FlightOffer], str]:
        """Discover a date alternative with Deals, then confirm the exact airport quote.

        Deals is curated, not an exhaustive calendar. Never fall back to a costly grid.
        """
        delta = timedelta(days=flight.flexible_days)
        depart = date.fromisoformat(flight.depart_date)
        start, end = max(date.today(), depart - delta), depart + delta
        if start >= end:
            return None, "No hay ventana futura suficiente para consultar fechas flexibles."
        params: dict[str, str | int] = {
            "engine": "google_flights_deals",
            "api_key": self.api_key,
            "departure_id": flight.origin,
            "currency": flight.currency,
            "adults": flight.adults,
            "type": "1" if flight.return_date else "2",
            "hl": "en",
        }
        back = date.fromisoformat(flight.return_date) if flight.return_date else None
        # The natural-language query can override structured date filters in Deals.
        # Include both windows in the query and validate every returned date below.
        trip = "Round trip" if back else "One way"
        query = (
            f"{trip} to {flight.destination} departing "
            f"{start.strftime('%B %d %Y')} to {end.strftime('%B %d %Y')}"
        )
        if back:
            query += (
                f" returning {(back - delta).strftime('%B %d %Y')}"
                f" to {(back + delta).strftime('%B %d %Y')}"
            )
        params["query"] = query
        try:
            response = requests.get(self.BASE_URL, params=params, timeout=60)
            response.raise_for_status()
            data = response.json()
            if "error" in data:
                return None, "No se pudo completar la busqueda flexible del proveedor."
            candidates: list[tuple[float, str, Optional[str]]] = []
            for deal in data.get("deals", []):
                try:
                    if (
                        deal.get("departure_airport_code") != flight.origin
                        or deal.get("arrival_airport_code") != flight.destination
                    ):
                        continue
                    dep = date.fromisoformat(deal.get("outbound_date") or deal["start_date"])
                    ret = (
                        date.fromisoformat(deal.get("return_date") or deal["end_date"])
                        if back
                        else None
                    )
                    price = float(deal["price"])
                    if not math.isfinite(price) or price <= 0 or not start <= dep <= end:
                        continue
                    if back and (
                        ret is None or not back - delta <= ret <= back + delta or ret <= dep
                    ):
                        continue
                    dates = (dep.isoformat(), ret.isoformat() if ret else None)
                    if dates == (flight.depart_date, flight.return_date):
                        continue
                    candidates.append((price, *dates))
                except (KeyError, TypeError, ValueError):
                    continue
            if not candidates:
                return (
                    None,
                    "Google no devolvio alternativas en este rango; no implica que no existan.",
                )
            _, dep_str, ret_str = min(candidates, key=lambda item: item[0])
            offer = self.fetch_cheapest_offer(
                replace(
                    flight,
                    depart_date=dep_str,
                    return_date=ret_str,
                    flexible_days=0,
                )
            )
            if offer is None:
                return None, "No se pudo confirmar el precio de la alternativa flexible."
            return (
                offer,
                "Oferta seleccionada por Google; no es una revision exhaustiva.",
            )
        except (requests.exceptions.RequestException, ValueError, TypeError, KeyError):
            return (
                None,
                "No se pudo completar la busqueda flexible; se mantienen las fechas exactas.",
            )
