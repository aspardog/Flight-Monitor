"""Functional tests for the main Flight Monitor workflows."""

import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import Mock, patch

from flight_monitor.clients.serpapi import SerpApiClient
from flight_monitor.config import AppConfig, FlightConfig
from flight_monitor.monitor import FlightMonitor
from flight_monitor.notifiers.base import FlightCheckResult, FlightOffer, Notifier
from flight_monitor.scheduler import RetryScheduler
from flight_monitor.storage.sqlite import SQLiteStorage


class FakeClient:
    """Return a predefined offer without making network requests."""

    def __init__(self, offer: FlightOffer | None):
        self.offer = offer
        self.calls: list[FlightConfig] = []

    def fetch_cheapest_offer(self, flight: FlightConfig) -> FlightOffer | None:
        self.calls.append(flight)
        return self.offer


class RecordingNotifier(Notifier):
    """Capture summaries instead of sending them externally."""

    def __init__(self, succeeds: bool = True):
        self.succeeds = succeeds
        self.summaries: list[list[FlightCheckResult]] = []

    def is_configured(self) -> bool:
        return True

    def send_summary(self, results: list[FlightCheckResult]) -> bool:
        self.summaries.append(results)
        return self.succeeds


class SerpApiClientTests(unittest.TestCase):
    @patch("flight_monitor.clients.serpapi.requests.get")
    def test_fetch_account_status_prefers_total_remaining(self, mock_get: Mock) -> None:
        response = Mock()
        response.json.return_value = {
            "plan_name": "Free",
            "plan_searches_left": 20,
            "extra_credits": 5,
            "total_searches_left": 25,
            "this_month_usage": 10,
        }
        response.raise_for_status.return_value = None
        mock_get.return_value = response

        status = SerpApiClient("secret").fetch_account_status()

        self.assertIsNotNone(status)
        assert status is not None
        self.assertEqual(status.remaining_searches, 25)
        mock_get.assert_called_once_with(
            SerpApiClient.ACCOUNT_URL,
            params={"api_key": "secret"},
            timeout=15,
        )

    @patch("flight_monitor.clients.serpapi.requests.get")
    def test_fetch_cheapest_offer_parses_cheapest_result(self, mock_get: Mock) -> None:
        response = Mock()
        response.json.return_value = {
            "best_flights": [
                {
                    "price": 500,
                    "total_duration": 180,
                    "flights": [
                        {
                            "airline": "Air One",
                            "departure_airport": {"id": "BOG", "time": "08:00"},
                            "arrival_airport": {"id": "MIA"},
                        }
                    ],
                }
            ],
            "other_flights": [
                {
                    "price": 450,
                    "total_duration": 240,
                    "flights": [
                        {
                            "airline": "Air Two",
                            "departure_airport": {"id": "BOG", "time": "09:00"},
                            "arrival_airport": {"id": "PTY"},
                        },
                        {
                            "airline": "Air Two",
                            "departure_airport": {"id": "PTY", "time": "12:00"},
                            "arrival_airport": {"id": "MIA"},
                        },
                    ],
                }
            ],
            "price_insights": {
                "typical_price_range": [480, 650],
                "price_level": "low",
            },
        }
        response.raise_for_status.return_value = None
        mock_get.return_value = response
        flight = FlightConfig(
            origin="BOG",
            destination="MIA",
            depart_date="2026-12-01",
            return_date="2026-12-15",
            adults=2,
            currency="USD",
        )

        offer = SerpApiClient("secret").fetch_cheapest_offer(flight)

        self.assertIsNotNone(offer)
        assert offer is not None
        self.assertEqual(offer.price, 450)
        self.assertEqual(offer.airline, "Air Two")
        self.assertEqual(offer.stops, 1)
        self.assertEqual(offer.price_category, "other")
        self.assertEqual(offer.typical_price_low, 480)
        self.assertEqual(offer.typical_price_high, 650)
        self.assertEqual(offer.price_level, "low")
        self.assertEqual(offer.price_per_person, 225)


class FlightMonitorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.db_path = str(Path(self.temp_dir.name) / "prices.db")
        self.flight = FlightConfig(
            origin="BOG",
            destination="MIA",
            depart_date="2026-12-01",
            return_date="2026-12-15",
        )
        self.config = AppConfig(
            serpapi_key="unused",
            db_path=self.db_path,
            flights=[self.flight],
        )

    def test_run_once_persists_offer_and_sends_recommendation(self) -> None:
        offer = FlightOffer(
            price=400,
            currency="USD",
            airline="Test Air",
            segments=["BOG -> MIA (08:00)"],
            stops=0,
            origin="BOG",
            destination="MIA",
            depart_date="2026-12-01",
            return_date="2026-12-15",
            typical_price_low=500,
            typical_price_high=650,
            price_level="low",
        )
        notifier = RecordingNotifier()
        storage = SQLiteStorage(self.db_path)
        monitor = FlightMonitor(
            self.config,
            FakeClient(offer),
            storage,
            [notifier],
        )

        succeeded = monitor.run_once()

        self.assertTrue(succeeded)
        history = storage.get_price_history("BOG", "MIA", "2026-12-01")
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0].price, 400)
        self.assertEqual(len(notifier.summaries), 1)
        result = notifier.summaries[0][0]
        self.assertTrue(result.succeeded)
        self.assertTrue(result.recommended)
        self.assertEqual(result.discount_pct, 20)

    def test_run_once_fails_when_provider_returns_no_offer(self) -> None:
        notifier = RecordingNotifier()
        monitor = FlightMonitor(
            self.config,
            FakeClient(None),
            SQLiteStorage(self.db_path),
            [notifier],
        )

        succeeded = monitor.run_once()

        self.assertFalse(succeeded)
        self.assertEqual(len(notifier.summaries), 1)
        self.assertFalse(notifier.summaries[0][0].succeeded)

    def test_run_once_marks_past_departure_date_unavailable(self) -> None:
        yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
        flight = FlightConfig(
            origin="BOG",
            destination="MIA",
            depart_date=yesterday,
        )
        config = AppConfig(
            serpapi_key="unused",
            db_path=self.db_path,
            flights=[flight],
        )
        notifier = RecordingNotifier()
        client = FakeClient(None)
        monitor = FlightMonitor(
            config,
            client,
            SQLiteStorage(self.db_path),
            [notifier],
        )

        succeeded = monitor.run_once()

        self.assertTrue(succeeded)
        self.assertEqual(client.calls, [])
        result = notifier.summaries[0][0]
        self.assertTrue(result.succeeded)
        self.assertTrue(result.date_unavailable)
        self.assertIsNone(result.error_message)


class RetrySchedulerTests(unittest.TestCase):
    def test_failed_slot_waits_then_retries_and_stops_after_success(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = Path(temp_dir) / "scheduler.json"
            scheduler = RetryScheduler(
                str(state_path),
                ["11:00"],
                retry_delay_minutes=60,
            )
            scheduled_time = datetime(2026, 6, 15, 11, 0)

            slot = scheduler.next_due_slot(scheduled_time)
            self.assertIsNotNone(slot)
            assert slot is not None

            scheduler.mark_attempt(slot, scheduled_time, succeeded=False)
            self.assertIsNone(scheduler.next_due_slot(scheduled_time + timedelta(minutes=59)))

            retry_time = scheduled_time + timedelta(minutes=60)
            self.assertEqual(scheduler.next_due_slot(retry_time), slot)

            scheduler.mark_attempt(slot, retry_time, succeeded=True)
            self.assertIsNone(scheduler.next_due_slot(retry_time + timedelta(hours=1)))

            state = json.loads(state_path.read_text())
            self.assertEqual(state[slot.slot_id]["last_status"], "success")
            self.assertIn("completed_at", state[slot.slot_id])


if __name__ == "__main__":
    unittest.main()
