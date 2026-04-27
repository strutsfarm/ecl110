# ecl110

Python service for integrating a **Danfoss ECL110 heating controller** with MQTT.

The service reads ECL110 values over **Modbus RTU (RS-485)** and publishes them to MQTT using a hierarchical topic model. It also supports writing selected controller values through MQTT `/set` topics.

---

## Table of Contents
- [Overview](#overview)
- [Architecture (v2)](#architecture-v2)
- [Key Features](#key-features)
- [MQTT Topic Hierarchy](#mqtt-topic-hierarchy)
- [Writing Values via `/set` Topics](#writing-values-via-set-topics)
- [Backward Compatibility](#backward-compatibility)
- [Project Structure](#project-structure)
- [Requirements](#requirements)
- [Installation](#installation)
- [Configuration](#configuration)
- [Usage](#usage)
- [Operational Notes](#operational-notes)
- [Troubleshooting](#troubleshooting)
- [Known Limitations](#known-limitations)
- [License](#license)

---

## Overview
`ecl110.py` is a long-running bridge service that:

1. Connects to the ECL110 controller over Modbus RTU
2. Connects to an MQTT broker
3. Polls configured registers every **60 seconds**
4. Publishes updates only when values change
5. Accepts writes through dedicated MQTT `/set` topics
6. Keeps a legacy JSON topic for compatibility with existing consumers

---

## Architecture (v2)
The service no longer waits for an external `measure` command to trigger reads. Instead, it runs an **internal autonomous polling loop**:

- Poll interval: **60 seconds** (`LOOP_TIME = 60`)
- First poll happens immediately after startup
- Subsequent polls run continuously while the process is alive

### Change detection behavior
Each cycle, the service compares current register values with the last published snapshot:

- If **no value changed**, nothing is published that cycle
- If **one or more values changed**, it publishes:
  - Individual hierarchical topic updates for changed points
  - A full legacy JSON payload to `ecl110/ecl110_data`

This reduces MQTT noise and unnecessary downstream processing.

---

## Key Features
- Autonomous Modbus polling every 60 seconds
- Change-only MQTT publishing
- Hierarchical MQTT topics per data point
- Writable parameters via `/set` topics
- Legacy JSON telemetry topic preserved for backward compatibility
- Retry logic for Modbus read/write operations
- MQTT reconnect backoff handling
- Watchdog reboot after repeated full Modbus-read failure cycles

---

## MQTT Topic Hierarchy
Root topic for per-point telemetry is:

- `ecl110`

Examples of published telemetry topics:

- `ecl110/flow_temp_control/slope`
- `ecl110/flow_temp_control/displace`
- `ecl110/flow_temp_control/flow_temp_min`
- `ecl110/flow_temp_control/flow_temp_max`
- `ecl110/room_temp_limit/room_gain_max`
- `ecl110/room_temp_limit/room_gain_min`
- `ecl110/return_temp_limit/limit`
- `ecl110/return_temp_limit/return_gain_max`
- `ecl110/return_temp_limit/return_gain_min`
- `ecl110/return_temp_limit/return_integration_time`
- `ecl110/optimization/auto_reduct`
- `ecl110/optimization/ramp`
- `ecl110/control_parameters/xp`
- `ecl110/control_parameters/tn`
- `ecl110/temperatures/set_room_temperature`
- `ecl110/temperatures/outdoor_temp`
- `ecl110/temperatures/room_temp`
- `ecl110/temperatures/flow_temp`
- `ecl110/temperatures/return_temp`
- `ecl110/temperatures/room_set_temp`
- `ecl110/temperatures/flow_set_temp`
- `ecl110/temperatures/accumulated_outdoor_temp`

### Example subscriber
```bash
mosquitto_sub -h 192.168.1.222 -p 1883 -u mqtt-user -P mqtt-user -t 'ecl110/#'
```

Values are JSON-encoded scalar values (for example `"21.5"`, `"0"`, `"-2"`).

---

## Writing Values via `/set` Topics
Writable parameters are controlled by publishing to the same hierarchy with a `/set` suffix.

### Topic pattern
- `ecl110/<group>/<parameter>/set`

### Examples
- `ecl110/flow_temp_control/displace/set`
- `ecl110/flow_temp_control/slope/set`
- `ecl110/return_temp_limit/limit/set`
- `ecl110/optimization/ramp/set`
- `ecl110/control_parameters/xp/set`
- `ecl110/temperatures/set_room_temperature/set`

### Accepted payload formats
The service accepts either:

1. Plain numeric payload
2. JSON payload with a `value` key

Examples:

```bash
# Plain numeric payload
mosquitto_pub -h 192.168.1.222 -p 1883 -u mqtt-user -P mqtt-user \
  -t 'ecl110/flow_temp_control/displace/set' -m '2'
```

```bash
# JSON payload
mosquitto_pub -h 192.168.1.222 -p 1883 -u mqtt-user -P mqtt-user \
  -t 'ecl110/flow_temp_control/displace/set' -m '{"value":2}'
```

If write succeeds, the service updates internal cache for immediate state consistency.

---

## Backward Compatibility
For consumers that still rely on the previous JSON contract, the service continues publishing to:

- `ecl110/ecl110_data`

Payload includes a timestamp and the full set of measured values (including legacy status fields such as `pump`, `valve_open`, `valve_closed`, `actual_mode`).

> Compatibility note: the legacy JSON topic is now emitted only when at least one measured value changes.

---

## Project Structure
```text
ecl110/
├── ecl110.py      # Main Modbus↔MQTT bridge service (v2 architecture)
├── measure.py     # Legacy helper script for command-based measurement flow
└── measure.sh     # Shell launcher
```

---

## Requirements
### Hardware
- Danfoss ECL110 controller (or compatible Modbus RTU target)
- RS-485/USB adapter (default serial device pattern: `/dev/ttyUSB*`)

### Software
- Python 3.8+
- MQTT broker (default: `192.168.1.222:1883`)

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

---

## Configuration
Configuration is currently in-code (`ecl110.py` / `measure.py`).

### MQTT defaults
```python
MQTT_BROKER = "192.168.1.222"
MQTT_PORT = 1883
username/password = "mqtt-user" / "mqtt-user"
```

### Modbus defaults
```python
BAUDRATE = 19200
SLAVE_ADDRESS = 5
serial parity = EVEN
```

### Poll interval
```python
LOOP_TIME = 60
```

---

## Usage
Start the service:

```bash
python3 ecl110.py
```

The process runs continuously, polling every 60 seconds and publishing only changed values.

---

## Operational Notes
- MQTT client IDs are randomized to avoid collisions
- Modbus reads/writes are retried up to 3 times
- MQTT reconnect uses exponential delay (`1s` to `120s`)
- Watchdog triggers reboot after 3 consecutive fully-failed measurement cycles
- Publish QoS is 1 for reliability

---

## Troubleshooting
- **Serial device not found**: verify adapter, wiring, permissions (`dialout`), and USB IDs
- **MQTT connection issues**: verify broker IP, credentials, network/firewall
- **No topic updates**: if controller values are stable, no publish occurs by design (change-only publishing)
- **Write does not apply**: verify topic path ends with `/set` and payload is numeric or `{"value": ...}`

---

## Known Limitations
- Broker/serial credentials and connection settings are hardcoded
- No packaged dependency lockfile is included
- `measure.py` reflects legacy command-trigger flow and is optional in v2 architecture

---

## License
No license file is currently included.
If you plan to distribute this project, add a `LICENSE` file (for example MIT, Apache-2.0, or GPL).
