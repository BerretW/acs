import uasyncio
from machine import Pin, UART
import ujson
import micropython

# Lokální knihovny (musí být nahrány na ESP32)
import protocol
from pro_wiegand_lib import WiegandController

micropython.alloc_emergency_exception_buf(100)

# --- CENTRÁLNÍ KONFIGURACE MODULU ---
CONFIG = {
    "HUB_ADDRESS": 1,
    "UART_BUS": {
        "id": 2,
        # Sjednoťte si rychlost, kterou opravdu chcete používat
        "baudrate": 115200,
        "tx_pin": 17,
        "rx_pin": 16
    },
    "DOORS": [
        {
            "id": 1,
            "name": "Dilna - vchod",
            "d0_pin": 33,
            "d1_pin": 32,
            "gled_pin": 18,
            "rled_pin": 19,
            "buzz_pin": 21,
            "rex_pin": 22,
            "contact_pin": 23
        },
    ]
}

# --- Globální proměnné a objekty ---
# Vlastní fronta místo uasyncio.Queue
message_queue = []

feedback_pins = {}
input_pins = {}
last_input_states = {}

# --- Wiegand Callback ---
def wiegand_callback(data_tuple):
    """Callback volaný z přerušení."""
    reader_id, card_data, bits = data_tuple
    
    payload = {
        "type": "card_read",
        "hub_addr": CONFIG["HUB_ADDRESS"],
        "rdr_id": reader_id,
        "card": card_data,
        "bits": bits
    }
    print(f"Přečtená karta: {card_data}, Reader ID: {reader_id}, Bits: {bits}")

    message = protocol.create_message(payload)
    if message:
        # Vložíme zprávu do fronty bez blokování
        message_queue.append(message)
    else:
        print("CHYBA: protocol.create_message vrátila neplatnou zprávu.")

# --- Asynchronní úlohy (Tasks) ---
async def sender_task(writer):
    """Jediná úloha, která má na starost odesílání zpráv z fronty."""
    print("Sender task spuštěn.")
    while True:
        if message_queue:
            message = message_queue.pop(0)  # Načte první zprávu
            print(f"Posílám zprávu na UART: {message}")
            writer.write(message.encode('utf-8'))
            await writer.drain()
        await uasyncio.sleep(0.1)

async def command_listener(reader):
    """Naslouchá příkazům od Mastera a zpracovává je."""
    print("Command listener spuštěn.")
    while True:
        raw_line = await reader.readline()
        if not raw_line: continue
        
        try:
            line_str = raw_line.decode('utf-8')
            parsed_data = protocol.parse_message(line_str)
        except (UnicodeError, ujson.JSONDecodeError):
            print(f"Přijata poškozená zpráva: {raw_line.strip()}")
            continue

        if not parsed_data:
            print(f"Přijata nevalidní zpráva: {raw_line.strip()}")
            continue

        if parsed_data.get('hub_addr') != CONFIG["HUB_ADDRESS"]:
            continue
        
        if parsed_data.get('type') == 'command':
            uasyncio.create_task(handle_feedback_command(parsed_data))

async def handle_feedback_command(cmd_data):
    """Zpracuje příkaz pro signalizaci na čtečce (grant/deny)."""
    rdr_id = cmd_data.get('rdr_id')
    cmd = cmd_data.get('cmd')
    pins = feedback_pins.get(rdr_id)
    if not pins: return
    
    print(f"Příkaz '{cmd}' pro dveře ID: {rdr_id}")
    
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
    """Periodicky kontroluje stav REX a dveřních kontaktů."""
    print("Monitoring vstupů spuštěn.")
    while True:
        for door in CONFIG["DOORS"]:
            rdr_id = door["id"]
            pins = input_pins[rdr_id]
            
            # Zpracování REX tlačítka
            if pins['rex'].value() == 0 and last_input_states.get((rdr_id, 'rex'), 1) == 1:
                payload = {"type": "event_rex", "hub_addr": CONFIG["HUB_ADDRESS"], "rdr_id": rdr_id}
                message_queue.append(protocol.create_message(payload))
            last_input_states[(rdr_id, 'rex')] = pins['rex'].value()

            # Zpracování dveřního kontaktu
            contact_val = pins['contact'].value()
            if contact_val != last_input_states.get((rdr_id, 'contact'), -1):
                state_str = "open" if contact_val == 1 else "closed"
                payload = {"type": "event_door_contact", "hub_addr": CONFIG["HUB_ADDRESS"], "rdr_id": rdr_id, "state": state_str}
                message_queue.append(protocol.create_message(payload))
            last_input_states[(rdr_id, 'contact')] = contact_val

        await uasyncio.sleep_ms(50)

async def heartbeat():
    """Periodicky posílá zprávu, že je modul naživu."""
    print("Heartbeat task spuštěn.")
    while True:
        await uasyncio.sleep(30)
        payload = {"type": "heartbeat", "hub_addr": CONFIG["HUB_ADDRESS"]}
        message_queue.append(protocol.create_message(payload))
        

async def main():
    """Hlavní funkce, která inicializuje systém a spustí všechny úlohy."""
    print("--- ACS Slave Modul se spouští ---")
    
    wiegand_configs = []
    for door in CONFIG["DOORS"]:
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
        print(f"Dveře ID:{d_id} ({door['name']}) nakonfigurovány.")

    WiegandController(wiegand_configs, wiegand_callback)

    # 3. POUZE JEDNA INSTANCE UART, ZDE
    uart_cfg = CONFIG["UART_BUS"]
    bus = UART(uart_cfg["id"], baudrate=uart_cfg["baudrate"], tx=uart_cfg["tx_pin"], rx=uart_cfg["rx_pin"])
    bus.write("UART inicializován...\n")
    
    reader = uasyncio.StreamReader(bus)
    writer = uasyncio.StreamWriter(bus, {})

    uasyncio.create_task(sender_task(writer))
    uasyncio.create_task(command_listener(reader))
    uasyncio.create_task(monitor_inputs())
    uasyncio.create_task(heartbeat())
    
    print("--- Systém je plně funkční ---")
    
    while True:
        await uasyncio.sleep(3600) # Můžeme spát déle, úlohy běží na pozadí

try:
    uasyncio.run(main())
except KeyboardInterrupt:
    print("Program byl ukončen.")
    import machine
    machine.reset()

