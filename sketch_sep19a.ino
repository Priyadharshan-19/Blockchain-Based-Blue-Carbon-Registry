// ------------------- Blynk Credentials -------------------
// Replace with your own Blynk credentials
#define BLYNK_TEMPLATE_ID   "REPLACE_WITH_YOUR_TEMPLATE_ID"
#define BLYNK_TEMPLATE_NAME "REPLACE_WITH_YOUR_TEMPLATE_NAME"
#define BLYNK_AUTH_TOKEN    "REPLACE_WITH_YOUR_BLYNK_AUTH_TOKEN"

// ------------------- Libraries -------------------
#include <WiFi.h>
#include <WiFiClient.h>
#include <HTTPClient.h>
#include <BlynkSimpleEsp32.h>
#include "DHT.h"
#include <ArduinoJson.h>

// ------------------- WiFi Credentials -------------------
// Replace with your WiFi SSID and password
const char* ssid     = "REPLACE_WITH_YOUR_WIFI_SSID";
const char* password = "REPLACE_WITH_YOUR_WIFI_PASSWORD";

// ------------------- Flask Server Endpoint -------------------
// Replace with your Flask server URL
const char* serverUrl = "http://REPLACE_WITH_YOUR_SERVER_IP:5000/api/upload";

// ------------------- Sensor Setup -------------------
#define DHT22TYPE DHT22
#define DHT11TYPE DHT11

// DHT pins/types per sensor: A=DHT22@4, B=DHT22@16, C=DHT11@17
const int   DHT_PINS[3]  = {4, 16, 17};
const int   DHT_TYPES[3] = {DHT22TYPE, DHT22TYPE, DHT11TYPE};

DHT dhts[3] = {
  DHT(DHT_PINS[0], DHT_TYPES[0]),
  DHT(DHT_PINS[1], DHT_TYPES[1]),
  DHT(DHT_PINS[2], DHT_TYPES[2])
};

// Soil sensor ADC1 pins (WiFi-safe ADC1 only)
const int SOIL_PINS[3] = {34, 35, 32};

// Optional: per-sensor soil calibration (raw ADC)
const int SOIL_DRY[3] = {4050, 4080, 4030};
const int SOIL_WET[3] = {1150, 1200, 1180};

// Status LEDs
#define FAILURE_LED_PIN 2   // Failure LED
#define SUCCESS_LED_PIN 5   // Success LED

// ------------------- Globals -------------------
WiFiClient wifiClient;
HTTPClient http;
BlynkTimer timer;

const uint32_t SENSOR_INTERVAL_MS = 3000;   // ≥2s for DHT22/11
const uint32_t BLYNK_INTERVAL_MS  = 3000;
const uint32_t FLASK_INTERVAL_MS  = 60000;

float lastTemp[3] = {NAN, NAN, NAN};
float lastHum[3]  = {NAN, NAN, NAN};
float lastSoil[3] = {NAN, NAN, NAN};
float lastCO2x[3] = {NAN, NAN, NAN};

bool sensorFailed  = false;
bool sensorSuccess = false;

// Blynk virtual pins base blocks per sensor:
// A: V0..V3, B: V4..V7, C: V8..V11
const uint8_t VP_BASE[3] = {0, 4, 8};

// ------------------- WiFi + Blynk helpers -------------------
void connectWifi() {
  if (WiFi.status() == WL_CONNECTED) return;
  WiFi.mode(WIFI_STA);
  WiFi.begin(ssid, password);

  Serial.print("Connecting to WiFi");
  unsigned long start = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - start < 20000) {
    delay(500);
    Serial.print(".");
  }
  Serial.println();

  if (WiFi.status() == WL_CONNECTED) {
    Serial.print("✅ WiFi Connected, IP: ");
    Serial.println(WiFi.localIP());
  } else {
    Serial.println("❌ WiFi connect timeout");
  }
}

void ensureBlynk() {
  if (!Blynk.connected() && WiFi.status() == WL_CONNECTED) {
    Blynk.connect(5000);
  }
}

// ------------------- Helpers -------------------
float soilToPercentCal(int raw, int idx) {
  raw = constrain(raw, 0, 4095);
  int dry = SOIL_DRY[idx];
  int wet = SOIL_WET[idx];
  float pct = (float)(dry - raw) / (float)(dry - wet) * 100.0f;
  return constrain(pct, 0.0f, 100.0f);
}

float co2Proxy(float tempC, float humPct, float soilPct) {
  // Demo-only proxy index: 0..100
  float val = 30.0f + 0.2f * humPct + 0.4f * soilPct - 0.3f * tempC;
  return constrain(val, 0.0f, 100.0f);
}

// ------------------- Sensor reading task -------------------
void readSensorsTask() {
  sensorFailed  = false;
  sensorSuccess = false;

  for (int i = 0; i < 3; i++) {
    float h = dhts[i].readHumidity();
    float t = dhts[i].readTemperature();
    int   soilRaw  = analogRead(SOIL_PINS[i]);
    float soilPct  = soilToPercentCal(soilRaw, i);
    float co2Index = NAN;

    if (!isnan(h) && !isnan(t)) {
      lastTemp[i] = t;
      lastHum[i]  = h;
      lastSoil[i] = soilPct;
      lastCO2x[i] = co2Index = co2Proxy(t, h, soilPct);
      sensorSuccess = true;

      Serial.printf("Sensor %c -> 🌡 %.2f°C | 💧 %.2f%% | 🌱 %.2f%% | CO2x %.2f\n",
                    'A' + i, t, h, soilPct, co2Index);

      // 🚨 Check soil = 0%
      if (soilPct <= 0.0f) {
        sensorFailed = true;
        Serial.printf("⚠️ Soil moisture 0%% detected on sensor %c\n", 'A' + i);
      }

    } else {
      sensorFailed = true;
      Serial.printf("❌ DHT read failed on sensor %c (GPIO %d, Type %s)\n",
                    'A' + i, DHT_PINS[i], (DHT_TYPES[i] == DHT22TYPE ? "DHT22" : "DHT11"));
    }
  }

  // LED status: steady red if failure, blink green if success
  digitalWrite(FAILURE_LED_PIN, sensorFailed ? HIGH : LOW);
  if (sensorSuccess && !sensorFailed) {
    digitalWrite(SUCCESS_LED_PIN, HIGH);
    delay(200);
    digitalWrite(SUCCESS_LED_PIN, LOW);
  }
}

// ------------------- Blynk push task -------------------
void pushBlynkTask() {
  if (!Blynk.connected()) return;

  for (int i = 0; i < 3; i++) {
    if (isnan(lastTemp[i]) || isnan(lastHum[i]) || isnan(lastSoil[i]) || isnan(lastCO2x[i])) continue;
    uint8_t base = VP_BASE[i];
    Blynk.virtualWrite(base + 0, lastTemp[i]);
    Blynk.virtualWrite(base + 1, lastHum[i]);
    Blynk.virtualWrite(base + 2, lastSoil[i]);
    Blynk.virtualWrite(base + 3, lastCO2x[i]);
  }
}

// ------------------- Flask POST task -------------------
void postFlaskTask() {
  if (WiFi.status() != WL_CONNECTED) {
    Serial.println("⚠️ WiFi Disconnected, skipping Flask POST");
    return;
  }

  StaticJsonDocument<768> doc;
  JsonArray arr = doc.to<JsonArray>();
  const char* area_ids[3] = {"A001","A002","A003"};
  uint32_t dev_ts = millis() / 1000;

  bool hasData = false;

  for (int i = 0; i < 3; i++) {
    if (isnan(lastTemp[i]) || isnan(lastHum[i]) || isnan(lastSoil[i]) || isnan(lastCO2x[i])) continue;

    hasData = true;
    JsonObject item = arr.createNestedObject();
    item["area_id"]       = area_ids[i];
    item["temperature"]   = roundf(lastTemp[i] * 100) / 100.0f;
    item["humidity"]      = roundf(lastHum[i] * 100) / 100.0f;
    item["soil_moisture"] = roundf(lastSoil[i] * 100) / 100.0f;
    item["co2_proxy"]     = roundf(lastCO2x[i] * 100) / 100.0f;
    item["ts"]            = dev_ts;
  }

  if (!hasData) {
    Serial.println("ℹ️ No valid sensor readings to POST");
    return;
  }

  String payload;
  serializeJson(arr, payload);

  http.setTimeout(7000);
  http.setReuse(true);
  if (http.begin(wifiClient, serverUrl)) {
    http.addHeader("Content-Type", "application/json");
    int code = http.POST(payload);
    if (code <= 0) {
      delay(500);
      code = http.POST(payload);
    }
    if (code > 0) {
      Serial.printf("✅ Flask POST Success, code: %d, bytes: %d\n", code, payload.length());
    } else {
      Serial.printf("❌ Flask POST Failed, code: %d\n", code);
    }
    http.end();
  } else {
    Serial.println("❌ HTTP begin() failed");
  }
}

// ------------------- Blynk events -------------------
BLYNK_CONNECTED()    { Serial.println("✅ Blynk Connected"); }
BLYNK_DISCONNECTED() { Serial.println("⚠️ Blynk Disconnected"); }

// ------------------- Setup / Loop -------------------
void setup() {
  Serial.begin(115200);

  // Init sensors
  for (int i = 0; i < 3; i++) dhts[i].begin();
  for (int i = 0; i < 3; i++) pinMode(SOIL_PINS[i], INPUT);

  // LEDs
  pinMode(FAILURE_LED_PIN, OUTPUT);
  pinMode(SUCCESS_LED_PIN, OUTPUT);
  digitalWrite(FAILURE_LED_PIN, LOW);
  digitalWrite(SUCCESS_LED_PIN, LOW);

  // Network / Blynk
  connectWifi();
  Blynk.config(BLYNK_AUTH_TOKEN);
  ensureBlynk();

  // Timers
  timer.setInterval(SENSOR_INTERVAL_MS, readSensorsTask);
  timer.setInterval(BLYNK_INTERVAL_MS,  pushBlynkTask);
  timer.setInterval(FLASK_INTERVAL_MS,  postFlaskTask);

  // Keepalive
  timer.setInterval(15000L, []() {
    connectWifi();
    ensureBlynk();
  });
}

void loop() {
  if (Blynk.connected()) Blynk.run();
  timer.run();
}
