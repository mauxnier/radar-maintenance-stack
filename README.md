# Radar Maintenance Dashboard

Real-time radar maintenance dashboard powered by TimescaleDB, Grafana, and a fault injection UI — fully dockerized.

## Overview

This stack simulates the telemetry of a surveillance radar and displays it in a live Grafana dashboard. A Python simulator generates realistic sensor data every second (rotation, temperatures, power, RF, humidity, vibration) and stores it in TimescaleDB. A web-based fault injector lets you trigger failure scenarios on demand to observe how the dashboard reacts.

## Architecture

```
┌─────────────────┐     INSERT 1/s     ┌───────────────────┐
│  simulator.py   │ ─────────────────► │                   │
│  (Python 3.11)  │                    │   TimescaleDB     │
│                 │ ◄── poll commands  │  (PostgreSQL 15)  │
└─────────────────┘                    │                   │
                                       └────────┬──────────┘
┌─────────────────┐     INSERT fault            │
│ fault_injector  │ ─────────────────►          │  SQL queries
│  (FastAPI 5001) │                    ┌────────▼──────────┐
└─────────────────┘                    │      Grafana      │
                                       │   (port 3000)     │
                                       └───────────────────┘
```

## Services

| Service | Image | Port | Description |
|---|---|---|---|
| `timescaledb` | `timescale/timescaledb:latest-pg15` | 5432 | Time-series database |
| `simulator` | `python:3.11-slim` | — | Generates radar metrics every second |
| `fault-injector` | `python:3.11-slim` | 5001 | Web UI to trigger fault scenarios |
| `grafana` | `grafana/grafana:latest` | 3000 | Real-time dashboard |

## Quick Start

**Prerequisites:** Docker and Docker Compose

```bash
git clone <repo-url>
cd radar-maintenance-stack
docker compose up -d
```

Then open:
- **Grafana dashboard:** [http://localhost:3000](http://localhost:3000) — `admin` / `admin`
- **Fault injector:** [http://localhost:5001](http://localhost:5001)

To stop:
```bash
docker compose down
```

To follow simulator logs:
```bash
docker compose logs -f simulator
```

## Monitored Metrics

### Antenna
- Rotation speed (RPM), azimuth angle (0–360°), rotation state (`rotating` / `fixed`)
- Cumulative rotation count, mechanical vibrations (g RMS)

### Thermal
- Cabin temperature, motor temperature, transmitter temperature, external temperature
- Internal and external humidity

### Power — Generator 1 & 2
- Active power (W), voltage (V), current (A), frequency (Hz)
- State: `running` / `standby` / `fault`

### RF
- Peak and average emitted power (W)
- VSWR (Voltage Standing Wave Ratio)

### Supervision
- Operational state: `OPERATIONAL` / `DEGRADED` / `FAULT` / `MAINTENANCE`
- Active alarm count
- Uptime since last maintenance

## Fault Injection

Open [http://localhost:5001](http://localhost:5001) to access the fault injector UI.

| Scenario | Duration | Effect |
|---|---|---|
| Generator 1 fault | 60 s | Gen1 goes to `fault`, power cut, alarm raised |
| Generator 2 fault | 60 s | Gen2 goes to `fault`, power cut, alarm raised |
| Blackout | 30 s | Gen1 + Gen2 fault simultaneously — radar enters `FAULT` state |
| Cabin overheat | 120 s | Cabin temperature +20 °C, crosses alarm threshold |
| VSWR spike | 30 s | VSWR forced above 2.5 — RF alarm |
| Antenna stop | 90 s | Rotation forced to `fixed` mode — RPM = 0 |
| Maintenance mode | 300 s | Operational state forced to `MAINTENANCE` |
| Reset | — | Immediately clears all active faults |

Faults are written to the `radar_commands` table and picked up by the simulator on the next tick.

## Database Schema

The schema is defined in [schema.sql](schema.sql) and auto-applied on first container start.

**Main tables:**
- `radar_metrics` — hypertable, 1 row/second, 90-day retention
- `radar_alarms` — alarm event history
- `radar_thresholds` — warning/critical thresholds reference
- `radar_commands` — fault injection queue

**Continuous aggregates:**
- `radar_metrics_1min` — per-minute averages for real-time graphs
- `radar_metrics_1h` — per-hour averages for historical graphs

## Project Structure

```
radar-maintenance-stack/
├── docker-compose.yml                      # Stack definition
├── schema.sql                              # TimescaleDB schema
├── simulator.py                            # Radar data simulator
├── fault_injector.py                       # Fault injection web UI
└── grafana/
    └── provisioning/
        ├── dashboards/
        │   ├── dashboards.yml              # Dashboard provisioning config
        │   └── radar_dashboard.json        # Grafana dashboard definition
        └── datasources/
            └── timescaledb.yml             # TimescaleDB datasource config
```
