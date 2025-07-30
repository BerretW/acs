#include <Arduino.h>
#include <Wiegand.h>
#include <ArduinoJson.h>
#include <EEPROM.h>

// --- KONFIGURACE ---
#define D0_PIN 2
#define D1_PIN 3
#define LED_GREEN 4
#define LED_RED 5
#define BUZZER 6

// !!! DŮLEŽITÉ: TOTO ID ZMĚŇTE PRO KAŽDÝ KUS HARDWARU !!!
// Můžete použít generátor hesel nebo GUID pro inspiraci.
#define UNIQUE_ID "NANO-2A4B-C5D6-E7F8" 

// Adresy v EEPROM pro uložení adresy sběrnice
#define EEPROM_ADDR_MAGIC 0
#define EEPROM_ADDR_HUB 1
#define EEPROM_MAGIC_VALUE 0xAC // "Magická" hodnota značící platnou konfiguraci

// --- GLOBÁLNÍ OBJEKTY A PROMĚNNÉ ---
WIEGAND wg;
String inputBuffer = "";
byte hubAddress = 0; // Aktuální adresa zařízení (0 = nekonfigurováno)

// Pro neblokující heartbeat
unsigned long previousHeartbeatMillis = 0;
const long heartbeatInterval = 30000; // 30 sekund

// Stavový automat pro neblokující signalizaci (zůstává stejný)
enum FeedbackState { STATE_IDLE, STATE_GRANT_START, STATE_GRANT_BUZZ_OFF, STATE_GRANT_END, STATE_DENY_START, STATE_DENY_BUZZ_OFF_1, STATE_DENY_BUZZ_ON_2, STATE_DENY_BUZZ_OFF_2, STATE_DENY_END };
FeedbackState currentFeedbackState = STATE_IDLE;
unsigned long feedbackTimer = 0;

// Prototypy funkcí
void handleCommand(const String& payload);
String calculateChecksum(const String& payload);
void sendJsonMessage(const JsonDocument& doc);


// --- FUNKCE PRO EEPROM ---
void loadAddressFromEEPROM() {
  if (EEPROM.read(EEPROM_ADDR_MAGIC) == EEPROM_MAGIC_VALUE) {
    hubAddress = EEPROM.read(EEPROM_ADDR_HUB);
    Serial.println("Info: Adresa nactena z EEPROM: " + String(hubAddress));
  } else {
    hubAddress = 0;
    Serial.println("Info: Zadna platna adresa v EEPROM, pouzivam adresu 0 (ceka na konfiguraci).");
  }
}

void saveAddressToEEPROM(byte newAddress) {
  EEPROM.write(EEPROM_ADDR_HUB, newAddress);
  EEPROM.write(EEPROM_ADDR_MAGIC, EEPROM_MAGIC_VALUE);
  hubAddress = newAddress;
  Serial.println("Info: Nova adresa " + String(newAddress) + " ulozena do EEPROM.");
}

// --- FUNKCE PROTOKOLU ---
String calculateChecksum(const String& payload) {
  byte checksum = 0;
  for (unsigned int i = 0; i < payload.length(); i++) {
    checksum ^= payload[i];
  }
  char hex[3];
  sprintf(hex, "%02X", checksum);
  return String(hex);
}

void sendJsonMessage(const JsonDocument& doc) {
  String json;
  serializeJson(doc, json);
  String chk = calculateChecksum(json);

  Serial.print("<");
  Serial.print(json);
  Serial.print(">|");
  Serial.print(chk);
  Serial.print("\n");
}

void sendCardReadMessage(uint32_t cardCode, byte bits) {
  if (hubAddress == 0) return; // Nekonfigurované zařízení neposílá data o kartách

  StaticJsonDocument<128> doc;
  doc["type"] = "card_read";
  doc["hub_addr"] = hubAddress;
  doc["rdr_id"] = 1; // Na Nano je vždy 1
  doc["card"] = String(cardCode);
  doc["bits"] = bits;
  sendJsonMessage(doc);
}

bool checkParity(uint32_t code, byte bits) {
  if (bits != 26) return true;
  uint32_t first13 = (code >> 13) & 0x1FFF;
  uint32_t last13 = code & 0x1FFF;
  return (__builtin_popcount(first13) % 2 == 0) && (__builtin_popcount(last13) % 2 == 1);
}

void handleCommand(const String& payload) {
  StaticJsonDocument<192> doc; // Zvětšený buffer pro delší příkazy
  DeserializationError err = deserializeJson(doc, payload);
  if (err) return;

  String cmd = doc["cmd"].as<String>();
  String type = doc["type"].as<String>();

  // Příkaz pro změnu adresy je speciální - reaguje na UID
  if (type == "command" && cmd == "set_address") {
    String targetUid = doc["target_uid"].as<String>();
    if (targetUid.equalsIgnoreCase(UNIQUE_ID)) {
      byte newAddr = doc["new_addr"].as<byte>();
      saveAddressToEEPROM(newAddr);

      StaticJsonDocument<128> ackDoc;
      ackDoc["type"] = "ack_set_address";
      ackDoc["status"] = "success";
      ackDoc["uid"] = UNIQUE_ID;
      ackDoc["new_addr"] = newAddr;
      sendJsonMessage(ackDoc);
      
      delay(100); // Dát čas na odeslání odpovědi
      void(*resetFunc)(void) = 0; // Deklarace ukazatele na reset funkci
      resetFunc(); // Reset Arduina pro aplikaci nové adresy
    }
    return;
  }
  
  // Příkaz identify je také speciální - reaguje vždy
  if (type == "command" && cmd == "identify") {
      StaticJsonDocument<128> responseDoc;
      responseDoc["type"] = "nano";
      responseDoc["uid"] = UNIQUE_ID;
      responseDoc["hub_addr"] = hubAddress;
      responseDoc["readers"] = 1;
      sendJsonMessage(responseDoc);
      return;
  }

  // Ostatní příkazy se zpracují, jen pokud sedí adresa (a není 0)
  if (hubAddress == 0 || doc["hub_addr"].as<int>() != hubAddress) return;
  if (type != "command" || doc["rdr_id"].as<int>() != 1) return;
  if (currentFeedbackState != STATE_IDLE) return; // Ignoruj, pokud signalizace běží

  if (cmd == "feedback_grant") {
    currentFeedbackState = STATE_GRANT_START;
  } else if (cmd == "feedback_deny") {
    currentFeedbackState = STATE_DENY_START;
  }
}

// Funkce `updateFeedback()` zůstává identická jako v předchozí verzi
void updateFeedback() {
  if (currentFeedbackState == STATE_IDLE) return;
  unsigned long currentMillis = millis();
  switch (currentFeedbackState) {
    case STATE_GRANT_START:
      digitalWrite(LED_GREEN, HIGH); digitalWrite(LED_RED, LOW); digitalWrite(BUZZER, HIGH);
      feedbackTimer = currentMillis; currentFeedbackState = STATE_GRANT_BUZZ_OFF; break;
    case STATE_GRANT_BUZZ_OFF:
      if (currentMillis - feedbackTimer >= 250) {
        digitalWrite(BUZZER, LOW); feedbackTimer = currentMillis; currentFeedbackState = STATE_GRANT_END;
      } break;
    case STATE_GRANT_END:
      if (currentMillis - feedbackTimer >= 1500) {
        digitalWrite(LED_GREEN, LOW); currentFeedbackState = STATE_IDLE;
      } break;
    case STATE_DENY_START:
      digitalWrite(LED_GREEN, LOW); digitalWrite(LED_RED, HIGH); digitalWrite(BUZZER, HIGH);
      feedbackTimer = currentMillis; currentFeedbackState = STATE_DENY_BUZZ_OFF_1; break;
    case STATE_DENY_BUZZ_OFF_1:
      if (currentMillis - feedbackTimer >= 150) {
        digitalWrite(BUZZER, LOW); feedbackTimer = currentMillis; currentFeedbackState = STATE_DENY_BUZZ_ON_2;
      } break;
    case STATE_DENY_BUZZ_ON_2:
      if (currentMillis - feedbackTimer >= 100) {
        digitalWrite(BUZZER, HIGH); feedbackTimer = currentMillis; currentFeedbackState = STATE_DENY_BUZZ_OFF_2;
      } break;
    case STATE_DENY_BUZZ_OFF_2:
      if (currentMillis - feedbackTimer >= 150) {
        digitalWrite(BUZZER, LOW); feedbackTimer = currentMillis; currentFeedbackState = STATE_DENY_END;
      } break;
    case STATE_DENY_END:
      if (currentMillis - feedbackTimer >= 1500) {
        digitalWrite(LED_RED, LOW); currentFeedbackState = STATE_IDLE;
      } break;
  }
}

// --- HLAVNÍ FUNKCE ---
void setup() {
  Serial.begin(115200);
  while (!Serial) { delay(10); }

  loadAddressFromEEPROM();

  pinMode(LED_GREEN, OUTPUT);
  pinMode(LED_RED, OUTPUT);
  pinMode(BUZZER, OUTPUT);
  digitalWrite(LED_GREEN, LOW);
  digitalWrite(LED_RED, LOW);
  digitalWrite(BUZZER, LOW);

  wg.begin(D0_PIN, D1_PIN);
  
  StaticJsonDocument<128> bootMsg;
  bootMsg["type"] = "boot";
  bootMsg["msg"] = "ACS Nano ready";
  bootMsg["uid"] = UNIQUE_ID;
  sendJsonMessage(bootMsg);
}

void loop() {
  unsigned long currentMillis = millis();
  
  if (wg.available()) {
    if (checkParity(wg.getCode(), wg.getWiegandType())) {
      sendCardReadMessage(wg.getCode(), wg.getWiegandType());
    } else {
      StaticJsonDocument<128> errDoc;
      errDoc["type"] = "event_error";
      errDoc["hub_addr"] = hubAddress;
      errDoc["rdr_id"] = 1;
      errDoc["error"] = "parity";
      sendJsonMessage(errDoc);
    }
  }

  while (Serial.available()) {
    char c = Serial.read();
    if (c == '\n') {
      inputBuffer.trim();
      if (inputBuffer.startsWith("<")) {
        int sepIndex = inputBuffer.lastIndexOf('|');
        if (sepIndex > 0) {
          String payloadPart = inputBuffer.substring(0, sepIndex);
          if (payloadPart.endsWith(">")) {
            String payload = payloadPart.substring(1, payloadPart.length() - 1);
            String receivedChecksum = inputBuffer.substring(sepIndex + 1);
            if (calculateChecksum(payload).equalsIgnoreCase(receivedChecksum)) {
              handleCommand(payload);
            }
          }
        }
      }
      inputBuffer = "";
    } else {
      if (inputBuffer.length() < 256) { inputBuffer += c; }
    }
  }

  if (hubAddress != 0 && currentMillis - previousHeartbeatMillis >= heartbeatInterval) {
    previousHeartbeatMillis = currentMillis;
    StaticJsonDocument<64> doc;
    doc["type"] = "heartbeat";
    doc["hub_addr"] = hubAddress;
    sendJsonMessage(doc);
  }

  updateFeedback();
}