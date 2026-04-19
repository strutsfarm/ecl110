import time
import uuid
import subprocess
import glob
import os
import minimalmodbus
import serial
import paho.mqtt.client as mqtt
import json
from datetime import datetime
import logging

# Set up logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

MQTT_BROKER = "192.168.1.222"
MQTT_PORT = 1883

LOOP_TIME = 60

# ECL110 REGISTERS

# Flow temp control 2000
SLOPE = 11174  # 0.1 - 4.0, 1 decimal
DISPLACE = 11175  # -20 - 20, 0 decimal
FLOW_TEMP_MIN = 11176  # 10 - 150C, 0 decimal
FLOW_TEMP_MAX = 11177  # 10 - 150C, 0 decimal

# Room T limit 3000
ROOM_INTEGRAION_TIM = 99999  # OFF/1-50. 3015, unknown address
ROOM_GAIN_MAX = 11181  # -9.9 - 0.0, 1 decimal
ROOM_GAIN_MIN = 11182  # 0.0 - 9.9, 1 decimal

# Return T limit 4000
LIMIT = 11029  # 10-110C, 0 decimals
RETURN_GAIN_MAX = 11034  # -9.9 - 9.9, 1 decimal
RETURN_GAIN_MIN = 11035  # -9.9 - 9.9, 1 decimal
RETURN_INTEGRATION_TIME = 11036  # OFF/1-50s, 0 decimals
PRIORITY = 11051  # OFF/ON

# Optimization 5000
AUTO_REDUCT = 11010  # -29 - 10C
BOOST = 10011  # 0-99%
RAMP = 11012  # 0-99m
OPTIMIZER = 11013  # OFF/10-59, see table
OPTIMIZER_BASED_ON = 11019  # ROOM/OUT
TOTAL_STOP = 11020  # ON/OFF
S1_T_FILTER = 99999  # 1-200. 5081, unknown address
CUT_OUT = 11178  # OFF/1-50C

# Control parameters 6000
XP = 11183  # Prop factor 1-250K
TN = 11184  # Integration time 5-999s

# 0 decimal
SET_ROOM_TEMPERATURE = 11179  # Börvärde på innetemperatur R/W
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
ACTUAL_MODE = 4210  # RO, controller mode?

# RS485 USB adapter identification
# Run this on your Pi to find your adapter's IDs:
#   udevadm info --query=property --name=/dev/ttyUSB0 | grep -E "ID_VENDOR_ID|ID_MODEL_ID|ID_SERIAL_SHORT"
USB_VENDOR_ID = "0403"    # Replace with your adapter's vendor ID
USB_PRODUCT_ID = "6001"   # Replace with your adapter's product ID
BAUDRATE = 19200
SLAVE_ADDRESS = 5

data_topic = "ecl110/ecl110_data"
command_topic = "ecl110/command"

# Watchdog: reboot after this many consecutive fully-failed measurements
# (i.e. every single register failed in N measurements in a row)
WATCHDOG_REBOOT_THRESHOLD = 3

Connected = False
ecl110 = None
mqttc = None
consecutive_total_failures = 0  # Watchdog counter


def find_usb_serial_port(vendor_id=None, product_id=None):
    """
    Find the /dev/ttyUSB* port for a specific USB-to-serial adapter
    by matching on vendor ID and/or product ID.

    To find your adapter's IDs, run:
        udevadm info --query=property --name=/dev/ttyUSB0
    """
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
                logger.info(f"Found USB serial adapter at {device} "
                            f"(vendor={props.get('ID_VENDOR_ID')}, "
                            f"model={props.get('ID_MODEL_ID')})")
                return device

        except Exception as e:
            logger.warning(f"Error querying {device}: {e}")
            continue

    return None


def watchdog_reboot(reason):
    """
    Cleanly disconnect MQTT and reboot the system.
    Requires the script to run as root, or the user to have
    passwordless sudo for /sbin/reboot. Example sudoers entry:
        pi ALL=(ALL) NOPASSWD: /sbin/reboot
    """
    logger.critical(f"WATCHDOG REBOOT: {reason}")
    logger.critical(f"Consecutive total failures: {consecutive_total_failures}")

    # Try to publish a last-will message so we know why it rebooted
    try:
        if mqttc and Connected:
            reboot_msg = json.dumps({
                "event": "watchdog_reboot",
                "reason": reason,
                "datetime": str(datetime.now()),
                "consecutive_failures": consecutive_total_failures
            })
            mqttc.publish(data_topic, reboot_msg, qos=1)
            time.sleep(1)  # Give the message time to send
            mqttc.disconnect()
            mqttc.loop_stop()
    except Exception as e:
        logger.error(f"Error during watchdog cleanup: {e}")

    # Reboot
    logger.critical("Rebooting now...")
    time.sleep(1)
    os.system("sudo /sbin/reboot")


def show_instrument_settings(instr: minimalmodbus.Instrument) -> None:
    print("Instrument settings:")
    print(repr(instr).replace(",", ",\n"))
    print(" ")


def safe_read_register(register, number_of_decimals=0, signed=False):
    """Read a Modbus register with error handling and retry."""
    for attempt in range(3):
        try:
            return ecl110.read_register(
                register,
                number_of_decimals=number_of_decimals,
                signed=signed
            )
        except Exception as e:
            logger.warning(f"Modbus read failed for register {register} "
                           f"(attempt {attempt + 1}/3): {e}")
            time.sleep(0.1)
    logger.error(f"Modbus read failed for register {register} after 3 attempts")
    return None


def on_connect(client, userdata, flags, rc):
    global Connected
    if rc == 0:
        logger.info("Connected to MQTT Broker!")
        Connected = True
        # Subscribe here so subscriptions are renewed on reconnect
        client.subscribe(command_topic, qos=1)
        logger.info(f"Subscribed to {command_topic}")
    else:
        logger.error(f"Failed to connect, return code {rc}")
        Connected = False


def on_disconnect(client, userdata, rc):
    global Connected
    Connected = False
    if rc != 0:
        logger.warning(f"Unexpected disconnection (rc={rc}). Client will auto-reconnect.")
    else:
        logger.info("Disconnected cleanly.")


def on_message(client, userdata, msg):
    logger.info(f"Received `{msg.payload.decode()}` from `{msg.topic}` topic")
    try:
        ecl110_command = json.loads(msg.payload.decode())
        command = ecl110_command.get('command', '')
        logger.info(f"Command: {command}")

        if command == 'measure':
            global consecutive_total_failures
            ecl110_data = {}
            now = datetime.now()
            ecl110_data["datetime"] = str(now)

            # Define all registers to read: (key, register, decimals, signed)
            registers = [
                ("slope", SLOPE, 1, True),
                ("displace", DISPLACE, 0, True),
                ("flow_temp_min", FLOW_TEMP_MIN, 0, True),
                ("flow_temp_max", FLOW_TEMP_MAX, 0, True),
                ("room_gain_max", ROOM_GAIN_MAX, 1, True),
                ("room_gain_min", ROOM_GAIN_MIN, 1, True),
                ("limit", LIMIT, 0, False),
                ("return_gain_max", RETURN_GAIN_MAX, 1, True),
                ("return_gain_min", RETURN_GAIN_MIN, 1, True),
                ("return_integration_time", RETURN_INTEGRATION_TIME, 0, False),
                ("auto_reduct", AUTO_REDUCT, 0, True),
                ("ramp", RAMP, 0, False),
                ("xp", XP, 0, False),
                ("tn", TN, 0, False),
                ("set_room_temperature", SET_ROOM_TEMPERATURE, 0, True),
                ("outdoor_temp", OUTDOOR_TEMP, 1, True),
                ("room_temp", ROOM_TEMP, 1, True),
                ("flow_temp", FLOW_TEMP, 1, True),
                ("return_temp", RETURN_TEMP, 1, True),
                ("room_set_temp", ROOM_SET_TEMP, 1, True),
                ("flow_set_temp", FLOW_SET_TEMP, 1, True),
                ("accumulated_outdoor_temp", ACCUMULATED_OUTDOOR_TEMP, 1, True),
                ("pump", PUMP, 0, False),
                ("valve_open", VALVE_OPEN, 0, False),
                ("valve_closed", VALVE_CLOSE, 0, False),
                ("actual_mode", ACTUAL_MODE, 0, False),
            ]

            failed_reads = 0
            for key, register, decimals, signed in registers:
                value = safe_read_register(register, decimals, signed)
                if value is not None:
                    ecl110_data[key] = value
                else:
                    failed_reads += 1

            if failed_reads > 0:
                logger.warning(f"{failed_reads}/{len(registers)} register(s) failed to read")

            # Watchdog: track consecutive total failures
            if failed_reads == len(registers):
                consecutive_total_failures += 1
                logger.error(f"ALL registers failed! "
                             f"Consecutive total failures: {consecutive_total_failures}"
                             f"/{WATCHDOG_REBOOT_THRESHOLD}")
                if consecutive_total_failures >= WATCHDOG_REBOOT_THRESHOLD:
                    watchdog_reboot(
                        f"All {len(registers)} Modbus registers failed to read "
                        f"in {WATCHDOG_REBOOT_THRESHOLD} consecutive measurements"
                    )
                    return  # Won't reach here after reboot, but just in case
            else:
                # At least one register succeeded — reset the counter
                if consecutive_total_failures > 0:
                    logger.info(f"Modbus communication restored. "
                                f"Resetting watchdog counter (was {consecutive_total_failures})")
                consecutive_total_failures = 0

            ecl110_data_json = json.dumps(ecl110_data)
            logger.info(f"Publishing: {ecl110_data_json}")
            result = client.publish(data_topic, ecl110_data_json, qos=1)
            if result.rc != mqtt.MQTT_ERR_SUCCESS:
                logger.error(f"Publish failed with rc={result.rc}")

        elif command == 'displace':
            new_value = ecl110_command.get('value')
            if new_value is not None:
                try:
                    ecl110.write_register(
                        DISPLACE,
                        number_of_decimals=0,
                        value=new_value,
                        functioncode=6,
                        signed=True
                    )
                    logger.info(f"Displace set to {new_value}")
                except Exception as e:
                    logger.error(f"Failed to write displace register: {e}")
            else:
                logger.warning("Displace command received without value")

    except json.JSONDecodeError as e:
        logger.error(f"Invalid JSON in message: {e}")
    except Exception as e:
        logger.error(f"Error processing message: {e}")


def main():
    global ecl110, mqttc, Connected

    # Auto-detect the USB serial port
    portname = find_usb_serial_port(vendor_id=USB_VENDOR_ID, product_id=USB_PRODUCT_ID)
    if portname is None:
        # Fallback: try first available ttyUSB device
        fallback_ports = sorted(glob.glob("/dev/ttyUSB*"))
        if fallback_ports:
            portname = fallback_ports[0]
            logger.warning(f"Adapter not found by ID, falling back to {portname}")
        else:
            logger.error("No USB serial adapter found! Exiting.")
            return

    # Set up connection to ECL110
    ecl110 = minimalmodbus.Instrument(portname, SLAVE_ADDRESS)
    if ecl110.serial is None:
        logger.error("Instrument.serial is None, exiting...")
        exit()
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

    # Enable automatic reconnection with backoff
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

    # Start the network loop (handles reconnection automatically)
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

    logger.info("Setup complete. Waiting for commands...")

    try:
        while True:
            time.sleep(10)
            if not Connected:
                logger.warning("Connection lost. Waiting for auto-reconnect...")
    except KeyboardInterrupt:
        logger.info("Shutting down...")
        mqttc.disconnect()
        mqttc.loop_stop()


if __name__ == "__main__":
    main()
