#include <WiFi.h>
#include <PubSubClient.h>
#include <ArduinoJson.h>

#define FAN_PIN 13

#define TVOC_THRESHOLD 220
#define eCO2_THRESHOLD 800
#define AQI_THRESHOLD 3

// =============== WiFi Configuration ===============
const char *ssid = "SSIoT-02";
const char *password = "SSIoT-02";
// const char *ssid = "K70";
// const char *password = "77777777";
// const char *ssid = "VIETTEL_03012024";
// const char *password = "Wifitang345@";
// const char *ssid = "KomLab_2";
// const char *password = "komedavnlab";

// =============== MQTT Configuration ===============
const char *mqtt_server = "192.168.72.156";
// const char *mqtt_server = "broker.emqx.io";
const int mqtt_port = 1883;
const char *mqtt_topic = "sensors/bedroom";
// const char *mqtt_topic = "sensors/workingroom";

WiFiClient espClient;
PubSubClient client(espClient);

// ===== Kết nối WiFi =====
void setup_wifi()
{
  Serial.println();
  Serial.print("[WiFi] Connecting to ");
  Serial.println(ssid);

  WiFi.begin(ssid, password);
  // Auto reconnect is set true as default
  // To set auto connect off, use the following function
  //  WiFi.setAutoReconnect(false);

  // Will try for about 10 seconds (20x 500ms)
  int tryDelay = 500;
  int numberOfTries = 20;

  // Wait for the WiFi event
  while (true)
  {
    switch (WiFi.status())
    {
    case WL_NO_SSID_AVAIL:
      Serial.println("[WiFi] SSID not found");
      break;
    case WL_CONNECT_FAILED:
      Serial.print("[WiFi] Failed - WiFi not connected! Reason: ");
      return;
      break;
    case WL_CONNECTION_LOST:
      Serial.println("[WiFi] Connection was lost");
      break;
    case WL_SCAN_COMPLETED:
      Serial.println("[WiFi] Scan is completed");
      break;
    case WL_DISCONNECTED:
      Serial.println("[WiFi] WiFi is disconnected");
      break;
    case WL_CONNECTED:
      Serial.println("[WiFi] WiFi is connected!");
      Serial.print("[WiFi] IP address: ");
      Serial.println(WiFi.localIP());
      return;
      break;
    default:
      Serial.print("[WiFi] WiFi Status: ");
      Serial.println(WiFi.status());
      break;
    }
    delay(tryDelay);

    if (numberOfTries <= 0)
    {
      Serial.print("[WiFi] Failed to connect to WiFi!");
      // Use disconnect function to force stop trying to connect
      WiFi.disconnect();
      return;
    }
    else
    {
      numberOfTries--;
    }
  }
  Serial.println("");
  Serial.println("WiFi connected");
  Serial.println("IP Address: " + WiFi.localIP());
}

// ===== Callback khi nhận MQTT =====
void mqtt_callback(char *topic, byte *payload, unsigned int length)
{
  // Chuyển payload thành String
  String jsonStr;
  jsonStr.reserve(length + 1);
  for (unsigned int i = 0; i < length; i++)
  {
    jsonStr += (char)payload[i];
  }

  Serial.println("Received JSON: " + jsonStr);

  // Parse JSON
  StaticJsonDocument<256> doc; // đủ cho dữ liệu mẫu
  DeserializationError error = deserializeJson(doc, jsonStr);
  if (error)
  {
    Serial.print("deserializeJson() failed: ");
    Serial.println(error.c_str());
    return;
  }

  // Trích xuất dữ liệu
  int tvoc = doc["tvoc"] | -1.0;
  float temperature = doc["temperature"] | -1.0;
  float humidity = doc["humidity"] | -1.0;
  int eco2 = doc["eco2"] | -1;
  int aqi = doc["aqi"] | -1;

  if ((tvoc >= TVOC_THRESHOLD) || (eco2 >= eCO2_THRESHOLD) || (aqi >= AQI_THRESHOLD))
  {
    digitalWrite(FAN_PIN, HIGH);
    Serial.println("Fan ON: High TVOC, eCO2 or AQI detected!");
  }
  else
  {
    digitalWrite(FAN_PIN, LOW);
    Serial.println("Fan OFF: Values are within normal range.");
  }
  // In ra kết quả
  Serial.println("===== Parsed Data =====");
  Serial.printf("TVOC        : %d ppb\n", tvoc);
  Serial.printf("Temperature : %.2f °C\n", temperature);
  Serial.printf("Humidity    : %.2f %%\n", humidity);
  Serial.printf("eCO2        : %d ppm\n", eco2);
  Serial.printf("AQI         : %d\n", aqi);
  Serial.println("=======================");
}

// ===== Reconnect MQTT =====
void mqtt_reconnect()
{
  while (!client.connected())
  {
    Serial.print("Attempting MQTT connection...");
    String clientId = "ESP32-" + String(random(0xffff), HEX);
    if (client.connect(clientId.c_str()))
    {
      Serial.println("connected");
      client.subscribe(mqtt_topic);
    }
    else
    {
      Serial.print("failed, rc=");
      Serial.print(client.state());
      Serial.println(" try again in 2 seconds");
      delay(2000);
    }
  }
}

void setup()
{
  pinMode(FAN_PIN, OUTPUT);
  digitalWrite(FAN_PIN, LOW);
  Serial.begin(115200);
  setup_wifi();

  client.setServer(mqtt_server, mqtt_port);
  client.setCallback(mqtt_callback);

  // Tăng buffer để tránh miss dữ liệu dài
  client.setBufferSize(512);
}

void loop()
{
  if (!client.connected())
  {
    mqtt_reconnect();
  }
  client.loop(); // Quan trọng: xử lý nhận liên tục
}
