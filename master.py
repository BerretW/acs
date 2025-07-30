# pc_master_tester.py
"""
Testovací skript pro PC, který simuluje Master ACS jednotku.
- Naslouchá na sériovém portu na zprávy ze Slave modulu.
- Umožňuje interaktivně odesílat příkazy (grant/deny/identify).
- Umožňuje měnit adresu Slave zařízení pomocí jejich unikátního ID (UID).
- Ověřuje funkčnost protokolu a Slave firmwaru.
"""

import asyncio
import json
import serial_asyncio
import sys
import serial # Potřeba pro výjimku

# --- KONFIGURACE ---
SERIAL_PORT = "COM6"  # Windows: "COM3", "COM4", atd. | Linux: "/dev/ttyUSB0"
BAUD_RATE = 115200

# ANSI kódy pro barvy v terminálu pro lepší čitelnost
class Colors:
    GREEN = '\033[92m'
    YELLOW = '\033[93m'
    RED = '\033[91m'
    BLUE = '\033[94m'
    CYAN = '\033[96m'
    MAGENTA = '\033[95m'
    ENDC = '\033[0m'

# --- PROTOKOLOVÉ FUNKCE ---

def calculate_checksum(payload_str):
    """Vypočítá 8-bit XOR checksum."""
    checksum = 0
    for char in payload_str:
        checksum ^= ord(char)
    return "{:02X}".format(checksum)

def create_message(payload_dict):
    """Vytvoří kompletní zprávu pro odeslání."""
    try:
        payload_str = json.dumps(payload_dict, separators=(',', ':'))
        checksum = calculate_checksum(payload_str)
        return f"<{payload_str}>|{checksum}\n"
    except Exception as e:
        print(f"{Colors.RED}Chyba při tvorbě zprávy: {e}{Colors.ENDC}")
        return None

def parse_message(raw_line_str):
    """Zpracuje přijatý řádek."""
    try:
        clean_line = raw_line_str.strip()
        if not clean_line.startswith('<') or '|' not in clean_line:
            return None
        payload_part, received_checksum = clean_line.rsplit('|', 1)
        if not payload_part.endswith('>'):
            return None
        payload_str = payload_part[1:-1]
        expected_checksum = calculate_checksum(payload_str)
        if expected_checksum.lower() != received_checksum.lower():
            print(f"{Colors.RED}CHYBA CHECKSUMU! Očekáváno: {expected_checksum}, Přijato: {received_checksum}{Colors.ENDC}")
            return None
        return json.loads(payload_str)
    except Exception:
        return None

# --- ASYNCHRONNÍ ÚLOHY ---

async def reader_task(reader):
    """Nepřetržitě čte data ze sériového portu a vypisuje je."""
    print(f"{Colors.CYAN}--- Naslouchám na portu {SERIAL_PORT} ---{Colors.ENDC}")
    while True:
        try:
            line_bytes = await reader.readline()
            if not line_bytes:
                continue

            line_str = line_bytes.decode('utf-8').strip()
            data = parse_message(line_str)

            if not data:
                print(f"{Colors.RED}Přijata nečitelná zpráva: {line_str}{Colors.ENDC}")
                continue

            msg_type = data.get("type")
            hub = data.get("hub_addr")
            
            print(f"{Colors.GREEN}IN <---", end=" ")
            if msg_type == "card_read":
                print(f"CARD_READ | Hub:{hub} | Čtečka:{data.get('rdr_id')} | Karta:{data.get('card')} | Bity:{data.get('bits')}{Colors.ENDC}")
            elif msg_type == "event_rex":
                print(f"REX EVENT | Hub:{hub} | Dveře:{data.get('rdr_id')} | Požadavek na odchod!{Colors.ENDC}")
            elif msg_type == "event_door_contact":
                print(f"DOOR_CONTACT | Hub:{hub} | Dveře:{data.get('rdr_id')} | Stav: {data.get('state').upper()}{Colors.ENDC}")
            elif msg_type == "heartbeat":
                print(f"HEARTBEAT | Hub:{hub} | Modul je online.{Colors.ENDC}")
            elif msg_type in ["nano", "esp32", "rp2040"]: # IDENTIFY response
                print(f"{Colors.MAGENTA}IDENTIFY | Typ: {msg_type.upper()} | UID: {data.get('uid')} | Hub:{hub} | Čteček: {data.get('readers')}{Colors.ENDC}")
            elif msg_type == "ack_set_address":
                if data.get("status") == "success":
                    print(f"{Colors.MAGENTA}ADDRESS SET OK | UID: {data.get('uid')} | Nová adresa: {data.get('new_addr')}{Colors.ENDC}")
                else:
                    print(f"{Colors.RED}ADDRESS SET FAIL | UID: {data.get('uid')} | Důvod: {data.get('reason')}{Colors.ENDC}")
            elif msg_type == "boot":
                 print(f"{Colors.CYAN}BOOT | Zařízení hlásí: {data.get('msg')} | {data}{Colors.ENDC}")
            else:
                print(f"{Colors.YELLOW}Neznámý typ zprávy: {data}{Colors.ENDC}")

        except Exception as e:
            print(f"{Colors.RED}Chyba v reader tasku: {e}{Colors.ENDC}")
            await asyncio.sleep(1)

async def interactive_writer_task(writer):
    """Umožňuje uživateli interaktivně posílat příkazy."""
    print(f"{Colors.CYAN}--- Interaktivní terminál připraven ---{Colors.ENDC}")
    loop = asyncio.get_running_loop()
    while True:
        print("\n" + Colors.BLUE + "Dostupné příkazy:" + Colors.ENDC)
        print(" [1] Povolit přístup (grant)")
        print(" [2] Zamítnout přístup (deny)")
        print(" [3] Identifikovat všechna zařízení (broadcast)")
        print(" [4] Změnit adresu slave zařízení (přes UID)")
        print(" [q] Ukončit")

        choice = await loop.run_in_executor(None, sys.stdin.readline)
        choice = choice.strip()

        if choice == 'q':
            break

        payload = None
        if choice == '3':
            # Identify je broadcast, hub_addr je irelevantní (může být 0)
            payload = {"type": "command", "cmd": "identify", "hub_addr": 0}

        elif choice == '4':
            try:
                target_uid = await loop.run_in_executor(None, lambda: input("  Zadej UID cílového zařízení: "))
                new_addr_str = await loop.run_in_executor(None, lambda: input(f"  Zadej novou adresu pro {target_uid.strip().upper()}: "))
                new_addr = int(new_addr_str)

                payload = {
                    "type": "command",
                    "cmd": "set_address",
                    "target_uid": target_uid.strip().upper(),
                    "new_addr": new_addr
                }
            except (ValueError, TypeError):
                print(f"{Colors.RED}Neplatný vstup, zadejte prosím platné hodnoty.{Colors.ENDC}")
            except Exception as e:
                print(f"{Colors.RED}Chyba: {e}{Colors.ENDC}")
        
        elif choice in ['1', '2']:
            try:
                hub_addr_str = await loop.run_in_executor(None, lambda: input("  Zadej adresu HUBu (např. 1): "))
                rdr_id_str = await loop.run_in_executor(None, lambda: input("  Zadej ID dveří/čtečky (např. 1): "))
                hub_addr = int(hub_addr_str)
                rdr_id = int(rdr_id_str)

                cmd = "feedback_grant" if choice == '1' else "feedback_deny"
                
                payload = {
                    "type": "command",
                    "hub_addr": hub_addr,
                    "cmd": cmd,
                    "rdr_id": rdr_id
                }
            except (ValueError, TypeError):
                print(f"{Colors.RED}Neplatný vstup, zadejte prosím čísla.{Colors.ENDC}")
            except Exception as e:
                print(f"{Colors.RED}Chyba: {e}{Colors.ENDC}")
        else:
            print(f"{Colors.RED}Neznámá volba.{Colors.ENDC}")

        if payload:
            message = create_message(payload)
            if message:
                print(f"{Colors.YELLOW}OUT ---> Posílám: {message.strip()}{Colors.ENDC}")
                writer.write(message.encode('utf-8'))
                await writer.drain()
    
    asyncio.get_event_loop().stop()

async def main():
    """Hlavní funkce, která spustí oba tasky."""
    try:
        reader, writer = await serial_asyncio.open_serial_connection(url=SERIAL_PORT, baudrate=BAUD_RATE)
        read_task = asyncio.create_task(reader_task(reader))
        write_task = asyncio.create_task(interactive_writer_task(writer))
        await asyncio.gather(read_task, write_task)
    except serial.SerialException as e:
        print(f"\n{Colors.RED}!!! Chyba sériového portu !!!{Colors.ENDC}")
        print(f"{Colors.RED}Nepodařilo se otevřít port '{SERIAL_PORT}'.{Colors.ENDC}")
        print(f"{Colors.YELLOW}Zkontrolujte, zda:")
        print("  1. Je převodník připojen k PC.")
        print("  2. Je název portu v proměnné SERIAL_PORT správný.")
        print("  3. Není port používán jiným programem (jako Arduino IDE, Putty, ...).")
        print(f"  4. Máte oprávnění k přístupu k portu (hlavně na Linuxu).{Colors.ENDC}")
        print(f"Systémová chyba: {e}")

if __name__ == "__main__":
    print(f"{Colors.CYAN}=== ACS Slave Tester v2.0 (s podporou UID) ===" + Colors.ENDC)
    loop = asyncio.get_event_loop()
    try:
        loop.run_until_complete(main())
    except KeyboardInterrupt:
        print("\nUkončuji...")
    finally:
        if not loop.is_closed():
            loop.close()
    print("Program byl ukončen.")