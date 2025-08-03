#!/usr/bin/env python3
"""
Script test gửi dữ liệu giả để test hệ thống
Chạy song song với server1.py để xem dashboard hoạt động
"""

import paho.mqtt.client as mqtt
import json
import time
import random
from datetime import datetime

# Cấu hình MQTT
MQTT_BROKER = 'localhost'
MQTT_PORT = 1883
MQTT_TOPIC = 'sensors/data'

def generate_fake_data():
    """Tạo dữ liệu cảm biến giả"""
    # TVOC: dao động từ 0.1-2 mg/m³ để tránh giá trị quá nhỏ
    tvoc = random.uniform(0.1, 2)
    print(f"TVOC thô trước khi làm tròn: {tvoc}")  # Debug
    scenario = random.randint(1, 10)
    if scenario == 1:  # TVOC cao
        tvoc = random.uniform(0.5, 3)
        print(f"TVOC cao (scenario 1): {tvoc}")  # Debug
    tvoc_rounded = round(tvoc, 2)
    print(f"TVOC sau khi làm tròn: {tvoc_rounded}")  # Debug
    return {
        'tvoc': tvoc_rounded,
        'temperature': round(random.uniform(20, 35), 1),
        'humidity': round(random.uniform(30, 80), 1),
        'timestamp': datetime.now().isoformat()
    }

def on_connect(client, userdata, flags, rc):
    if rc == 0:
        print("✅ Kết nối MQTT thành công")
    else:
        print(f"❌ Lỗi kết nối MQTT: {rc}")

def main():
    print("🧪 Bắt đầu test gửi dữ liệu giả...")
    print("📡 MQTT Broker:", MQTT_BROKER)
    print("📊 Topic:", MQTT_TOPIC)
    print("⏱️  Gửi dữ liệu mỗi 5 giây")
    print("🛑 Nhấn Ctrl+C để dừng")
    print("-" * 50)
    
    # Khởi tạo MQTT client
    client = mqtt.Client()
    client.on_connect = on_connect
    
    try:
        client.connect(MQTT_BROKER, MQTT_PORT, 60)
        client.loop_start()
        
        counter = 1
        while True:
            # Tạo dữ liệu giả
            data = generate_fake_data()
            
            # Chuyển thành JSON
            json_data = json.dumps(data)
            print(f"JSON gửi đi: {json_data}")  # Debug
            
            # Gửi qua MQTT
            result = client.publish(MQTT_TOPIC, json_data)
            
            if result.rc == 0:
                print(f"📤 [{counter:03d}] Sent: TVOC={data['tvoc']}mg/m³, T={data['temperature']}°C, H={data['humidity']}%")
                
                # Hiển thị cảnh báo nếu có
                alerts = []
                if data['tvoc'] > 0.5:
                    alerts.append(f"🚨 TVOC cao ({data['tvoc']}mg/m³)")
                if data['temperature'] > 30:
                    alerts.append(f"🔥 Nhiệt độ cao ({data['temperature']}°C)")
                if data['humidity'] > 70:
                    alerts.append(f"💧 Độ ẩm cao ({data['humidity']}%)")
                
                if alerts:
                    print(f"     ⚠️  {', '.join(alerts)}")
                    
            else:
                print(f"❌ [{counter:03d}] Lỗi gửi dữ liệu")
            
            counter += 1
            time.sleep(5)  # Gửi mỗi 5 giây
            
    except KeyboardInterrupt:
        print("\n🛑 Dừng test...")
    except Exception as e:
        print(f"❌ Lỗi: {e}")
    finally:
        client.loop_stop()
        client.disconnect()
        print("👋 Đã ngắt kết nối MQTT")

if __name__ == "__main__":
    main()