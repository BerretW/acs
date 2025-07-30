# protocol.py
"""
Knihovna pro obsluhu komunikačního protokolu ACS.
Zajišťuje vytváření a parsování zpráv ve formátu:
<JSON_PAYLOAD>|CHECKSUM\n
"""

import ujson

def calculate_checksum(payload_str):
    """
    Vypočítá jednoduchý 8-bitový XOR kontrolní součet pro daný řetězec.

    Args:
        payload_str (str): Řetězec, pro který se má vypočítat checksum.

    Returns:
        str: Dvouznakový hexadecimální řetězec reprezentující checksum.
    """
    checksum = 0
    for char in payload_str:
        checksum ^= ord(char)
    return "{:02X}".format(checksum)

def create_message(payload_dict):
    """
    Vytvoří kompletní, formátovanou zprávu připravenou k odeslání.
    Ze slovníku vytvoří JSON, vypočítá checksum a zabalí do rámce.

    Args:
        payload_dict (dict): Slovník obsahující data ke zprávě.

    Returns:
        str or None: Kompletní zpráva jako řetězec, nebo None při chybě.
    """
    try:
        # Používáme dumps bez mezer pro co nejkratší zprávu
        payload_str = ujson.dumps(payload_dict, separators=(',', ':'))
        checksum = calculate_checksum(payload_str)
        return f"<{payload_str}>|{checksum}\n"
    except Exception as e:
        print(f"Chyba při tvorbě zprávy: {e}")
        return None

def parse_message(raw_line_str):
    """
    Zpracuje přijatý řádek ze sběrnice, ověří formát, zkontroluje
    checksum a extrahuje data.

    Args:
        raw_line_str (str): Řádek přijatý ze sériové linky.

    Returns:
        dict or None: Slovník s daty ze zprávy, nebo None při jakékoli chybě.
    """
    try:
        # 1. Očistit řádek od bílých znaků (zejména od koncového '\n')
        clean_line = raw_line_str.strip()
        
        # 2. Základní kontrola formátu
        if not clean_line.startswith('<') or '|' not in clean_line:
            # Nejedná se o platný rámec
            return None

        # 3. Rozdělení na datovou část a přijatý checksum
        # rsplit zajistí, že se rozdělí podle posledního '|', pro případ, že by byl v datech
        payload_part, received_checksum = clean_line.rsplit('|', 1)
        
        # 4. Extrakce samotného JSON payloadu
        if not payload_part.endswith('>'):
            return None # Chybí koncová značka
            
        payload_str = payload_part[1:-1] # Odstranění '<' a '>'
            
        # 5. Výpočet a ověření checksumu
        expected_checksum = calculate_checksum(payload_str)
        if expected_checksum.lower() != received_checksum.lower():
            # Checksum nesouhlasí, zpráva je poškozená
            print(f"Chyba checksumu! Očekáváno: {expected_checksum}, Přijato: {received_checksum}")
            return None

        # 6. Parsrování JSONu
        # Pokud vše sedí, parsujeme JSON a vrátíme ho jako slovník
        return ujson.loads(payload_str)
        
    except (ValueError, IndexError, TypeError) as e:
        # Zachytí chyby při parsování JSONu, dělení stringu atd.
        print(f"Chyba při parsování zprávy: {e} | Data: {raw_line_str.strip()}")
        return None
