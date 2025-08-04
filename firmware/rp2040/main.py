import uasyncio
from machine import Pin, I2C, unique_id, reset
import ujson
import ubinascii
import micropython
import struct
import utime

# Lokální knihovny
from pro_wiegand_lib import WiegandController

micropython.alloc_emergency_exception_buf(200)

# --- KONFIGURACE A GLOBÁLNÍ PROMĚNNÉ ---
CONFIG_FILE = 'config.json'
ADDR_FILE = 'i2c_addr.dat'
HW_UNIQUE_ID = ubinascii.hexlify(unique_id()).decode('utf-8').upper()
CONFIG = {}
UNIQUE_ID = ""

# I2C a fronta pro odchozí zprávy
i2c = None
i2c_address = 0
tx_queue = [] # Fronta binárních zpráv pro Mastera

# Slovníky pro piny
feedback_pins = {}
input_pins = {}
last_input_states = {}

# --- JEDNOTNÝ BINÁRNÍ PROTOKOL ---
UNCONFIGURED_I2C_ADDRESS = 0x08
# Příkazy od Mastera
CMD_IDENTIFY          = 0x01
CMD_SET_ADDRESS       = 0x02
CMD_FEEDBACK_GRANT    = 0x10
CMD_FEEDBACK_DENY     = 0x11
# Zprávy od Slave
RESP_IDENTIFY         = 0x41
ACK_SET_ADDRESS       = 0x42
EVENT_CARD_READ       = 0x81
EVENT_HEARTBEAT       = 0x82
EVENT_REX             = 0x83
EVENT_DOOR_CONTACT    = 0x84
STATUS_OK             = 0x01


# --- LADÍCÍ FUNKCE ---
def print_hex_buffer(data):
    """Vypíše bytes objekt v HEX formátu."""
    print(' '.join(['{:02X}'.format(b) for b in data]))

# --- FUNKCE PRO PRÁCI S ADRESOU A PROTOKOLEM ---
# (Tyto funkce jsou identické s verzí pro ESP32)
def load_config():
    global CONFIG, UNIQUE_ID
    try:
        with open(CONFIG_FILE, 'r') as f:
            CONFIG = ujson.load(f)
        UNIQUE_ID = CONFIG.get("UNIQUE_ID_OVERRIDE") or HW_UNIQUE_ID
        print(f"[DBG] Konfigurace načtena. UID: {UNIQUE_ID}")
    except (OSError, ValueError):
        print("[DBG] Konfigurační soubor nenalezen/poškozen. Používám hardwarové UID.")
        CONFIG = {"DOORS": []}
        UNIQUE_ID = HW_UNIQUE_ID

def load_address():
    global i2c_address
    try:
        with open(ADDR_FILE, 'rb') as f:
            addr_bytes = f.read()
            if len(addr_bytes) == 1:
                i2c_address = addr_bytes[0]
                print(f"[DBG] I2C adresa načtena ze souboru: {i2c_address} (0x{i2c_address:X})")
                return
    except OSError:
        pass
    i2c_address = UNCONFIGURED_I2C_ADDRESS
    print(f"[DBG] Platná adresa nenalezena, používám defaultní: {i2c_address} (0x{i2c_address:X})")

def save_address(new_addr):
    print(f"[DBG] Ukládám novou adresu {new_addr} do souboru.")
    try:
        with open(ADDR_FILE, 'wb') as f:
            f.write(bytes([new_addr]))
        print(f"[DBG] Nová I2C adresa {new_addr} uložena.")
    except OSError as e:
        print(f"CHYBA: Nepodařilo se uložit adresu: {e}")

def calculate_checksum(data):
    checksum = 0
    for byte in data: checksum ^= byte
    return checksum

def prepare_message(payload):
    if len(tx_queue) > 20: 
        print("[DBG] VAROVÁNÍ: Fronta pro odeslani je plna, zprava zahozena!")
        return
    
    checksum = calculate_checksum(payload)
    message = payload + bytes([checksum])
    tx_queue.append(message)
    print(f"[DBG] Pripravena zprava k odeslani ({len(message)} bytu): ", end="")
    print_hex_buffer(message)

# --- CALLBACKY A PŘÍPRAVA ZPRÁV ---
def wiegand_callback(data_tuple):
    if i2c_address == UNCONFIGURED_I2C_ADDRESS: return
    rdr_id, card_data, bits = data_tuple
    print(f"[EVT] Wiegand data prijata - Rdr: {rdr_id}, Kod: {card_data}, Bity: {bits}")
    payload = struct.pack('>BBBB_I', EVENT_CARD_READ, 6, rdr_id, bits, card_data)
    prepare_message(payload)

def handle_i2c_command(data):
    print(f"[I2C] Prijat prikaz od Mastera ({len(data)} bytu): ", end="")
    print_hex_buffer(data)

    if not data: return
    cmd = data[0]
    
    if cmd == CMD_IDENTIFY:
        print(f"  -> Prikaz: IDENTIFY (0x{cmd:02X})")
        payload = bytes([RESP_IDENTIFY, len(UNIQUE_ID)]) + UNIQUE_ID.encode()
        prepare_message(payload)

    elif cmd == CMD_SET_ADDRESS and len(data) > 1:
        new_addr = data[1]
        print(f"  -> Prikaz: SET_ADDRESS (0x{cmd:02X}) na 0x{new_addr:X}")
        save_address(new_addr)
        payload = struct.pack('>BBBB', ACK_SET_ADDRESS, 2, STATUS_OK, new_addr)
        prepare_message(payload)
        
        async def do_reset():
            print("[DBG] Restart za 1 sekundu pro aplikovani nove adresy...")
            await uasyncio.sleep(1)
            reset()
        uasyncio.create_task(do_reset())

    elif (cmd == CMD_FEEDBACK_GRANT or cmd == CMD_FEEDBACK_DENY) and len(data) > 1:
        rdr_id = data[1]
        cmd_name = "GRANT" if cmd == CMD_FEEDBACK_GRANT else "DENY"
        print(f"  -> Prikaz: FEEDBACK_{cmd_name} (0x{cmd:02X}) pro rdr_id {rdr_id}")
        uasyncio.create_task(handle_feedback_command(rdr_id, cmd))
    
    else:
        print(f"  -> Prikaz: NEZNAMY (0x{cmd:02X})")

# DŮLEŽITÁ ZMĚNA: Toto je místo, kde se liší RP2040 a ESP32
# Na RP2040 není IRQ, proto musíme vytvořit polling task
async def i2c_polling_task():
    print("[DBG] I2C polling task spuštěn.")
    while True:
        try:
            # Čekání na příchozí data (simulace blokujícího čtení)
            # V reálném PIO slave by to bylo neblokující
            if i2c.any_write(): # Hypotetická funkce PIO knihovny
                data = i2c.read_data() # Hypotetická funkce
                handle_i2c_command(data)

            # Odeslání dat, pokud si je Master žádá
            if i2c.is_read_pending(): # Hypotetická funkce
                if tx_queue:
                    message_to_send = tx_queue.pop(0)
                    i2c.write_data(message_to_send) # Hypotetická funkce
                else:
                    i2c.write_data(b'\x00')

        except Exception as e:
            # I2C Polling by neměl padat, ale pro jistotu
            print(f"CHYBA v I2C Polling Task: {e}")
        
        await uasyncio.sleep_ms(10) # Často se dotazujeme


# --- ASYNCHRONNÍ ÚLOHY ---
# (Tyto úlohy jsou identické s verzí pro ESP32)
async def monitor_inputs():
    while True:
        if i2c_address != UNCONFIGURED_I2C_ADDRESS:
            for door in CONFIG.get("DOORS", []):
                rdr_id = door["id"]
                pins = input_pins[rdr_id]
                
                if pins['rex'].value() == 0 and last_input_states.get((rdr_id, 'rex'), 1) == 1:
                    print(f"[EVT] Stisknuto REX tlacitko na dverich ID: {rdr_id}")
                    payload = struct.pack('>BBB', EVENT_REX, 1, rdr_id)
                    prepare_message(payload)
                last_input_states[(rdr_id, 'rex')] = pins['rex'].value()

                contact_val = pins['contact'].value()
                if contact_val != last_input_states.get((rdr_id, 'contact'), -1):
                    state = 1 if contact_val == 1 else 0
                    state_str = "otevren" if state == 1 else "zavren"
                    print(f"[EVT] Zmena stavu dverniho kontaktu ID: {rdr_id} -> {state_str}")
                    payload = struct.pack('>BBBB', EVENT_DOOR_CONTACT, 2, rdr_id, state)
                    prepare_message(payload)
                last_input_states[(rdr_id, 'contact')] = contact_val
        await uasyncio.sleep_ms(50)

async def heartbeat():
    while True:
        await uasyncio.sleep(30)
        if i2c_address != UNCONFIGURED_I2C_ADDRESS:
            print("[DBG] Cas na Heartbeat.")
            payload = struct.pack('>BB', EVENT_HEARTBEAT, 0)
            prepare_message(payload)

async def handle_feedback_command(rdr_id, cmd_type):
    pins = feedback_pins.get(rdr_id)
    if not pins: return
    cmd_name = "GRANT" if cmd_type == CMD_FEEDBACK_GRANT else "DENY"
    print(f"[DBG] Spoustim zpetnou vazbu (Feedback) -> {cmd_name} pro rdr_id {rdr_id}")
    if cmd_type == CMD_FEEDBACK_GRANT:
        pins['gled'].on(); pins['rled'].off(); pins['buzz'].on()
        await uasyncio.sleep_ms(250); pins['buzz'].off(); await uasyncio.sleep_ms(1500); pins['gled'].off()
    elif cmd_type == CMD_FEEDBACK_DENY:
        pins['gled'].off(); pins['rled'].on(); pins['buzz'].on()
        await uasyncio.sleep_ms(150); pins['buzz'].off()
        await uasyncio.sleep_ms(100); pins['buzz'].on()
        await uasyncio.sleep_ms(150); pins['buzz'].off()
        await uasyncio.sleep_ms(1500); pins['rled'].off()
    print(f"[DBG] Dokoncena zpetna vazba (Feedback) pro rdr_id {rdr_id}")

async def main():
    print(f"\n--- ACS I2C Slave Modul (RP2040) ---")
    load_config()
    load_address()
    
    wiegand_configs = []
    for door in CONFIG.get("DOORS", []):
        d_id = door["id"]
        wiegand_configs.append({'id': d_id, 'd0_pin': door['d0_pin'], 'd1_pin': door['d1_pin']})
        feedback_pins[d_id] = {'gled': Pin(door['gled_pin'], Pin.OUT, value=0),'rled': Pin(door['rled_pin'], Pin.OUT, value=0),'buzz': Pin(door['buzz_pin'], Pin.OUT, value=0)}
        input_pins[d_id] = {'rex': Pin(door['rex_pin'], Pin.IN, Pin.PULL_UP),'contact': Pin(door['contact_pin'], Pin.IN, Pin.PULL_UP)}
        print(f"[DBG] Dvere ID:{d_id} ({door.get('name', 'N/A')}) nakonfigurovány.")
    WiegandController(wiegand_configs, wiegand_callback)

    # Zde by byla inicializace PIO I2C Slave
    # Protože neexistuje standardní knihovna, tato část je hypotetická
    # global i2c
    # i2c = I2CSlave(0, scl_pin=1, sda_pin=0, slave_address=i2c_address)
    print("CHYBA: Standardni machine.I2C na RP2040 nepodporuje slave mod.")
    print("       Pro plnou funkcionalitu je potreba specialni PIO knihovna.")


    # Spuštění asynchronních úloh
    # uasyncio.create_task(i2c_polling_task()) # Tento task by nahradil IRQ
    uasyncio.create_task(monitor_inputs())
    uasyncio.create_task(heartbeat())
    
    print(f"--- System bezi (v omezene kapacite bez I2C slave) ---")
    while True: await uasyncio.sleep(3600)

try:
    uasyncio.run(main())
except KeyboardInterrupt:
    print("Program ukončen.")
    reset()
except Exception as e:
    print(f"FATÁLNÍ CHYBA: {e}")
    reset()