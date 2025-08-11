#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
TVOC Monitoring Server for Raspberry Pi 5
Handles MQTT, Database, Telegram alerts, and Web Dashboard for Bedroom and Workingroom
"""

import json
import logging
import os
import threading
import time
from datetime import datetime, timedelta

import mariadb
import paho.mqtt.client as mqtt
import requests
from dotenv import load_dotenv
from flask import Flask, jsonify, render_template_string, request
from flask_socketio import SocketIO, emit

from config import (
    DB_HOST,
    DB_NAME,
    DB_PASSWORD,
    DB_PORT,
    DB_USER,
    FLASK_SECRET_KEY,
    MQTT_BROKER,
    MQTT_PORT,
    MQTT_TOPIC_BEDROOM,
    MQTT_TOPIC_WORKINGROOM,
    TELEGRAM_BOT_TOKEN,
    TELEGRAM_CHAT_ID,
)

# Load .env file
load_dotenv()

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Initialize Flask app
app = Flask(__name__)
app.config["SECRET_KEY"] = FLASK_SECRET_KEY
socketio = SocketIO(app, cors_allowed_origins="*")

# Database configuration
DB_CONFIG = {
    "host": DB_HOST,
    "port": DB_PORT,
    "user": DB_USER,
    "password": DB_PASSWORD,
    "database": DB_NAME,
}

# Default thresholds for TVOC and eCO2
DEFAULT_THRESHOLDS = {
    "tvoc_good": 65,  # ppb
    "tvoc_normal": 220,  # ppb
    "tvoc_high": 660,  # ppb
    "temp_min": 18,
    "temp_max": 35,
    "humidity_min": 30,
    "humidity_max": 70,
    "eco2_min": 400,  # ppm (low threshold)
    "eco2_normal": 1000,  # ppm (normal threshold)
    "eco2_high": 2000,  # ppm (high threshold)
}

# Global variables for data storage
current_data_bedroom = {
    "tvoc": 0,
    "temperature": 0,
    "humidity": 0,
    "eco2": 0,
    "aqi": 1,
    "timestamp": datetime.now(),
}

current_data_workingroom = {
    "tvoc": 0,
    "temperature": 0,
    "humidity": 0,
    "eco2": 0,
    "aqi": 1,
    "timestamp": datetime.now(),
}

thresholds_bedroom = DEFAULT_THRESHOLDS.copy()
thresholds_workingroom = DEFAULT_THRESHOLDS.copy()
last_alert_time_bedroom = {}
last_alert_time_workingroom = {}


class DatabaseManager:
    """Database manager class for handling MariaDB operations"""

    def __init__(self):
        self.connection = None
        self.connect()
        self.create_tables()

    def connect(self):
        """Establish database connection"""
        try:
            self.connection = mariadb.connect(**DB_CONFIG)
            logger.info("Database connection successful")
        except Exception as e:
            logger.error(f"Database connection error: {e}")

    def create_tables(self):
        """Create necessary database tables"""
        try:
            cursor = self.connection.cursor()

            # Sensor data table for Bedroom
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS sensor_data (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    tvoc FLOAT NOT NULL,
                    temperature FLOAT NOT NULL,
                    humidity FLOAT NOT NULL,
                    eco2 FLOAT NOT NULL,
                    aqi INT NOT NULL,
                    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
                )
            """
            )

            # Sensor data table for Workingroom
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS sensor_data1 (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    tvoc FLOAT NOT NULL,
                    temperature FLOAT NOT NULL,
                    humidity FLOAT NOT NULL,
                    eco2 FLOAT NOT NULL,
                    aqi INT NOT NULL,
                    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
                )
            """
            )

            # Thresholds table
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS thresholds (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    room VARCHAR(20) NOT NULL,
                    tvoc_max FLOAT DEFAULT 660,
                    temp_min FLOAT DEFAULT 18,
                    temp_max FLOAT DEFAULT 35,
                    humidity_min FLOAT DEFAULT 30,
                    humidity_max FLOAT DEFAULT 70,
                    eco2_min FLOAT DEFAULT 400,
                    eco2_max FLOAT DEFAULT 1000,
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
                )
            """
            )

            # Alert history table
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS alert_history (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    room VARCHAR(20) NOT NULL,
                    alert_type VARCHAR(50) NOT NULL,
                    message TEXT NOT NULL,
                    value FLOAT NOT NULL,
                    threshold_value FLOAT NOT NULL,
                    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
                )
            """
            )

            self.connection.commit()
            logger.info("Database tables created successfully")

        except Exception as e:
            logger.error(f"Error creating tables: {e}")

    def insert_sensor_data(self, room, tvoc, temperature, humidity, eco2, aqi):
        """Insert sensor data into database"""
        try:
            cursor = self.connection.cursor()
            table = "sensor_data" if room == "bedroom" else "sensor_data1"
            cursor.execute(
                f"INSERT INTO {table} (tvoc, temperature, humidity, eco2, aqi) VALUES (?, ?, ?, ?, ?)",
                (tvoc, temperature, humidity, eco2, aqi),
            )
            self.connection.commit()
        except Exception as e:
            logger.error(f"Error saving data for {room}: {e}")
            self.connect()  # Try to reconnect

    def get_recent_data(self, room, hours=24):
        """Get recent sensor data from database"""
        try:
            cursor = self.connection.cursor()
            table = "sensor_data" if room == "bedroom" else "sensor_data1"
            cursor.execute(
                f"""
                SELECT tvoc, temperature, humidity, eco2, aqi, timestamp 
                FROM {table} 
                WHERE timestamp >= NOW() - INTERVAL ? HOUR 
                ORDER BY timestamp ASC
            """,
                (hours,),
            )

            data = []
            for row in cursor.fetchall():
                data.append(
                    {
                        "tvoc": row[0],
                        "temperature": row[1],
                        "humidity": row[2],
                        "eco2": row[3],
                        "aqi": row[4],
                        "timestamp": row[5].strftime("%Y-%m-%d %H:%M:%S"),
                    }
                )
            return data
        except Exception as e:
            logger.error(f"Error reading data for {room}: {e}")
            return []

    def update_thresholds(self, room, new_thresholds):
        """Update threshold values in database"""
        try:
            cursor = self.connection.cursor()
            # Check if record exists for the room
            cursor.execute("SELECT id FROM thresholds WHERE room = ? LIMIT 1", (room,))
            row = cursor.fetchone()

            if row:
                # Update existing record
                cursor.execute(
                    """
                    UPDATE thresholds 
                    SET tvoc_max=?, temp_min=?, temp_max=?, 
                        humidity_min=?, humidity_max=?, 
                        eco2_min=?, eco2_max=?,
                        updated_at=CURRENT_TIMESTAMP 
                    WHERE id=? AND room=?
                """,
                    (
                        new_thresholds["tvoc_max"],
                        new_thresholds["temp_min"],
                        new_thresholds["temp_max"],
                        new_thresholds["humidity_min"],
                        new_thresholds["humidity_max"],
                        new_thresholds["eco2_min"],
                        new_thresholds["eco2_max"],
                        row[0],
                        room,
                    ),
                )
            else:
                # Insert new record
                cursor.execute(
                    """
                    INSERT INTO thresholds (
                        room, tvoc_max, temp_min, temp_max, 
                        humidity_min, humidity_max,
                        eco2_min, eco2_max
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                    (
                        room,
                        new_thresholds["tvoc_max"],
                        new_thresholds["temp_min"],
                        new_thresholds["temp_max"],
                        new_thresholds["humidity_min"],
                        new_thresholds["humidity_max"],
                        new_thresholds["eco2_min"],
                        new_thresholds["eco2_max"],
                    ),
                )

            self.connection.commit()
            logger.info(f"Thresholds updated successfully for {room}")
        except Exception as e:
            logger.error(f"Error updating thresholds for {room}: {e}")

    def log_alert(self, room, alert_type, message, value, threshold_value):
        """Log alert to database"""
        try:
            cursor = self.connection.cursor()
            cursor.execute(
                """
                INSERT INTO alert_history (room, alert_type, message, value, threshold_value)
                VALUES (?, ?, ?, ?, ?)
            """,
                (room, alert_type, message, value, threshold_value),
            )
            self.connection.commit()
        except Exception as e:
            logger.error(f"Error logging alert for {room}: {e}")


# Initialize Database Manager
db = DatabaseManager()


def send_telegram_alert(room, message):
    """Send alert via Telegram"""
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        data = {
            "chat_id": TELEGRAM_CHAT_ID,
            "text": f"🚨 ALERT from {room.capitalize()} 🚨\n\n{message}",
            "parse_mode": "HTML",
        }
        response = requests.post(url, data=data, timeout=10)
        if response.status_code == 200:
            logger.info(f"Telegram message sent successfully for {room}")
        else:
            logger.error(f"Telegram error for {room}: {response.status_code}")
    except Exception as e:
        logger.error(f"Telegram error for {room}: {e}")


def check_thresholds_and_alert(room, tvoc, temperature, humidity, eco2):
    """Check thresholds and send alerts for TVOC, temperature, humidity and eCO2"""
    thresholds = thresholds_bedroom if room == "bedroom" else thresholds_workingroom
    last_alert_time = (
        last_alert_time_bedroom if room == "bedroom" else last_alert_time_workingroom
    )
    current_time = datetime.now()
    alerts = []
    alert_messages = []
    should_send_alert = False

    # Check TVOC with new thresholds
    if tvoc > thresholds["tvoc_high"]:  # Too high
        alert_key = "tvoc_too_high"
        alert_severity = "danger"
        alert_message = f"⚠️ TVOC too high: {tvoc:.2f} ppb (Critical level > {thresholds['tvoc_high']} ppb)"
    elif tvoc > thresholds["tvoc_normal"]:  # High
        alert_key = "tvoc_high"
        alert_severity = "warning"
        alert_message = f"⚡ TVOC high: {tvoc:.2f} ppb"
    elif tvoc > thresholds["tvoc_good"]:  # Normal
        alert_key = "tvoc_normal"
        alert_severity = "info"
        alert_message = None
    else:  # Good
        alert_key = "tvoc_good"
        alert_severity = "success"
        alert_message = None

    if alert_message and (
        alert_key not in last_alert_time
        or (current_time - last_alert_time[alert_key]).seconds > 300
    ):  # 5 minutes
        should_send_alert = True
        last_alert_time[alert_key] = current_time
        alert_messages.append(alert_message)
        alerts.append(
            {
                "type": alert_key,
                "message": f"TVOC Level: {tvoc:.2f} ppb",
                "severity": alert_severity,
                "level": (
                    "too_high"
                    if tvoc > thresholds["tvoc_high"]
                    else (
                        "high"
                        if tvoc > thresholds["tvoc_normal"]
                        else "normal" if tvoc > thresholds["tvoc_good"] else "good"
                    )
                ),
            }
        )

    # Check temperature with new thresholds
    if temperature > thresholds["temp_max"]:  # High
        temp_status = "high"
        alert_key = "temp_high"
        alert_severity = "warning"
        alert_message = f"🥵 Temperature high: {temperature}°C (Maximum: {thresholds['temp_max']}°C)"
    elif temperature < thresholds["temp_min"]:  # Low
        temp_status = "low"
        alert_key = "temp_low"
        alert_severity = "warning"
        alert_message = (
            f"🥶 Temperature low: {temperature}°C (Minimum: {thresholds['temp_min']}°C)"
        )
    else:  # Normal
        temp_status = "normal"
        alert_key = "temp_normal"
        alert_severity = "success"
        alert_message = None
<<<<<<< HEAD

    if alert_message and (
        alert_key not in last_alert_time
        or (current_time - last_alert_time[alert_key]).seconds > 600
    ):  # 10 minutes
        should_send_alert = True
        last_alert_time[alert_key] = current_time
        alert_messages.append(alert_message)
        alerts.append(
            {
                "type": alert_key,
                "message": f"Temperature: {temperature}°C",
                "severity": alert_severity,
                "level": temp_status,
            }
        )

    # Check humidity with new thresholds
    if humidity > thresholds["humidity_max"]:  # High
        humidity_status = "high"
        alert_key = "humidity_high"
        alert_severity = "warning"
        alert_message = (
            f"💧 Humidity high: {humidity}% (Maximum: {thresholds['humidity_max']}%)"
        )
    elif humidity < thresholds["humidity_min"]:  # Low
        humidity_status = "low"
        alert_key = "humidity_low"
        alert_severity = "warning"
        alert_message = (
            f"🏜️ Humidity low: {humidity}% (Minimum: {thresholds['humidity_min']}%)"
        )
    else:  # Normal
        humidity_status = "normal"
        alert_key = "humidity_normal"
        alert_severity = "success"
        alert_message = None

    if alert_message and (
        alert_key not in last_alert_time
        or (current_time - last_alert_time[alert_key]).seconds > 600
    ):  # 10 minutes
        should_send_alert = True
        last_alert_time[alert_key] = current_time
        alert_messages.append(alert_message)
        alerts.append(
            {
                "type": alert_key,
                "message": f"Humidity: {humidity}%",
                "severity": alert_severity,
                "level": humidity_status,
            }
        )
        alert_messages.append(
            f"🏜️ Humidity too low: {humidity}% "
            f"(Minimum: {thresholds['humidity_min']}%)"
        )
    else:
        alert_messages.append(
            f"💧 Humidity too high: {humidity}% "
            f"(Maximum: {thresholds['humidity_max']}%)"
        )
    alerts.append(
        {
            "type": "humidity_abnormal",
            "message": f"Abnormal humidity: {humidity}%",
            "severity": "warning",
        }
    )

    # Check eCO2
    if eco2 < thresholds["eco2_min"] or eco2 > thresholds["eco2_max"]:
        alert_key = "eco2_abnormal"
        if (
            alert_key not in last_alert_time
            or (current_time - last_alert_time[alert_key]).seconds > 600
        ):  # 10 minutes
            should_send_alert = True
            last_alert_time[alert_key] = current_time
            if eco2 < thresholds["eco2_min"]:
                alert_messages.append(
                    f"🌿 CO2 level too low: {eco2} ppm "
                    f"(Minimum: {thresholds['eco2_min']} ppm)"
                )
            else:
                alert_messages.append(
                    f"⚠️ CO2 level too high: {eco2} ppm "
                    f"(Maximum: {thresholds['eco2_max']} ppm)"
                )
            alerts.append(
                {
                    "type": "eco2_abnormal",
                    "message": f"Abnormal CO2 level: {eco2} ppm",
                    "severity": (
                        "warning" if eco2 < thresholds["eco2_min"] else "danger"
                    ),
                }
            )
=======

    if alert_message and (
        alert_key not in last_alert_time
        or (current_time - last_alert_time[alert_key]).seconds > 600
    ):  # 10 minutes
        should_send_alert = True
        last_alert_time[alert_key] = current_time
        alert_messages.append(alert_message)
        alerts.append(
            {
                "type": alert_key,
                "message": f"Temperature: {temperature}°C",
                "severity": alert_severity,
                "level": temp_status,
            }
        )

    # Check humidity with new thresholds
    if humidity > thresholds["humidity_max"]:  # High
        humidity_status = "high"
        alert_key = "humidity_high"
        alert_severity = "warning"
        alert_message = (
            f"💧 Humidity high: {humidity}% (Maximum: {thresholds['humidity_max']}%)"
        )
    elif humidity < thresholds["humidity_min"]:  # Low
        humidity_status = "low"
        alert_key = "humidity_low"
        alert_severity = "warning"
        alert_message = (
            f"🏜️ Humidity low: {humidity}% (Minimum: {thresholds['humidity_min']}%)"
        )
    else:  # Normal
        humidity_status = "normal"
        alert_key = "humidity_normal"
        alert_severity = "success"
        alert_message = None

    if alert_message and (
        alert_key not in last_alert_time
        or (current_time - last_alert_time[alert_key]).seconds > 600
    ):  # 10 minutes
        should_send_alert = True
        last_alert_time[alert_key] = current_time
        alert_messages.append(alert_message)
        alerts.append(
            {
                "type": alert_key,
                "message": f"Humidity: {humidity}%",
                "severity": alert_severity,
                "level": humidity_status,
            }
        )
        alert_messages.append(
            f"🏜️ Humidity too low: {humidity}% "
            f"(Minimum: {thresholds['humidity_min']}%)"
        )
    else:
        alert_messages.append(
            f"💧 Humidity too high: {humidity}% "
            f"(Maximum: {thresholds['humidity_max']}%)"
        )
    alerts.append(
        {
            "type": "humidity_abnormal",
            "message": f"Abnormal humidity: {humidity}%",
            "severity": "warning",
        }
    )

    # Check eCO2 with new thresholds
    if eco2 > thresholds["eco2_high"]:  # Too high (> 2000 ppm)
        eco2_status = "too_high"
        alert_key = "eco2_too_high"
        alert_severity = "danger"
        alert_message = f"🚨 CO2 level too high: {eco2} ppm (Critical level > {thresholds['eco2_high']} ppm)"
    elif eco2 > thresholds["eco2_normal"]:  # High (1000-2000 ppm)
        eco2_status = "high"
        alert_key = "eco2_high"
        alert_severity = "warning"
        alert_message = f"⚠️ CO2 level high: {eco2} ppm"
    elif eco2 >= thresholds["eco2_min"]:  # Normal (400-1000 ppm)
        eco2_status = "normal"
        alert_key = "eco2_normal"
        alert_severity = "success"
        alert_message = None
    else:  # Low (< 400 ppm)
        eco2_status = "low"
        alert_key = "eco2_low"
        alert_severity = "warning"
        alert_message = (
            f"🌿 CO2 level too low: {eco2} ppm (Minimum: {thresholds['eco2_min']} ppm)"
        )

    if alert_message and (
        alert_key not in last_alert_time
        or (current_time - last_alert_time[alert_key]).seconds > 600
    ):  # 10 minutes
        should_send_alert = True
        last_alert_time[alert_key] = current_time
        alert_messages.append(alert_message)
        alerts.append(
            {
                "type": alert_key,
                "message": f"CO2 Level: {eco2} ppm",
                "severity": alert_severity,
                "level": eco2_status,
            }
        )
>>>>>>> 2b20299db8633deb2585ff935350567929384196

    # Get AQI description
    aqi_descriptions = {
        1: "Excellent",
        2: "Good",
        3: "Morderate",
        4: "Poor",
        5: "Unhealthy",
    }
    aqi = (current_data_bedroom if room == "bedroom" else current_data_workingroom).get(
        "aqi", 1
    )
    aqi_text = aqi_descriptions.get(aqi, "Undefined")
    alert_messages.append(f"🌟 AQI Level: {aqi_text}")

    # Send combined alert if there are any messages
    if should_send_alert:
        combined_message = "\n".join(alert_messages)
        recommendations = "\n💨 Recommendation: Open windows or increase ventilation!"
        send_telegram_alert(room, combined_message + recommendations)

        # Log alerts to database
        for message in alert_messages[:-1]:  # Exclude AQI message from logging
            db.log_alert(room, "environmental_alert", message, 0, 0)

    return alerts


# MQTT Client functions
def on_connect(client, userdata, flags, rc):
    """MQTT connection callback"""
    if rc == 0:
        logger.info("MQTT connection successful")
        client.subscribe([(MQTT_TOPIC_BEDROOM, 0), (MQTT_TOPIC_WORKINGROOM, 0)])
    else:
        logger.error(f"MQTT connection error: {rc}")


def on_message(client, userdata, msg):
    """MQTT message callback"""
    try:
        data = json.loads(msg.payload.decode())
        room = "bedroom" if msg.topic == MQTT_TOPIC_BEDROOM else "workingroom"
        current_data = (
            current_data_bedroom if room == "bedroom" else current_data_workingroom
        )

        tvoc = float(data.get("tvoc", data.get("TVOC", 0)))
        temperature = float(data.get("temperature", data.get("Temperature", 0)))
        humidity = float(data.get("humidity", data.get("Humidity", 0)))
        eco2 = float(data.get("eco2", data.get("eCO2", 0)))
        aqi = int(data.get("aqi", data.get("AQI", 1)))

        # Update current data
        current_data.update(
            {
                "tvoc": tvoc,
                "temperature": temperature,
                "humidity": humidity,
                "eco2": eco2,
                "aqi": aqi,
                "timestamp": datetime.now(),
            }
        )

        # Save to database
        db.insert_sensor_data(room, tvoc, temperature, humidity, eco2, aqi)

        # Check thresholds and alert
        alerts = check_thresholds_and_alert(room, tvoc, temperature, humidity, eco2)

        # Send real-time data via WebSocket
        socketio.emit(
            f"sensor_data_{room}",
            {
                "tvoc": tvoc,
                "temperature": temperature,
                "humidity": humidity,
                "eco2": eco2,
                "aqi": aqi,
                "timestamp": current_data["timestamp"].strftime("%H:%M:%S"),
                "alerts": alerts,
            },
            namespace=f"/{room}",
        )

        logger.info(
            f"Received data for {room}: TVOC={tvoc:.2f}ppb, T={temperature}°C, "
            f"H={humidity}%, eCO2={eco2}ppm, AQI={aqi}"
        )

    except Exception as e:
        logger.error(f"MQTT processing error: {e}")


# Initialize MQTT Client
mqtt_client = mqtt.Client()
mqtt_client.on_connect = on_connect
mqtt_client.on_message = on_message


def start_mqtt():
    """Start MQTT client"""
    try:
        mqtt_client.connect(MQTT_BROKER, MQTT_PORT, 60)
        mqtt_client.loop_forever()
    except Exception as e:
        logger.error(f"MQTT error: {e}")


# HTML Templates
MAIN_PAGE_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>🌱 Air Quality Monitoring - Select Room</title>
    <style>
        * {
            margin: 0;
            padding: 0;
            box-sizing: border-box;
        }
        body {
            font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            min-height: 100vh;
            display: flex;
            justify-content: center;
            align-items: center;
            color: #333;
        }
        .container {
            max-width: 800px;
            padding: 20px;
            text-align: center;
        }
        .header {
            margin-bottom: 40px;
            color: white;
        }
        .header h1 {
            font-size: 2.5rem;
            margin-bottom: 10px;
            text-shadow: 2px 2px 4px rgba(0, 0, 0, 0.3);
        }
        .header p {
            font-size: 1.1rem;
            opacity: 0.9;
        }
        .grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(300px, 1fr));
            gap: 20px;
        }
        .card {
            background: rgba(255, 255, 255, 0.95);
            backdrop-filter: blur(10px);
            border-radius: 20px;
            padding: 30px;
            box-shadow: 0 8px 32px rgba(0, 0, 0, 0.1);
            border: 1px solid rgba(255, 255, 255, 0.2);
            transition: transform 0.3s ease, box-shadow 0.3s ease;
            cursor: pointer;
            text-align: center;
        }
        .card:hover {
            transform: translateY(-5px);
            box-shadow: 0 12px 40px rgba(0, 0, 0, 0.15);
        }
        .card h2 {
            font-size: 1.8rem;
            color: #444;
            margin-bottom: 10px;
        }
        .card p {
            color: #666;
            font-size: 1rem;
        }
        .card.bedroom {
            border-left: 5px solid #ff6b6b;
        }
        .card.workingroom {
            border-left: 5px solid #4ecdc4;
        }
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h1>🌱 Air Quality Monitoring</h1>
            <p>Select a room to monitor indoor air quality</p>
        </div>
        <div class="grid">
            <div class="card bedroom" onclick="window.location.href='/bedroom'">
                <h2>Bedroom</h2>
                <p>Monitor air quality in the bedroom</p>
            </div>
            <div class="card workingroom" onclick="window.location.href='/workingroom'">
                <h2>Working Room</h2>
                <p>Monitor air quality in the working room</p>
            </div>
        </div>
    </div>
</body>
</html>
"""

DASHBOARD_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1.0" />
    <title>🌱 {{ room_name }} Air Quality Dashboard</title>
    <script src="https://cdnjs.cloudflare.com/ajax/libs/socket.io/4.0.0/socket.io.js"></script>
    <script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/3.9.1/chart.min.js"></script>
    <style>
        /* AQI Status Styles */
        .aqi-excellent {
            background: linear-gradient(45deg, #4CAF50, #45a049) !important;
            color: white !important;
            transform: scale(1.05);
            transition: transform 0.3s ease;
        }
        .aqi-good {
            background: linear-gradient(45deg, #8BC34A, #7CB342) !important;
            color: white !important;
            transform: scale(1.03);
            transition: transform 0.3s ease;
        }
        .aqi-morderate {
            background: linear-gradient(45deg, #FFC107, #FFB300) !important;
            color: white !important;
            transform: scale(1);
            transition: transform 0.3s ease;
        }
        .aqi-poor {
            background: linear-gradient(45deg, #FF9800, #F57C00) !important;
            color: white !important;
            animation: pulse 2s infinite;
        }
        .aqi-unhealthy {
            background: linear-gradient(45deg, #f44336, #d32f2f) !important;
            color: white !important;
            animation: pulse 2s infinite;
        }
        
        /* Base Styles */
        * {
            margin: 0;
            padding: 0;
            box-sizing: border-box;
        }
        body {
            font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            min-height: 100vh;
            color: #333;
        }
        .container {
            max-width: 1400px;
            margin: 0 auto;
            padding: 20px;
        }
        .header {
            text-align: center;
            margin-bottom: 30px;
            color: white;
            position: relative;
        }
        .header h1 {
            font-size: 2.5rem;
            margin-bottom: 10px;
            text-shadow: 2px 2px 4px rgba(0, 0, 0, 0.3);
        }
        .header p {
            font-size: 1.1rem;
            opacity: 0.9;
        }
        .back-btn {
            position: absolute;
            left: 20px;
            top: 20px;
            background: linear-gradient(45deg, #2196f3, #1976d2);
            color: white;
            padding: 10px 20px;
            border: none;
            border-radius: 25px;
            font-size: 1rem;
            font-weight: 600;
            cursor: pointer;
            transition: all 0.3s ease;
        }
        .back-btn:hover {
            transform: translateY(-2px);
            box-shadow: 0 5px 15px rgba(102, 126, 234, 0.4);
        }
        .status-bar {
            background: rgba(255, 255, 255, 0.1);
            backdrop-filter: blur(10px);
            border-radius: 15px;
            padding: 15px;
            margin-bottom: 25px;
            border: 1px solid rgba(255, 255, 255, 0.2);
            color: white;
            text-align: center;
        }
        .grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(300px, 1fr));
            gap: 20px;
            margin-bottom: 30px;
            max-width: 1400px;
            margin-left: auto;
            margin-right: auto;
        }
        @media (max-width: 1200px) {
            #thresholdForm {
                grid-template-columns: repeat(2, 1fr) !important;
            }
        }
        @media (max-width: 768px) {
            #thresholdForm {
                grid-template-columns: 1fr !important;
            }
        }
        .sensors-grid {
            display: grid;
            grid-template-columns: repeat(4, 1fr);
            gap: 20px;
            margin-bottom: 30px;
            max-width: 1400px;
            margin-left: auto;
            margin-right: auto;
        }
        @media (max-width: 1200px) {
            .sensors-grid {
                grid-template-columns: repeat(2, 1fr);
            }
        }
        @media (max-width: 768px) {
            .sensors-grid {
                grid-template-columns: 1fr;
            }
        }
        .card {
            background: rgba(255, 255, 255, 0.95);
            backdrop-filter: blur(10px);
            border-radius: 20px;
            padding: 25px;
            box-shadow: 0 8px 32px rgba(0, 0, 0, 0.1);
            border: 1px solid rgba(255, 255, 255, 0.2);
            transition: transform 0.3s ease, box-shadow 0.3s ease;
        }
        .card:hover {
            transform: translateY(-5px);
            box-shadow: 0 12px 40px rgba(0, 0, 0, 0.15);
        }
        .card h3 {
            color: #444;
            margin-bottom: 15px;
            font-size: 1.3rem;
            display: flex;
            align-items: center;
            gap: 10px;
        }
        .sensor-card {
            text-align: center;
        }
        .sensor-value {
            font-size: 3rem;
            font-weight: bold;
            margin: 15px 0;
            text-shadow: 1px 1px 2px rgba(0, 0, 0, 0.1);
        }
        .sensor-unit {
            font-size: 1.2rem;
            color: #666;
            margin-left: 5px;
        }
        .sensor-status {
            padding: 8px 16px;
            border-radius: 25px;
            font-weight: bold;
            text-transform: uppercase;
            font-size: 0.9rem;
            display: inline-block;
            margin-top: 10px;
        }
        .status-normal {
            background: linear-gradient(45deg, #4caf50, #45a049);
            color: white;
        }
        .status-warning {
            background: linear-gradient(45deg, #ff9800, #f57c00);
            color: white;
        }
        .status-danger {
            background: linear-gradient(45deg, #f44336, #d32f2f);
            color: white;
            animation: pulse 2s infinite;
        }
        .status-excellent {
            background: linear-gradient(45deg, #4CAF50, #45a049);
            color: white;
        }
        .status-good {
            background: linear-gradient(45deg, #8BC34A, #7CB342);
            color: white;
        }
        .status-moderate {
            background: linear-gradient(45deg, #FFC107, #FFB300);
            color: white;
        }
        .status-poor {
            background: linear-gradient(45deg, #FF9800, #F57C00);
            color: white;
            animation: pulse 2s infinite;
        }
        .status-unhealthy {
            background: linear-gradient(45deg, #f44336, #d32f2f);
            color: white;
            animation: pulse 2s infinite;
        }
        @keyframes pulse {
            0% { transform: scale(1); }
            50% { transform: scale(1.05); }
            100% { transform: scale(1); }
        }
        .tvoc-card { border-left: 5px solid #ff6b6b; }
        .temp-card { border-left: 5px solid #4ecdc4; }
        .humidity-card { border-left: 5px solid #45b7d1; }
        .chart-container {
            position: relative;
            height: 400px;
            margin-top: 20px;
        }
        .controls-card {
            grid-column: 1 / -1;
        }
        .form-group {
            margin-bottom: 20px;
        }
        .form-row {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
            gap: 15px;
            margin-bottom: 15px;
        }
        .form-control {
            display: flex;
            flex-direction: column;
        }
        .form-control label {
            font-weight: 600;
            margin-bottom: 5px;
            color: #555;
        }
        .form-control input {
            padding: 12px 15px;
            border: 2px solid #e1e5e9;
            border-radius: 10px;
            font-size: 1rem;
            transition: border-color 0.3s ease, box-shadow 0.3s ease;
            background: rgba(255, 255, 255, 0.8);
        }
        .form-control input:focus {
            outline: none;
            border-color: #667eea;
            box-shadow: 0 0 0 3px rgba(102, 126, 234, 0.1);
        }
        .btn {
            background: linear-gradient(45deg, #667eea, #764ba2);
            color: white;
            padding: 12px 30px;
            border: none;
            border-radius: 25px;
            font-size: 1rem;
            font-weight: 600;
            cursor: pointer;
            transition: all 0.3s ease;
            text-transform: uppercase;
            letter-spacing: 0.5px;
        }
        .btn:hover {
            transform: translateY(-2px);
            box-shadow: 0 5px 15px rgba(102, 126, 234, 0.4);
        }
        .btn:active {
            transform: translateY(0);
        }
        .alerts {
            margin-top: 20px;
        }
        .alert {
            padding: 15px 20px;
            border-radius: 10px;
            margin-bottom: 10px;
            font-weight: 500;
            display: flex;
            align-items: center;
            gap: 10px;
            animation: slideIn 0.5s ease;
        }
        @keyframes slideIn {
            from {
                opacity: 0;
                transform: translateX(-20px);
            }
            to {
                opacity: 1;
                transform: translateX(0);
            }
        }
        .alert-danger {
            background: linear-gradient(45deg, #ff6b6b, #ee5a52);
            color: white;
        }
        .alert-warning {
            background: linear-gradient(45deg, #ffa726, #fb8c00);
            color: white;
        }
        .last-update {
            text-align: center;
            color: rgba(255, 255, 255, 0.8);
            font-size: 0.9rem;
            margin-top: 10px;
        }
        .icon {
            font-size: 1.5rem;
        }
        .connection-status {
            position: fixed;
            top: 20px;
            right: 20px;
            padding: 10px 15px;
            border-radius: 25px;
            font-size: 0.9rem;
            font-weight: 600;
            z-index: 1000;
        }
        .connected {
            background: #4caf50;
            color: white;
        }
        .disconnected {
            background: #f44336;
            color: white;
            animation: pulse 1s infinite;
        }
        .success-message {
            background: #4caf50;
            color: white;
            padding: 10px 20px;
            border-radius: 10px;
            margin: 10px 0;
            display: none;
        }
    </style>
</head>
<body>
    <div class="connection-status" id="connectionStatus">
        🔴 Connecting...
    </div>
    <div class="container">
        <div class="header">
            <button class="back-btn" onclick="window.location.href='/'">⬅ Back</button>
            <h1>🌱 {{ room_name }} Air Quality Dashboard</h1>
            <p>Monitor indoor air quality in the {{ room_name | lower }}</p>
        </div>
        <div class="status-bar" id="statusBar">
            <div id="lastUpdate">Loading data...</div>
        </div>
        <div class="sensors-grid">
            <!-- TVOC Card -->
            <div class="card sensor-card tvoc-card">
                <h3><span class="icon">💨</span> TVOC Level</h3>
                <div class="sensor-value" id="tvocValue">
                    0<span class="sensor-unit">ppb</span>
                </div>
                <div class="sensor-status status-normal" id="tvocStatus">
                    Normal
                </div>
            </div>
            <!-- Temperature Card -->
            <div class="card sensor-card temp-card">
                <h3><span class="icon">🌡️</span> Temperature</h3>
                <div class="sensor-value" id="tempValue">
                    0<span class="sensor-unit">°C</span>
                </div>
                <div class="sensor-status status-normal" id="tempStatus">
                    Normal
                </div>
            </div>
            <!-- Humidity Card -->
            <div class="card sensor-card humidity-card">
                <h3><span class="icon">💧</span> Humidity</h3>
                <div class="sensor-value" id="humidityValue">
                    0<span class="sensor-unit">%</span>
                </div>
                <div class="sensor-status status-normal" id="humidityStatus">
                    Normal
                </div>
            </div>
            <!-- eCO2 Card -->
            <div class="card sensor-card eco2-card" style="border-left: 5px solid #9CCC65;">
                <h3><span class="icon">🌿</span> eCO2 Level</h3>
                <div class="sensor-value" id="eco2Value">
                    0<span class="sensor-unit">ppm</span>
                </div>
                <div class="sensor-status status-normal" id="eco2Status">
                    Normal
                </div>
            </div>
            <!-- AQI Card -->
            <div class="card sensor-card aqi-card" style="grid-column: 1 / -1; margin-top: 20px;">
                <h3><span class="icon">👁️</span> AQI Index</h3>
                <div class="sensor-status" id="aqiStatus" style="font-size: 1.5rem; padding: 15px 30px; margin: 15px 0;">
                    Excellent
                </div>
            </div>
        </div>
        <div class="grid">
            <!-- Chart Card -->
            <div class="card" style="grid-column: 1 / -1">
                <h3><span class="icon">📊</span> Timeline chart</h3>
                <div class="chart-container">
                    <canvas id="sensorChart"></canvas>
                </div>
            </div>
            <!-- Controls Card -->
            <form id="thresholdForm" style="
                width: 100%;
                grid-column: 1 / -1;
                display: grid;
                grid-template-columns: repeat(4, 1fr);
                gap: 20px;
            ">
                <div class="card tvoc-card">
                    <h3><span class="icon">💨</span> TVOC Threshold Settings</h3>
                    <div class="form-group">
                        <div class="form-control">
                            <label for="tvocMax">Maximum TVOC (ppb)</label>
                            <input type="number" id="tvocMax" name="tvoc_max" value="660" 
                                   min="0" max="1000" step="10" />
                        </div>
                    </div>
                </div>
                <div class="card temp-card">
                    <h3><span class="icon">🌡️</span> Temperature Threshold Settings</h3>
                    <div class="form-row">
                        <div class="form-control">
                            <label for="tempMin">Minimum Temperature (°C)</label>
                            <input type="number" id="tempMin" name="temp_min" value="18" 
                                   min="0" max="50" step="1" />
                        </div>
                        <div class="form-control">
                            <label for="tempMax">Maximum Temperature (°C)</label>
                            <input type="number" id="tempMax" name="temp_max" value="35" 
                                   min="0" max="50" step="1" />
                        </div>
                    </div>
                </div>
                <div class="card humidity-card">
                    <h3><span class="icon">💧</span> Humidity Threshold Settings</h3>
                    <div class="form-row">
                        <div class="form-control">
                            <label for="humidityMin">Minimum Humidity (%)</label>
                            <input type="number" id="humidityMin" name="humidity_min" value="30" 
                                   min="0" max="100" step="5" />
                        </div>
                        <div class="form-control">
                            <label for="humidityMax">Maximum Humidity (%)</label>
                            <input type="number" id="humidityMax" name="humidity_max" value="70" 
                                   min="0" max="100" step="5" />
                        </div>
                    </div>
                </div>
                <div class="card eco2-card" style="border-left: 5px solid #9CCC65;">
                    <h3><span class="icon">🌿</span> eCO2 Threshold Settings</h3>
                    <div class="form-row">
                        <div class="form-control">
                            <label for="eco2Min">Minimum eCO2 (ppm)</label>
                            <input type="number" id="eco2Min" name="eco2_min" value="400" 
                                   min="0" max="5000" step="50" />
                        </div>
                        <div class="form-control">
                            <label for="eco2Max">Maximum eCO2 (ppm)</label>
                            <input type="number" id="eco2Max" name="eco2_max" value="1000" 
                                   min="0" max="5000" step="50" />
                        </div>
                    </div>
                </div>
                <div style="text-align: center; grid-column: 1 / -1">
                    <button type="submit" class="btn" 
                            style="background: linear-gradient(45deg, #2196f3, #1976d2)">
                        💾 Update Thresholds
                    </button>
                    <div class="success-message" id="successMessage" style="margin-top: 10px">
                        ✅ Thresholds updated successfully!
                    </div>
                </div>
            </form>
        </div>
        <div id="alertsContainer" class="alerts"></div>
    </div>
    <script>
        // Initialize global variables
        let socket;
        let sensorChart;
        let chartData = {
            labels: [],
            tvocData: [],
            tempData: [],
            humidityData: [],
            eco2Data: [],
        };
        const room = "{{ room_name | lower }}";

        // Initialize when page loads
        document.addEventListener("DOMContentLoaded", function () {
            console.log("🚀 Initializing dashboard for " + room + "...");
            initializeSocket();
            initializeChart();
            loadInitialData();
            setupThresholdForm();
        });

        // Initialize Socket.IO
        function initializeSocket() {
            socket = io("/" + room);

            socket.on("connect", function () {
                console.log("✅ Socket.IO connection successful");
                updateConnectionStatus(true);
            });

            socket.on("disconnect", function () {
                console.log("❌ Socket.IO connection lost");
                updateConnectionStatus(false);
            });

            socket.on("sensor_data_" + room, function (data) {
                console.log("📊 Received sensor data:", data);
                updateSensorDisplay(data);
                updateChart(data);
                showAlerts(data.alerts || []);
            });

            socket.on("thresholds_updated", function (thresholds) {
                console.log("⚙️ Thresholds updated:", thresholds);
                updateThresholdForm(thresholds);
            });
        }

        function updateConnectionStatus(connected) {
            const statusEl = document.getElementById("connectionStatus");
            if (connected) {
                statusEl.textContent = "🟢 Connected";
                statusEl.className = "connection-status connected";
            } else {
                statusEl.textContent = "🔴 Disconnected";
                statusEl.className = "connection-status disconnected";
            }
        }

        function updateSensorDisplay(data) {
            document.getElementById("tvocValue").innerHTML = 
                `${data.tvoc.toFixed(2)}<span class="sensor-unit">ppb</span>`;
            document.getElementById("tempValue").innerHTML = 
                `${data.temperature}<span class="sensor-unit">°C</span>`;
            document.getElementById("humidityValue").innerHTML = 
                `${data.humidity}<span class="sensor-unit">%</span>`;
            document.getElementById("eco2Value").innerHTML = 
                `${data.eco2}<span class="sensor-unit">ppm</span>`;
            // Cập nhật mức TVOC đúng 5 mức
            let tvocLevel = "";
            let tvocClass = "status-normal";
            if (data.tvoc >= 0 && data.tvoc < 65) {
                tvocLevel = "Excellent";
                tvocClass = "status-excellent";
            } else if (data.tvoc >= 65 && data.tvoc < 220) {
                tvocLevel = "Good";
                tvocClass = "status-good";
            } else if (data.tvoc >= 220 && data.tvoc < 650) {
                tvocLevel = "Moderate";
                tvocClass = "status-moderate";
            } else if (data.tvoc >= 650 && data.tvoc < 2200) {
                tvocLevel = "Poor";
                tvocClass = "status-poor";
            } else if (data.tvoc >= 2200) {
                tvocLevel = "Unhealthy";
                tvocClass = "status-unhealthy";
            }
            const tvocStatusEl = document.getElementById("tvocStatus");
            tvocStatusEl.textContent = `${tvocLevel}`;
            tvocStatusEl.className = `sensor-status ${tvocClass}`;
            updateSensorStatus("temp", data.temperature);
            updateSensorStatus("humidity", data.humidity);
            updateSensorStatus("eco2", data.eco2);
            if (data.aqi) {
                updateAQIStatus(data.aqi);
            }
            document.getElementById("lastUpdate").textContent = 
                `Last update: ${data.timestamp}`;
        }

        function updateAQIStatus(aqi) {
            const statusEl = document.getElementById('aqiStatus');
            let className = '';
            let text = '';
            let icon = '';
            switch(aqi) {
                case 1:
                    className = 'aqi-excellent';
                    text = 'Excellent';
                    icon = '🌟';
                    break;
                case 2:
                    className = 'aqi-good';
                    text = 'Good';
                    icon = '✨';
                    break;
                case 3:
                    className = 'aqi-morderate';
                    text = 'Morderate';
                    icon = '⚡';
                    break;
                case 4:
                    className = 'aqi-poor';
                    text = 'Poor';
                    icon = '⚠️';
                    break;
                case 5:
                    className = 'aqi-unhealthy';
                    text = 'Unhealthy';
                    icon = '🚨';
                    break;
                default:
                    className = 'aqi-excellent';
                    text = 'Excellent';
                    icon = '🌟';
            }
            statusEl.className = `sensor-status ${className}`;
            statusEl.innerHTML = `${icon} ${text}`;
        }

        function updateSensorStatus(sensorType, value) {
            const statusEl = document.getElementById(
                sensorType === "temp" ? "tempStatus" : sensorType + "Status"
            );
            let status = "status-normal";
            let text = "Normal";
            if (sensorType === "tvoc" && value > 660) {
                status = "status-danger";
                text = "Too High";
            } else if (sensorType === "temp" && (value < 18 || value > 35)) {
                status = "status-warning";
                text = "Abnormal";
            } else if (sensorType === "humidity" && (value < 30 || value > 70)) {
                status = "status-warning";
                text = "Abnormal";
            } else if (sensorType === "eco2" && (value < 400 || value > 1000)) {
                status = "status-warning";
                text = "Abnormal";
            }
            statusEl.className = `sensor-status ${status}`;
            statusEl.textContent = text;
        }

        function initializeChart() {
            const ctx = document.getElementById("sensorChart").getContext("2d");
            console.log("📊 Initializing chart...");
            sensorChart = new Chart(ctx, {
                type: "line",
                data: {
                    labels: chartData.labels,
                    datasets: [
                        {
                            label: "TVOC (ppb)",
                            data: chartData.tvocData,
                            borderColor: "#FF6B6B",
                            backgroundColor: "rgba(255, 107, 107, 0.1)",
                            tension: 0.4,
                            yAxisID: "y",
                            fill: false,
                        },
                        {
                            label: "Temperature (°C)",
                            data: chartData.tempData,
                            borderColor: "#4ECDC4",
                            backgroundColor: "rgba(78, 205, 196, 0.1)",
                            tension: 0.4,
                            yAxisID: "y1",
                            fill: false,
                        },
                        {
                            label: "Humidity (%)",
                            data: chartData.humidityData,
                            borderColor: "#45B7D1",
                            backgroundColor: "rgba(69, 183, 209, 0.1)",
                            tension: 0.4,
                            yAxisID: "y1",
                            fill: false,
                        },
                        {
                            label: "eCO2 (ppm)",
                            data: chartData.eco2Data,
                            borderColor: "#9CCC65",
                            backgroundColor: "rgba(156, 204, 101, 0.1)",
                            tension: 0.4,
                            yAxisID: "y2",
                            fill: false,
                        },
                    ],
                },
                options: {
                    responsive: true,
                    maintainAspectRatio: false,
                    interaction: {
                        intersect: false,
                        mode: "index",
                    },
                    scales: {
                        x: {
                            display: true,
                            title: {
                                display: true,
                                text: "Timeline",
                                color: "#666",
                            },
                            grid: {
                                color: "rgba(0,0,0,0.1)",
                            },
                        },
                        y: {
                            type: "linear",
                            display: true,
                            position: "left",
                            title: {
                                display: true,
                                text: "TVOC (ppb)",
                                color: "#FF6B6B",
                            },
                            grid: {
                                color: "rgba(255, 107, 107, 0.1)",
                            },
                            min: 0,
                            max: 1000,
                        },
                        y1: {
                            type: "linear",
                            display: true,
                            position: "right",
                            title: {
                                display: true,
                                text: "Temperature (°C) / Humidity (%)",
                                color: "#4ECDC4",
                            },
                            grid: {
                                drawOnChartArea: false,
                                color: "rgba(78, 205, 196, 0.1)",
                            },
                            min: 0,
                            max: 100,
                        },
                        y2: {
                            type: "linear",
                            display: true,
                            position: "right",
                            title: {
                                display: true,
                                text: "eCO2 (ppm)",
                                color: "#9CCC65",
                            },
                            grid: {
                                drawOnChartArea: false,
                                color: "rgba(156, 204, 101, 0.1)",
                            },
                            min: 0,
                            max: 2000,
                        },
                    },
                    plugins: {
                        legend: {
                            display: true,
                            position: "top",
                            labels: {
                                color: "#333",
                                usePointStyle: true,
                                padding: 20,
                            },
                        },
                        tooltip: {
                            backgroundColor: "rgba(0,0,0,0.8)",
                            titleColor: "white",
                            bodyColor: "white",
                            borderColor: "rgba(255,255,255,0.2)",
                            borderWidth: 1,
                        },
                    },
                    animation: {
                        duration: 750,
                    },
                },
            });
            console.log("✅ Chart initialized successfully");
        }

        function updateChart(data) {
            const now = data.timestamp || new Date().toLocaleTimeString();
            chartData.labels.push(now);
            chartData.tvocData.push(data.tvoc);
            chartData.tempData.push(data.temperature);
            chartData.humidityData.push(data.humidity);
            chartData.eco2Data.push(data.eco2);
            if (chartData.labels.length > 10) {
                console.log("Data limit reached, removing old point:", chartData.labels.length);
                chartData.labels.shift();
                chartData.tvocData.shift();
                chartData.tempData.shift();
                chartData.humidityData.shift();
                chartData.eco2Data.shift();
            }
            sensorChart.data.labels = chartData.labels;
            sensorChart.data.datasets[0].data = chartData.tvocData;
            sensorChart.data.datasets[1].data = chartData.tempData;
            sensorChart.data.datasets[2].data = chartData.humidityData;
            sensorChart.data.datasets[3].data = chartData.eco2Data;
            sensorChart.update("active");
            console.log("📈 Chart updated:", {
                time: now,
                tvoc: data.tvoc,
                temp: data.temperature,
                humidity: data.humidity,
                eco2: data.eco2,
            });
        }

        function loadInitialData() {
            fetch("/api/current-data/" + room)
                .then((response) => response.json())
                .then((data) => {
                    console.log("📥 Current data:", data);
                    updateSensorDisplay({
                        tvoc: data.tvoc,
                        temperature: data.temperature,
                        humidity: data.humidity,
                        eco2: data.eco2,
                        aqi: data.aqi,
                        timestamp: new Date(data.timestamp).toLocaleTimeString(),
                    });
                    if (data.thresholds) {
                        updateThresholdForm(data.thresholds);
                    }
                })
                .catch((error) => {
                    console.error("❌ Error loading current data:", error);
                });
            fetch("/api/history/" + room + "?hours=1")
                .then((response) => response.json())
                .then((data) => {
                    console.log("📈 Historical data:", data);
                    chartData.labels = [];
                    chartData.tvocData = [];
                    chartData.tempData = [];
                    chartData.humidityData = [];
                    chartData.eco2Data = [];
                    if (data && data.length > 0) {
                        const recentData = data.slice(-5);
                        recentData.forEach((item) => {
                            const time = new Date(item.timestamp).toLocaleTimeString();
                            chartData.labels.push(time);
                            chartData.tvocData.push(item.tvoc);
                            chartData.tempData.push(item.temperature);
                            chartData.humidityData.push(item.humidity);
                            chartData.eco2Data.push(item.eco2);
                        });
                    } else {
                        const now = new Date().toLocaleTimeString();
                        chartData.labels.push(now);
                        chartData.tvocData.push(0);
                        chartData.tempData.push(25);
                        chartData.humidityData.push(50);
                        chartData.eco2Data.push(400);
                    }
                    sensorChart.data.labels = chartData.labels;
                    sensorChart.data.datasets[0].data = chartData.tvocData;
                    sensorChart.data.datasets[1].data = chartData.tempData;
                    sensorChart.data.datasets[2].data = chartData.humidityData;
                    sensorChart.data.datasets[3].data = chartData.eco2Data;
                    sensorChart.update();
                })
                .catch((error) => {
                    console.error("❌ Error loading historical data:", error);
                    const now = new Date().toLocaleTimeString();
                    chartData.labels = [now];
                    chartData.tvocData = [0];
                    chartData.tempData = [25];
                    chartData.humidityData = [50];
                    chartData.eco2Data = [400];
                    sensorChart.data.labels = chartData.labels;
                    sensorChart.data.datasets[0].data = chartData.tvocData;
                    sensorChart.data.datasets[1].data = chartData.tempData;
                    sensorChart.data.datasets[2].data = chartData.humidityData;
                    sensorChart.data.datasets[3].data = chartData.eco2Data;
                    sensorChart.update();
                });
        }

        function setupThresholdForm() {
            const form = document.getElementById("thresholdForm");
            form.addEventListener("submit", function (e) {
                e.preventDefault();
                console.log("📝 Updating thresholds...");
                const formData = {
                    tvoc_max: parseFloat(document.getElementById("tvocMax").value),
                    temp_min: parseFloat(document.getElementById("tempMin").value),
                    temp_max: parseFloat(document.getElementById("tempMax").value),
                    humidity_min: parseFloat(document.getElementById("humidityMin").value),
                    humidity_max: parseFloat(document.getElementById("humidityMax").value),
                    eco2_min: parseFloat(document.getElementById("eco2Min").value),
                    eco2_max: parseFloat(document.getElementById("eco2Max").value),
                };
                console.log("📤 Sending data:", formData);
                fetch("/api/thresholds/" + room, {
                    method: "POST",
                    headers: {
                        "Content-Type": "application/json",
                    },
                    body: JSON.stringify(formData),
                })
                    .then((response) => response.json())
                    .then((data) => {
                        console.log("✅ Update successful:", data);
                        if (data.success) {
                            showSuccessMessage();
                            updateThresholdForm(data.thresholds);
                        } else {
                            console.error("❌ Update error:", data.error);
                            alert("Error updating thresholds: " + data.error);
                        }
                    })
                    .catch((error) => {
                        console.error("❌ Connection error:", error);
                        alert("Connection error while updating thresholds");
                    });
            });
        }

        function updateThresholdForm(thresholds) {
            document.getElementById("tvocMax").value = thresholds.tvoc_max;
            document.getElementById("tempMin").value = thresholds.temp_min;
            document.getElementById("tempMax").value = thresholds.temp_max;
            document.getElementById("humidityMin").value = thresholds.humidity_min;
            document.getElementById("humidityMax").value = thresholds.humidity_max;
            document.getElementById("eco2Min").value = thresholds.eco2_min;
            document.getElementById("eco2Max").value = thresholds.eco2_max;
        }

        function showSuccessMessage() {
            const message = document.getElementById("successMessage");
            message.style.display = "block";
            setTimeout(() => {
                message.style.display = "none";
            }, 3000);
        }

        function showAlerts(alerts) {
            const container = document.getElementById("alertsContainer");
            container.innerHTML = "";
            alerts.forEach((alert) => {
                const alertEl = document.createElement("div");
                alertEl.className = `alert alert-${alert.severity}`;
                alertEl.innerHTML = `
                    <span class="icon">${alert.severity === "danger" ? "🚨" : "⚠️"}</span>
                    ${alert.message}
                `;
                container.appendChild(alertEl);
            });
        }
    </script>
</body>
</html>
"""


# Flask Routes
@app.route("/")
def main_page():
    """Main selection page"""
    return render_template_string(MAIN_PAGE_TEMPLATE)


@app.route("/bedroom")
def bedroom_dashboard():
    """Bedroom dashboard route"""
    return render_template_string(DASHBOARD_TEMPLATE, room_name="Bedroom")


@app.route("/workingroom")
def workingroom_dashboard():
    """Workingroom dashboard route"""
    return render_template_string(DASHBOARD_TEMPLATE, room_name="Workingroom")


@app.route("/api/current-data/<room>")
def api_current_data(room):
    """API endpoint for current sensor data"""
    if room not in ["bedroom", "workingroom"]:
        return jsonify({"error": "Invalid room"}), 400
    current_data = (
        current_data_bedroom if room == "bedroom" else current_data_workingroom
    )
    thresholds = thresholds_bedroom if room == "bedroom" else thresholds_workingroom
    return jsonify(
        {
            "tvoc": current_data["tvoc"],
            "temperature": current_data["temperature"],
            "humidity": current_data["humidity"],
            "eco2": current_data["eco2"],
            "aqi": current_data["aqi"],
            "timestamp": current_data["timestamp"].strftime("%Y-%m-%d %H:%M:%S"),
            "thresholds": thresholds,
        }
    )


@app.route("/api/history/<room>")
def api_history(room):
    """API endpoint for historical data"""
    if room not in ["bedroom", "workingroom"]:
        return jsonify({"error": "Invalid room"}), 400
    hours = request.args.get("hours", 24, type=int)
    data = db.get_recent_data(room, hours)
    return jsonify(data)


@app.route("/api/thresholds/<room>", methods=["GET", "POST"])
def api_thresholds(room):
    """API endpoint for threshold management"""
    if room not in ["bedroom", "workingroom"]:
        return jsonify({"error": "Invalid room"}), 400
    thresholds = thresholds_bedroom if room == "bedroom" else thresholds_workingroom
    if request.method == "POST":
        try:
            new_thresholds = request.get_json()
            required_keys = [
                "tvoc_max",
                "temp_min",
                "temp_max",
                "humidity_min",
                "humidity_max",
                "eco2_min",
                "eco2_max",
            ]
            for key in required_keys:
                if key not in new_thresholds:
                    return jsonify({"error": f"Missing field {key}"}), 400
            for key in required_keys:
                thresholds[key] = float(new_thresholds[key])
            if thresholds["temp_min"] > thresholds["temp_max"]:
                return (
                    jsonify(
                        {
                            "error": "Minimum temperature must be less than or equal to maximum temperature!"
                        }
                    ),
                    400,
                )
            if thresholds["humidity_min"] > thresholds["humidity_max"]:
                return (
                    jsonify(
                        {
                            "error": "Minimum humidity must be less than or equal to maximum humidity!"
                        }
                    ),
                    400,
                )
            if thresholds["eco2_min"] > thresholds["eco2_max"]:
                return (
                    jsonify(
                        {
                            "error": "Minimum eCO2 must be less than or equal to maximum eCO2!"
                        }
                    ),
                    400,
                )
            db.update_thresholds(room, thresholds)
            socketio.emit("thresholds_updated", thresholds, namespace=f"/{room}")
            logger.info(f"Thresholds updated successfully for {room}: {thresholds}")
            return jsonify({"success": True, "thresholds": thresholds})
        except Exception as e:
            logger.error(f"Error updating thresholds for {room}: {e}")
            return jsonify({"error": str(e)}), 500
    return jsonify(thresholds)


@app.route("/api/test-alert/<room>")
def test_alert(room):
    """API for testing alert system"""
    if room not in ["bedroom", "workingroom"]:
        return jsonify({"error": "Invalid room"}), 400
    message = f"🧪 TEST ALERT from {room.capitalize()} - System is working normally!"
    send_telegram_alert(room, message)
    return jsonify({"message": f"Test alert sent for {room}"})


# SocketIO Events
@socketio.on("connect", namespace="/bedroom")
def on_socketio_connect_bedroom():
    """Handle SocketIO client connection for bedroom"""
    logger.info(f"SocketIO client connected to bedroom: {request.sid}")
    emit(
        "sensor_data_bedroom",
        {
            "tvoc": current_data_bedroom["tvoc"],
            "temperature": current_data_bedroom["temperature"],
            "humidity": current_data_bedroom["humidity"],
            "eco2": current_data_bedroom["eco2"],
            "aqi": current_data_bedroom["aqi"],
            "timestamp": current_data_bedroom["timestamp"].strftime("%H:%M:%S"),
            "alerts": [],
        },
        namespace="/bedroom",
    )


@socketio.on("connect", namespace="/workingroom")
def on_socketio_connect_workingroom():
    """Handle SocketIO client connection for workingroom"""
    logger.info(f"SocketIO client connected to workingroom: {request.sid}")
    emit(
        "sensor_data_workingroom",
        {
            "tvoc": current_data_workingroom["tvoc"],
            "temperature": current_data_workingroom["temperature"],
            "humidity": current_data_workingroom["humidity"],
            "eco2": current_data_workingroom["eco2"],
            "aqi": current_data_workingroom["aqi"],
            "timestamp": current_data_workingroom["timestamp"].strftime("%H:%M:%S"),
            "alerts": [],
        },
        namespace="/workingroom",
    )


@socketio.on("disconnect", namespace="/bedroom")
def on_socketio_disconnect_bedroom():
    """Handle SocketIO client disconnection for bedroom"""
    logger.info(f"SocketIO client disconnected from bedroom: {request.sid}")


@socketio.on("disconnect", namespace="/workingroom")
def on_socketio_disconnect_workingroom():
    """Handle SocketIO client disconnection for workingroom"""
    logger.info(f"SocketIO client disconnected from workingroom: {request.sid}")


if __name__ == "__main__":
    # Run MQTT in separate thread
    mqtt_thread = threading.Thread(target=start_mqtt)
    mqtt_thread.daemon = True
    mqtt_thread.start()

    logger.info("🚀 Starting TVOC Monitoring Server...")
    logger.info("📊 Main Page: http://localhost:5000")
    logger.info("📡 MQTT Topics: " + MQTT_TOPIC_BEDROOM + ", " + MQTT_TOPIC_WORKINGROOM)
    socketio.run(app, host="0.0.0.0", port=5000, debug=False)
