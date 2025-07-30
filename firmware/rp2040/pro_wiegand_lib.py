# pro_wiegand_lib.py
"""
Profesionální, plně na přerušeních založená knihovna pro Wiegand.
Identifikace čteček pomocí číselného ID, konfigurace z externího zdroje.
S přidanou kontrolou parity pro 26-bit formát.

Verze: 3.2 (Robustní kontrola vstupů)
"""
import machine
import utime
import micropython

micropython.alloc_emergency_exception_buf(100)

class _WiegandReader:
    """Interní třída pro obsluhu jedné Wiegand čtečky."""
    
    _timer_id_counter = 0

    def __init__(self, d0_pin, d1_pin, callback, reader_id):
        self._pin_d0 = machine.Pin(d0_pin, machine.Pin.IN, machine.Pin.PULL_UP)
        self._pin_d1 = machine.Pin(d1_pin, machine.Pin.IN, machine.Pin.PULL_UP)
        self._callback = callback
        self._reader_id = reader_id

        # Použijeme volné ID timeru, abychom se vyhnuli konfliktům
        self._timer = machine.Timer(_WiegandReader._timer_id_counter)
        _WiegandReader._timer_id_counter = (_WiegandReader._timer_id_counter + 1) % 4

        self.MIN_PULSE_WIDTH_US = 200
        self.TIMEOUT_MS = 50

        self._last_pulse_time_us = 0
        self._data = 0
        self._bits = 0

        self._pin_d0.irq(trigger=machine.Pin.IRQ_FALLING, handler=self._on_data0)
        self._pin_d1.irq(trigger=machine.Pin.IRQ_FALLING, handler=self._on_data1)

    def _on_data_pulse(self, bit_val):
        current_time_us = utime.ticks_us()
        if utime.ticks_diff(current_time_us, self._last_pulse_time_us) < self.MIN_PULSE_WIDTH_US:
            return
        self._last_pulse_time_us = current_time_us
        
        if self._bits < 64: # Ochrana proti přetečení
            self._bits += 1
            self._data <<= 1
            if bit_val == 1: self._data |= 1
        
        self._timer.init(mode=machine.Timer.ONE_SHOT, period=self.TIMEOUT_MS, callback=self._finalize_read)

    def _on_data0(self, pin): self._on_data_pulse(0)
    def _on_data1(self, pin): self._on_data_pulse(1)

    def _check_parity(self):
        """Zkontroluje paritu pro standardní 26-bit Wiegand formát."""
        if self._bits != 26:
            return True

        first_13_bits = (self._data >> 13) & 0x1FFF
        if bin(first_13_bits).count('1') % 2 != 0:
            return False

        last_13_bits = self._data & 0x1FFF
        if bin(last_13_bits).count('1') % 2 != 1:
            return False
            
        return True

    def _finalize_read(self, timer_instance):
        if self._bits > 0:
            if self._check_parity():
                micropython.schedule(self._callback, (self._reader_id, self._data, self._bits))
            else:
                print(f"Chyba parity pro čtečku ID: {self._reader_id}. Data: {self._data:#0{self._bits//4+4}x}, Bity: {self._bits}")
        self._data, self._bits = 0, 0
        
    def deinit(self):
        self._pin_d0.irq(handler=None)
        self._pin_d1.irq(handler=None)
        self._timer.deinit()

class WiegandController:
    """Hlavní třída pro správu více Wiegand čteček."""
    def __init__(self, readers_config, unified_callback):
        """
        Args:
            readers_config (list): Seznam slovníků (dict), typicky načtený z JSON.
                                   Každý slovník musí obsahovat 'id', 'd0_pin', 'd1_pin'.
            unified_callback (function): Funkce, která obdrží (reader_id, data, bity).
        """
        self._readers = {}
        
        # Vylepšená kontrola vstupů pro snadnější ladění
        if not isinstance(readers_config, list):
            raise TypeError("WiegandController ocekava seznam (list) konfiguraci, ale dostal jiny typ.")

        for config in readers_config:
            if not isinstance(config, dict):
                print(f"CHYBA: Polozka v konfiguraci ctecek neni slovnik (dict), ale: {config}")
                print("-> Zkontrolujte, zda 'DOORS' v config.json obsahuje seznam objektu a ne retezcu.")
                continue

            try:
                reader_id = config['id']
                d0 = config['d0_pin']
                d1 = config['d1_pin']
            except KeyError as e:
                print(f"CHYBA: Konfiguraci ctecky chybi povinny klic: {e}")
                continue
            
            if reader_id in self._readers:
                print(f"Varování: Duplicitní ID čtečky ({reader_id}) v konfiguraci. Ignoruji.")
                continue

            reader_instance = _WiegandReader(d0, d1, unified_callback, reader_id)
            self._readers[reader_id] = reader_instance
        
        print(f"Wiegand Controller inicializován pro {len(self._readers)} čteček.")

    def deinit(self):
        print("Uvolňuji zdroje Wiegand controlleru...")
        for reader in self._readers.values():
            reader.deinit()