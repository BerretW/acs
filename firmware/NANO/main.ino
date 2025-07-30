#include <Arduino.h>
#include <Wiegand.h>
#include <ArduinoJson.h>

#define D0_PIN 2
#define D1_PIN 3
#define LED_GREEN 4
#define LED_RED 5
#define BUZZER 6

#define HUB_ADDRESS 1
#define READER_ID 1

WIEGAND wg;
String inputBuffer;

String calculateChecksum(const String& payload) {
  byte checksum = 0;
  for (unsigned int i = 0; i < payload.length(); i++) {
    checksum ^= payload[i];
  }
  char hex[3];
  sprintf(hex, "%02X", checksum);
  return String(hex);
}

void sendMessage(uint32_t cardCode, byte bits) {
  StaticJsonDocument<128> doc;
  doc["type"] = "card_read";
  doc["hub_addr"] = HUB_ADDRESS;
  doc["rdr_id"] = READER_ID;
  doc["card"] = String(cardCode);
  doc["bits"] = bits;

  String json;
  serializeJson(doc, json);
  String chk = calculateChecksum(json);

  Serial.print("<");
  Serial.print(json);
  Serial.print(">|");
  Serial.print(chk);
  Serial.print("\n");
}

bool checkParity(uint32_t code, byte bits) {
  if (bits != 26) return true;
  uint32_t first13 = (code >> 13) & 0x1FFF;
  uint32_t last13 = code & 0x1FFF;
  return (__builtin_popcount(first13) % 2 == 0) && (__builtin_popcount(last13) % 2 == 1);
}

void handleCommand(const String& payload) {
  StaticJsonDocument<128> doc;
  DeserializationError err = deserializeJson(doc, payload);
  if (err) return;

  if (doc["hub_addr"].as<int>() != HUB_ADDRESS) return;
  if (doc["type"].as<String>() != "command") return;
  if (doc["rdr_id"].as<int>() != READER_ID) return;

  String cmd = doc["cmd"].as<String>();
  if (cmd == "feedback_grant") {
    digitalWrite(LED_GREEN, HIGH);
    digitalWrite(LED_RED, LOW);
    digitalWrite(BUZZER, HIGH);
    delay(250);
    digitalWrite(BUZZER, LOW);
    delay(1500);
    digitalWrite(LED_GREEN, LOW);
  } else if (cmd == "feedback_deny") {
    digitalWrite(LED_GREEN, LOW);
    digitalWrite(LED_RED, HIGH);
    digitalWrite(BUZZER, HIGH);
    delay(150);
    digitalWrite(BUZZER, LOW);
    delay(100);
    digitalWrite(BUZZER, HIGH);
    delay(150);
    digitalWrite(BUZZER, LOW);
    delay(1500);
    digitalWrite(LED_RED, LOW);
  }
}

void setup() {
  Serial.begin(115200);
  while (!Serial) delay(10);

  pinMode(LED_GREEN, OUTPUT);
  pinMode(LED_RED, OUTPUT);
  pinMode(BUZZER, OUTPUT);
  digitalWrite(LED_GREEN, LOW);
  digitalWrite(LED_RED, LOW);
  digitalWrite(BUZZER, LOW);

  wg.begin(D0_PIN, D1_PIN);
  Serial.println("ACS Nano připraven.");
}

void loop() {
  if (wg.available()) {
    uint32_t code = wg.getCode();
    byte bits = wg.getWiegandType();

    if (checkParity(code, bits)) {
      sendMessage(code, bits);
    } else {
      Serial.println("Chyba parity.");
    }
  }

  while (Serial.available()) {
    char c = Serial.read();
    if (c == '\n') {
      inputBuffer.trim();
      int sep = inputBuffer.lastIndexOf('|');
      if (inputBuffer.startsWith("<") && sep > 0 && !inputBuffer.endsWith(">")) {
        String payload = inputBuffer.substring(1, sep - 1);
        String receivedChecksum = inputBuffer.substring(sep + 1);
        String expectedChecksum = calculateChecksum(payload);
        if (expectedChecksum.equalsIgnoreCase(receivedChecksum)) {
          handleCommand(payload);
        } else {
          Serial.print("Neplatný checksum: ");
          Serial.println(inputBuffer);
        }
      }
      inputBuffer = "";
    } else {
      inputBuffer += c;
    }
  }
}
