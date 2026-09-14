"""Flexible windows, provider filtering, confirmation and notification coverage."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from flight_monitor.clients.serpapi import SerpApiClient
from flight_monitor.config import AppConfig, FlightConfig, load_flights_from_yaml
from flight_monitor.monitor import FlightMonitor
from flight_monitor.notifiers.base import FlightCheckResult, FlightOffer, flexible_summary
from flight_monitor.notifiers.email import EmailNotifier
from flight_monitor.storage.sqlite import SQLiteStorage


class FlexibleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.flight = FlightConfig(
            "BOG", "SMR", "2099-12-27", "2100-01-01", currency="COP", flexible_days=5
        )
        self.client = SerpApiClient("test-key")
        self.offer = FlightOffer(80, "COP", "Test", [], 0, "BOG", "SMR", "2099-12-25", "2100-01-03")

    def deal(self, **overrides: object) -> dict[str, object]:
        return dict(
            dict(
                departure_airport_code="BOG",
                arrival_airport_code="SMR",
                start_date="2099-12-25",
                end_date="2100-01-03",
                price=80,
            ),
            **overrides,
        )

    @patch("flight_monitor.clients.serpapi.requests.get")
    def test_independent_windows_filter_invalid_and_confirm(self, get: Mock) -> None:
        get.return_value.json.return_value = {
            "deals": [
                self.deal(arrival_airport_code="CTG", price=1),
                self.deal(start_date="2099-12-21", price=2),
                self.deal(end_date="2100-01-07", price=3),
                self.deal(start_date="2099-12-30", end_date="2099-12-29", price=4),
                self.deal(price=float("nan")),
                self.deal(price=None),
                self.deal(),
            ]
        }
        with patch.object(self.client, "fetch_cheapest_offer", return_value=self.offer) as confirm:
            offer, _ = self.client.fetch_flexible_offer(self.flight)
        self.assertIs(offer, self.offer)
        params = get.call_args.kwargs["params"]
        self.assertIn("December 22 2099 to January 01 2100", params["query"])
        self.assertIn("December 27 2099 to January 06 2100", params["query"])
        confirmed = confirm.call_args.args[0]
        self.assertEqual(
            (confirmed.depart_date, confirmed.return_date), ("2099-12-25", "2100-01-03")
        )
        self.assertEqual(confirmed.flexible_days, 0)
        self.assertEqual(confirmed.destination, "SMR")

    @patch("flight_monitor.clients.serpapi.requests.get")
    def test_no_deals_does_not_claim_no_cheaper_dates(self, get: Mock) -> None:
        get.return_value.json.return_value = {"deals": []}
        with patch.object(self.client, "fetch_cheapest_offer") as confirm:
            offer, message = self.client.fetch_flexible_offer(self.flight)
        self.assertIsNone(offer)
        self.assertIn("no implica", message)
        confirm.assert_not_called()

    @patch("flight_monitor.clients.serpapi.requests.get")
    def test_provider_error_is_reported(self, get: Mock) -> None:
        get.return_value.json.return_value = {"error": "failed"}
        offer, message = self.client.fetch_flexible_offer(self.flight)
        self.assertIsNone(offer)
        self.assertIn("No se pudo", message)

    def test_config_and_summary(self) -> None:
        for invalid in (-1, 6, True, "5"):
            with self.assertRaises(ValueError):
                FlightConfig("BOG", "SMR", "2099-12-27", flexible_days=invalid)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "flights.yaml"
            path.write_text(
                "flights:\n  - origin: BOG\n    destination: SMR\n"
                "    depart_date: 2099-12-27\n    flexible_days: 5\n"
            )
            self.assertEqual(load_flights_from_yaml(path)[0].flexible_days, 5)
        result = FlightCheckResult(
            "BOG", "SMR", "2099-12-27", "2100-01-01", flexible_days=5, flexible_offer=self.offer
        )
        result.offer = FlightOffer(
            100, "COP", "Test", [], 0, "BOG", "SMR", "2099-12-27", "2100-01-01"
        )
        summary = "\n".join(flexible_summary(result))
        self.assertIn("20.0%", summary)
        self.assertIn("9 dias", summary)
        self.assertIn("2099-12-25", "\n".join(EmailNotifier()._build_flight_detail(result)))

    def test_monitor_keeps_exact_and_saves_flexible(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            exact = FlightOffer(100, "COP", "Test", [], 0, "BOG", "SMR", "2099-12-27", "2100-01-01")
            client = Mock()
            client.fetch_cheapest_offer.return_value = exact
            client.fetch_flexible_offer.return_value = (self.offer, "Verified")
            storage = SQLiteStorage(str(Path(tmp) / "prices.db"))
            monitor = FlightMonitor(AppConfig("key", flights=[self.flight]), client, storage, [])
            result = monitor.check_all_flights()[0]
            self.assertIs(result.offer, exact)
            self.assertIs(result.flexible_offer, self.offer)
            self.assertTrue(result.succeeded)
            self.assertEqual(len(storage.get_price_history("BOG", "SMR", "2099-12-25")), 1)
            client.fetch_flexible_offer.return_value = (None, "Provider failed")
            result = monitor.check_all_flights()[0]
            self.assertTrue(result.succeeded)
            self.assertEqual(result.flexible_message, "Provider failed")
