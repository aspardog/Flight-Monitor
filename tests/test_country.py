"""Same-country destination alternatives: catalog, single search, and reporting."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from flight_monitor.clients.serpapi import SerpApiClient
from flight_monitor.config import (
    AppConfig,
    CountryConfig,
    FlightConfig,
    load_countries,
    load_flights_from_yaml,
)
from flight_monitor.monitor import FlightMonitor, country_destinations
from flight_monitor.notifiers.base import (
    CountryAlternative,
    FlightCheckResult,
    FlightOffer,
    country_summary,
)
from flight_monitor.notifiers.email import EmailNotifier
from flight_monitor.storage.sqlite import SQLiteStorage

AIRPORTS_YAML = """
countries:
  MX:
    name: Mexico
    airports: [MEX, CUN, GDL, MTY, TIJ, PVR, SJD]
  CO:
    name: Colombia
    airports: [BOG, MDE, CTG]
  XX:
    name: Sin aeropuertos
    airports: []
"""


def leg(origin: str, destination: str, airline: str = "COPA") -> dict[str, object]:
    return {
        "airline": airline,
        "departure_airport": {"id": origin, "time": "08:00"},
        "arrival_airport": {"id": destination, "time": "13:00"},
    }


class CountryCatalogTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "airports.yaml"
        self.path.write_text(AIRPORTS_YAML)

    def test_catalog_derives_airport_index_and_skips_empty_countries(self) -> None:
        countries, index = load_countries(self.path)
        self.assertEqual(set(countries), {"MX", "CO"})
        self.assertEqual(countries["MX"].name, "Mexico")
        self.assertEqual(index["GDL"], "MX")
        self.assertEqual(index["CTG"], "CO")
        self.assertNotIn("XX", countries)

    def test_missing_file_and_missing_block_are_empty(self) -> None:
        self.assertEqual(load_countries(Path(self.tmp.name) / "nope.yaml"), ({}, {}))
        other = Path(self.tmp.name) / "other.yaml"
        other.write_text("alternatives:\n  BOG: [MDE]\n")
        self.assertEqual(load_countries(other), ({}, {}))

    def test_flight_yaml_keys_and_validation(self) -> None:
        path = Path(self.tmp.name) / "flights.yaml"
        path.write_text(
            "flights:\n  - origin: BOG\n    destination: GDL\n"
            "    depart_date: 2099-10-15\n    country_alternatives: true\n"
            "    country_alternatives_max: 3\n"
        )
        flight = load_flights_from_yaml(path)[0]
        self.assertTrue(flight.country_alternatives)
        self.assertEqual(flight.country_alternatives_max, 3)
        self.assertFalse(FlightConfig("BOG", "GDL", "2099-10-15").country_alternatives)
        for invalid in (0, 7, True, "3"):
            with self.assertRaises(ValueError):
                FlightConfig(
                    "BOG", "GDL", "2099-10-15", country_alternatives_max=invalid  # type: ignore[arg-type]
                )


class CountryDestinationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = AppConfig(
            serpapi_key="unused",
            countries={
                "MX": CountryConfig("MX", "Mexico", ["MEX", "CUN", "GDL", "MTY", "TIJ", "PVR"])
            },
            airport_country={a: "MX" for a in ("MEX", "CUN", "GDL", "MTY", "TIJ", "PVR")},
        )

    def flight(self, **overrides: object) -> FlightConfig:
        params: dict[str, object] = dict(
            origin="BOG", destination="GDL", depart_date="2099-10-15", country_alternatives=True
        )
        params.update(overrides)
        return FlightConfig(**params)  # type: ignore[arg-type]

    def test_excludes_primary_and_applies_cap(self) -> None:
        self.assertEqual(
            country_destinations(self.config, self.flight()), ["MEX", "CUN", "MTY", "TIJ"]
        )
        self.assertEqual(
            country_destinations(self.config, self.flight(country_alternatives_max=2)),
            ["MEX", "CUN"],
        )

    def test_domestic_trip_never_routes_back_to_the_origin(self) -> None:
        config = AppConfig(
            serpapi_key="unused",
            countries={"CO": CountryConfig("CO", "Colombia", ["BOG", "MDE", "CTG", "SMR", "BAQ"])},
            airport_country={a: "CO" for a in ("BOG", "MDE", "CTG", "SMR", "BAQ")},
        )
        self.assertEqual(
            country_destinations(config, self.flight(destination="SMR")),
            ["MDE", "CTG", "BAQ"],
        )

    def test_empty_when_opted_out_or_unknown_airport(self) -> None:
        opted_out = self.flight(country_alternatives=False)
        self.assertEqual(country_destinations(self.config, opted_out), [])
        self.assertEqual(country_destinations(self.config, self.flight(destination="LIM")), [])


class CountrySearchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = SerpApiClient("secret")
        self.flight = FlightConfig(
            "BOG", "GDL", "2099-10-15", "2099-10-25", currency="COP", country_alternatives=True
        )
        self.response = {
            "other_flights": [
                {
                    "flights": [leg("BOG", "MEX", "Avianca")],
                    "total_duration": 275,
                    "price": 1848120,
                },
                {
                    "flights": [leg("BOG", "PTY"), leg("PTY", "MEX")],
                    "total_duration": 595,
                    "price": 1760780,
                },
                {
                    "flights": [leg("BOG", "PTY"), leg("PTY", "CUN")],
                    "total_duration": 622,
                    "price": 1507720,
                },
                {"flights": [leg("BOG", "GDL")], "price": 900000},  # destino principal
                {"flights": [leg("BOG", "LIM")], "price": 100},  # fuera del pedido
                {"flights": [], "price": 50},
                {"flights": [leg("BOG", "MTY")], "price": None},
            ],
            "airports": [
                {
                    "departure": [{"airport": {"id": "BOG"}, "city": "Bogota"}],
                    "arrival": [
                        {"airport": {"id": "MEX"}, "city": "Ciudad de Mexico"},
                        {"airport": {"id": "CUN"}, "city": "Cancun"},
                    ],
                }
            ],
        }

    @patch("flight_monitor.clients.serpapi.requests.get")
    def test_groups_by_airport_and_keeps_cheapest(self, get: Mock) -> None:
        get.return_value.json.return_value = self.response
        alternatives, missing, message = self.client.fetch_country_offers(
            self.flight, ["MEX", "CUN", "MTY", "TIJ"]
        )

        params = get.call_args.kwargs["params"]
        self.assertEqual(params["arrival_id"], "MEX,CUN,MTY,TIJ")
        self.assertEqual(params["sort_by"], "2")
        self.assertEqual(params["return_date"], "2099-10-25")

        self.assertEqual([alt.destination for alt in alternatives], ["CUN", "MEX"])
        self.assertEqual(alternatives[0].city, "Cancun")
        self.assertEqual(alternatives[0].price, 1507720)
        self.assertEqual(alternatives[1].price, 1760780)  # el mas barato de los dos a MEX
        self.assertEqual(alternatives[1].offer.stops, 1)
        self.assertEqual(alternatives[1].offer.destination, "MEX")
        self.assertEqual(alternatives[1].offer.price_category, "other")
        self.assertIsNone(alternatives[1].offer.typical_price_low)
        self.assertEqual(missing, ["MTY", "TIJ"])
        self.assertIn("mismas fechas exactas", message.lower())

    @patch("flight_monitor.clients.serpapi.requests.get")
    def test_no_itineraries_reports_every_destination_missing(self, get: Mock) -> None:
        get.return_value.json.return_value = {"other_flights": []}
        alternatives, missing, message = self.client.fetch_country_offers(self.flight, ["MEX"])
        self.assertEqual(alternatives, [])
        self.assertEqual(missing, ["MEX"])
        self.assertIn("no devolvio itinerarios", message)

    @patch("flight_monitor.clients.serpapi.requests.get")
    def test_provider_error_keeps_key_secret(self, get: Mock) -> None:
        get.return_value.json.return_value = {"error": "failed"}
        alternatives, missing, message = self.client.fetch_country_offers(self.flight, ["MEX"])
        self.assertEqual((alternatives, missing), ([], ["MEX"]))
        self.assertIn("no devolvio alternativas", message)

        get.side_effect = ValueError("bad json with secret")
        with patch("builtins.print") as printed:
            _, _, message = self.client.fetch_country_offers(self.flight, ["MEX"])
        self.assertIn("No se pudo", message)
        self.assertNotIn("secret", " ".join(str(c) for c in printed.call_args_list))

    def test_no_destinations_skips_the_request(self) -> None:
        with patch("flight_monitor.clients.serpapi.requests.get") as get:
            self.assertEqual(self.client.fetch_country_offers(self.flight, []), ([], [], ""))
        get.assert_not_called()


class CountryMonitorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.flight = FlightConfig(
            "BOG", "GDL", "2099-10-15", "2099-10-25", currency="COP", country_alternatives=True
        )
        self.config = AppConfig(
            serpapi_key="unused",
            flights=[self.flight],
            countries={"MX": CountryConfig("MX", "Mexico", ["MEX", "CUN", "GDL"])},
            airport_country={"MEX": "MX", "CUN": "MX", "GDL": "MX"},
        )
        self.exact = FlightOffer(
            2850000, "COP", "Avianca", [], 0, "BOG", "GDL", "2099-10-15", "2099-10-25"
        )
        self.alternative = CountryAlternative(
            "MEX",
            "Ciudad de Mexico",
            FlightOffer(
                1760780, "COP", "Viva", [], 1, "BOG", "MEX", "2099-10-15", "2099-10-25",
                total_duration=595,
            ),
        )

    def build(self, enabled: bool = True) -> tuple[FlightMonitor, Mock, SQLiteStorage]:
        client = Mock()
        client.fetch_cheapest_offer.return_value = self.exact
        client.fetch_country_offers.return_value = ([self.alternative], ["CUN"], "Listo")
        storage = SQLiteStorage(str(Path(self.tmp.name) / "prices.db"))
        monitor = FlightMonitor(
            self.config, client, storage, [], country_alternatives_enabled=enabled
        )
        return monitor, client, storage

    def test_attaches_alternatives_and_persists_them(self) -> None:
        monitor, client, storage = self.build()
        result = monitor.check_all_flights()[0]

        client.fetch_country_offers.assert_called_once_with(self.flight, ["MEX", "CUN"])
        self.assertEqual(result.country_name, "Mexico")
        self.assertEqual([alt.destination for alt in result.country_alternatives], ["MEX"])
        self.assertEqual(result.country_missing, ["CUN"])
        self.assertIs(result.offer, self.exact)
        self.assertTrue(result.succeeded)
        self.assertEqual(len(storage.get_price_history("BOG", "MEX", "2099-10-15")), 1)

    def test_quota_degradation_skips_the_search_without_failing_the_run(self) -> None:
        monitor, client, _ = self.build(enabled=False)
        result = monitor.check_all_flights()[0]

        client.fetch_country_offers.assert_not_called()
        self.assertEqual(result.country_alternatives, [])
        self.assertIn("cuota", result.country_message or "")
        self.assertTrue(result.succeeded)

    def test_provider_without_support_is_reported(self) -> None:
        client = Mock(spec=["fetch_cheapest_offer"])
        client.fetch_cheapest_offer.return_value = self.exact
        storage = SQLiteStorage(str(Path(self.tmp.name) / "prices.db"))
        monitor = FlightMonitor(self.config, client, storage, [])
        result = monitor.check_all_flights()[0]
        self.assertIn("no admite", result.country_message or "")

    def test_past_departure_skips_the_search(self) -> None:
        self.config.flights = [
            FlightConfig("BOG", "GDL", "2000-01-01", country_alternatives=True)
        ]
        monitor, client, _ = self.build()
        result = monitor.check_all_flights()[0]
        client.fetch_country_offers.assert_not_called()
        self.assertTrue(result.date_unavailable)


class CountryReportingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.result = FlightCheckResult(
            "BOG",
            "GDL",
            "2099-10-15",
            "2099-10-25",
            offer=FlightOffer(
                2850000, "COP", "Avianca", [], 0, "BOG", "GDL", "2099-10-15", "2099-10-25"
            ),
            country_name="Mexico",
            country_missing=["TIJ"],
            country_message="Mismas fechas exactas, una sola consulta a Google Flights.",
        )
        self.result.country_alternatives = [
            CountryAlternative(
                "MEX",
                "Ciudad de Mexico",
                FlightOffer(
                    1760780, "COP", "Viva", [], 1, "BOG", "MEX", "2099-10-15", "2099-10-25",
                    total_duration=595,
                ),
            ),
            CountryAlternative(
                "CUN",
                "Cancun",
                FlightOffer(
                    3100000, "COP", "COPA", [], 0, "BOG", "CUN", "2099-10-15", "2099-10-25"
                ),
            ),
        ]

    def test_email_section_shows_savings_and_gaps(self) -> None:
        section = "\n".join(EmailNotifier()._build_country_section([self.result]))
        self.assertIn("ALTERNATIVAS EN EL MISMO PAIS", section)
        self.assertIn("BOG → GDL (Mexico)", section)
        self.assertIn("Ciudad de Mexico", section)
        self.assertIn("38% mas barato", section)
        self.assertIn("ahorras COP 1,089,220", section)
        self.assertIn("9% mas caro", section)
        self.assertIn("Sin itinerarios: TIJ", section)
        self.assertIn("9h 55m", section)
        self.assertIn("google.com/travel/flights", section)

    def test_email_section_absent_without_data(self) -> None:
        empty = FlightCheckResult("BOG", "SMR", "2099-12-27")
        self.assertEqual(EmailNotifier()._build_country_section([empty]), [])

    def test_compact_summary_drops_notes_for_telegram(self) -> None:
        compact = country_summary(self.result, include_notes=False)
        self.assertEqual(len(compact), 2)
        self.assertIn("BOG → MEX (Ciudad de Mexico)", compact[0])
        self.assertIn("38% mas barato", compact[0])
        self.assertIn("Sin itinerarios", "\n".join(country_summary(self.result)))

    def test_summary_without_reference_price_omits_savings(self) -> None:
        self.result.offer = None
        line = country_summary(self.result, include_notes=False)[0]
        self.assertIn("COP 1,760,780", line)
        self.assertNotIn("barato", line)


if __name__ == "__main__":
    unittest.main()
