"""Configuration management using environment variables."""

import os
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Optional

import yaml
from dotenv import load_dotenv


@dataclass
class FlightConfig:
    """Configuration for a single flight to monitor."""
    origin: str
    destination: str
    depart_date: str
    return_date: Optional[str] = None
    adults: int = 1
    currency: str = "USD"
    check_alternatives: bool = False  # Opt-in for alternative airports


@dataclass
class AppConfig:
    """Application-wide configuration."""
    # SerpApi
    serpapi_key: str
    serpapi_min_searches_left: int = 5

    # Email notifications
    email_sender: Optional[str] = None
    email_password: Optional[str] = None
    email_receiver: Optional[str] = None

    # Telegram notifications
    telegram_bot_token: Optional[str] = None
    telegram_chat_id: Optional[str] = None

    # Database
    db_path: str = "flight_prices.db"

    # Monitoring
    check_interval_minutes: int = 60
    retry_delay_minutes: int = 60
    scheduled_times: list[str] = field(default_factory=lambda: ["11:00"])
    scheduler_state_path: str = ".flight_monitor_scheduler.json"

    # Flights to monitor
    flights: list[FlightConfig] = field(default_factory=list)

    # Airport alternatives mapping (destination -> [alternative destinations])
    airport_alternatives: dict[str, list[str]] = field(default_factory=dict)


def load_airport_alternatives(path: Path) -> dict[str, list[str]]:
    """Load airport alternatives mapping from YAML file."""
    if not path.exists():
        return {}

    with open(path, "r") as f:
        data = yaml.safe_load(f)

    if not data or "alternatives" not in data:
        return {}

    alternatives: dict[str, list[str]] = data["alternatives"]
    return alternatives


def _normalize_date(value: str | date | None) -> str | None:
    """Convert date objects to ISO format strings (YAML may parse dates automatically)."""
    if value is None:
        return None
    if isinstance(value, date):
        return value.strftime("%Y-%m-%d")
    return str(value)


def load_flights_from_yaml(path: Path) -> list[FlightConfig]:
    """Load flight configurations from a YAML file."""
    if not path.exists():
        return []

    with open(path, "r") as f:
        data = yaml.safe_load(f)

    if not data or "flights" not in data:
        return []

    flights = []
    for flight_data in data["flights"]:
        flights.append(FlightConfig(
            origin=flight_data["origin"],
            destination=flight_data["destination"],
            depart_date=_normalize_date(flight_data["depart_date"]),  # type: ignore[arg-type]
            return_date=_normalize_date(flight_data.get("return_date")),
            adults=flight_data.get("adults", 1),
            currency=flight_data.get("currency", "USD"),
            check_alternatives=flight_data.get("check_alternatives", False),
        ))

    return flights


def load_config(
    env_path: Optional[Path] = None,
    flights_path: Optional[Path] = None,
    airports_path: Optional[Path] = None,
) -> AppConfig:
    """
    Load configuration from environment variables and optional YAML file.

    Args:
        env_path: Path to .env file (default: looks in current directory)
        flights_path: Path to flights.yaml file (default: looks in current directory)
        airports_path: Path to airports.yaml file (default: looks in current directory)

    Returns:
        AppConfig with all settings loaded
    """
    # Load .env file
    if env_path:
        load_dotenv(env_path)
    else:
        load_dotenv()

    # Load flights from YAML
    flights_file = flights_path or Path("flights.yaml")
    flights = load_flights_from_yaml(flights_file)

    # Load airport alternatives
    airports_file = airports_path or Path("airports.yaml")
    airport_alternatives = load_airport_alternatives(airports_file)
    scheduled_times_raw = os.getenv("SCHEDULED_TIMES", "11:00")
    scheduled_times = [item.strip() for item in scheduled_times_raw.split(",") if item.strip()]

    # If no flights.yaml, check for single flight in env vars (backwards compatibility)
    if not flights:
        origin = os.getenv("FLIGHT_ORIGIN")
        destination = os.getenv("FLIGHT_DESTINATION")
        depart_date = os.getenv("FLIGHT_DEPART_DATE")

        if origin and destination and depart_date:
            flights.append(FlightConfig(
                origin=origin,
                destination=destination,
                depart_date=depart_date,
                return_date=os.getenv("FLIGHT_RETURN_DATE"),
                adults=int(os.getenv("FLIGHT_ADULTS", "1")),
                currency=os.getenv("FLIGHT_CURRENCY", "USD"),
            ))

    return AppConfig(
        serpapi_key=os.getenv("SERPAPI_KEY", ""),
        serpapi_min_searches_left=int(os.getenv("SERPAPI_MIN_SEARCHES_LEFT", "5")),
        email_sender=os.getenv("EMAIL_SENDER"),
        email_password=os.getenv("EMAIL_PASSWORD"),
        email_receiver=os.getenv("EMAIL_RECEIVER"),
        telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN"),
        telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID"),
        db_path=os.getenv("DB_PATH", "flight_prices.db"),
        check_interval_minutes=int(os.getenv("CHECK_INTERVAL_MINUTES", "60")),
        retry_delay_minutes=int(os.getenv("RETRY_DELAY_MINUTES", "60")),
        scheduled_times=scheduled_times,
        scheduler_state_path=os.getenv(
            "SCHEDULER_STATE_PATH", ".flight_monitor_scheduler.json"
        ),
        flights=flights,
        airport_alternatives=airport_alternatives,
    )
