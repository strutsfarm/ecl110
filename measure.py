import time
import uuid
import paho.mqtt.client as mqtt
import json
import logging

# Set up logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

MQTT_BROKER = "192.168.1.222"
MQTT_PORT = 1883

command_topic = "ecl110/command"


def main():
    # Unique client ID to avoid broker conflicts
    client_id = f"measure-ecl110-{uuid.uuid4().hex[:8]}"
    try:
        mqttc = mqtt.Client(mqtt.CallbackAPIVersion.VERSION1, client_id)
    except AttributeError:
        mqttc = mqtt.Client(client_id)

    mqttc.username_pw_set("mqtt-user", "mqtt-user")

    try:
        mqttc.connect(MQTT_BROKER, MQTT_PORT)
    except OSError as e:
        logger.error(f"Could not connect to broker: {e}")
        return

    # Start the network loop so the publish actually gets sent
    mqttc.loop_start()

    ecl110_command = {"command": "measure"}
    ecl110_command_json = json.dumps(ecl110_command)
    logger.info(f"Publishing: {ecl110_command_json}")

    # Publish with QoS 1 and wait for delivery
    result = mqttc.publish(command_topic, ecl110_command_json, qos=1)
    result.wait_for_publish(timeout=5)

    if result.rc == mqtt.MQTT_ERR_SUCCESS:
        logger.info("Command published successfully")
    else:
        logger.error(f"Publish failed with rc={result.rc}")

    # Clean disconnect
    mqttc.disconnect()
    mqttc.loop_stop()


if __name__ == "__main__":
    main()
