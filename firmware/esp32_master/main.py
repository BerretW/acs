import uasyncio
from machine import Pin, I2C
import ujson
import utime
import struct # Pro práci s binárními daty

# --- KONFIGURACE A GLOBÁLNÍ PROMĚNNÉ ---
CONFIG_FILE = 'config.json'
CONFIG = {}
i2c = None

# Slovník pro držení stavu známých (nakonfigurovaných) slave modulů
# Klíč: final_address, Hodnota: slovník se stavem
known_slaves = {}

# --- JEDNOTNÝ BINÁRNÍ PROTOKOL ---
UNCONFIGURED_I2C_ADDRESS = 0x08

# Příkazy od Mastera pro Slave
CMD_IDENTIFY          = 0x01
CMD_SET_ADDRESS       = 0x02
CMD_FEEDBACK_GRANT    = 0x10
CMD_FEEDBACK_DENY     = 0x11

# Zprávy od Slave pro Mastera
RESP_IDENTIFY         = 0x41
ACK_SET_ADDRESS       = 0x42
EVENT_CARD_READ       = 0x81
EVENT_HEARTBEAT       = 0x82
EVENT_REX             = 0x83
EVENT_DOOR_CONTACT    = 0x84
STATUS_OK             = 0x01

# --- FUNKCE PRO PRÁCI S KONFIGURACÍ ---
def load_config():
    global CONFIG
    try:
        with open(CONFIG_FILE, 'r') as f:
            CONFIG = ujson.load(f)
        print("INFO: Konfigurace načtena.")
    except (OSError, ValueError):
        print("CHYBA: Konfigurační soubor nenalezen/poškozen! Systém nemůže pokračovat.")
        # Zde by se systém mohl zastavit, protože bez konfigurace slavů je k ničemu.
        # Pro jednoduchost pokračujeme, ale polling nebude fungovat.
        CONFIG = {"SLAVES": []}


# --- FUNKCE BINÁRNÍHO PROTOKOLU ---
def calculate_checksum(data):
    checksum = 0
    for byte in data:
        checksum ^= byte
    return checksum

def parse_slave_response(data, slave_address):
    """Zpracuje binární odpověď od slave modulu."""
    if data == b'\x00':
        return {"type": "empty"} # Speciální typ pro prázdnou, ale platnou odpověď

    if len(data) < 3:
        print(f"CHYBA: Neplatná délka odpovědi od {slave_address}")
        return None

    payload = data[:-1]
    received_checksum = data[-1]
    expected_checksum = calculate_checksum(payload)

    if received_checksum != expected_checksum:
        print(f"CHYBA: Chybný checksum od slave na adrese {slave_address}!")
        return None
    
    msg_type = payload[0]
    msg_len = payload[1]

    # Ověření délky payloadu
    if (len(payload) - 2) != msg_len:
         print(f"CHYBA: Nesouhlasí délka zprávy od {slave_address}!")
         return None

    try:
        if msg_type == EVENT_CARD_READ:
            rdr_id, bits, card_code = struct.unpack('>BB_I', payload[2:])
            return {"type": "card_read", "addr": slave_address, "rdr_id": rdr_id, "bits": bits, "card": card_code}
        
        elif msg_type == EVENT_HEARTBEAT:
            return {"type": "heartbeat", "addr": slave_address}
            
        elif msg_type == RESP_IDENTIFY:
            uid = payload[2:].decode('utf-8')
            return {"type": "identity", "uid": uid}

        elif msg_type == EVENT_REX:
            rdr_id = payload[2]
            return {"type": "event_rex", "addr": slave_address, "rdr_id": rdr_id}
        
        elif msg_type == EVENT_DOOR_CONTACT:
            rdr_id, state = struct.unpack('>BB', payload[2:])
            return {"type": "event_door_contact", "addr": slave_address, "rdr_id": rdr_id, "state": "open" if state == 1 else "closed"}

    except Exception as e:
        print(f"CHYBA: Chyba při parsování zprávy od {slave_address}: {e}")
    
    return None

# --- ASYNCHRONNÍ ÚLOHY ---

async def discovery_task():
    """Periodicky hledá nové, nekonfigurované moduly."""
    print("INFO: Discovery task spuštěn.")
    await uasyncio.sleep(5) 

    while True:
        try:
            # 1. Zkusíme, jestli na unconfigured adrese něco je
            i2c.writeto(UNCONFIGURED_I2C_ADDRESS, bytes([CMD_IDENTIFY]))
            await uasyncio.sleep_ms(100)

            # 2. Přečteme odpověď
            response = i2c.readfrom(UNCONFIGURED_I2C_ADDRESS, 32)
            parsed = parse_slave_response(response, UNCONFIGURED_I2C_ADDRESS)

            if parsed and parsed.get("type") == "identity":
                uid = parsed["uid"]
                print(f"INFO: Nalezen nekonfigurovaný modul s UID: {uid}")

                # 3. Najdeme pro něj konfiguraci
                slave_config = next((s for s in CONFIG.get("SLAVES", []) if s["uid"] == uid), None)
                
                if slave_config:
                    final_addr = slave_config["final_address"]
                    if final_addr in known_slaves and known_slaves[final_addr]['status'] == 'online':
                         print(f"VAROVÁNÍ: Modul s UID {uid} je již nakonfigurován a online na adrese {final_addr}. Ignoruji.")
                    else:
                        print(f"INFO: Konfiguruji modul {uid} na novou adresu {final_addr} (0x{final_addr:X})")
                        set_addr_cmd = bytes([CMD_SET_ADDRESS, final_addr])
                        i2c.writeto(UNCONFIGURED_I2C_ADDRESS, set_addr_cmd)
                        # Po restartu ho najde polling_task
                        known_slaves.pop(final_addr, None) # Odstranit starý záznam, pokud existoval
                else:
                    print(f"CHYBA: Nalezen modul s UID {uid}, ale v config.json pro něj není žádný záznam!")

        except OSError:
            # To je v pořádku, znamená to, že na unconfigured adrese žádné zařízení není.
            pass
        
        await uasyncio.sleep(10)


async def polling_task():
    """Periodicky se dotazuje všech známých slave modulů na data."""
    print("INFO: Polling task spuštěn.")
    
    for s_cfg in CONFIG.get("SLAVES", []):
        addr = s_cfg["final_address"]
        known_slaves[addr] = {"config": s_cfg, "last_seen": 0, "status": "unknown"}

    while True:
        if not known_slaves:
            await uasyncio.sleep_ms(1000)
            continue

        for addr, state in known_slaves.items():
            try:
                # 1. Požádáme o data
                response_data = i2c.readfrom(addr, 32)
                
                # 2. Zpracujeme odpověď
                parsed = parse_slave_response(response_data, addr)
                
                if not parsed:
                    # Neplatná odpověď, ale zařízení je na sběrnici.
                    # Můžeme to považovat za známku života.
                    state["last_seen"] = utime.ticks_ms()
                    continue

                # Zařízení je prokazatelně online
                if state["status"] != "online":
                    print(f"INFO: Modul '{state['config']['name']}' (Adresa: {addr}) je nyní ONLINE.")
                    state["status"] = "online"
                state["last_seen"] = utime.ticks_ms()


                if parsed["type"] == "card_read":
                    rdr_id = parsed['rdr_id']
                    print(f"EVENT: Přečtena karta na '{state['config']['name']}' (Rdr: {rdr_id}) -> Kód: {parsed['card']}, Bity: {parsed['bits']}")
                    await send_feedback_command(addr, rdr_id, CMD_FEEDBACK_GRANT)
                
                elif parsed["type"] == "event_rex":
                    rdr_id = parsed['rdr_id']
                    print(f"EVENT: Stisknuto REX na '{state['config']['name']}' (Rdr: {rdr_id})")
                    # Zde by se otevíraly dveře
                    await send_feedback_command(addr, rdr_id, CMD_FEEDBACK_GRANT)
            
            except OSError:
                if state["status"] != "offline":
                    state["status"] = "offline"
                    print(f"CHYBA: Modul '{state['config']['name']}' (Adresa: {addr}) je OFFLINE!")

            await uasyncio.sleep_ms(150) # Krátká pauza mezi dotazy
            
        # Kontrola timeoutů
        for addr, state in known_slaves.items():
            if state['status'] == 'online' and utime.ticks_diff(utime.ticks_ms(), state['last_seen']) > 45000:
                print(f"CHYBA: Modul '{state['config']['name']}' (Adresa: {addr}) se odmlčel. Označuji jako OFFLINE.")
                state['status'] = 'offline'

async def send_feedback_command(address, rdr_id, command):
    """Pomocná funkce pro odeslání příkazu na určitou adresu a reader ID."""
    try:
        cmd_name = "GRANT" if command == CMD_FEEDBACK_GRANT else "DENY"
        print(f"CMD: Posílám FEEDBACK_{cmd_name} na adresu {address} pro rdr_id {rdr_id}")
        i2c.writeto(address, bytes([command, rdr_id]))
    except OSError:
        print(f"CHYBA: Nepodařilo se odeslat příkaz na adresu {address}, zařízení neodpovídá.")

async def main():
    print(f"\n--- ACS Master Modul (ESP32) ---")
    load_config()
    
    global i2c
    i2c_cfg = CONFIG["I2C_BUS"]
    i2c = I2C(i2c_cfg["id"], scl=Pin(i2c_cfg["scl_pin"]), sda=Pin(i2c_cfg["sda_pin"]), freq=i2c_cfg["freq"])

    print("INFO: I2C Master inicializován.")
    print("INFO: Skenuji I2C sběrnici...")
    try:
        devices = i2c.scan()
        if devices:
            print("INFO: Nalezená zařízení na adresách: " + ", ".join([f"0x{dev:X}" for dev in devices]))
        else:
            print("VAROVÁNÍ: Na I2C sběrnici nebyla nalezena žádná zařízení.")
    except Exception as e:
        print(f"CHYBA: Skenování I2C selhalo: {e}")

    uasyncio.create_task(discovery_task())
    uasyncio.create_task(polling_task())
    
    print("--- Systém je plně funkční ---")
    while True:
        await uasyncio.sleep(3600)

try:
    uasyncio.run(main())
except KeyboardInterrupt:
    print("Program ukončen.")
except Exception as e:
    print(f"FATÁLNÍ CHYBA na nejvyšší úrovni: {e}")