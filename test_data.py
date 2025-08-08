#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Test Data Generator for TVOC Monitoring Server
Simulates an ESP32 device publishing sensor data to either Bedroom or Workingroom MQTT topic
Run two instances in separate terminals for Bedroom and Workingroom
"""

import argparse
import json
import logging
import random
import time
from datetime import datetime

import paho.mqtt.client as mqtt
from dotenv import load_dotenv

from config import MQTT_BROKER, MQTT_PORT, MQTT_TOPIC_BEDROOM, MQTT_TOPIC_WORKINGROOM

# Load .env file
load_dotenv()

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# MQTT Client
client = mqtt.Client()

def on_connect(client, userdata, flags, rc):
    """MQTT connection callback"""
    if rc == 0:
        logger.info("MQTT connection successful")
    else:
        logger.error(f"MQTT connection failed: {rc}")

def generate_sensor_data():
    """Generate random but realistic sensor data"""
    # Realistic ranges for sensor data
    tvoc = random.uniform(0, 1000)  # TVOC in ppb (0 to 1000)
    temperature = random.uniform(15, 40)  # Temperature in °C (15 to 40)
    humidity = random.uniform(20, 80)  # Humidity in % (20 to 80)
    eco2 = random.uniform(350, 1500)  # eCO2 in ppm (350 to 1500)
    aqi = random.randint(1, 5)  # AQI level (1 to 5)

    return {
        "tvoc": round(tvoc, 2),
        "temperature": round(temperature, 1),
        "humidity": round(humidity, 1),
        "eco2": round(eco2),
        "aqi": aqi
    }

def publish_data(topic):
    """Publish sensor data to the specified MQTT topic"""
    try:
        data = generate_sensor_data()
        client.publish(topic, json.dumps(data))
        logger.info(f"Published to {topic}: {data}")
    except Exception as e:
        logger.error(f"Error publishing data to {topic}: {e}")

def main():
    """Main function to connect to MQTT and publish data"""
    # Parse command-line arguments
    parser = argparse.ArgumentParser(description="Simulate ESP32 sensor data for Bedroom or Workingroom")
    parser.add_argument("room", choices=["bedroom", "workingroom"], help="Room to simulate (bedroom or workingroom)")
    args = parser.parse_args()

    # Select MQTT topic based on room
    topic = MQTT_TOPIC_BEDROOM if args.room == "bedroom" else MQTT_TOPIC_WORKINGROOM
    logger.info(f"Starting test data generator for {args.room.capitalize()}...")
    logger.info(f"MQTT Broker: {MQTT_BROKER}:{MQTT_PORT}")
    logger.info(f"Topic: {topic}")

    # Set up MQTT client
    client.on_connect = on_connect
    try:
        client.connect(MQTT_BROKER, MQTT_PORT, 60)
    except Exception as e:
        logger.error(f"Failed to connect to MQTT broker: {e}")
        return

    # Start the MQTT client loop
    client.loop_start()

    # Publish data every 5 seconds
    try:
        while True:
            publish_data(topic)
            time.sleep(5)  # Wait 5 seconds between publications
    except KeyboardInterrupt:
        logger.info(f"Stopping test data generator for {args.room.capitalize()}...")
    finally:
        client.loop_stop()
        client.disconnect()

if __name__ == "__main__":
    main()