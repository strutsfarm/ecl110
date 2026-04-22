import time
import uuid
import subprocess
import glob
import os
import json
import logging
from datetime import datetime

import minimalmodbus
import serial
import paho.mqtt.client as mqtt


# Set up logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

MQTT_BROKER = "192.168.1.222"
MQTT_PORT = 1883

LOOP_TIME = 60
JSON_DATA_TOPIC = "ecl110/ecl110_data"  # Kept for backward compatibility
TOPIC_ROOT = "/ecl110"

# Watchdog: reboot after this many consecutive fully-failed measurements
# (i.e. every single register failed in N measurements in a row)
WATCHDOG_REBOOT_THRESHOLD = 3

# Flow temp control 2000
SLOPE = 11174  # 0.1 - 4.0, 1 decimal
DISPLACE = 11175  # -20 - 20, 0 decimal
FLOW_TEMP_MIN = 11176  # 10 - 150C, 0 decimal
FLOW_TEMP_MAX = 11177  # 10 - 150C, 0 decimal

# Room T limit 3000
ROOM_GAIN_MAX = 11181  # -9.9 - 0.0, 1 decimal
ROOM_GAIN_MIN = 11182  # 0.0 - 9.9, 1 decimal

# Return T limit 4000
LIMIT = 11029  # 10-110C, 0 decimals
RETURN_GAIN_MAX = 11034  # -9.9 - 9.9, 1 decimal
RETURN_GAIN_MIN = 11035  # -9.9 - 9.9, 1 decimal
RETURN_INTEGRATION_TIME = 11036  # OFF/1-50s, 0 decimals

# Optimization 5000
AUTO_REDUCT = 11010  # -29 - 10C
RAMP = 11012  # 0-99m

# Control parameters 6000
XP = 11183  # Prop factor 1-250K
TN = 11184  # Integration time 5-999s

# 0 decimal
SET_ROOM_TEMPERATURE = 11179  # R/W
# 1 decimal
OUTDOOR_TEMP = 11200  # RO
ROOM_TEMP = 11201  # RO
FLOW_TEMP = 11202  # RO
RETURN_TEMP = 11203  # RO
ROOM_SET_TEMP = 11228  # RO
FLOW_SET_TEMP = 11229  # RO
ACCUMULATED_OUTDOOR_TEMP = 11099  # RO

# Binary 0/1
PUMP = 4001  # RO
VALVE_OPEN = 4100  # RO
VALVE_CLOSE = 4101  # RO
ACTUAL_MODE = 4210  # RO

# RS485 USB adapter identification
USB_VENDOR_ID = "0403"
USB_PRODUCT_ID = "6001"
BAUDRATE = 19200
SLAVE_ADDRESS = 5

# Register definitions: (key, register, decimals, signed, topic_suffix, writable)
REGISTER_DEFINITIONS = [
    ("slope", SLOPE, 1, True, "flow_temp_control/slope", True),
    ("displace", DISPLACE, 0, True, "flow_temp_control/displace", True),
    ("flow_temp_min", FLOW_TEMP_MIN, 0, True, "flow_temp_control/flow_temp_min", True),
    ("flow_temp_max", FLOW_TEMP_MAX, 0, True, "flow_temp_control/flow_temp_max", True),
    ("room_gain_max", ROOM_GAIN_MAX, 1, True, "room_temp_limit/room_gain_max", True),
    ("room_gain_min", ROOM_GAIN_MIN, 1, True, "room_temp_limit/room_gain_min", True),
    ("limit", LIMIT, 0, False, "return_temp_limit/limit", True),
    ("return_gain_max", RETURN_GAIN_MAX, 1, True, "return_temp_limit/return_gain_max", True),
    ("return_gain_min", RETURN_GAIN_MIN, 1, True, "return_temp_limit/return_gain_min", True),
    ("return_integration_time", RETURN_INTEGRATION_TIME, 0, False, "return_temp_limit/return_integration_time", True),
    ("auto_reduct", AUTO_REDUCT, 0, True, "optimization/auto_reduct", True),
    ("ramp", RAMP, 0, False, "optimization/ramp", True),
    ("xp", XP, 0, False, "control_parameters/xp", True),
    ("tn", TN, 0, False, "control_parameters/tn", True),
    ("set_room_temperature", SET_ROOM_TEMPERATURE, 0, True, "temperatures/set_room_temperature", True),
    ("outdoor_temp", OUTDOOR_TEMP, 1, True, "temperatures/outdoor_temp", False),
    ("room_temp", ROOM_TEMP, 1, True, "temperatures/room_temp", False),
    ("flow_temp", FLOW_TEMP, 1, True, "temperatures/flow_temp", False),
    ("return_temp", RETURN_TEMP, 1, True, "temperatures/return_temp", False),
    ("room_set_temp", ROOM_SET_TEMP, 1, True, "temperatures/room_set_temp", False),
    ("flow_set_temp", FLOW_SET_TEMP, 1, True, "temperatures/flow_set_temp", False),
    ("accumulated_outdoor_temp", ACCUMULATED_OUTDOOR_TEMP, 1, True, "temperatures/accumulated_outdoor_temp", False),
]

# Optional legacy telemetry fields (kept in JSON for backward compatibility)
LEGACY_JSON_ONLY_DEFINITIONS = [
    ("pump", PUMP, 0, False),
    ("valve_open", VALVE_OPEN, 0, False),
    ("valve_closed", VALVE_CLOSE, 0, False),
    ("actual_mode", ACTUAL_MODE, 0, False),
]

FULL_TOPIC_BY_KEY = {key: f"{TOPIC_ROOT}/{suffix}" for key, _, _, _, suffix, _ in REGISTER_DEFINITIONS}
WRITABLE_DEFINITION_BY_SET_TOPIC = {
    f"{TOPIC_ROOT}/{suffix}/set": (key, register, decimals, signed)
    for key, register, decimals, signed, suffix, writable in REGISTER_DEFINITIONS
    if writable
}

Connected = False
ecl110 = None
mqttc = None
consecutive_total_failures = 0
previous_values = {}


def find_usb_serial_port(vendor_id=None, product_id=None):
    for device in sorted(glob.glob("/dev/ttyUSB*")):
        try:
            result = subprocess.run(
                ["udevadm", "info", "--query=property", f"--name={device}"],
                capture_output=True, text=True, timeout=5
            )
            props = {}
            for line in result.stdout.splitlines():
                if "=" in line:
                    key, val = line.split("=", 1)
                    props[key] = val

            match = True
            if vendor_id and props.get("ID_VENDOR_ID", "").lower() != vendor_id.lower():
                match = False
            if product_id and props.get("ID_MODEL_ID", "").lower() != product_id.lower():
                match = False

            if match:
                logger.info(
                    f"Found USB serial adapter at {device} "
                    f"(vendor={props.get('ID_VENDOR_ID')}, model={props.get('ID_MODEL_ID')})"
                )
                return device

        except Exception as e:
            logger.warning(f"Error querying {device}: {e}")
            continue

    return None


def watchdog_reboot(reason):
    logger.critical(f"WATCHDOG REBOOT: {reason}")
    logger.critical(f"Consecutive total failures: {consecutive_total_failures}")

    try:
        if mqttc and Connected:
            reboot_msg = json.dumps({
                "event": "watchdog_reboot",
                "reason": reason,
                "datetime": str(datetime.now()),
                "consecutive_failures": consecutive_total_failures,
            })
            mqttc.publish(JSON_DATA_TOPIC, reboot_msg, qos=1)
            time.sleep(1)
            mqttc.disconnect()
            mqttc.loop_stop()
    except Exception as e:
        logger.error(f"Error during watchdog cleanup: {e}")

    logger.critical("Rebooting now...")
    time.sleep(1)
    os.system("sudo /sbin/reboot")


def safe_read_register(register, number_of_decimals=0, signed=False):
    for attempt in range(3):
        try:
            return ecl110.read_register(
                register,
                number_of_decimals=number_of_decimals,
                signed=signed,
            )
        except Exception as e:
            logger.warning(
                f"Modbus read failed for register {register} "
                f"(attempt {attempt + 1}/3): {e}"
            )
            time.sleep(0.1)
    logger.error(f"Modbus read failed for register {register} after 3 attempts")
    return None


def safe_write_register(register, value, number_of_decimals=0, signed=False):
    for attempt in range(3):
        try:
            ecl110.write_register(
                register,
                number_of_decimals=number_of_decimals,
                value=value,
                functioncode=6,
                signed=signed,
            )
            return True
        except Exception as e:
            logger.warning(
                f"Modbus write failed for register {register} value={value} "
                f"(attempt {attempt + 1}/3): {e}"
            )
            time.sleep(0.1)
    logger.error(f"Modbus write failed for register {register} after 3 attempts")
    return False


def parse_set_payload(raw_payload, decimals):
    payload_text = raw_payload.decode().strip()

    # Accept both plain numeric payloads and JSON payloads with "value"
    parsed = None
    try:
        parsed = json.loads(payload_text)
    except json.JSONDecodeError:
        parsed = payload_text

    if isinstance(parsed, dict):
        if "value" not in parsed:
            raise ValueError("JSON payload must contain 'value'")
        parsed = parsed["value"]

    if decimals == 0:
        return int(round(float(parsed)))
    return float(parsed)


def publish_single_value(client, topic, value):
    result = client.publish(topic, json.dumps(value), qos=1)
    if result.rc != mqtt.MQTT_ERR_SUCCESS:
        logger.error(f"Publish failed for topic {topic} with rc={result.rc}")
    else:
        logger.info(f"Published topic update: {topic}={value}")


def read_measurements():
    data = {}
    failed_reads = 0

    for key, register, decimals, signed, _, _ in REGISTER_DEFINITIONS:
        value = safe_read_register(register, decimals, signed)
        if value is None:
            failed_reads += 1
        else:
            data[key] = value

    for key, register, decimals, signed in LEGACY_JSON_ONLY_DEFINITIONS:
        value = safe_read_register(register, decimals, signed)
        if value is None:
            failed_reads += 1
        else:
            data[key] = value

    total_registers = len(REGISTER_DEFINITIONS) + len(LEGACY_JSON_ONLY_DEFINITIONS)
    return data, failed_reads, total_registers


def perform_measurement_and_publish(client):
    global consecutive_total_failures, previous_values

    measurements, failed_reads, total_registers = read_measurements()

    if failed_reads > 0:
        logger.warning(f"{failed_reads}/{total_registers} register(s) failed to read")

    # Watchdog: track consecutive total failures
    if failed_reads == total_registers:
        consecutive_total_failures += 1
        logger.error(
            f"ALL registers failed! Consecutive total failures: "
            f"{consecutive_total_failures}/{WATCHDOG_REBOOT_THRESHOLD}"
        )
        if consecutive_total_failures >= WATCHDOG_REBOOT_THRESHOLD:
            watchdog_reboot(
                f"All {total_registers} Modbus registers failed to read "
                f"in {WATCHDOG_REBOOT_THRESHOLD} consecutive measurements"
            )
        return

    if consecutive_total_failures > 0:
        logger.info(
            f"Modbus communication restored. "
            f"Resetting watchdog counter (was {consecutive_total_failures})"
        )
    consecutive_total_failures = 0

    changed_keys = [
        key for key, value in measurements.items()
        if key not in previous_values or previous_values[key] != value
    ]

    if not changed_keys:
        logger.info("No data changes detected. Nothing published this cycle.")
        return

    logger.info("Detected changes in keys: %s", ", ".join(sorted(changed_keys)))

    # Publish per-point hierarchical topics only for changed values
    for key in changed_keys:
        topic = FULL_TOPIC_BY_KEY.get(key)
        if topic is not None:
            publish_single_value(client, topic, measurements[key])

    # Keep old JSON payload format in parallel, but only when any value changed
    json_payload = dict(measurements)
    json_payload["datetime"] = str(datetime.now())

    payload_str = json.dumps(json_payload)
    result = client.publish(JSON_DATA_TOPIC, payload_str, qos=1)
    if result.rc != mqtt.MQTT_ERR_SUCCESS:
        logger.error(f"JSON publish failed with rc={result.rc}")
    else:
        logger.info(f"Published JSON payload to {JSON_DATA_TOPIC}")

    previous_values = measurements


def on_connect(client, userdata, flags, rc):
    global Connected
    if rc == 0:
        logger.info("Connected to MQTT Broker")
        Connected = True
        set_topics = list(WRITABLE_DEFINITION_BY_SET_TOPIC.keys())
        for topic in set_topics:
            client.subscribe(topic, qos=1)
        logger.info(f"Subscribed to {len(set_topics)} writable /set topics")
    else:
        logger.error(f"Failed to connect, return code {rc}")
        Connected = False


def on_disconnect(client, userdata, rc):
    global Connected
    Connected = False
    if rc != 0:
        logger.warning(f"Unexpected disconnection (rc={rc}). Client will auto-reconnect.")
    else:
        logger.info("Disconnected cleanly")


def on_message(client, userdata, msg):
    topic = msg.topic
    logger.info(f"Received message on {topic}: {msg.payload!r}")

    writable_def = WRITABLE_DEFINITION_BY_SET_TOPIC.get(topic)
    if writable_def is None:
        logger.warning(f"Ignoring unsupported topic: {topic}")
        return

    key, register, decimals, signed = writable_def
    try:
        value = parse_set_payload(msg.payload, decimals)
    except Exception as e:
        logger.error(f"Invalid payload for {topic}: {e}")
        return

    if safe_write_register(register, value, decimals, signed):
        logger.info(f"Updated {key} via {topic} to value {value}")
        # Immediate state sync: update cached value to avoid duplicate publish next cycle
        previous_values[key] = value



def main():
    global ecl110, mqttc, Connected

    portname = find_usb_serial_port(vendor_id=USB_VENDOR_ID, product_id=USB_PRODUCT_ID)
    if portname is None:
        fallback_ports = sorted(glob.glob("/dev/ttyUSB*"))
        if fallback_ports:
            portname = fallback_ports[0]
            logger.warning(f"Adapter not found by ID, falling back to {portname}")
        else:
            logger.error("No USB serial adapter found! Exiting.")
            return

    # Set up Modbus connection
    ecl110 = minimalmodbus.Instrument(portname, SLAVE_ADDRESS)
    if ecl110.serial is None:
        logger.error("Instrument.serial is None, exiting...")
        return

    ecl110.serial.baudrate = BAUDRATE
    ecl110.serial.parity = serial.PARITY_EVEN
    logger.info("ECL110 Modbus connection configured")

    # Set up MQTT client with unique ID to avoid conflicts
    client_id = f"ecl110-{uuid.uuid4().hex[:8]}"
    try:
        mqttc = mqtt.Client(mqtt.CallbackAPIVersion.VERSION1, client_id)
    except AttributeError:
        mqttc = mqtt.Client(client_id)

    mqttc.username_pw_set("mqtt-user", "mqtt-user")
    mqttc.on_connect = on_connect
    mqttc.on_disconnect = on_disconnect
    mqttc.on_message = on_message
    mqttc.reconnect_delay_set(min_delay=1, max_delay=120)

    # Initial connection with retry
    conn = False
    count = 0
    while not conn:
        try:
            mqttc.connect(MQTT_BROKER, MQTT_PORT, keepalive=120)
            conn = True
            logger.info("Initial MQTT connection established")
        except OSError as e:
            count += 1
            logger.warning(f"Network issue ({e})... retrying ({count}/10)")
            time.sleep(2)
            if count > 10:
                logger.error("Could not connect after 10 attempts. Exiting.")
                return

    mqttc.loop_start()

    # Wait for initial connection
    timeout = 30
    elapsed = 0
    while not Connected and elapsed < timeout:
        time.sleep(0.1)
        elapsed += 0.1
    if not Connected:
        logger.error("Connection timed out. Exiting.")
        mqttc.loop_stop()
        return

    logger.info("Setup complete. Starting internal polling loop.")

    next_poll_at = time.monotonic()  # Start immediately
    try:
        while True:
            now = time.monotonic()
            if now >= next_poll_at:
                if Connected:
                    perform_measurement_and_publish(mqttc)
                else:
                    logger.warning("MQTT disconnected; skipping measurement cycle")
                next_poll_at = now + LOOP_TIME
            time.sleep(0.2)
    except KeyboardInterrupt:
        logger.info("Shutting down...")
        mqttc.disconnect()
        mqttc.loop_stop()


if __name__ == "__main__":
    main()
