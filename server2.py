#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
TVOC Monitoring Server for Raspberry Pi 5
Handles MQTT, Database, Telegram alerts, and Web Dashboard
"""

from flask import Flask, render_template, request, jsonify, render_template_string
from flask_socketio import SocketIO, emit
import paho.mqtt.client as mqtt
import mariadb
import json
import requests
import threading
import time
from datetime import datetime, timedelta
import logging
import os
from dotenv import load_dotenv

# Load file .env
load_dotenv()

# Cấu hình logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Khởi tạo Flask app
app = Flask(__name__)
from config import FLASK_SECRET_KEY
app.config['SECRET_KEY'] = FLASK_SECRET_KEY
socketio = SocketIO(app, cors_allowed_origins="*")

# Cấu hình Database
from config import DB_HOST, DB_PORT, DB_NAME, DB_USER, DB_PASSWORD

DB_CONFIG = {
'host': DB_HOST,
'port': DB_PORT,
'user': DB_USER,
'password': DB_PASSWORD,
'database': DB_NAME
}

# Cấu hình MQTT
from config import MQTT_BROKER, MQTT_PORT, MQTT_TOPIC

# Cấu hình Telegram Bot
from config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
# Ngưỡng mặc định cho TVOC
DEFAULT_THRESHOLDS = {
    'tvoc_max': 0.5,  # mg/m³
    'temp_min': 18,
    'temp_max': 30,
    'humidity_min': 30,
    'humidity_max': 70
}

# Biến global để lưu trữ
current_data = {
    'tvoc': 0,
    'temperature': 0,
    'humidity': 0,
    'timestamp': datetime.now()
}

thresholds = DEFAULT_THRESHOLDS.copy()
last_alert_time = {}

class DatabaseManager:
    def __init__(self):
        self.connection = None
        self.connect()
        self.create_tables()
    
    def connect(self):
        try:
            self.connection = mariadb.connect(**DB_CONFIG)
            logger.info("Kết nối Database thành công")
        except Exception as e:
            logger.error(f"Lỗi kết nối Database: {e}")
    
    def create_tables(self):
        try:
            cursor = self.connection.cursor()
            
            # Bảng dữ liệu cảm biến
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS sensor_data (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    tvoc FLOAT NOT NULL,
                    temperature FLOAT NOT NULL,
                    humidity FLOAT NOT NULL,
                    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
                )
            """)
            
            # Bảng ngưỡng cảnh báo
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS thresholds (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    tvoc_max FLOAT DEFAULT 0.5,
                    temp_min FLOAT DEFAULT 18,
                    temp_max FLOAT DEFAULT 30,
                    humidity_min FLOAT DEFAULT 30,
                    humidity_max FLOAT DEFAULT 70,
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
                )
            """)
            
            # Bảng lịch sử cảnh báo
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS alert_history (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    alert_type VARCHAR(50) NOT NULL,
                    message TEXT NOT NULL,
                    value FLOAT NOT NULL,
                    threshold_value FLOAT NOT NULL,
                    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
                )
            """)
            
            self.connection.commit()
            logger.info("Tạo bảng Database thành công")
            
        except Exception as e:
            logger.error(f"Lỗi tạo bảng: {e}")
    
    def insert_sensor_data(self, tvoc, temperature, humidity):
        try:
            cursor = self.connection.cursor()
            cursor.execute(
                "INSERT INTO sensor_data (tvoc, temperature, humidity) VALUES (?, ?, ?)",
                (tvoc, temperature, humidity)
            )
            self.connection.commit()
        except Exception as e:
            logger.error(f"Lỗi lưu dữ liệu: {e}")
            self.connect()  # Thử kết nối lại
    
    def get_recent_data(self, hours=24):
        try:
            cursor = self.connection.cursor()
            cursor.execute("""
                SELECT tvoc, temperature, humidity, timestamp 
                FROM sensor_data 
                WHERE timestamp >= NOW() - INTERVAL ? HOUR 
                ORDER BY timestamp ASC
            """, (hours,))
            
            data = []
            for row in cursor.fetchall():
                data.append({
                    'tvoc': row[0],
                    'temperature': row[1],
                    'humidity': row[2],
                    'timestamp': row[3].strftime('%Y-%m-%d %H:%M:%S')
                })
            return data
        except Exception as e:
            logger.error(f"Lỗi đọc dữ liệu: {e}")
            return []
    
    def update_thresholds(self, new_thresholds):
        try:
            cursor = self.connection.cursor()
            cursor.execute("DELETE FROM thresholds")  # Xóa cũ
            cursor.execute("""
                INSERT INTO thresholds (tvoc_max, temp_min, temp_max, humidity_min, humidity_max)
                VALUES (?, ?, ?, ?, ?)
            """, (
                new_thresholds['tvoc_max'],
                new_thresholds['temp_min'],
                new_thresholds['temp_max'],
                new_thresholds['humidity_min'],
                new_thresholds['humidity_max']
            ))
            self.connection.commit()
            logger.info("Cập nhật ngưỡng thành công")
        except Exception as e:
            logger.error(f"Lỗi cập nhật ngưỡng: {e}")
    
    def log_alert(self, alert_type, message, value, threshold_value):
        try:
            cursor = self.connection.cursor()
            cursor.execute("""
                INSERT INTO alert_history (alert_type, message, value, threshold_value)
                VALUES (?, ?, ?, ?)
            """, (alert_type, message, value, threshold_value))
            self.connection.commit()
        except Exception as e:
            logger.error(f"Lỗi lưu lịch sử cảnh báo: {e}")

# Khởi tạo Database Manager
db = DatabaseManager()

def send_telegram_alert(message):
    """Gửi cảnh báo qua Telegram"""
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        data = {
            'chat_id': TELEGRAM_CHAT_ID,
            'text': f"🚨 CẢNH BÁO 🚨\n\n{message}",
            'parse_mode': 'HTML'
        }
        response = requests.post(url, data=data, timeout=10)
        if response.status_code == 200:
            logger.info("Gửi Telegram thành công")
        else:
            logger.error(f"Lỗi gửi Telegram: {response.status_code}")
    except Exception as e:
        logger.error(f"Lỗi Telegram: {e}")

def check_thresholds_and_alert(tvoc, temperature, humidity):
    """Kiểm tra ngưỡng và gửi cảnh báo"""
    global last_alert_time
    current_time = datetime.now()
    alerts = []
    
    # Kiểm tra TVOC
    if tvoc > thresholds['tvoc_max']:
        alert_key = 'tvoc_high'
        if alert_key not in last_alert_time or \
           (current_time - last_alert_time[alert_key]).seconds > 300:  # 5 phút
            
            message = f"⚠️ TVOC quá cao: {tvoc:.2f} mg/m³ (Ngưỡng: {thresholds['tvoc_max']:.2f} mg/m³)\n"
            message += "💨 Khuyến nghị: Mở cửa sổ hoặc tăng thông gió!"
            
            send_telegram_alert(message)
            db.log_alert('tvoc_high', message, tvoc, thresholds['tvoc_max'])
            last_alert_time[alert_key] = current_time
            
            alerts.append({
                'type': 'tvoc_high',
                'message': f'TVOC cao: {tvoc:.2f} mg/m³',
                'severity': 'danger'
            })
    
    # Kiểm tra nhiệt độ
    if temperature < thresholds['temp_min'] or temperature > thresholds['temp_max']:
        alert_key = 'temp_abnormal'
        if alert_key not in last_alert_time or \
           (current_time - last_alert_time[alert_key]).seconds > 600:  # 10 phút
            
            if temperature < thresholds['temp_min']:
                message = f"🥶 Nhiệt độ quá thấp: {temperature}°C (Tối thiểu: {thresholds['temp_min']}°C)"
            else:
                message = f"🥵 Nhiệt độ quá cao: {temperature}°C (Tối đa: {thresholds['temp_max']}°C)"
            
            send_telegram_alert(message)
            db.log_alert('temp_abnormal', message, temperature, 
                        thresholds['temp_min'] if temperature < thresholds['temp_min'] else thresholds['temp_max'])
            last_alert_time[alert_key] = current_time
            
            alerts.append({
                'type': 'temp_abnormal',
                'message': f'Nhiệt độ bất thường: {temperature}°C',
                'severity': 'warning'
            })
    
    # Kiểm tra độ ẩm
    if humidity < thresholds['humidity_min'] or humidity > thresholds['humidity_max']:
        alert_key = 'humidity_abnormal'
        if alert_key not in last_alert_time or \
           (current_time - last_alert_time[alert_key]).seconds > 600:  # 10 phút
            
            if humidity < thresholds['humidity_min']:
                message = f"🏜️ Độ ẩm quá thấp: {humidity}% (Tối thiểu: {thresholds['humidity_min']}%)"
            else:
                message = f"💧 Độ ẩm quá cao: {humidity}% (Tối đa: {thresholds['humidity_max']}%)"
            
            send_telegram_alert(message)
            db.log_alert('humidity_abnormal', message, humidity,
                        thresholds['humidity_min'] if humidity < thresholds['humidity_min'] else thresholds['humidity_max'])
            last_alert_time[alert_key] = current_time
            
            alerts.append({
                'type': 'humidity_abnormal',
                'message': f'Độ ẩm bất thường: {humidity}%',
                'severity': 'warning'
            })
    
    return alerts

# MQTT Client
def on_connect(client, userdata, flags, rc):
    if rc == 0:
        logger.info("Kết nối MQTT thành công")
        client.subscribe(MQTT_TOPIC)
    else:
        logger.error(f"Lỗi kết nối MQTT: {rc}")

def on_message(client, userdata, msg):
    try:
        data = json.loads(msg.payload.decode())
        
        tvoc = float(data.get('tvoc', 0))
        temperature = float(data.get('temperature', 0))
        humidity = float(data.get('humidity', 0))
        
        # Cập nhật dữ liệu hiện tại
        current_data.update({
            'tvoc': tvoc,
            'temperature': temperature,
            'humidity': humidity,
            'timestamp': datetime.now()
        })
        
        # Lưu vào database
        db.insert_sensor_data(tvoc, temperature, humidity)
        
        # Kiểm tra ngưỡng và cảnh báo
        alerts = check_thresholds_and_alert(tvoc, temperature, humidity)
        
        # Gửi dữ liệu real-time qua WebSocket
        socketio.emit('sensor_data', {
            'tvoc': tvoc,
            'temperature': temperature,
            'humidity': humidity,
            'timestamp': current_data['timestamp'].strftime('%H:%M:%S'),
            'alerts': alerts
        })
        
        logger.info(f"Nhận dữ liệu: TVOC={tvoc:.2f}mg/m³, T={temperature}°C, H={humidity}%")
        
    except Exception as e:
        logger.error(f"Lỗi xử lý MQTT: {e}")

# Khởi tạo MQTT Client
mqtt_client = mqtt.Client()
mqtt_client.on_connect = on_connect
mqtt_client.on_message = on_message

def start_mqtt():
    try:
        mqtt_client.connect(MQTT_BROKER, MQTT_PORT, 60)
        mqtt_client.loop_forever()
    except Exception as e:
        logger.error(f"Lỗi MQTT: {e}")

# HTML Template (nội tuyến)
HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="vi">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>🌱 Smart Indoor Air Quality Monitoring Dashboard</title>
    <script src="https://cdnjs.cloudflare.com/ajax/libs/socket.io/4.0.0/socket.io.js"></script>
    <script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/3.9.1/chart.min.js"></script>
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
        }

        .header h1 {
            font-size: 2.5rem;
            margin-bottom: 10px;
            text-shadow: 2px 2px 4px rgba(0,0,0,0.3);
        }

        .header p {
            font-size: 1.1rem;
            opacity: 0.9;
        }

        .status-bar {
            background: rgba(255,255,255,0.1);
            backdrop-filter: blur(10px);
            border-radius: 15px;
            padding: 15px;
            margin-bottom: 25px;
            border: 1px solid rgba(255,255,255,0.2);
            color: white;
            text-align: center;
        }

        .grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(300px, 1fr));
            gap: 20px;
            margin-bottom: 30px;
            max-width: 1000px;
            margin-left: auto;
            margin-right: auto;
        }

        .sensors-grid {
            display: grid;
            grid-template-columns: repeat(3, 1fr);
            gap: 20px;
            margin-bottom: 30px;
            max-width: 1000px;
            margin-left: auto;
            margin-right: auto;
        }

        @media (max-width: 768px) {
            .sensors-grid {
                grid-template-columns: 1fr;
            }
        }

        .card {
            background: rgba(255,255,255,0.95);
            backdrop-filter: blur(10px);
            border-radius: 20px;
            padding: 25px;
            box-shadow: 0 8px 32px rgba(0,0,0,0.1);
            border: 1px solid rgba(255,255,255,0.2);
            transition: transform 0.3s ease, box-shadow 0.3s ease;
        }

        .card:hover {
            transform: translateY(-5px);
            box-shadow: 0 12px 40px rgba(0,0,0,0.15);
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
            text-shadow: 1px 1px 2px rgba(0,0,0,0.1);
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
            background: linear-gradient(45deg, #4CAF50, #45a049);
            color: white;
        }

        .status-warning {
            background: linear-gradient(45deg, #FF9800, #f57c00);
            color: white;
        }

        .status-danger {
            background: linear-gradient(45deg, #f44336, #d32f2f);
            color: white;
            animation: pulse 2s infinite;
        }

        @keyframes pulse {
            0% { transform: scale(1); }
            50% { transform: scale(1.05); }
            100% { transform: scale(1); }
        }

        .tvoc-card { border-left: 5px solid #FF6B6B; }
        .temp-card { border-left: 5px solid #4ECDC4; }
        .humidity-card { border-left: 5px solid #45B7D1; }

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
            background: rgba(255,255,255,0.8);
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
            from { opacity: 0; transform: translateX(-20px); }
            to { opacity: 1; transform: translateX(0); }
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
            color: rgba(255,255,255,0.8);
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
            background: #4CAF50;
            color: white;
        }

        .disconnected {
            background: #f44336;
            color: white;
            animation: pulse 1s infinite;
        }

        .success-message {
            background: #4CAF50;
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
        🔴 Đang kết nối...
    </div>

    <div class="container">
        <div class="header">
            <h1>🌱 Smart Indoor Air Quality Monitoring Dashboard</h1>
            <p>Giám sát chất lượng không khí trong phòng kín</p>
        </div>

        <div class="status-bar" id="statusBar">
            <div id="lastUpdate">Đang tải dữ liệu...</div>
        </div>

        <div class="sensors-grid">
            <!-- TVOC Card -->
            <div class="card sensor-card tvoc-card">
                <h3><span class="icon">💨</span> Nồng độ TVOC</h3>
                <div class="sensor-value" id="tvocValue">
                    0<span class="sensor-unit">mg/m³</span>
                </div>
                <div class="sensor-status status-normal" id="tvocStatus">Bình thường</div>
            </div>

            <!-- Temperature Card -->
            <div class="card sensor-card temp-card">
                <h3><span class="icon">🌡️</span> Nhiệt độ</h3>
                <div class="sensor-value" id="tempValue">
                    0<span class="sensor-unit">°C</span>
                </div>
                <div class="sensor-status status-normal" id="tempStatus">Bình thường</div>
            </div>

            <!-- Humidity Card -->
            <div class="card sensor-card humidity-card">
                <h3><span class="icon">💧</span> Độ ẩm</h3>
                <div class="sensor-value" id="humidityValue">
                    0<span class="sensor-unit">%</span>
                </div>
                <div class="sensor-status status-normal" id="humidityStatus">Bình thường</div>
            </div>
        </div>

        <div class="grid">
            <!-- Chart Card -->
            <div class="card" style="grid-column: 1 / -1;">
                <h3><span class="icon">📊</span> Biểu đồ theo thời gian</h3>
                <div class="chart-container">
                    <canvas id="sensorChart"></canvas>
                </div>
            </div>

            <!-- Controls Card -->
            <div class="card controls-card">
                <h3><span class="icon">⚙️</span> Cài đặt ngưỡng cảnh báo</h3>
                <div class="success-message" id="successMessage">
                    ✅ Cập nhật ngưỡng thành công!
                </div>
                <form id="thresholdForm">
                    <div class="form-row">
                        <div class="form-control">
                            <label for="tvocMax">TVOC tối đa (mg/m³)</label>
                            <input type="number" id="tvocMax" name="tvoc_max" value="0.5" min="0" max="5" step="0.1">
                        </div>
                        <div class="form-control">
                            <label for="tempMin">Nhiệt độ tối thiểu (°C)</label>
                            <input type="number" id="tempMin" name="temp_min" value="18" min="0" max="50" step="1">
                        </div>
                        <div class="form-control">
                            <label for="tempMax">Nhiệt độ tối đa (°C)</label>
                            <input type="number" id="tempMax" name="temp_max" value="30" min="0" max="50" step="1">
                        </div>
                    </div>
                    <div class="form-row">
                        <div class="form-control">
                            <label for="humidityMin">Độ ẩm tối thiểu (%)</label>
                            <input type="number" id="humidityMin" name="humidity_min" value="30" min="0" max="100" step="5">
                        </div>
                        <div class="form-control">
                            <label for="humidityMax">Độ ẩm tối đa (%)</label>
                            <input type="number" id="humidityMax" name="humidity_max" value="70" min="0" max="100" step="5">
                        </div>
                        <div class="form-control" style="display: flex; align-items: end;">
                            <button type="submit" class="btn">💾 Cập nhật ngưỡng</button>
                        </div>
                    </div>
                </form>
            </div>
        </div>

        <!-- Alerts -->
        <div id="alertsContainer" class="alerts"></div>
    </div>

    <script>
        // Khởi tạo biến global
        let socket;
        let sensorChart;
        let chartData = {
            labels: [],
            tvocData: [],
            tempData: [],
            humidityData: []
        };

        // Khởi tạo khi trang load
        document.addEventListener('DOMContentLoaded', function() {
            console.log('🚀 Đang khởi tạo dashboard...');
            initializeSocket();
            initializeChart();
            loadInitialData();
            setupThresholdForm();
        });

        // Khởi tạo Socket.IO
        function initializeSocket() {
            socket = io();
            
            // Kết nối thành công
            socket.on('connect', function() {
                console.log('✅ Socket.IO kết nối thành công');
                updateConnectionStatus(true);
            });

            // Mất kết nối
            socket.on('disconnect', function() {
                console.log('❌ Socket.IO mất kết nối');
                updateConnectionStatus(false);
            });

            // Nhận dữ liệu sensor real-time
            socket.on('sensor_data', function(data) {
                console.log('📊 Nhận dữ liệu sensor:', data);
                updateSensorDisplay(data);
                updateChart(data);
                showAlerts(data.alerts || []);
            });

            // Nhận thông báo cập nhật ngưỡng
            socket.on('thresholds_updated', function(thresholds) {
                console.log('⚙️ Ngưỡng được cập nhật:', thresholds);
                updateThresholdForm(thresholds);
            });
        }

        // Cập nhật trạng thái kết nối
        function updateConnectionStatus(connected) {
            const statusEl = document.getElementById('connectionStatus');
            if (connected) {
                statusEl.textContent = '🟢 Đã kết nối';
                statusEl.className = 'connection-status connected';
            } else {
                statusEl.textContent = '🔴 Mất kết nối';
                statusEl.className = 'connection-status disconnected';
            }
        }

        // Cập nhật hiển thị dữ liệu sensor
        function updateSensorDisplay(data) {
            // Cập nhật giá trị
            document.getElementById('tvocValue').innerHTML = 
                `${data.tvoc.toFixed(2)}<span class="sensor-unit">mg/m³</span>`;
            document.getElementById('tempValue').innerHTML = 
                `${data.temperature}<span class="sensor-unit">°C</span>`;
            document.getElementById('humidityValue').innerHTML = 
                `${data.humidity}<span class="sensor-unit">%</span>`;

            // Cập nhật trạng thái
            updateSensorStatus('tvoc', data.tvoc);
            updateSensorStatus('temp', data.temperature);
            updateSensorStatus('humidity', data.humidity);

            // Cập nhật thời gian
            document.getElementById('lastUpdate').textContent = 
                `Cập nhật lần cuối: ${data.timestamp}`;
        }

        // Cập nhật trạng thái sensor
        function updateSensorStatus(sensorType, value) {
            const statusEl = document.getElementById(sensorType === 'temp' ? 'tempStatus' : sensorType + 'Status');
            
            let status = 'status-normal';
            let text = 'Bình thường';
            
            if (sensorType === 'tvoc' && value > 0.5) {
                status = 'status-danger';
                text = 'Quá cao';
            } else if (sensorType === 'temp' && (value < 18 || value > 30)) {
                status = 'status-warning';
                text = 'Bất thường';
            } else if (sensorType === 'humidity' && (value < 30 || value > 70)) {
                status = 'status-warning';
                text = 'Bất thường';
            }
            
            statusEl.className = `sensor-status ${status}`;
            statusEl.textContent = text;
        }

        // Khởi tạo biểu đồ
        function initializeChart() {
            const ctx = document.getElementById('sensorChart').getContext('2d');
            console.log('📊 Đang khởi tạo biểu đồ...');
            
            sensorChart = new Chart(ctx, {
                type: 'line',
                data: {
                    labels: chartData.labels,
                    datasets: [
                        {
                            label: 'TVOC (mg/m³)',
                            data: chartData.tvocData,
                            borderColor: '#FF6B6B',
                            backgroundColor: 'rgba(255, 107, 107, 0.1)',
                            tension: 0.4,
                            yAxisID: 'y',
                            fill: false
                        },
                        {
                            label: 'Nhiệt độ (°C)',
                            data: chartData.tempData,
                            borderColor: '#4ECDC4',
                            backgroundColor: 'rgba(78, 205, 196, 0.1)',
                            tension: 0.4,
                            yAxisID: 'y1',
                            fill: false
                        },
                        {
                            label: 'Độ ẩm (%)',
                            data: chartData.humidityData,
                            borderColor: '#45B7D1',
                            backgroundColor: 'rgba(69, 183, 209, 0.1)',
                            tension: 0.4,
                            yAxisID: 'y1',
                            fill: false
                        }
                    ]
                },
                options: {
                    responsive: true,
                    maintainAspectRatio: false,
                    interaction: {
                        intersect: false,
                        mode: 'index'
                    },
                    scales: {
                        x: {
                            display: true,
                            title: {
                                display: true,
                                text: 'Thời gian',
                                color: '#666'
                            },
                            grid: {
                                color: 'rgba(0,0,0,0.1)'
                            }
                        },
                        y: {
                            type: 'linear',
                            display: true,
                            position: 'left',
                            title: {
                                display: true,
                                text: 'TVOC (mg/m³)',
                                color: '#FF6B6B'
                            },
                            grid: {
                                color: 'rgba(255, 107, 107, 0.1)'
                            },
                            min: 0,
                            max: 2
                        },
                        y1: {
                            type: 'linear',
                            display: true,
                            position: 'right',
                            title: {
                                display: true,
                                text: 'Nhiệt độ (°C) / Độ ẩm (%)',
                                color: '#4ECDC4'
                            },
                            grid: {
                                drawOnChartArea: false,
                                color: 'rgba(78, 205, 196, 0.1)'
                            },
                            min: 0,
                            max: 100
                        }
                    },
                    plugins: {
                        legend: {
                            display: true,
                            position: 'top',
                            labels: {
                                color: '#333',
                                usePointStyle: true,
                                padding: 20
                            }
                        },
                        tooltip: {
                            backgroundColor: 'rgba(0,0,0,0.8)',
                            titleColor: 'white',
                            bodyColor: 'white',
                            borderColor: 'rgba(255,255,255,0.2)',
                            borderWidth: 1
                        }
                    },
                    animation: {
                        duration: 750
                    }
                }
            });
            
            console.log('✅ Biểu đồ đã được khởi tạo thành công');
        }

        // Cập nhật biểu đồ với dữ liệu real-time
        function updateChart(data) {
            const now = data.timestamp || new Date().toLocaleTimeString();
            
            // Thêm dữ liệu mới
            chartData.labels.push(now);
            chartData.tvocData.push(data.tvoc);
            chartData.tempData.push(data.temperature);
            chartData.humidityData.push(data.humidity);
            
            // Giới hạn số điểm hiển thị (10 điểm gần nhất)
            if (chartData.labels.length > 10) {
                console.log('Giới hạn dữ liệu, xóa điểm cũ:', chartData.labels.length);
                chartData.labels.shift();
                chartData.tvocData.shift();
                chartData.tempData.shift();
                chartData.humidityData.shift();
            }
            
            // Cập nhật dữ liệu biểu đồ
            sensorChart.data.labels = chartData.labels;
            sensorChart.data.datasets[0].data = chartData.tvocData;
            sensorChart.data.datasets[1].data = chartData.tempData;
            sensorChart.data.datasets[2].data = chartData.humidityData;
            
            // Cập nhật biểu đồ với animation mượt
            sensorChart.update('active');
            
            console.log('📈 Biểu đồ đã được cập nhật:', {
                time: now,
                tvoc: data.tvoc,
                temp: data.temperature,
                humidity: data.humidity
            });
        }

        // Load dữ liệu ban đầu
        function loadInitialData() {
            // Load dữ liệu hiện tại
            fetch('/api/current-data')
                .then(response => response.json())
                .then(data => {
                    console.log('📥 Dữ liệu hiện tại:', data);
                    updateSensorDisplay({
                        tvoc: data.tvoc,
                        temperature: data.temperature,
                        humidity: data.humidity,
                        timestamp: new Date(data.timestamp).toLocaleTimeString()
                    });
                    
                    // Cập nhật form ngưỡng
                    if (data.thresholds) {
                        updateThresholdForm(data.thresholds);
                    }
                })
                .catch(error => {
                    console.error('❌ Lỗi load dữ liệu hiện tại:', error);
                });

            // Load dữ liệu lịch sử cho biểu đồ
            fetch('/api/history?hours=1')
                .then(response => response.json())
                .then(data => {
                    console.log('📈 Dữ liệu lịch sử:', data);
                    
                    // Reset chart data
                    chartData.labels = [];
                    chartData.tvocData = [];
                    chartData.tempData = [];
                    chartData.humidityData = [];
                    
                    // Nếu có dữ liệu lịch sử, thêm vào biểu đồ
                    if (data && data.length > 0) {
                        const recentData = data.slice(-5); // Chỉ lấy 5 điểm cuối
                        recentData.forEach(item => {
                            const time = new Date(item.timestamp).toLocaleTimeString();
                            chartData.labels.push(time);
                            chartData.tvocData.push(item.tvoc);
                            chartData.tempData.push(item.temperature);
                            chartData.humidityData.push(item.humidity);
                        });
                    } else {
                        // Nếu không có dữ liệu lịch sử, tạo 1 điểm mặc định
                        const now = new Date().toLocaleTimeString();
                        chartData.labels.push(now);
                        chartData.tvocData.push(0);
                        chartData.tempData.push(25);
                        chartData.humidityData.push(50);
                    }
                    
                    // Cập nhật biểu đồ
                    sensorChart.data.labels = chartData.labels;
                    sensorChart.data.datasets[0].data = chartData.tvocData;
                    sensorChart.data.datasets[1].data = chartData.tempData;
                    sensorChart.data.datasets[2].data = chartData.humidityData;
                    sensorChart.update();
                })
                .catch(error => {
                    console.error('❌ Lỗi load dữ liệu lịch sử:', error);
                    // Tạo dữ liệu mặc định nếu lỗi
                    const now = new Date().toLocaleTimeString();
                    chartData.labels = [now];
                    chartData.tvocData = [0];
                    chartData.tempData = [25];
                    chartData.humidityData = [50];
                    
                    sensorChart.data.labels = chartData.labels;
                    sensorChart.data.datasets[0].data = chartData.tvocData;
                    sensorChart.data.datasets[1].data = chartData.tempData;
                    sensorChart.data.datasets[2].data = chartData.humidityData;
                    sensorChart.update();
                });
        }

        // Thiết lập form cập nhật ngưỡng
        function setupThresholdForm() {
            const form = document.getElementById('thresholdForm');
            
            form.addEventListener('submit', function(e) {
                e.preventDefault(); // Ngăn form submit mặc định
                console.log('📝 Đang cập nhật ngưỡng...');
                
                // Lấy dữ liệu từ form
                const formData = {
                    tvoc_max: parseFloat(document.getElementById('tvocMax').value),
                    temp_min: parseFloat(document.getElementById('tempMin').value),
                    temp_max: parseFloat(document.getElementById('tempMax').value),
                    humidity_min: parseFloat(document.getElementById('humidityMin').value),
                    humidity_max: parseFloat(document.getElementById('humidityMax').value)
                };
                
                console.log('📤 Dữ liệu gửi:', formData);
                
                // Gửi POST request
                fetch('/api/thresholds', {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/json',
                    },
                    body: JSON.stringify(formData)
                })
                .then(response => response.json())
                .then(data => {
                    console.log('✅ Cập nhật thành công:', data);
                    
                    if (data.success) {
                        // Hiển thị thông báo thành công
                        showSuccessMessage();
                        
                        // Cập nhật lại form với dữ liệu mới
                        updateThresholdForm(data.thresholds);
                    } else {
                        console.error('❌ Lỗi cập nhật:', data.error);
                        alert('Lỗi cập nhật ngưỡng: ' + data.error);
                    }
                })
                .catch(error => {
                    console.error('❌ Lỗi kết nối:', error);
                    alert('Lỗi kết nối khi cập nhật ngưỡng');
                });
            });
        }

        // Cập nhật form ngưỡng
        function updateThresholdForm(thresholds) {
            document.getElementById('tvocMax').value = thresholds.tvoc_max;
            document.getElementById('tempMin').value = thresholds.temp_min;
            document.getElementById('tempMax').value = thresholds.temp_max;
            document.getElementById('humidityMin').value = thresholds.humidity_min;
            document.getElementById('humidityMax').value = thresholds.humidity_max;
        }

        // Hiển thị thông báo thành công
        function showSuccessMessage() {
            const message = document.getElementById('successMessage');
            message.style.display = 'block';
            
            setTimeout(() => {
                message.style.display = 'none';
            }, 3000);
        }

        // Hiển thị cảnh báo
        function showAlerts(alerts) {
            const container = document.getElementById('alertsContainer');
            container.innerHTML = '';
            
            alerts.forEach(alert => {
                const alertEl = document.createElement('div');
                alertEl.className = `alert alert-${alert.severity}`;
                alertEl.innerHTML = `
                    <span class="icon">${alert.severity === 'danger' ? '🚨' : '⚠️'}</span>
                    ${alert.message}
                `;
                container.appendChild(alertEl);
            });
        }
    </script>
</body>
</html>
"""

# Routes
@app.route('/')
def dashboard():
    return render_template_string(HTML_TEMPLATE)

@app.route('/api/current-data')
def api_current_data():
    return jsonify({
        'tvoc': current_data['tvoc'],
        'temperature': current_data['temperature'],
        'humidity': current_data['humidity'],
        'timestamp': current_data['timestamp'].strftime('%Y-%m-%d %H:%M:%S'),
        'thresholds': thresholds
    })

@app.route('/api/history')
def api_history():
    hours = request.args.get('hours', 24, type=int)
    data = db.get_recent_data(hours)
    return jsonify(data)

@app.route('/api/thresholds', methods=['GET', 'POST'])
def api_thresholds():
    if request.method == 'POST':
        try:
            new_thresholds = request.get_json()
            
            # Validate dữ liệu
            required_keys = ['tvoc_max', 'temp_min', 'temp_max', 'humidity_min', 'humidity_max']
            for key in required_keys:
                if key not in new_thresholds:
                    return jsonify({'error': f'Thiếu trường {key}'}), 400
                thresholds[key] = float(new_thresholds[key])
            
            # Lưu vào database
            db.update_thresholds(thresholds)
            
            # Gửi thông báo cập nhật qua WebSocket
            socketio.emit('thresholds_updated', thresholds)
            
            logger.info(f"Cập nhật ngưỡng thành công: {thresholds}")
            
            return jsonify({'success': True, 'thresholds': thresholds})
            
        except Exception as e:
            logger.error(f"Lỗi cập nhật ngưỡng: {e}")
            return jsonify({'error': str(e)}), 500
    
    return jsonify(thresholds)

@app.route('/api/test-alert')
def test_alert():
    """API test gửi cảnh báo"""
    message = "🧪 TEST ALERT - Hệ thống hoạt động bình thường!"
    send_telegram_alert(message)
    return jsonify({'message': 'Test alert sent'})

# SocketIO Events
@socketio.on('connect')
def on_socketio_connect():
    logger.info(f"Client kết nối SocketIO: {request.sid}")
    
    # Gửi dữ liệu hiện tại cho client mới kết nối
    emit('sensor_data', {
        'tvoc': current_data['tvoc'],
        'temperature': current_data['temperature'],
        'humidity': current_data['humidity'],
        'timestamp': current_data['timestamp'].strftime('%H:%M:%S'),
        'alerts': []
    })

@socketio.on('disconnect')
def on_socketio_disconnect():
    logger.info(f"Client ngắt kết nối SocketIO: {request.sid}")

if __name__ == '__main__':
    # Chạy MQTT trong thread riêng
    mqtt_thread = threading.Thread(target=start_mqtt)
    mqtt_thread.daemon = True
    mqtt_thread.start()
    
    logger.info("🚀 Khởi động TVOC Monitoring Server...")
    logger.info("📊 Dashboard: http://localhost:5000")
    logger.info("📡 MQTT Topic: " + MQTT_TOPIC)
    
    # Chạy Flask với SocketIO
    socketio.run(app, host='0.0.0.0', port=5000, debug=False)