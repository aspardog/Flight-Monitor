# Flight Monitor

[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![UV](https://img.shields.io/badge/uv-package%20manager-blueviolet)](https://github.com/astral-sh/uv)

> Monitor inteligente de precios de vuelos que usa **Google Flights** (via SerpApi) para rastrear precios y recomendarte comprar cuando el precio esta por debajo del rango tipico que Google considera normal para esa ruta.

---

## Caracteristicas

- **Multi-vuelo**: Monitorea multiples rutas simultaneamente con ejecucion concurrente
- **Clasificacion inteligente**: Distingue entre precios LOW (mejores ofertas) y OTHER usando la clasificacion de Google Flights
- **Alertas precisas**: Recomienda comprar cuando el precio esta **por debajo del rango tipico de Google Flights**
- **Aeropuertos alternativos**: Opcionalmente compara destinos cercanos definidos en `airports.yaml`
- **Notificaciones flexibles**: Soporte para Email (Gmail SMTP) y Telegram Bot (multiples destinatarios)
- **Persistencia**: Almacena todo el historial de precios en SQLite para analisis
- **Optimizado para API limitada**: Disenado para el plan gratuito de SerpApi (250 llamadas/mes)
- **Modo cron/launchd**: Flags `--once` y `--scheduled` (con reintentos automaticos) para tareas programadas
- **Verificacion automatica**: CI con Ruff, mypy estricto y pruebas funcionales sin consumo de API

---

## Quick Start

```bash
# 1. Clonar repositorio
git clone https://github.com/aspardog/Flight-Monitor.git
cd Flight-Monitor

# 2. Instalar dependencias (requiere UV)
uv sync --locked --no-editable

# 3. Configurar credenciales
cp .env.example .env
# Editar .env con tu API key de SerpApi y credenciales de notificacion

# 4. Configurar vuelos a monitorear
cp flights.yaml.example flights.yaml
# Editar flights.yaml con tus rutas

# 5. Ejecutar
uv run --no-editable python -m flight_monitor --once
```

---

## Tabla de Contenidos

- [Arquitectura](#arquitectura)
- [Pipeline de Datos](#pipeline-de-datos)
- [Logica de Alertas](#logica-de-alertas)
- [Configuracion](#configuracion)
- [Uso](#uso)
- [Base de Datos](#base-de-datos)
- [Extensibilidad](#extensibilidad)
- [Optimizacion de API](#optimizacion-de-api)

---

## Arquitectura

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                              FLIGHT MONITOR                                  │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                             │
│  ┌─────────────┐     ┌─────────────┐     ┌─────────────┐     ┌───────────┐ │
│  │   CONFIG    │     │   CLIENT    │     │   STORAGE   │     │ NOTIFIERS │ │
│  │             │     │             │     │             │     │           │ │
│  │ .env        │     │ SerpApi     │     │ SQLite      │     │ Email     │ │
│  │ flights.yaml│     │ (Google     │     │ (Historial) │     │ Telegram  │ │
│  │             │     │  Flights)   │     │             │     │           │ │
│  └──────┬──────┘     └──────┬──────┘     └──────┬──────┘     └─────┬─────┘ │
│         │                   │                   │                   │       │
│         └───────────────────┴───────────────────┴───────────────────┘       │
│                                     │                                       │
│                          ┌──────────▼──────────┐                           │
│                          │      MONITOR        │                           │
│                          │   (Orquestador)     │                           │
│                          │                     │                           │
│                          │ - check_flight()    │                           │
│                          │ - should_recommend()│                           │
│                          │ - run() / run_once()│                           │
│                          └─────────────────────┘                           │
│                                                                             │
└─────────────────────────────────────────────────────────────────────────────┘
```

### Estructura del Proyecto

```
flight-monitor/
├── src/
│   └── flight_monitor/
│       ├── __init__.py          # Version del paquete
│       ├── __main__.py          # Punto de entrada CLI
│       ├── config.py            # Carga de configuracion (.env + YAML)
│       ├── monitor.py           # Orquestador principal (async)
│       ├── scheduler.py         # Programador con reintentos (--scheduled)
│       │
│       ├── clients/             # Proveedores de datos de vuelos
│       │   └── serpapi.py       # Cliente Google Flights via SerpApi
│       │
│       ├── storage/             # Backends de persistencia
│       │   └── sqlite.py        # Almacenamiento SQLite
│       │
│       └── notifiers/           # Plugins de notificacion
│           ├── base.py          # Protocolo base y dataclasses
│           ├── email.py         # Notificador Gmail SMTP
│           └── telegram.py      # Notificador Telegram Bot
│
├── .env.example                 # Plantilla de variables de entorno
├── flights.yaml.example         # Plantilla de vuelos a monitorear
├── airports.yaml                # Mapa de aeropuertos alternativos
├── pyproject.toml               # Configuracion del proyecto (UV/pip)
└── README.md
```

### Patrones de Diseno

| Patron | Implementacion | Beneficio |
|--------|----------------|-----------|
| **Dependency Injection** | Componentes reciben dependencias via constructor | Facilita testing y desacoplamiento |
| **Plugin Architecture** | Notifiers implementan protocolo `Notifier` | Agregar nuevos canales sin modificar core |
| **Protocol (Duck Typing)** | `FlightClient` protocol para proveedores | Intercambiar SerpApi por otro proveedor |
| **Async Concurrency** | `asyncio` + `ThreadPoolExecutor` | Chequeo paralelo de multiples vuelos |

---

## Pipeline de Datos

### Flujo de Chequeo (por vuelo)

```
┌──────────────────┐
│ 1. FETCH PRICE   │     SerpApiClient.fetch_cheapest_offer()
│    (SerpApi)     │     → Consulta Google Flights API
│                  │     → Extrae price_insights: typical_price_range,
│                  │       price_level ("low" / "typical" / "high")
└────────┬─────────┘
         │
         ▼
┌──────────────────┐
│ 2. CATEGORIZE    │     Google Flights clasifica internamente:
│                  │     • best_flights  → price_category = "best" (LOW)
│                  │     • other_flights → price_category = "other"
│                  │     Se toma el vuelo mas barato de ambas listas
└────────┬─────────┘
         │
         ▼
┌──────────────────┐
│ 3. SAVE PRICE    │     SQLiteStorage.insert_price()
│                  │     → Guarda: price, currency, airline,
│                  │       price_category, timestamp
└────────┬─────────┘
         │
         ▼
┌──────────────────┐
│ 4. COMPARE vs    │     FlightMonitor.should_recommend()
│    TYPICAL RANGE │     discount_pct = (typical_low - price) / typical_low × 100
│                  │     recommended = discount_pct > 0
│                  │     (precio < rango tipico → recomienda comprar)
└────────┬─────────┘
         │
         ▼
┌──────────────────┐
│ 5. RETURN RESULT │     FlightCheckResult con offer, discount_pct, recommended
└────────┬─────────┘
         │
         ▼ (luego de chequear TODOS los vuelos)
┌──────────────────┐
│ 6. SEND SUMMARY  │     Para cada notifier configurado:
│                  │     • EmailNotifier  → Resumen diario Gmail SMTP
│                  │     • TelegramNotif  → Resumen diario Telegram Bot
└──────────────────┘
```

### Ejecucion Concurrente

Cuando hay multiples vuelos configurados, se ejecutan en paralelo:

```
                    ┌─────────────────────┐
                    │  check_all_flights  │
                    │       _async()      │
                    └──────────┬──────────┘
                               │
                               ▼
                    ┌─────────────────────┐
                    │  ThreadPoolExecutor │
                    │  (max_workers = N)  │
                    └──────────┬──────────┘
                               │
           ┌───────────────────┼───────────────────┐
           │                   │                   │
           ▼                   ▼                   ▼
    ┌─────────────┐     ┌─────────────┐     ┌─────────────┐
    │ check_flight│     │ check_flight│     │ check_flight│
    │ (BOG→MIA)   │     │ (BOG→MAD)   │     │ (BOG→LIM)   │
    └─────────────┘     └─────────────┘     └─────────────┘
           │                   │                   │
           └───────────────────┼───────────────────┘
                               │
                               ▼
                    ┌─────────────────────┐
                    │   asyncio.gather()  │
                    │   (espera todos)    │
                    └─────────────────────┘
```

---

## Logica de Alertas

### Fuente de Comparacion: Rango Tipico de Google

En lugar de mantener un promedio historico propio, el monitor usa el **rango tipico de precios** que Google Flights publica para cada ruta. Este dato viene en la respuesta de la API como `price_insights`:

| Campo | Significado |
|-------|-------------|
| `typical_price_low` | Limite inferior del rango tipico |
| `typical_price_high` | Limite superior del rango tipico |
| `price_level` | Clasificacion: `"low"`, `"typical"`, o `"high"` |

### Algoritmo de Decision

```python
def should_recommend(self, offer: FlightOffer) -> tuple[bool, float]:
    # 1. Necesita rango tipico de Google para comparar
    if offer.typical_price_low is None:
        return False, 0.0

    # 2. Calcular descuento vs limite inferior del rango tipico
    discount_pct = ((offer.typical_price_low - offer.price) / offer.typical_price_low) * 100

    # 3. Recomendar si el precio esta POR DEBAJO del limite inferior
    recommended = discount_pct > 0

    return recommended, discount_pct
```

### Ejemplo Practico

```
Google Flights para BOG → MIA:
  Rango tipico: USD 430 - USD 550
  Nivel Google: LOW

Precio encontrado: USD 400

Descuento = (430 - 400) / 430 × 100 = 7.0%

7.0% > 0% → RECOMENDADO COMPRAR
```

### Formato de Notificacion (Resumen Diario)

Cada corrida envia un **resumen** de todos los vuelos monitoreados con formato visual mejorado:

```
══════════════════════════════════════════════════════════
          ✈️  RESUMEN DIARIO DE VUELOS  ✈️
══════════════════════════════════════════════════════════
  📅 Fecha: Mie 21 May 2026
  🕐 Hora:  11:00
══════════════════════════════════════════════════════════

╔══════════════════════════════════════════════════════════╗
║                    RESUMEN RAPIDO                        ║
╠══════════════════════════════════════════════════════════╣
║  BOG → IAD      COP 2,133,610        🟡 Esperar         ║
║  BOG → LIM      COP 1,556,782        🟢 COMPRAR         ║
╚══════════════════════════════════════════════════════════╝

──────────────────────────────────────────────────────────
✈️  VUELO: BOG → LIM (ida y vuelta)
──────────────────────────────────────────────────────────
  📅 Ida:        Dom 07 Sep 2026
  📅 Vuelta:     Vie 12 Sep 2026
  ⏱️  Duracion:   5 dias

  💰 Precio:     COP 1,050,000
  🛫 Aerolinea:  LATAM (Directo)
  ⏱️  Vuelo:      3h 45m

  📊 Rango tipico Google: COP 1,100,000 - 1,450,000
  📉 vs Tipico:  4.5% MAS BARATO 🎉
  🏷️  Nivel:      BAJO 🟢

  👉 🟢 COMPRAR AHORA

  🔗 Ver en Google Flights:
     https://www.google.com/travel/flights?q=...

══════════════════════════════════════════════════════════
```

**Caracteristicas del formato:**
- 📅 Fechas en español (Lun, Mar, Mie...) con dia de semana
- ⏱️ Duracion del viaje y tiempo de vuelo
- 🟢🟡🔴 Indicadores visuales de precio (bajo/tipico/alto)
- 🧭 Alternativas mas baratas cuando `check_alternatives: true`
- 🔗 Links directos a Google Flights
- 📊 Resumen rapido al inicio

El asunto del email es `[COMPRAR] Resumen diario de vuelos - DD/MM/YYYY` si al menos un vuelo es recomendado, o `Resumen diario de vuelos - DD/MM/YYYY` en caso contrario.

### Formato Telegram

```
✈️ *RESUMEN DE VUELOS*
📅 Mie 21 May • 11:00

🟡 *BOG → IAD*
   🗓 Jue 25 Jun → Lun 30 Jun (5d)
   💰 COP 2,133,610
   🛫 United • 12h 30m • 1 escala(s)
   📈 25% sobre típico
   ⏳ Esperar mejor precio

🟢 *BOG → LIM*
   🗓 Dom 07 Sep → Vie 12 Sep (5d)
   💰 COP 1,050,000
   🛫 LATAM • 3h 45m • directo
   📉 4% bajo típico 🎉
   ✅ *COMPRAR AHORA*

🔔 *¡Hay vuelos recomendados para comprar!*

🔍 google.com/flights
```

---

## Configuracion

### Variables de Entorno (`.env`)

```bash
# ═══════════════════════════════════════════════════════════════
# API DE VUELOS (REQUERIDO)
# ═══════════════════════════════════════════════════════════════
SERPAPI_KEY=tu_api_key_aqui          # https://serpapi.com

# ═══════════════════════════════════════════════════════════════
# EMAIL (OPCIONAL) - Gmail SMTP
# ═══════════════════════════════════════════════════════════════
EMAIL_SENDER=tu-correo@example.com
EMAIL_PASSWORD=xxxx xxxx xxxx xxxx   # App Password (16 chars)
EMAIL_RECEIVER=destino@example.com,otro@example.com  # Multiples separados por coma

# ═══════════════════════════════════════════════════════════════
# TELEGRAM (OPCIONAL)
# ═══════════════════════════════════════════════════════════════
TELEGRAM_BOT_TOKEN=tu_bot_token_aqui
TELEGRAM_CHAT_ID=tu_chat_id_aqui

# ═══════════════════════════════════════════════════════════════
# OPCIONALES
# ═══════════════════════════════════════════════════════════════
DB_PATH=flight_prices.db             # Ruta base de datos SQLite
CHECK_INTERVAL_MINUTES=60            # Intervalo en modo continuo
SCHEDULED_TIMES=11:00                # Horario base para modo programado
SERPAPI_MIN_SEARCHES_LEFT=5          # Umbral para detenerse antes de agotar cuota
RETRY_DELAY_MINUTES=60               # Reintento cuando falle un chequeo
SCHEDULER_STATE_PATH=.flight_monitor_scheduler.json
```

**Obtener credenciales:**

| Servicio | Como obtener |
|----------|--------------|
| SerpApi | [serpapi.com](https://serpapi.com) - 250 busquedas gratis/mes |
| Gmail App Password | [myaccount.google.com/apppasswords](https://myaccount.google.com/apppasswords) |
| Telegram Bot | [@BotFather](https://t.me/botfather) para token, [@userinfobot](https://t.me/userinfobot) para chat_id |

### Vuelos a Monitorear (`flights.yaml`)

```yaml
flights:
  # Vuelo ida y vuelta
  - origin: BOG              # Codigo IATA aeropuerto origen
    destination: MIA         # Codigo IATA aeropuerto destino
    depart_date: "2026-12-01"
    return_date: "2026-12-15"
    adults: 1
    currency: USD

  # Vuelo solo ida
  - origin: BOG
    destination: MAD
    depart_date: "2026-11-20"
    adults: 2
    currency: EUR

  # Vuelo en moneda local
  - origin: BOG
    destination: LIM
    depart_date: "2026-09-14"
    currency: COP

  # Comparar tambien aeropuertos alternativos del destino
  - origin: BOG
    destination: MIA
    depart_date: "2026-12-01"
    return_date: "2026-12-15"
    currency: USD
    check_alternatives: true
```

### Aeropuertos Alternativos (`airports.yaml`)

Si un vuelo tiene `check_alternatives: true`, el monitor consulta la ruta principal y
las alternativas del destino configuradas en `airports.yaml`. El origen no cambia.

Ejemplo: con `destination: MIA`, el archivo incluido consulta tambien `FLL` y `PBI`.
Las alternativas se reportan en el resumen cuando son mas baratas que la ruta principal.

Cada alternativa consume una busqueda adicional de SerpApi. Una ruta con dos
alternativas equivale a tres consultas de vuelos por corrida.

---

## Uso

### Modo Unico (Recomendado)

```bash
uv run --no-editable python -m flight_monitor --once
```

Ejecuta un solo chequeo de todos los vuelos y termina. Ideal para **cron jobs**.

Si falla alguna consulta de vuelos, el proceso termina con codigo de error. Si el chequeo
de vuelos fue exitoso pero falla una notificacion, el proceso solo imprime un aviso para
evitar repetir consultas ya completadas.

### Modo Programado con Reintentos

```bash
uv run --no-editable python -m flight_monitor --scheduled
```

Este modo consulta el archivo de estado y solo corre cuando:

- ya llego uno de los horarios configurados en `SCHEDULED_TIMES`
- hay una corrida pendiente porque no se completo
- paso al menos `RETRY_DELAY_MINUTES` desde el ultimo intento fallido

Sirve para recuperarse de dos casos:

- el equipo o `cron` no ejecuto la corrida del horario previsto
- la corrida si arranco pero fallo por red o SerpApi

Los fallos de email o Telegram no fuerzan reintento si las consultas de vuelos ya fueron
exitosas.

### Modo Continuo

```bash
uv run --no-editable python -m flight_monitor
```

Ejecuta chequeos en loop segun `CHECK_INTERVAL_MINUTES`.

### Configuracion con Cron

Para habilitar reintentos una hora despues, programa una invocacion **cada hora** y deja
que `--scheduled` decida si hay una corrida pendiente:

```bash
# Editar crontab
crontab -e

# Ejemplo: revisar cada hora y ejecutar solo a las 11:00 o sus reintentos
0 * * * * cd /ruta/a/flight-monitor && uv run --no-editable python -m flight_monitor --scheduled >> monitor.log 2>&1
```

Con `SCHEDULED_TIMES=11:00`:

- si la corrida de las 11:00 no sucede, la siguiente invocacion horaria la recupera
- si la corrida de las 11:00 falla por red, la siguiente invocacion horaria la vuelve a intentar
- una vez la corrida termina bien, no se repite hasta el siguiente dia

### Configuracion con launchd (macOS)

El directorio `launchd/` incluye una plantilla para `launchd` (el equivalente a cron en
macOS). La plantilla incluida invoca `--scheduled` a las 11:00 y tambien al cargar el
agente:

```bash
# Crear una copia local y ajustar la ruta del proyecto
cp launchd/com.flight-monitor.plist.example launchd/com.flight-monitor.plist
vim launchd/com.flight-monitor.plist

# Instalar el agente
cp launchd/com.flight-monitor.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.flight-monitor.plist

# Ver logs
tail -f monitor.log

# Desinstalar
launchctl unload ~/Library/LaunchAgents/com.flight-monitor.plist
```

Para tener reintentos el mismo dia con `launchd`, la invocacion debe ocurrir mas de una
vez al dia, por ejemplo cada hora. `--scheduled` decide si realmente hay una ventana
pendiente antes de consumir SerpApi.

### Configuracion con GitHub Actions (Recomendado)

La forma mas confiable de ejecutar el monitor es usando GitHub Actions. No requiere que tu computadora este encendida.

El workflow `.github/workflows/monitor.yml` se ejecuta automaticamente a las 11:00 AM hora Colombia (16:00 UTC) todos los dias.

Antes de consultar vuelos, el programa llama al Account API de SerpApi para verificar cuantas búsquedas quedan. Si la cuota restante cae por debajo de `SERPAPI_MIN_SEARCHES_LEFT`, la ejecución se corta antes de gastar más búsquedas.

**Configurar secrets en GitHub:**

1. Ve a tu repositorio → Settings → Secrets and variables → Actions
2. Agrega los siguientes secrets:

| Secret | Descripcion | Requerido |
|--------|-------------|-----------|
| `SERPAPI_KEY` | Tu API key de SerpApi | Si |
| `FLIGHTS_YAML` | Contenido completo de tu `flights.yaml` | Si |
| `EMAIL_SENDER` | Correo Gmail para enviar | No |
| `EMAIL_PASSWORD` | App Password de Gmail (16 caracteres) | No |
| `EMAIL_RECEIVER` | Correo(s) destino, separados por coma | No |
| `TELEGRAM_BOT_TOKEN` | Token del bot de Telegram | No |
| `TELEGRAM_CHAT_ID` | Chat ID de Telegram | No |

El workflow usa el umbral por defecto `SERPAPI_MIN_SEARCHES_LEFT=5`. Para cambiarlo en
GitHub Actions, agrega esa variable al paso `Run flight monitor`.

**Ejemplo de `FLIGHTS_YAML`:**

```yaml
flights:
  - origin: BOG
    destination: MIA
    depart_date: "2026-12-01"
    return_date: "2026-12-15"
    adults: 1
    currency: USD
```

**Ejecutar manualmente:** Actions → Flight Monitor → Run workflow

### Output de Ejemplo

```
==================================================
  Flight Monitor (SerpApi) - Modo unico
  Chequeando 2 vuelo(s)
  Alerta: cuando precio < rango tipico de Google
==================================================

[DB] Base de datos inicializada: flight_prices.db

==================================================
[2026-12-01 14:30 UTC] Chequeando BOG -> MIA (2026-12-01)
[SerpApi] Categoria de precio: LOW
[SerpApi] Rango tipico Google: USD 430 - 550
[SerpApi] Nivel de precio Google: LOW
[Monitor] Precio encontrado: USD 385 (American Airlines, 1 escala(s)) [LOW]
[DB] Guardado: USD 385 (American Airlines) [LOW]
[Monitor] Precio 10.4% POR DEBAJO del rango tipico
[Monitor] *** RECOMENDADO COMPRAR ***

[Monitor] Chequeo completado.
[Email] Resumen diario enviado a destino@example.com
[Telegram] Resumen enviado.
```

---

## Base de Datos

### Esquema SQLite

```sql
CREATE TABLE prices (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    origin          TEXT NOT NULL,     -- Codigo IATA origen
    destination     TEXT NOT NULL,     -- Codigo IATA destino
    depart_date     TEXT NOT NULL,     -- Fecha salida (YYYY-MM-DD)
    return_date     TEXT,              -- Fecha regreso (NULL si solo ida)
    price           REAL NOT NULL,     -- Precio en moneda especificada
    currency        TEXT NOT NULL,     -- Codigo moneda (USD, EUR, COP)
    airline         TEXT,              -- Aerolinea principal
    price_category  TEXT DEFAULT 'other',  -- "best" (LOW) o "other"
    checked_at      TEXT NOT NULL      -- Timestamp ISO 8601
);
```

### Consultas Utiles

```bash
# Abrir base de datos
sqlite3 flight_prices.db

# Ver ultimos 10 precios
SELECT * FROM prices ORDER BY checked_at DESC LIMIT 10;

# Promedio de precios LOW por ruta
SELECT origin, destination, depart_date,
       ROUND(AVG(price), 2) as avg_low,
       COUNT(*) as registros
FROM prices
WHERE price_category = 'best'
GROUP BY origin, destination, depart_date;

# Precio minimo historico por ruta
SELECT origin, destination,
       MIN(price) as min_price,
       currency
FROM prices
GROUP BY origin, destination;

# Evolucion de precios para una ruta
SELECT date(checked_at) as fecha,
       ROUND(AVG(price), 2) as precio_promedio,
       MIN(price) as min,
       MAX(price) as max
FROM prices
WHERE origin = 'BOG' AND destination = 'MIA'
GROUP BY date(checked_at)
ORDER BY fecha;
```

---

## Extensibilidad

### Agregar Nuevo Notificador

1. Crear `src/flight_monitor/notifiers/slack.py`:

```python
from typing import Optional
import requests
from .base import FlightOffer, Notifier, PriceStats

class SlackNotifier(Notifier):
    def __init__(self, webhook_url: Optional[str] = None):
        self.webhook_url = webhook_url

    def is_configured(self) -> bool:
        return bool(self.webhook_url)

    def send(self, offer: FlightOffer, stats: PriceStats, discount_pct: float) -> bool:
        if not self.is_configured():
            return False
        text = self.build_message(offer, stats, discount_pct)
        response = requests.post(self.webhook_url, json={"text": text})
        return response.ok
```

2. Registrar en `__main__.py`:

```python
from .notifiers.slack import SlackNotifier

notifiers = [
    EmailNotifier(...),
    TelegramNotifier(...),
    SlackNotifier(webhook_url=os.getenv("SLACK_WEBHOOK_URL")),
]
```

### Agregar Nuevo Proveedor de Vuelos

1. Crear `src/flight_monitor/clients/amadeus.py`:

```python
from typing import Optional
from ..config import FlightConfig
from ..notifiers.base import FlightOffer

class AmadeusClient:
    def __init__(self, api_key: str, api_secret: str):
        self.api_key = api_key
        self.api_secret = api_secret

    def fetch_cheapest_offer(self, flight: FlightConfig) -> Optional[FlightOffer]:
        # Implementar llamada a Amadeus API
        # Retornar FlightOffer o None
        pass
```

2. Intercambiar en `__main__.py`:

```python
from .clients.amadeus import AmadeusClient

client = AmadeusClient(
    api_key=config.amadeus_api_key,
    api_secret=config.amadeus_api_secret
)
```

---

## Optimizacion de API

### Plan Gratuito SerpApi: 250 llamadas/mes

| Vuelos | Chequeos/dia | Llamadas/mes | Status |
|--------|--------------|--------------|--------|
| 1 | 1 | 30 | OK |
| 1 | 2 | 60 | OK |
| 1 | 3 | 90 | OK |
| 2 | 1 | 60 | OK |
| 2 | 2 | 120 | OK |
| 3 | 2 | 180 | OK |
| 4 | 2 | 240 | OK |
| 5 | 2 | 300 | Excede limite |

Cuenta cada ruta consultada. Si activas `check_alternatives`, suma la ruta principal y
cada destino alternativo.

### Recomendaciones

- **1 vuelo**: Hasta 3 chequeos/dia (90 llamadas/mes)
- **2 vuelos**: Maximo 1 chequeo/dia (60 llamadas/mes)
- **Horarios optimos**: 6 AM y 6 PM suelen tener variaciones de precio

---

## Desarrollo

### Comandos

```bash
# Instalar exactamente las dependencias del lockfile
uv sync --locked --all-groups --no-editable

# Ejecutar linter
uv run --no-editable ruff check .

# Ejecutar type checker
uv run --no-editable mypy src/

# Ejecutar pruebas funcionales locales
uv run --no-editable python -m unittest discover -s tests -v
```

Las pruebas usan respuestas simuladas de SerpApi y una base SQLite temporal. Validan:

- lectura de cuota y parseo de ofertas de SerpApi
- seleccion de la oferta mas barata y calculo por persona
- recomendacion de compra, persistencia SQLite y resumen
- manejo de consultas fallidas
- reintentos y cierre de ventanas del scheduler

No consumen cuota de SerpApi ni envian email o mensajes de Telegram.

El workflow `.github/workflows/ci.yml` ejecuta estos controles en cada push a `main` y en
cada pull request.

El proyecto usa `uv_build`. Los comandos incluyen `--no-editable` para evitar que Python
omita archivos `.pth` marcados como ocultos en algunos entornos macOS. UV reinstala el
paquete cuando detecta cambios locales.

---

## Seguridad

### Archivos Sensibles

Estos archivos estan en `.gitignore` y **nunca** deben commitearse:

| Archivo | Contenido | Riesgo |
|---------|-----------|--------|
| `.env` | API keys, contrasenas | CRITICO |
| `flights.yaml` | Planes de viaje | MEDIO |
| `*.db` | Historial de busquedas | BAJO |
| `*.log` | Puede contener datos | BAJO |

### Buenas Practicas

1. Usar **repositorio privado** en GitHub
2. Usar **App Passwords** de Gmail (no contrasena principal)
3. Rotar API keys periodicamente
4. No compartir archivos de configuracion

---

## Troubleshooting

### Error: "Falta la API key de SerpApi"

Asegurate de que `.env` existe y contiene `SERPAPI_KEY=tu_key`.

### Error: "No hay vuelos configurados"

Crea `flights.yaml` basado en `flights.yaml.example`.

### Error: "Cuota SerpApi baja"

El monitor se detiene antes de gastar mas busquedas cuando la cuota restante es menor o
igual a `SERPAPI_MIN_SEARCHES_LEFT`. Sube el umbral si quieres mas margen, o bajalo si
aceptas acercarte mas al limite mensual.

### No llegan emails

1. Verifica que usas un **App Password** de Gmail, no tu contrasena normal
2. Revisa que `EMAIL_SENDER`, `EMAIL_PASSWORD` y `EMAIL_RECEIVER` esten configurados
3. Revisa la carpeta de spam

Si Gmail responde `535 Username and Password not accepted`, genera una App Password nueva,
actualiza `EMAIL_PASSWORD` y vuelve a ejecutar. La contrasena normal de la cuenta no funciona
con este cliente SMTP.

### No llegan mensajes de Telegram

1. Inicia una conversacion con tu bot primero
2. Verifica el `TELEGRAM_CHAT_ID` con [@userinfobot](https://t.me/userinfobot)

---

## Licencia

MIT License - Ver archivo [LICENSE](LICENSE) para detalles.

---

## Contribuir

Las contribuciones son bienvenidas. Por favor:

1. Fork el repositorio
2. Crea una rama para tu feature (`git checkout -b feature/nueva-funcionalidad`)
3. Commit tus cambios (`git commit -m 'Agregar nueva funcionalidad'`)
4. Push a la rama (`git push origin feature/nueva-funcionalidad`)
5. Abre un Pull Request

---

<p align="center">
  Desarrollado con la asistencia de <a href="https://claude.ai">Claude Code</a> (Anthropic)
</p>
