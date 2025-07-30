# firmware/rp2040/main.py
"""
Firmware pro ACS Slave modul postavený na Raspberry Pi Pico (RP2040).
- Založeno na robustní asynchronní verzi pro ESP32.
- Podporuje až 4 čtečky/dveře ve výchozí konfiguraci.
- Ukládá konfigurovatelnou adresu do souboru ve flash paměti.
- Identifikuje se pomocí unikátního hardwarového ID čipu.
"""

import uasyncio
from machine import Pin, UART, unique_id, reset
import ujson
import ubinascii
import micropython

# Lokální knihovny (musí být nahrány na RP2040)
import protocol
from pro_wiegand_lib import WiegandController

micropython.alloc_emergency_exception_buf(100)

# --- KONFIGURACE A GLOBÁLNÍ PROMĚNNÉ ---
CONFIG_FILE = 'config.json'
UNIQUE_ID = ubinascii.hexlify(unique_id()).decode('utf-8').upper()

# Defaultní konfigurace, pokud soubor neexistuje
DEFAULT_CONFIG = {
    "HUB_ADDRESS": 0,  # Adresa 0 znamená "nekonfigurováno", čeká na přiřazení
    "UART_BUS": {
        "id": 0, # UART0 na RP2040
        "baudrate": 115200,
        "tx_pin": 0, # GP0
        "rx_pin": 1  # GP1
    },
    "DOORS": [
        {
            "id": 1, "name": "Dvere 1", "d0_pin": 2, "d1_pin": 3,
            "gled_pin": 4, "rled_pin": 5, "buzz_pin": 6,
            "rex_pin": 7, "contact_pin": 8
        },
        {
            "id": 2, "name": "Dvere 2", "d0_pin": 9, "d1_pin": 10,
            "gled_pin": 11, "rled_pin": 12, "buzz_pin": 13,
            "rex_pin": 14, "contact_pin": 15
        },
        {
            "id": 3, "name": "Dvere 3", "d0_pin": 16, "d1_pin": 17,
            "gled_pin": 18, "rled_pin": 19, "buzz_pin": 20,
            "rex_pin": 21, "contact_pin": 22
        },
        {
            # Poznámka: Piny 23, 24, 25, 29 mají na desce Pico W speciální funkce,
            # ale na standardním Pico jsou to běžné GPIO. Na Pico W by měly
            # fungovat, pokud nepoužíváte WiFi/BT. GP25 je palubní LED.
            "id": 4, "name": "Dvere 4", "d0_pin": 26, "d1_pin": 27,
            "gled_pin": 28, "rled_pin": 25, "buzz_pin": 24,
            "rex_pin": 23, "contact_pin": 29
        }
    ]
}
CONFIG = {}

message_queue = []
feedback_pins = {}
input_pins = {}
last_input_states = {}

# --- FUNKCE PRO PRÁCI S KONFIGURACÍ ---
def load_config():
    global CONFIG
    try:
        with open(CONFIG_FILE, 'r') as f:
            loaded_data = ujson.load(f)
        if not isinstance(loaded_data, dict):
            raise TypeError("Konfigurace neni slovnik (dict)")
        CONFIG = loaded_data
        print(f"Konfigurace načtena, adresa sběrnice: {CONFIG.get('HUB_ADDRESS')}")
    except (OSError, ValueError, TypeError) as e:
        print(f"Info: Konfig. soubor nenalezen/poškozen ({e}), používám a ukládám defaultní.")
        CONFIG = DEFAULT_CONFIG
        save_config()

def save_config():
    try:
        with open(CONFIG_FILE, 'w') as f:
            ujson.dump(CONFIG, f)
        print(f"Konfigurace uložena. Nová adresa: {CONFIG.get('HUB_ADDRESS')}")
    except OSError as e:
        print(f"CHYBA: Nepodařilo se uložit konfiguraci: {e}")

# --- CALLBACKY A ASYNCHRONNÍ ÚLOHY ---
# Tyto funkce jsou identické s verzí pro ESP32.

def wiegand_callback(data_tuple):
    hub_addr = CONFIG.get("HUB_ADDRESS", 0)
    if hub_addr == 0: return

    reader_id, card_data, bits = data_tuple
    payload = {
        "type": "card_read", "hub_addr": hub_addr, "rdr_id": reader_id,
        "card": card_data, "bits": bits
    }
    message_queue.append(protocol.create_message(payload))

async def sender_task(writer):
    print("Sender task spuštěn.")
    while True:
        if message_queue:
            message = message_queue.pop(0)
            writer.write(message.encode('utf-8'))
            await writer.drain()
        await uasyncio.sleep_ms(20)

async def command_listener(reader):
    print("Command listener spuštěn.")
    while True:
        raw_line = await reader.readline()
        if not raw_line: continue
        
        parsed_data = protocol.parse_message(raw_line.decode('utf-8'))
        if not parsed_data: continue

        cmd_type = parsed_data.get('type')
        hub_addr = CONFIG.get("HUB_ADDRESS", 0)

        if cmd_type == 'command' and parsed_data.get('cmd') == 'set_address':
            if parsed_data.get('target_uid') == UNIQUE_ID:
                new_addr = parsed_data.get('new_addr')
                CONFIG['HUB_ADDRESS'] = new_addr
                save_config()
                
                ack_payload = {"type": "ack_set_address", "status": "success", "uid": UNIQUE_ID, "new_addr": new_addr}
                message_queue.append(protocol.create_message(ack_payload))
                
                await uasyncio.sleep(1)
                reset()
            continue

        if cmd_type == 'command' and parsed_data.get('cmd') == 'identify':
            ident_payload = {
                "type": "rp2040", # Identifikace typu zařízení
                "uid": UNIQUE_ID, 
                "hub_addr": hub_addr,
                "readers": len(CONFIG.get("DOORS", []))
            }
            message_queue.append(protocol.create_message(ident_payload))
            continue

        if hub_addr == 0 or parsed_data.get('hub_addr') != hub_addr:
            continue
        
        if cmd_type == 'command':
            uasyncio.create_task(handle_feedback_command(parsed_data))

async def handle_feedback_command(cmd_data):
    rdr_id = cmd_data.get('rdr_id')
    cmd = cmd_data.get('cmd')
    pins = feedback_pins.get(rdr_id)
    if not pins: return
    
    if cmd == "feedback_grant":
        pins['gled'].on(); pins['rled'].off(); pins['buzz'].on()
        await uasyncio.sleep_ms(250)
        pins['buzz'].off()
        await uasyncio.sleep_ms(1500)
        pins['gled'].off()
    elif cmd == "feedback_deny":
        pins['gled'].off(); pins['rled'].on(); pins['buzz'].on()
        await uasyncio.sleep_ms(150)
        pins['buzz'].off()
        await uasyncio.sleep_ms(100)
        pins['buzz'].on()
        await uasyncio.sleep_ms(150)
        pins['buzz'].off()
        await uasyncio.sleep_ms(1500)
        pins['rled'].off()

async def monitor_inputs():
    print("Monitoring vstupů spuštěn.")
    while True:
        hub_addr = CONFIG.get("HUB_ADDRESS", 0)
        if hub_addr != 0:
            for door in CONFIG.get("DOORS", []):
                rdr_id = door["id"]
                pins = input_pins[rdr_id]
                
                if pins['rex'].value() == 0 and last_input_states.get((rdr_id, 'rex'), 1) == 1:
                    payload = {"type": "event_rex", "hub_addr": hub_addr, "rdr_id": rdr_id}
                    message_queue.append(protocol.create_message(payload))
                last_input_states[(rdr_id, 'rex')] = pins['rex'].value()

                contact_val = pins['contact'].value()
                if contact_val != last_input_states.get((rdr_id, 'contact'), -1):
                    state = "open" if contact_val == 1 else "closed"
                    payload = {"type": "event_door_contact", "hub_addr": hub_addr, "rdr_id": rdr_id, "state": state}
                    message_queue.append(protocol.create_message(payload))
                last_input_states[(rdr_id, 'contact')] = contact_val
        await uasyncio.sleep_ms(50)

async def heartbeat():
    print("Heartbeat task spuštěn.")
    while True:
        await uasyncio.sleep(30)
        hub_addr = CONFIG.get("HUB_ADDRESS", 0)
        if hub_addr != 0:
            payload = {"type": "heartbeat", "hub_addr": hub_addr}
            message_queue.append(protocol.create_message(payload))

async def main():
    print(f"--- ACS Slave Modul (RP2040) ---")
    print(f"Unikátní ID (UID): {UNIQUE_ID}")
    
    load_config()
    
    wiegand_configs = []
    for door in CONFIG.get("DOORS", []):
        d_id = door["id"]
        wiegand_configs.append({'id': d_id, 'd0_pin': door['d0_pin'], 'd1_pin': door['d1_pin']})
        feedback_pins[d_id] = {
            'gled': Pin(door['gled_pin'], Pin.OUT, value=0),
            'rled': Pin(door['rled_pin'], Pin.OUT, value=0),
            'buzz': Pin(door['buzz_pin'], Pin.OUT, value=0)
        }
        input_pins[d_id] = {
            'rex': Pin(door['rex_pin'], Pin.IN, Pin.PULL_UP),
            'contact': Pin(door['contact_pin'], Pin.IN, Pin.PULL_UP)
        }
        print(f"Dveře ID:{d_id} ({door.get('name', '')}) nakonfigurovány na pinech D0/D1: {door['d0_pin']}/{door['d1_pin']}.")

    WiegandController(wiegand_configs, wiegand_callback)

    uart_cfg = CONFIG["UART_BUS"]
    bus = UART(uart_cfg["id"], baudrate=uart_cfg["baudrate"], tx=Pin(uart_cfg["tx_pin"]), rx=Pin(uart_cfg["rx_pin"]))
    
    reader = uasyncio.StreamReader(bus)
    writer = uasyncio.StreamWriter(bus, {})

    uasyncio.create_task(sender_task(writer))
    uasyncio.create_task(command_listener(reader))
    uasyncio.create_task(monitor_inputs())
    uasyncio.create_task(heartbeat())
    
    print("--- Systém je plně funkční ---")
    
    while True:
        await uasyncio.sleep(3600)

try:
    uasyncio.run(main())
except KeyboardInterrupt:
    print("Program ukončen.")
    reset()
except Exception as e:
    print(f"Neocekavana chyba na nejvyssi urovni: {e}")
    reset()