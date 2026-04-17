# ecl110

A lightweight Python service for integrating a **Danfoss ECL110 heating controller** with an MQTT-based automation stack.

This project reads control and telemetry values from an ECL110 over **Modbus RTU (RS-485)** and exposes the data through MQTT. It also listens for MQTT commands to trigger measurements and update selected controller parameters.

---

## Table of Contents
- [Overview](#overview)
- [Key Features](#key-features)
- [How It Works](#how-it-works)
- [Project Structure](#project-structure)
- [Requirements](#requirements)
- [Installation](#installation)
- [Configuration](#configuration)
- [Usage](#usage)
  - [1) Start the bridge service](#1-start-the-bridge-service)
  - [2) Trigger a measurement](#2-trigger-a-measurement)
  - [3) Set displacement value](#3-set-displacement-value)
- [MQTT Contract](#mqtt-contract)
- [Modbus Registers Covered](#modbus-registers-covered)
- [Operational Notes](#operational-notes)
- [Troubleshooting](#troubleshooting)
- [Known Issues](#known-issues)
- [License](#license)

---

## Overview
The repository contains a small integration layer intended for heating system monitoring/control scenarios where:

- An ECL110 controller is reachable over serial Modbus RTU (`/dev/ttyUSB0` by default)
- An MQTT broker is available on the local network
- External systems (Home Assistant, Node-RED, scripts, etc.) communicate by publishing commands and consuming telemetry

The main script (`ecl110.py`) runs as a long-lived process that:

1. Connects to the ECL110 via Modbus
2. Connects to MQTT with automatic reconnect behavior
3. Subscribes to a command topic
4. On command, reads multiple registers and publishes a JSON payload
5. Optionally writes the displacement register via a command

---

## Key Features
- **Modbus RTU polling** of key ECL110 parameters
- **MQTT command/response pattern**
- **Single JSON telemetry payload** with timestamp and register values
- **Safe register reads with retries** (`3` attempts per register)
- **MQTT reconnect backoff** for improved network resilience
- **Simple command utility** (`measure.py`) to request measurement on demand

---

## How It Works
- `ecl110.py` subscribes to: `ecl110/command`
- If it receives:
  - `{"command": "measure"}` → reads configured registers and publishes to `ecl110/ecl110_data`
  - `{"command": "displace", "value": <int>}` → writes the displacement register

Data exchange is JSON over MQTT with QoS 1 for publish reliability.

---

## Project Structure
```text
ecl110/
├── ecl110.py      # Main Modbus↔MQTT bridge service
├── measure.py     # Helper script that publishes {"command": "measure"}
└── measure.sh     # Shell launcher (currently references a non-existing filename)
```

---

## Requirements
### Hardware
- Danfoss ECL110 controller (or compatible Modbus RTU target)
- RS-485/USB adapter exposed as a serial device (default: `/dev/ttyUSB0`)

### Software
- Python 3.8+ (recommended)
- MQTT broker (default configured host: `192.168.1.222`, port `1883`)

### Python dependencies
- `minimalmodbus`
- `pyserial`
- `paho-mqtt`

---

## Installation
```bash
git clone https://github.com/strutsfarm/ecl110
cd ecl110

python3 -m venv .venv
source .venv/bin/activate

pip install minimalmodbus pyserial paho-mqtt
```

Optional sanity check:
```bash
python3 -m py_compile ecl110.py measure.py
```

---

## Configuration
The project currently uses in-file constants in `ecl110.py` and `measure.py`.

### MQTT settings
```python
MQTT_BROKER = "192.168.1.222"
MQTT_PORT = 1883
mqtt username/password = "mqtt-user" / "mqtt-user"
```

### Serial/Modbus settings
```python
PORTNAME = "/dev/ttyUSB0"
BAUDRATE = 19200
SLAVE_ADDRESS = 5
serial parity = EVEN
```

### MQTT topics
- Command topic: `ecl110/command`
- Data topic: `ecl110/ecl110_data`

> For production usage, consider moving credentials and connection values to environment variables or a config file.

---

## Usage

### 1) Start the bridge service
```bash
python3 ecl110.py
```

This process stays running and waits for MQTT commands.

### 2) Trigger a measurement
Use the helper:
```bash
python3 measure.py
```

Or publish manually:
```bash
mosquitto_pub -h 192.168.1.222 -p 1883 -u mqtt-user -P mqtt-user \
  -t ecl110/command -m '{"command":"measure"}'
```

### 3) Set displacement value
Publish a command like:
```bash
mosquitto_pub -h 192.168.1.222 -p 1883 -u mqtt-user -P mqtt-user \
  -t ecl110/command -m '{"command":"displace","value":2}'
```

---

## MQTT Contract
### Command messages (`ecl110/command`)
Examples:
```json
{"command":"measure"}
```

```json
{"command":"displace","value":2}
```

### Telemetry output (`ecl110/ecl110_data`)
Example payload shape:
```json
{
  "datetime": "2026-04-17 10:20:30.123456",
  "slope": 1.4,
  "displace": 0,
  "flow_temp_min": 30,
  "flow_temp_max": 70,
  "room_temp": 21.5,
  "flow_temp": 45.2,
  "return_temp": 37.0,
  "pump": 1,
  "valve_open": 0,
  "valve_closed": 1,
  "actual_mode": 2
}
```

If individual register reads fail, the script logs warnings and omits unreadable values.

---

## Modbus Registers Covered
The implementation reads/writes a curated set of ECL110 registers grouped by control area, including:

- Flow temperature curve parameters (slope/displacement/min/max)
- Room and return temperature control gains
- Optimization parameters
- Control parameters (`xp`, `tn`)
- Measured temperatures (outdoor/room/flow/return)
- Binary status values (pump/valve/mode)

Register addresses and decimal/signed handling are defined as constants in `ecl110.py`.

---

## Operational Notes
- MQTT client IDs are randomized (`uuid` suffix) to avoid broker session collisions.
- `safe_read_register()` retries failed Modbus reads up to 3 times with short delay.
- MQTT reconnect strategy uses exponential delay (`1s` to `120s`).
- The bridge publishes with **QoS 1** and checks publish return codes.

---

## Troubleshooting
- **Cannot open serial device**
  - Check adapter path (`/dev/ttyUSB0`), user permissions (`dialout` group), and cable wiring.
- **MQTT connect failures**
  - Verify broker IP/port, credentials, and firewall/network reachability.
- **No data returned**
  - Confirm correct Modbus slave address, baud rate, parity, and physical bus integrity.
- **Intermittent missing fields**
  - Some register reads may fail transiently; inspect logs for per-register errors.

---

## Known Issues
1. `measure.sh` currently calls `python measure_ecl110.py`, but the repository contains `measure.py`.
   - Suggested fix:
     ```bash
     python measure.py
     ```

2. Settings are hardcoded in source files (broker, credentials, serial settings).
   - Consider external configuration for safer deployments.

3. No dependency lockfile (`requirements.txt`/`pyproject.toml`) is included.

---

## License
No license file is included in the repository at the time of analysis.

If you intend to distribute or reuse this project, add an explicit license (for example, MIT, Apache-2.0, or GPL) in a `LICENSE` file.
