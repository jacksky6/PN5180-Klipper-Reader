# PN5180 NFC reader support for Klipper.
#
# Supports ISO14443A / NTAG and ISO15693 tags over SPI. The driver uses
# fixed command delays like pypn5180.
# Place this module in klippy/extras/ and configure it with a [pn5180 <name>]
# section.

import json
import logging
import re
import time

from . import bus


# PN5180 registers
SYSTEM_CONFIG = 0x00
IRQ_STATUS = 0x02
IRQ_CLEAR = 0x03
CRC_RX_CONFIG = 0x12
RX_STATUS = 0x13
CRC_TX_CONFIG = 0x19
RF_STATUS = 0x1D

# PN5180 EEPROM addresses
PRODUCT_VERSION = 0x10
FIRMWARE_VERSION = 0x12
EEPROM_VERSION = 0x14

# PN5180 direct commands
CMD_WRITE_REGISTER = 0x00
CMD_WRITE_REGISTER_OR_MASK = 0x01
CMD_WRITE_REGISTER_AND_MASK = 0x02
CMD_READ_REGISTER = 0x04
CMD_READ_EEPROM = 0x07
CMD_SEND_DATA = 0x09
CMD_READ_DATA = 0x0A
CMD_LOAD_RF_CONFIG = 0x11
CMD_RF_ON = 0x16
CMD_RF_OFF = 0x17

# IRQ bits
RX_IRQ_STAT = 1 << 0
TX_IRQ_STAT = 1 << 1
IDLE_IRQ_STAT = 1 << 2
TX_RFOFF_IRQ_STAT = 1 << 8
TX_RFON_IRQ_STAT = 1 << 9
GENERAL_ERROR_IRQ_STAT = 1 << 17

RX_BYTES_RECEIVED_MASK = 0x1FF
TRANSCEIVE_STATE_SHIFT = 24
TRANSCEIVE_STATE_MASK = 0x07
TRANSCEIVE_STATE_WAIT_TRANSMIT = 1
TRANSCEIVE_STATE_IDLE = 0

MIFARE_CMD_READ = 0x30

ISO15693_CMD_INVENTORY = 0x01
ISO15693_CMD_READ_SINGLE_BLOCK = 0x20
ISO15693_CMD_READ_MULTIPLE_BLOCKS = 0x23
ISO15693_FLAG_HIGH_DATA_RATE = 0x02
ISO15693_FLAG_INVENTORY = 0x04
ISO15693_FLAG_ADDRESS = 0x20
ISO15693_FLAG_ONE_SLOT = 0x20

DEFAULT_NTAG_START_PAGE = 4
DEFAULT_NTAG_END_PAGE = 67
DEFAULT_ISO15693_START_BLOCK = 0
DEFAULT_ISO15693_END_BLOCK = 79
RESET_SCHEDULE_DELAY = 0.100
RESET_LOW_TIME = 0.100
RESET_BOOT_DELAY = 0.200


class PN5180Error(Exception):
    pass


class PN5180Handler:
    def __init__(self, printer, spi, config):
        self.printer = printer
        self.reactor = printer.get_reactor()
        self.gcode = printer.lookup_object("gcode")
        self.spi = spi
        self.mcu = spi.get_mcu()

        self.command_delay = config.getfloat("command_delay", 0.005, minval=0.0)
        self.rf_timeout = config.getfloat("rf_timeout", 0.05, above=0.0)
        self.rf_poll_interval = config.getfloat(
            "rf_poll_interval", 0.001, minval=0.0001)
        self.rf_on_delay = config.getfloat("rf_on_delay", 0.05, minval=0.0)
        self.page_read_retries = config.getint("page_read_retries", 2, minval=1)

        reset_pin_name = config.get("reset_pin", None)
        if not reset_pin_name:
            raise config.error(
                "Option 'reset_pin' in section '%s' must be specified. "
                "PN5180 RST must be connected to an MCU GPIO so Klipper can "
                "recover the reader by hardware reset." % (config.get_name(),))
        pins = printer.lookup_object("pins")
        self.reset_pin = pins.setup_pin("digital_out", reset_pin_name)
        self.reset_pin.setup_max_duration(0.0)
        self.reset_pin.setup_start_value(1, 1)

        self.initialized = False
        self.firmware = None
        self.product_version = None
        self.eeprom_version = None
        self.diag_registers = {}
        self.current_uid = None
        self.current_uid_lsb_first = None
        self.current_uid_hex = ""
        self.last_read_stats = {}

    def _mcu_print_time(self, eventtime=None):
        if eventtime is None:
            eventtime = self.reactor.monotonic()
        return self.mcu.estimated_print_time(eventtime)

    def _command_delay(self):
        if self.command_delay:
            time.sleep(self.command_delay)

    def _transceive_command(self, send_data, recv_len=0):
        self.spi.spi_send(send_data)
        self._command_delay()

        if not recv_len:
            return []

        response = self.spi.spi_transfer([0xFF] * recv_len)
        self._command_delay()

        return list(bytearray(response["response"]))

    @staticmethod
    def _u32_to_le(value):
        return [
            value & 0xFF,
            (value >> 8) & 0xFF,
            (value >> 16) & 0xFF,
            (value >> 24) & 0xFF,
        ]

    @staticmethod
    def _le_to_u32(data):
        data = list(data)
        return data[0] | (data[1] << 8) | (data[2] << 16) | (data[3] << 24)

    def write_register(self, reg, value):
        self._transceive_command(
            [CMD_WRITE_REGISTER, reg] + self._u32_to_le(value))

    def write_register_or_mask(self, reg, mask):
        self._transceive_command(
            [CMD_WRITE_REGISTER_OR_MASK, reg] + self._u32_to_le(mask))

    def write_register_and_mask(self, reg, mask):
        self._transceive_command(
            [CMD_WRITE_REGISTER_AND_MASK, reg] + self._u32_to_le(mask))

    def read_register(self, reg):
        data = self._transceive_command([CMD_READ_REGISTER, reg], 4)
        if len(data) != 4:
            raise PN5180Error("short register response")
        return self._le_to_u32(data)

    def read_eeprom(self, addr, length):
        return self._transceive_command([CMD_READ_EEPROM, addr, length], length)

    def clear_irq_status(self, mask=0xFFFFFFFF):
        self.write_register(IRQ_CLEAR, mask)

    def load_rf_config(self, tx_conf, rx_conf):
        self._transceive_command([CMD_LOAD_RF_CONFIG, tx_conf, rx_conf])

    def rf_on(self):
        self.clear_irq_status(TX_RFON_IRQ_STAT)
        self._transceive_command([CMD_RF_ON, 0x00])
        self._wait_irq(TX_RFON_IRQ_STAT, timeout=0.5, raise_on_error=False)
        self.clear_irq_status(TX_RFON_IRQ_STAT)
        if self.rf_on_delay:
            time.sleep(self.rf_on_delay)

    def rf_off(self):
        self.clear_irq_status(TX_RFOFF_IRQ_STAT)
        self._transceive_command([CMD_RF_OFF, 0x00])
        self._wait_irq(TX_RFOFF_IRQ_STAT, timeout=0.5, raise_on_error=False)
        self.clear_irq_status(TX_RFOFF_IRQ_STAT)

    def _wait_transceive_state(self, expected, timeout=0.05):
        start = time.time()
        while time.time() - start < timeout:
            rf_status = self.read_register(RF_STATUS)
            state = (rf_status >> TRANSCEIVE_STATE_SHIFT) & TRANSCEIVE_STATE_MASK
            if state == expected:
                return True
            time.sleep(self.rf_poll_interval)
        return False

    def send_data(self, data, valid_bits=0):
        if len(data) > 260:
            raise PN5180Error("PN5180 sendData payload too large")
        self.clear_irq_status(0xFFFFFFFF)
        self.write_register_and_mask(SYSTEM_CONFIG, 0xFFFFFFF8)
        self.write_register_or_mask(SYSTEM_CONFIG, 0x00000003)
        self._wait_transceive_state(TRANSCEIVE_STATE_WAIT_TRANSMIT)
        self._transceive_command([CMD_SEND_DATA, valid_bits] + list(data))

    def read_data(self, length):
        return self._transceive_command([CMD_READ_DATA, 0x00], length)

    def rx_bytes_received(self):
        return self.read_register(RX_STATUS) & RX_BYTES_RECEIVED_MASK

    def _wait_irq(self, mask, timeout=None, raise_on_error=True):
        timeout = self.rf_timeout if timeout is None else timeout
        start = time.time()
        while time.time() - start < timeout:
            irq = self.read_register(IRQ_STATUS)
            if irq & mask:
                return True
            if irq & GENERAL_ERROR_IRQ_STAT:
                if raise_on_error:
                    raise PN5180Error("PN5180 general error IRQ")
                return False
            time.sleep(self.rf_poll_interval)
        return False

    def hardware_reset(self):
        eventtime = self.reactor.monotonic()
        print_time = self._mcu_print_time(eventtime) + RESET_SCHEDULE_DELAY
        self.reset_pin.set_digital(print_time, 0)
        self.reset_pin.set_digital(print_time + RESET_LOW_TIME, 1)
        self.reactor.pause(
            eventtime + RESET_SCHEDULE_DELAY + RESET_LOW_TIME + RESET_BOOT_DELAY)
        self._wait_irq(IDLE_IRQ_STAT, timeout=1.0, raise_on_error=False)
        self.clear_irq_status(0xFFFFFFFF)
        return True

    def setup_type_a_rf(self):
        # PN5180 can stay in Receive/Transceive after a failed or completed tag
        # session. Always force a clean RF state before a new Type A activation.
        try:
            self.rf_off()
        except Exception:
            pass
        self.write_register_and_mask(SYSTEM_CONFIG, 0xFFFFFFF8)
        self._wait_transceive_state(TRANSCEIVE_STATE_IDLE, timeout=0.05)
        self.clear_irq_status(0xFFFFFFFF)
        self.load_rf_config(0x00, 0x80)
        self.rf_on()
        self.write_register_or_mask(SYSTEM_CONFIG, 0x00000003)

    def setup_iso15693_rf(self):
        try:
            self.rf_off()
        except Exception:
            pass
        self.write_register_and_mask(SYSTEM_CONFIG, 0xFFFFFFF8)
        self._wait_transceive_state(TRANSCEIVE_STATE_IDLE, timeout=0.05)
        self.clear_irq_status(0xFFFFFFFF)
        self.load_rf_config(0x0D, 0x8D)
        self.rf_on()
        self.write_register_or_mask(SYSTEM_CONFIG, 0x00000003)

    def check_communication(self):
        product = self.read_eeprom(PRODUCT_VERSION, 2)
        firmware = self.read_eeprom(FIRMWARE_VERSION, 2)
        eeprom = self.read_eeprom(EEPROM_VERSION, 2)
        if (self._is_suspicious_bytes(product)
                or self._is_suspicious_bytes(firmware)
                or self._is_suspicious_bytes(eeprom)):
            raise PN5180Error(
                "invalid EEPROM response product=%s firmware=%s eeprom=%s; "
                "PN5180 did not return valid SPI data" % (
                    self._format_bytes(product),
                    self._format_bytes(firmware),
                    self._format_bytes(eeprom)))

        registers = {
            "SYSTEM_CONFIG": self.read_register(SYSTEM_CONFIG),
            "IRQ_STATUS": self.read_register(IRQ_STATUS),
            "RX_STATUS": self.read_register(RX_STATUS),
            "RF_STATUS": self.read_register(RF_STATUS),
        }
        values = list(registers.values())
        if all(value == 0xFFFFFFFF for value in values):
            raise PN5180Error(
                "all diagnostic registers read 0xFFFFFFFF; check SPI MISO/CS/bus/power")
        if all(value == 0x00000000 for value in values):
            raise PN5180Error(
                "all diagnostic registers read 0x00000000; check SPI MISO/CS/bus/power")
        return product, firmware, eeprom, registers

    def initialize(self, announce=True):
        try:
            self.hardware_reset()
            (self.product_version, self.firmware, self.eeprom_version,
             self.diag_registers) = self.check_communication()
            self.setup_type_a_rf()
            self.initialized = True
            version = "%d.%d" % (self.firmware[1], self.firmware[0])
            diag = self._format_diag_summary()
            logging.info("PN5180 initialized: firmware v%s; %s", version, diag)
            if announce:
                self.gcode.respond_info(
                    "PN5180 initialized: firmware v%s; %s" % (version, diag))
            return True
        except PN5180Error as e:
            self.initialized = False
            logging.error("PN5180 initialization failed: %s", e)
            self.gcode.respond_info("PN5180 initialization failed: %s" % (e,))
            return False
        except Exception as e:
            self.initialized = False
            logging.exception("PN5180 initialization failed: %s", e)
            self.gcode.respond_info("PN5180 initialization failed: %s" % (e,))
            return False

    @staticmethod
    def _is_suspicious_bytes(data):
        if not data:
            return True
        return all(b == 0x00 for b in data) or all(b == 0xFF for b in data)

    @staticmethod
    def _format_bytes(data):
        return " ".join("%02X" % (b,) for b in data)

    def _format_diag_summary(self):
        parts = [
            "product=%s" % (self._format_bytes(self.product_version or []),),
            "firmware=%s" % (self._format_bytes(self.firmware or []),),
            "eeprom=%s" % (self._format_bytes(self.eeprom_version or []),),
        ]
        for name in ("SYSTEM_CONFIG", "IRQ_STATUS", "RX_STATUS", "RF_STATUS"):
            if name in self.diag_registers:
                parts.append("%s=0x%08X" % (name, self.diag_registers[name]))
        return "; ".join(parts)

    @staticmethod
    def _bcc_ok(bytes5):
        if len(bytes5) != 5:
            return False
        return (bytes5[0] ^ bytes5[1] ^ bytes5[2] ^ bytes5[3]) == bytes5[4]

    def activate_type_a(self, wakeup=True):
        buffer = [0] * 10
        self.setup_type_a_rf()
        self.write_register_and_mask(SYSTEM_CONFIG, 0xFFFFFFBF)
        self.write_register_and_mask(CRC_RX_CONFIG, 0xFFFFFFFE)
        self.write_register_and_mask(CRC_TX_CONFIG, 0xFFFFFFFE)

        self.send_data([0x52 if wakeup else 0x26], valid_bits=0x07)
        if not self._wait_irq(RX_IRQ_STAT, timeout=self.rf_timeout,
                              raise_on_error=False):
            logging.debug("PN5180 no ATQA response")
            return None
        atqa = self.read_data(2)
        if len(atqa) != 2:
            return None
        if self._is_suspicious_bytes(atqa):
            logging.debug("PN5180 rejected suspicious ATQA: %s", atqa)
            return None
        if atqa[0] == 0xFF or atqa[1] == 0xFF:
            logging.debug("PN5180 rejected noisy ATQA: %s", atqa)
            return None
        buffer[0:2] = atqa

        self.send_data([0x93, 0x20], valid_bits=0x00)
        if not self._wait_irq(RX_IRQ_STAT, timeout=self.rf_timeout,
                              raise_on_error=False):
            logging.debug("PN5180 no CL1 anticollision response")
            return None
        cl1 = self.read_data(5)
        if len(cl1) != 5:
            return None
        if self._is_suspicious_bytes(cl1):
            logging.debug("PN5180 rejected suspicious CL1 response: %s", cl1)
            return None
        if not self._bcc_ok(cl1):
            logging.debug("PN5180 rejected CL1 with bad BCC: %s", cl1)
            return None

        self.write_register_or_mask(CRC_RX_CONFIG, 0x01)
        self.write_register_or_mask(CRC_TX_CONFIG, 0x01)

        self.send_data([0x93, 0x70] + cl1, valid_bits=0x00)
        if not self._wait_irq(RX_IRQ_STAT, timeout=self.rf_timeout,
                              raise_on_error=False):
            logging.debug("PN5180 no SAK response")
            return None
        sak = self.read_data(1)
        if len(sak) != 1:
            return None
        buffer[2] = sak[0]
        if sak[0] in (0xFF, 0x7F, 0x80):
            logging.debug("PN5180 rejected suspicious SAK: %s", sak)
            return None

        if cl1[0] == 0x88:
            uid = cl1[1:4]
            self.write_register_and_mask(CRC_RX_CONFIG, 0xFFFFFFFE)
            self.write_register_and_mask(CRC_TX_CONFIG, 0xFFFFFFFE)
            self.send_data([0x95, 0x20], valid_bits=0x00)
            if not self._wait_irq(RX_IRQ_STAT, timeout=self.rf_timeout,
                                  raise_on_error=False):
                logging.debug("PN5180 no CL2 anticollision response")
                return None
            cl2 = self.read_data(5)
            if len(cl2) != 5:
                return None
            if self._is_suspicious_bytes(cl2):
                logging.debug("PN5180 rejected suspicious CL2 response: %s", cl2)
                return None
            if not self._bcc_ok(cl2):
                logging.debug("PN5180 rejected CL2 with bad BCC: %s", cl2)
                return None

            self.write_register_or_mask(CRC_RX_CONFIG, 0x01)
            self.write_register_or_mask(CRC_TX_CONFIG, 0x01)
            self.send_data([0x95, 0x70] + cl2, valid_bits=0x00)
            if not self._wait_irq(RX_IRQ_STAT, timeout=self.rf_timeout,
                                  raise_on_error=False):
                logging.debug("PN5180 no SAK2 response")
                return None
            sak2 = self.read_data(1)
            if len(sak2) != 1:
                return None
            buffer[2] = sak2[0]
            uid += cl2[0:4]
        else:
            uid = cl1[0:4]

        if self._is_suspicious_bytes(uid):
            logging.debug("PN5180 rejected suspicious UID: %s", uid)
            return None

        # First version is focused on NTAG / Ultralight style tags.
        if buffer[2] not in (0x00, 0x04):
            logging.debug("PN5180 rejected non-NTAG SAK=0x%02X UID=%s",
                          buffer[2], uid)
            return None

        self.current_uid = list(uid)
        self.current_uid_lsb_first = None
        self.current_uid_hex = " ".join("%02X" % (b,) for b in self.current_uid)
        return {
            "uid": self.current_uid,
            "uid_length": len(self.current_uid),
            "atqa": atqa,
            "sak": buffer[2],
        }

    def mifare_halt(self):
        try:
            self.send_data([0x50, 0x00], valid_bits=0x00)
        except Exception:
            pass

    def read_passive_target_id(self, timeout=0.5):
        if not self.initialized:
            return False, None
        try:
            tag = self.activate_type_a(wakeup=True)
            if not tag:
                self.current_uid = None
                self.current_uid_lsb_first = None
                self.current_uid_hex = ""
                return False, None
            return True, tag["uid"]
        except Exception as e:
            logging.debug("PN5180 tag detection failed: %s", e)
            self.initialize(announce=False)
            self.current_uid = None
            self.current_uid_lsb_first = None
            self.current_uid_hex = ""
            return False, None

    def iso15693_inventory(self):
        self.setup_iso15693_rf()
        flags = (ISO15693_FLAG_HIGH_DATA_RATE | ISO15693_FLAG_INVENTORY
                 | ISO15693_FLAG_ONE_SLOT)
        self.send_data([flags, ISO15693_CMD_INVENTORY, 0x00], valid_bits=0x00)
        if not self._wait_irq(RX_IRQ_STAT, timeout=self.rf_timeout,
                              raise_on_error=False):
            logging.debug("PN5180 no ISO15693 inventory response")
            return None
        rx_len = self.rx_bytes_received()
        if rx_len < 10 or rx_len == RX_BYTES_RECEIVED_MASK:
            logging.debug("PN5180 invalid ISO15693 inventory length %d", rx_len)
            return None
        data = self.read_data(rx_len)
        if len(data) < 10 or data[0] & 0x01:
            logging.debug("PN5180 invalid ISO15693 inventory data: %s", data)
            return None
        uid_lsb_first = data[2:10]
        uid = list(reversed(uid_lsb_first))
        self.current_uid = uid
        self.current_uid_lsb_first = list(uid_lsb_first)
        self.current_uid_hex = " ".join("%02X" % (b,) for b in uid)
        return {
            "uid": uid,
            "uid_length": len(uid),
            "dsfid": data[1],
        }

    def read_iso15693_target_id(self):
        if not self.initialized:
            return False, None
        try:
            tag = self.iso15693_inventory()
            if not tag:
                self.current_uid = None
                self.current_uid_lsb_first = None
                self.current_uid_hex = ""
                return False, None
            return True, tag["uid"]
        except Exception as e:
            logging.debug("PN5180 ISO15693 detection failed: %s", e)
            self.initialize(announce=False)
            self.current_uid = None
            self.current_uid_lsb_first = None
            self.current_uid_hex = ""
            return False, None

    def iso15693_read_single_block(self, block):
        self.send_data([
            ISO15693_FLAG_HIGH_DATA_RATE,
            ISO15693_CMD_READ_SINGLE_BLOCK,
            block & 0xFF,
        ], valid_bits=0x00)
        if not self._wait_irq(RX_IRQ_STAT, timeout=self.rf_timeout,
                              raise_on_error=False):
            raise PN5180Error("timeout waiting ISO15693 block %d" % (block,))
        rx_len = self.rx_bytes_received()
        if rx_len in (0, RX_BYTES_RECEIVED_MASK):
            raise PN5180Error(
                "invalid ISO15693 RX_STATUS length %d" % (rx_len,))
        data = self.read_data(rx_len)
        if not data:
            raise PN5180Error("short ISO15693 block response")
        if data[0] & 0x01:
            code = data[1] if len(data) > 1 else 0
            raise PN5180Error(
                "ISO15693 block %d error response 0x%02X" % (block, code))
        return data[1:]

    def iso15693_read_multiple_blocks(self, start_block, block_count):
        if block_count < 1:
            return bytearray()
        if block_count > 16:
            raise PN5180Error("ISO15693 batch too large: %d" % (block_count,))
        if not self.current_uid_lsb_first or len(self.current_uid_lsb_first) != 8:
            raise PN5180Error("ISO15693 addressed read requires a detected UID")
        self.send_data(
            [ISO15693_FLAG_HIGH_DATA_RATE | ISO15693_FLAG_ADDRESS,
             ISO15693_CMD_READ_MULTIPLE_BLOCKS]
            + list(self.current_uid_lsb_first)
            + [start_block & 0xFF, (block_count - 1) & 0xFF],
            valid_bits=0x00)
        if not self._wait_irq(RX_IRQ_STAT, timeout=self.rf_timeout,
                              raise_on_error=False):
            raise PN5180Error(
                "timeout waiting ISO15693 blocks %d-%d" % (
                    start_block, start_block + block_count - 1))
        rx_len = self.rx_bytes_received()
        expected_len = 1 + block_count * 4
        if rx_len in (0, RX_BYTES_RECEIVED_MASK):
            raise PN5180Error(
                "invalid ISO15693 RX_STATUS length %d" % (rx_len,))
        data = self.read_data(rx_len)
        if not data:
            raise PN5180Error("short ISO15693 multiple-block response")
        if data[0] & 0x01:
            code = data[1] if len(data) > 1 else 0
            raise PN5180Error(
                "ISO15693 blocks %d-%d error response 0x%02X" % (
                    start_block, start_block + block_count - 1, code))
        if len(data) != expected_len:
            raise PN5180Error(
                "unexpected ISO15693 blocks %d-%d length %d, expected %d" % (
                    start_block, start_block + block_count - 1,
                    len(data), expected_len))
        return bytearray(data[1:])

    def iso15693_read_user_memory(self, start_block=DEFAULT_ISO15693_START_BLOCK,
                                  end_block=DEFAULT_ISO15693_END_BLOCK,
                                  batch_size=1):
        read_start = time.time()
        user_data = bytearray()
        reads = 0
        blocks_read = 0
        stop_reason = "end_block"
        last_block = start_block - 1
        batch_size = max(1, min(int(batch_size), 16))
        block = start_block
        while block <= end_block:
            count = min(batch_size, end_block - block + 1)
            if count == 1:
                block_data = bytearray(self.iso15693_read_single_block(block))
            else:
                block_data = self.iso15693_read_multiple_blocks(block, count)
            reads += 1
            blocks_read += count
            last_block = block + count - 1
            if not block_data:
                stop_reason = "empty_response"
                break
            user_data.extend(block_data)
            expected_len = self._expected_tlv_total_length(user_data)
            if expected_len and len(user_data) >= expected_len:
                user_data = user_data[:expected_len]
                stop_reason = "tlv_complete"
                break
            if expected_len is None and self._is_empty_data(block_data):
                stop_reason = "empty_block"
                break
            block += count
        try:
            self.rf_off()
        except Exception:
            pass
        elapsed = time.time() - read_start
        self.last_read_stats = {
            "protocol": "iso15693",
            "read_mode": "multiple" if batch_size > 1 else "single",
            "batch_size": batch_size,
            "reads": reads,
            "blocks": blocks_read,
            "bytes": len(user_data),
            "first_block": start_block,
            "last_block": last_block,
            "elapsed_ms": int(elapsed * 1000.0),
            "avg_read_ms": int((elapsed * 1000.0 / reads) if reads else 0),
            "stop_reason": stop_reason,
        }
        return user_data

    def ntag_read_page(self, page):
        last_error = None
        for attempt in range(self.page_read_retries):
            try:
                self.send_data([MIFARE_CMD_READ, page], valid_bits=0x00)
                if not self._wait_irq(RX_IRQ_STAT, timeout=self.rf_timeout,
                                      raise_on_error=False):
                    raise PN5180Error("timeout waiting NTAG page %d" % (page,))

                rx_len = self.rx_bytes_received()
                if rx_len in (0, RX_BYTES_RECEIVED_MASK):
                    raise PN5180Error(
                        "invalid RX_STATUS length %d; possible SPI/noise issue" % (
                            rx_len,))
                if rx_len != 16:
                    raise PN5180Error(
                        "unexpected NTAG page %d length %d" % (page, rx_len))

                data = self.read_data(16)
                if len(data) != 16:
                    raise PN5180Error("short NTAG page response")
                return data
            except Exception as e:
                last_error = e
                logging.debug("PN5180 read page %d attempt %d failed: %s",
                              page, attempt + 1, e)
                if attempt + 1 < self.page_read_retries:
                    self.initialize(announce=False)
                    if not self.activate_type_a(wakeup=True):
                        last_error = PN5180Error(
                            "tag lost while retrying page %d" % (page,))
                        break
                time.sleep(0.01)
        raise PN5180Error("read page %d failed: %s" % (page, last_error))

    def ntag_read_user_memory(self, start_page=DEFAULT_NTAG_START_PAGE,
                              end_page=DEFAULT_NTAG_END_PAGE):
        read_start = time.time()
        user_data = bytearray()
        page = start_page
        reads = 0
        stop_reason = "end_page"
        last_page = start_page - 1
        while page <= end_page:
            block = self.ntag_read_page(page)
            reads += 1
            last_page = page
            remaining_pages = end_page - page + 1
            copy_len = min(remaining_pages, 4) * 4
            copied = block[:copy_len]
            user_data.extend(copied)
            expected_len = self._expected_tlv_total_length(user_data)
            if expected_len and len(user_data) >= expected_len:
                user_data = user_data[:expected_len]
                stop_reason = "tlv_complete"
                break
            if expected_len is None and self._is_empty_data(copied):
                stop_reason = "empty_page"
                break
            page += 4
        self.mifare_halt()
        elapsed = time.time() - read_start
        self.last_read_stats = {
            "protocol": "ntag",
            "reads": reads,
            "pages": reads * 4,
            "bytes": len(user_data),
            "first_page": start_page,
            "last_page": last_page,
            "elapsed_ms": int(elapsed * 1000.0),
            "avg_read_ms": int((elapsed * 1000.0 / reads) if reads else 0),
            "stop_reason": stop_reason,
        }
        return user_data

    @staticmethod
    def _is_empty_data(data):
        return bool(data) and all(b == 0x00 for b in data)

    @staticmethod
    def _expected_tlv_total_length(data):
        data = bytes(data)
        offsets = [0]
        if len(data) >= 4 and data[0] in (0xE1, 0xE2):
            offsets.insert(0, 4)
        for offset in offsets:
            total = PN5180Handler._expected_tlv_total_length_at(data, offset)
            if total is not None:
                return total
        return None

    @staticmethod
    def _expected_tlv_total_length_at(data, offset):
        pos = offset
        while pos < len(data):
            tlv_type = data[pos]
            pos += 1
            if tlv_type == 0x00:
                continue
            if tlv_type == 0xFE:
                return pos
            if pos >= len(data):
                return None
            tlv_len = data[pos]
            pos += 1
            if tlv_len == 0xFF:
                if pos + 2 > len(data):
                    return None
                tlv_len = (data[pos] << 8) | data[pos + 1]
                pos += 2
            total = pos + tlv_len
            if tlv_type == 0x03:
                if total < len(data) and data[total] == 0xFE:
                    total += 1
                return total
            pos = total
        return None


class PN5180Service:
    def __init__(self, reactor, period):
        self.reactor = reactor
        self.period = period
        self.running = False
        self.timer = None
        self.func = None
        self.params = None
        self.callback = None

    def start(self):
        if self.running:
            return False
        self.running = True
        waketime = self.reactor.monotonic() + self.period
        self.timer = self.reactor.register_timer(self.periodic_task, waketime)
        return True

    def stop(self):
        if not self.running:
            return False
        self.running = False
        self.teardown()
        return True

    def schedule(self, func, params=None, callback=None):
        if self.func and self.running:
            logging.warning("PN5180 scheduled function already running")
            return False
        self.func = func
        self.params = params
        self.callback = callback
        return True

    def teardown(self):
        if self.timer:
            self.reactor.unregister_timer(self.timer)
            self.timer = None
        self.func = None
        self.params = None
        self.callback = None

    def periodic_task(self, eventtime):
        if self.func is None or self.timer is None:
            return self.reactor.NEVER

        result = self.func(**self.params) if self.params is not None else self.func()
        if result and self.callback:
            self.callback(result)

        next_waketime = self.reactor.monotonic() + self.period
        if self.timer:
            self.reactor.update_timer(self.timer, next_waketime)
            return next_waketime
        return self.reactor.NEVER


class PN5180Manager:
    def __init__(self, printer, spi, config):
        self.printer = printer
        self.reactor = printer.get_reactor()
        self.gcode = printer.lookup_object("gcode")
        self.handler = PN5180Handler(printer, spi, config)
        self.tag_protocol = config.get("tag_protocol", "auto").lower()
        if self.tag_protocol not in ("auto", "ntag", "iso15693"):
            raise config.error(
                "Option 'tag_protocol' in section '%s' must be auto, ntag, "
                "or iso15693" % (config.get_name(),))
        self.debug_log = config.getboolean("debug_log", True)
        self.happyhare_enable = config.getboolean("happyhare_enable", True)
        self.iso15693_blocks_per_read = config.getint(
            "iso15693_blocks_per_read", 8, minval=1, maxval=16)
        self.comm_check_interval = config.getint(
            "comm_check_interval", 10, minval=0)
        self.last_uid = None
        self.last_tag_protocol = ""
        self.waiting_for_removal = False
        self.waiting_notice_sent = False
        self.consecutive_no_tag = 0
        self.communication_lost = False
        self.communication_lost_notice_sent = False
        self.scan_count = 0
        self.last_scan_time = 0.0
        self.last_scan_result = "idle"
        self.last_scan_message = ""
        self.last_error = ""
        self.last_protocol = ""
        self.last_uid_hex = ""
        self.last_spool_id = ""
        self.last_data_preview = ""
        self.recent_events = []

    def initialize(self):
        return self.handler.initialize()

    def _now(self):
        return self.reactor.monotonic()

    def _uid_hex(self, uid):
        return " ".join("%02X" % (b,) for b in uid)

    def _add_event(self, result, message="", uid=None, spool_id="",
                   protocol=""):
        event = {
            "time": self._now(),
            "result": result,
            "message": message,
            "protocol": protocol or "",
            "uid": self._uid_hex(uid) if uid else "",
            "spool_id": spool_id or "",
        }
        self.recent_events.append(event)
        self.recent_events = self.recent_events[-12:]

    def _set_scan_status(self, result, message="", uid=None, spool_id="",
                         error="", data_preview=None, protocol=None,
                         add_event=True):
        self.last_scan_time = self._now()
        self.last_scan_result = result
        self.last_scan_message = message
        self.last_error = error or ""
        if protocol is not None:
            self.last_protocol = protocol
        if uid is not None:
            self.last_uid_hex = self._uid_hex(uid)
        if spool_id:
            self.last_spool_id = spool_id
        if data_preview is not None:
            self.last_data_preview = data_preview
        if add_event:
            self._add_event(
                result, message, uid=uid, spool_id=spool_id,
                protocol=self.last_protocol)

    def _debug_scan_line(self):
        if not self.debug_log:
            return
        parts = [
            "PN5180 scan #%d:" % (self.scan_count,),
            self.last_scan_result,
        ]
        if self.last_scan_message:
            parts.append(self.last_scan_message)
        if self.last_protocol:
            parts.append("protocol=%s" % (self.last_protocol,))
        if self.last_uid_hex:
            parts.append("uid=%s" % (self.last_uid_hex,))
        if self.last_spool_id:
            parts.append("spool_id=%s" % (self.last_spool_id,))
        if self.waiting_for_removal:
            parts.append("waiting_removal=1")
        if self.last_error:
            parts.append("error=%s" % (self.last_error,))
        line = " ".join(parts)
        self.gcode.respond_info(line)
        logging.info(line)

    def _debug_profile_line(self, timings=None):
        if not self.debug_log:
            return
        stats = dict(self.handler.last_read_stats or {})
        parts = ["PN5180 profile:"]
        if timings:
            for key in ("detect_ms", "read_ms", "decode_ms", "total_ms"):
                if key in timings:
                    parts.append("%s=%d" % (key, timings[key]))
        for key in (
                "protocol", "read_mode", "batch_size", "reads", "blocks",
                "pages", "bytes",
                "first_block", "last_block", "first_page", "last_page",
                "elapsed_ms", "avg_read_ms", "stop_reason"):
            if key in stats:
                parts.append("%s=%s" % (key, stats[key]))
        line = " ".join(parts)
        self.gcode.respond_info(line)
        logging.info(line)

    def _communication_watchdog(self):
        if not self.comm_check_interval:
            return True
        if self.consecutive_no_tag < self.comm_check_interval:
            return True
        if self.consecutive_no_tag % self.comm_check_interval:
            return True

        try:
            self.handler.check_communication()
            self.communication_lost = False
            self.communication_lost_notice_sent = False
            return True
        except Exception as e:
            message = (
                "PN5180 communication lost: %s. "
                "SPI reads are invalid; trying hardware reset."
                % (e,))
            logging.warning(message)

            self.gcode.respond_info(
                "PN5180 communication watchdog triggered; trying hardware reset.")
            if self.handler.initialize():
                self.consecutive_no_tag = 0
                self.communication_lost = False
                self.communication_lost_notice_sent = False
                self._set_scan_status(
                    "comm_recovered",
                    "PN5180 recovered by hardware reset")
                return True

            self.communication_lost = True
            self.handler.initialized = False
            self._set_scan_status(
                "comm_lost",
                "PN5180 SPI reads invalid after hardware reset",
                error=str(e))
            if not self.communication_lost_notice_sent:
                self.gcode.respond_info(message)
                self.communication_lost_notice_sent = True
            return False

    def get_status(self):
        return {
            "tag_protocol": self.tag_protocol,
            "debug_log": self.debug_log,
            "happyhare_enable": self.happyhare_enable,
            "iso15693_blocks_per_read": self.iso15693_blocks_per_read,
            "comm_check_interval": self.comm_check_interval,
            "consecutive_no_tag": self.consecutive_no_tag,
            "communication_lost": self.communication_lost,
            "scan_count": self.scan_count,
            "last_scan_time": self.last_scan_time,
            "last_scan_result": self.last_scan_result,
            "last_scan_message": self.last_scan_message,
            "last_error": self.last_error,
            "last_protocol": self.last_protocol,
            "last_uid": self.last_uid_hex,
            "last_spool_id": self.last_spool_id,
            "last_data_preview": self.last_data_preview,
            "waiting_for_removal": self.waiting_for_removal,
            "recent_events": list(self.recent_events),
        }

    def _protocols_to_try(self):
        if self.tag_protocol == "ntag":
            return ["ntag"]
        if self.tag_protocol == "iso15693":
            return ["iso15693"]
        return ["ntag", "iso15693"]

    def _protocol_label(self, protocol):
        if protocol == "iso15693":
            return "ISO15693"
        return "NTAG"

    def _scan_message(self):
        if self.tag_protocol == "auto":
            return "Scanning for NTAG/ISO15693"
        return "Scanning for %s" % (self._protocol_label(self.tag_protocol),)

    def _detect_tag(self):
        for protocol in self._protocols_to_try():
            if protocol == "ntag":
                success, uid = self.handler.read_passive_target_id(timeout=0.5)
            else:
                success, uid = self.handler.read_iso15693_target_id()
            if success and uid:
                return {
                    "protocol": protocol,
                    "uid": list(uid),
                }
        return None

    def _search_spool_id_in_obj(self, obj):
        if isinstance(obj, dict):
            for key, value in obj.items():
                if key.lower() in ("spool_id", "spool", "filament"):
                    if isinstance(value, (str, int, float)):
                        return str(value).strip()
                nested = self._search_spool_id_in_obj(value)
                if nested:
                    return nested
        elif isinstance(obj, list):
            for item in obj:
                nested = self._search_spool_id_in_obj(item)
                if nested:
                    return nested
        return None

    def _decode_ndef_text_record(self, data):
        pos = 0
        while pos + 3 <= len(data):
            header = data[pos]
            type_len = data[pos + 1]
            short_record = bool(header & 0x10)
            has_id = bool(header & 0x08)
            pos += 2
            if short_record:
                if pos >= len(data):
                    return None
                payload_len = data[pos]
                pos += 1
            else:
                if pos + 4 > len(data):
                    return None
                payload_len = ((data[pos] << 24) | (data[pos + 1] << 16)
                               | (data[pos + 2] << 8) | data[pos + 3])
                pos += 4
            id_len = 0
            if has_id:
                if pos >= len(data):
                    return None
                id_len = data[pos]
                pos += 1
            if pos + type_len + id_len + payload_len > len(data):
                return None
            record_type = data[pos:pos + type_len]
            pos += type_len + id_len
            payload = data[pos:pos + payload_len]
            pos += payload_len

            if record_type == b"T" and payload:
                status = payload[0]
                lang_len = status & 0x3F
                if 1 + lang_len <= len(payload):
                    text = payload[1 + lang_len:]
                    encoding = "utf-16" if status & 0x80 else "utf-8"
                    return text.decode(encoding, errors="replace").strip()
            if header & 0x40:
                break
        return None

    def _decode_tag_user_data(self, user_data):
        data = bytes(user_data)
        offsets = [0]
        if len(data) >= 4 and data[0] in (0xE1, 0xE2):
            offsets.insert(0, 4)
        for offset in offsets:
            text = self._decode_tlv_data(data, offset)
            if text:
                return text

        terminator = data.find(b"\xFE")
        if terminator >= 0:
            data = data[:terminator]
        return data.decode("utf-8", errors="ignore").rstrip("\x00").strip()

    def _decode_tlv_data(self, data, offset=0):
        pos = offset
        while pos < len(data):
            tlv_type = data[pos]
            pos += 1
            if tlv_type == 0x00:
                continue
            if tlv_type == 0xFE:
                break
            if pos >= len(data):
                break
            tlv_len = data[pos]
            pos += 1
            if tlv_len == 0xFF:
                if pos + 2 > len(data):
                    break
                tlv_len = (data[pos] << 8) | data[pos + 1]
                pos += 2
            value = data[pos:pos + tlv_len]
            pos += tlv_len
            if tlv_type == 0x03:
                text = self._decode_ndef_text_record(value)
                if text:
                    return text
                return value.decode("utf-8", errors="ignore").strip()
        return None

    def _decode_ntag_user_data(self, user_data):
        return self._decode_tag_user_data(user_data)

    def _extract_spool_id(self, data_str):
        if not data_str:
            return None

        text = data_str.strip()
        try:
            obj = json.loads(text)
            spool_id = self._search_spool_id_in_obj(obj)
            if spool_id:
                return spool_id
        except Exception:
            pass

        pattern = re.compile(
            r'"?(?:spool_id|spool|filament)"?\s*[:=]\s*["`]?([0-9A-Za-z_\-]+)',
            re.IGNORECASE | re.DOTALL)
        match = pattern.search(text)
        if match:
            value = re.sub(r"\s+", "", match.group(1))
            if value:
                return value
        return None

    def _apply_spool_id(self, spool_id):
        if not self.happyhare_enable:
            self.gcode.respond_info(
                "HappyHare spool ID found: %s. Dispatch disabled." % (
                    spool_id,))
            logging.info(
                "PN5180 spool ID found but HappyHare dispatch disabled: %s",
                spool_id)
            return True
        command = "MMU_GATE_MAP NEXT_SPOOLID=%s" % (spool_id,)
        try:
            self.gcode.run_script(command)
            self.gcode.respond_info(
                "HappyHare spool ID found: %s. Command dispatched." % (
                    spool_id,))
            logging.info("PN5180 dispatched command: %s", command)
            return True
        except Exception as e:
            logging.exception("Failed to dispatch spool ID '%s': %s", spool_id, e)
            self.gcode.respond_info(
                "Failed to apply spool ID '%s'. Check logs." % (spool_id,))
            self._set_scan_status(
                "dispatch_error",
                "Failed to dispatch spool ID",
                spool_id=spool_id,
                error=str(e))
            return False

    def rfid_read(self, report_no_tag=False, iso15693_blocks_per_read=None):
        if iso15693_blocks_per_read is None:
            iso15693_blocks_per_read = self.iso15693_blocks_per_read
        scan_start = time.time()
        self.scan_count += 1
        self._set_scan_status(
            "scanning", self._scan_message(), protocol="", add_event=False)
        try:
            if self.communication_lost:
                self._set_scan_status(
                    "comm_lost",
                    "PN5180 SPI reads invalid; trying hardware reset")
                if not self.handler.initialize():
                    self._debug_scan_line()
                    return None
                self.communication_lost = False
                self.communication_lost_notice_sent = False

            if self.waiting_for_removal:
                tag = self._detect_tag()
                if tag:
                    self.consecutive_no_tag = 0
                    uid_list = tag["uid"]
                    protocol = tag["protocol"]
                    if (self.last_uid and uid_list == self.last_uid
                            and protocol == self.last_tag_protocol):
                        self._set_scan_status(
                            "waiting_removal",
                            "Tag already processed; waiting for removal",
                            uid=uid_list,
                            protocol=protocol,
                            add_event=False)
                        if not self.waiting_notice_sent:
                            self.gcode.respond_info(
                                "Tag already processed. Remove it before re-reading.")
                            self.waiting_notice_sent = True
                        self._debug_scan_line()
                        return None

                    self.waiting_for_removal = False
                    self.waiting_notice_sent = False
                    self.last_uid = None
                    self.last_tag_protocol = ""
                else:
                    self.consecutive_no_tag += 1
                    if self.waiting_notice_sent:
                        self.gcode.respond_info("Tag removed. Reader is ready.")
                    self._set_scan_status(
                        "tag_removed", "Tag removed; reader ready",
                        protocol="")
                    self.waiting_for_removal = False
                    self.waiting_notice_sent = False
                    self.last_uid = None
                    self.last_tag_protocol = ""
                    self._communication_watchdog()
                    self._debug_scan_line()
                    return None

            detect_start = time.time()
            tag = self._detect_tag()
            detect_ms = int((time.time() - detect_start) * 1000.0)
            if not tag:
                self.consecutive_no_tag += 1
                self._set_scan_status(
                    "no_tag", "No supported tag detected", protocol="",
                    add_event=report_no_tag)
                if not self._communication_watchdog():
                    self._debug_scan_line()
                    return None
                if report_no_tag:
                    self.gcode.respond_info("PN5180 scan complete: no tag detected")
                self._debug_scan_line()
                return None

            self.consecutive_no_tag = 0
            self.communication_lost = False
            self.communication_lost_notice_sent = False
            protocol = tag["protocol"]
            protocol_label = self._protocol_label(protocol)
            uid_list = tag["uid"]
            uid_str = " ".join("%02X" % (b,) for b in uid_list)
            self.gcode.respond_info("=" * 50)
            self.gcode.respond_info("PN5180 %s detected" % (protocol_label,))
            self.gcode.respond_info("Card UID: %s" % (uid_str,))
            logging.info(
                "PN5180 %s card detected UID=%s", protocol_label, uid_str)
            self._set_scan_status(
                "tag_detected", "%s detected" % (protocol_label,),
                uid=uid_list, protocol=protocol)

            read_start = time.time()
            if protocol == "iso15693":
                user_data = self.handler.iso15693_read_user_memory(
                    batch_size=iso15693_blocks_per_read)
            else:
                user_data = self.handler.ntag_read_user_memory()
            read_ms = int((time.time() - read_start) * 1000.0)
            if not user_data:
                self.gcode.respond_info("No data on tag")
                self._set_scan_status(
                    "empty_tag", "No data on tag", uid=uid_list,
                    protocol=protocol)
                self._debug_scan_line()
                return None

            self.last_uid = uid_list
            self.last_tag_protocol = protocol
            self.waiting_for_removal = True
            self.waiting_notice_sent = False

            preview = " ".join("%02X" % (b,) for b in user_data[:32])
            self.gcode.respond_info("Data preview: %s" % (preview,))

            decode_start = time.time()
            data_str = self._decode_tag_user_data(user_data)
            data_preview = data_str[:120]
            if not data_str:
                self.gcode.respond_info("Tag is empty")
                self._set_scan_status(
                    "empty_tag", "Tag is empty", uid=uid_list,
                    data_preview=data_preview, protocol=protocol)
                self._debug_scan_line()
                return None

            json_start = data_str.find("{")
            if json_start > 0:
                data_str = data_str[json_start:]

            json_end = data_str.rfind("}")
            if json_end > 0 and json_end < len(data_str) - 1:
                data_str = data_str[:json_end + 1]
            decode_ms = int((time.time() - decode_start) * 1000.0)

            spool_id = self._extract_spool_id(data_str)
            if spool_id:
                if self._apply_spool_id(spool_id):
                    result = ("spool_applied" if self.happyhare_enable
                              else "spool_detected")
                    message = ("Spool ID applied" if self.happyhare_enable
                               else "Spool ID detected; dispatch disabled")
                    self._set_scan_status(
                        result,
                        message,
                        uid=uid_list,
                        spool_id=spool_id,
                        data_preview=data_preview,
                        protocol=protocol)
            else:
                self.gcode.respond_info("No HappyHare spool ID found in tag data.")
                self._set_scan_status(
                    "no_spool_id",
                    "No HappyHare spool ID found",
                    uid=uid_list,
                    data_preview=data_preview,
                    protocol=protocol)

            try:
                data_json = json.loads(data_str)
                formatted = json.dumps(data_json, indent=2, ensure_ascii=False)
                self.gcode.respond_info("%s data (JSON):" % (protocol_label,))
                for line in formatted.split("\n"):
                    self.gcode.respond_info("  %s" % (line,))
                self.gcode.respond_info("=" * 50)
                self._debug_profile_line({
                    "detect_ms": detect_ms,
                    "read_ms": read_ms,
                    "decode_ms": decode_ms,
                    "total_ms": int((time.time() - scan_start) * 1000.0),
                })
                self._debug_scan_line()
                return formatted
            except json.JSONDecodeError:
                cleaned = []
                for ch in data_str:
                    if (32 <= ord(ch) <= 126) or ch in ("\n", "\r"):
                        cleaned.append(ch)
                    else:
                        cleaned.append("<0x%02X>" % (ord(ch),))
                self.gcode.respond_info("Text data: %s" % ("".join(cleaned),))
                self.gcode.respond_info("=" * 50)
                self._debug_profile_line({
                    "detect_ms": detect_ms,
                    "read_ms": read_ms,
                    "decode_ms": decode_ms,
                    "total_ms": int((time.time() - scan_start) * 1000.0),
                })
                self._debug_scan_line()
                return data_str
        except Exception as e:
            self.gcode.respond_info("Error reading PN5180 tag: %s" % (e,))
            logging.exception("PN5180 read failed: %s", e)
            self._set_scan_status("error", "Read failed", error=str(e))
            self.handler.initialize(announce=False)
            self._debug_scan_line()
            return None


class PN5180:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.name = config.get_name().split()[1]
        self.gcode = self.printer.lookup_object("gcode")
        self.scan_period = config.getfloat("scan_period", 3.0, above=0.0)
        self.config = config

        self.spi = bus.MCU_SPI_from_config(
            config=config,
            mode=0,
            pin_option="cs_pin",
            default_speed=config.getint("spi_speed", 100000, minval=100000),
            share_type=None,
            cs_active_high=False)

        self.manager = PN5180Manager(self.printer, self.spi, self.config)
        self.service = None

        self.printer.register_event_handler("klippy:connect", self._handle_connect)
        self.gcode.register_mux_command(
            cmd="PN5180", key="NAME", value=self.name, func=self.cmd_PN5180)

    def _handle_connect(self):
        reactor = self.printer.get_reactor()
        reactor.register_timer(self._delayed_init, reactor.monotonic() + 1.0)

    def _delayed_init(self, eventtime):
        self.manager.initialize()
        return self.printer.get_reactor().NEVER

    def _init_service(self):
        self.service = PN5180Service(self.printer.get_reactor(), self.scan_period)

    def read_begin(self, iso15693_blocks_per_read=None):
        if self.manager is None:
            self.gcode.respond_info("PN5180 manager is not initialized")
            return
        if not self.manager.handler.initialized:
            if not self.manager.initialize():
                return
        if self.service is None:
            self._init_service()
        self.service.schedule(
            func=self.manager.rfid_read,
            params={"iso15693_blocks_per_read": iso15693_blocks_per_read})
        ret = self.service.start()
        if ret:
            self.gcode.respond_info(
                "PN5180 read started. ISO15693 blocks_per_read=%d" % (
                    iso15693_blocks_per_read
                    if iso15693_blocks_per_read is not None
                    else self.manager.iso15693_blocks_per_read,))
        else:
            self.gcode.respond_info("PN5180 read is already running.")

    def read_end(self):
        if self.service is None:
            self.gcode.respond_info("PN5180 read is not running.")
            return
        ret = self.service.stop()
        self.gcode.respond_info(
            "PN5180 read stopped." if ret else "PN5180 read is not running.")

    def scan_once(self, iso15693_blocks_per_read=None):
        if self.manager is None:
            self.gcode.respond_info("PN5180 manager is not initialized")
            return
        if not self.manager.handler.initialized:
            if not self.manager.initialize():
                return
        self.manager.rfid_read(
            report_no_tag=True,
            iso15693_blocks_per_read=iso15693_blocks_per_read)

    def get_status(self, eventtime):
        status = {
            "name": self.name,
            "reading": bool(self.service and self.service.running),
            "scan_period": self.scan_period,
        }
        if self.manager is None:
            status.update({
                "initialized": False,
                "tag_protocol": "",
                "debug_log": False,
                "happyhare_enable": False,
                "comm_check_interval": 0,
                "consecutive_no_tag": 0,
                "communication_lost": False,
                "scan_count": 0,
                "last_scan_result": "not_initialized",
                "last_scan_message": "PN5180 manager is not initialized",
                "last_error": "",
                "last_protocol": "",
                "last_uid": "",
                "last_spool_id": "",
                "last_data_preview": "",
                "waiting_for_removal": False,
                "recent_events": [],
            })
            return status
        handler = self.manager.handler
        status.update(self.manager.get_status())
        status.update({
            "initialized": handler.initialized,
            "firmware": ("%d.%d" % (handler.firmware[1], handler.firmware[0])
                         if handler.firmware else ""),
        })
        return status

    def show_status(self):
        if self.manager is None:
            self.gcode.respond_info("PN5180 manager is not initialized")
            return
        handler = self.manager.handler
        self.gcode.respond_info("PN5180 initialized: %s" % (handler.initialized,))
        if handler.product_version:
            self.gcode.respond_info("PN5180 product raw: %s" % (
                handler._format_bytes(handler.product_version),))
        if handler.firmware:
            self.gcode.respond_info(
                "PN5180 firmware: v%d.%d" % (
                    handler.firmware[1], handler.firmware[0]))
            self.gcode.respond_info("PN5180 firmware raw: %s" % (
                handler._format_bytes(handler.firmware),))
        if handler.eeprom_version:
            self.gcode.respond_info("PN5180 EEPROM raw: %s" % (
                handler._format_bytes(handler.eeprom_version),))
        if handler.current_uid_hex:
            self.gcode.respond_info("Last UID: %s" % (handler.current_uid_hex,))
        status = self.manager.get_status()
        self.gcode.respond_info("PN5180 reading: %s" % (
            bool(self.service and self.service.running),))
        self.gcode.respond_info("PN5180 tag protocol: %s" % (
            status["tag_protocol"],))
        self.gcode.respond_info("PN5180 debug log: %s" % (
            status["debug_log"],))
        self.gcode.respond_info("PN5180 HappyHare dispatch: %s" % (
            status["happyhare_enable"],))
        self.gcode.respond_info("PN5180 ISO15693 blocks per read: %d" % (
            status["iso15693_blocks_per_read"],))
        self.gcode.respond_info("PN5180 communication lost: %s" % (
            status["communication_lost"],))
        self.gcode.respond_info("PN5180 consecutive no-tag scans: %d" % (
            status["consecutive_no_tag"],))
        self.gcode.respond_info("PN5180 scan count: %d" % (
            status["scan_count"],))
        self.gcode.respond_info("PN5180 last result: %s - %s" % (
            status["last_scan_result"], status["last_scan_message"]))
        if status["last_protocol"]:
            self.gcode.respond_info("PN5180 last protocol: %s" % (
                status["last_protocol"],))
        if status["last_uid"]:
            self.gcode.respond_info("PN5180 last UID: %s" % (status["last_uid"],))
        if status["last_spool_id"]:
            self.gcode.respond_info("PN5180 last spool ID: %s" % (
                status["last_spool_id"],))
        if status["last_error"]:
            self.gcode.respond_info("PN5180 last error: %s" % (
                status["last_error"],))
        if status["recent_events"]:
            self.gcode.respond_info("PN5180 recent events:")
            for event in status["recent_events"][-5:]:
                detail = event["result"]
                if event.get("protocol"):
                    detail += " protocol=%s" % (event["protocol"],)
                if event["uid"]:
                    detail += " uid=%s" % (event["uid"],)
                if event["spool_id"]:
                    detail += " spool_id=%s" % (event["spool_id"],)
                if event["message"]:
                    detail += " - %s" % (event["message"],)
                self.gcode.respond_info("  %s" % (detail,))

    def run_diag(self):
        if self.manager is None:
            self.gcode.respond_info("PN5180 manager is not initialized")
            return
        handler = self.manager.handler
        self.gcode.respond_info("PN5180 diagnostics:")
        for label, addr in [
                ("EEPROM product raw", PRODUCT_VERSION),
                ("EEPROM firmware raw", FIRMWARE_VERSION),
                ("EEPROM version raw", EEPROM_VERSION)]:
            try:
                data = handler.read_eeprom(addr, 2)
                self.gcode.respond_info("  %s: %s" % (
                    label, handler._format_bytes(data)))
            except Exception as e:
                self.gcode.respond_info("  %s read failed: %s" % (label, e))
        for name, reg in [
                ("SYSTEM_CONFIG", SYSTEM_CONFIG),
                ("IRQ_STATUS", IRQ_STATUS),
                ("RX_STATUS", RX_STATUS),
                ("RF_STATUS", RF_STATUS)]:
            try:
                value = handler.read_register(reg)
                self.gcode.respond_info("  %s: 0x%08X" % (name, value))
            except Exception as e:
                self.gcode.respond_info("  %s read failed: %s" % (name, e))
        self.gcode.respond_info(
            "  If values are all 0xFFFFFFFF, check MISO/CS/SPI bus/power.")

    def recover(self):
        if self.manager is None:
            self.gcode.respond_info("PN5180 manager is not initialized")
            return
        ok = self.manager.initialize()
        if ok:
            self.manager.communication_lost = False
            self.manager.communication_lost_notice_sent = False
            self.gcode.respond_info("PN5180 hardware reset complete.")
        else:
            self.gcode.respond_info(
                "PN5180 hardware reset failed; run DIAG=1 and check power/SPI/RST.")

    def cmd_PN5180(self, gcmd):
        read_flag = gcmd.get_int("READ", None)
        scan_flag = gcmd.get_int("SCAN", 0)
        init_flag = gcmd.get_int("INIT", 0)
        status_flag = gcmd.get_int("STATUS", 0)
        diag_flag = gcmd.get_int("DIAG", 0)
        recover_flag = gcmd.get_int("RECOVER", 0)
        debug_flag = gcmd.get_int("DEBUG", None)
        happyhare_flag = gcmd.get_int("HAPPYHARE", None)
        iso15693_blocks_per_read = gcmd.get_int(
            "ISO15693_BLOCKS_PER_READ", None, minval=1, maxval=16)

        if happyhare_flag is not None:
            self.manager.happyhare_enable = bool(happyhare_flag)
            self.gcode.respond_info("PN5180 HappyHare dispatch: %s" % (
                self.manager.happyhare_enable,))
        elif debug_flag is not None:
            self.manager.debug_log = bool(debug_flag)
            self.gcode.respond_info("PN5180 debug log: %s" % (
                self.manager.debug_log,))
        elif read_flag == 1:
            self.read_begin(
                iso15693_blocks_per_read=iso15693_blocks_per_read)
        elif read_flag == 0:
            self.read_end()
        elif scan_flag == 1:
            self.scan_once(
                iso15693_blocks_per_read=iso15693_blocks_per_read)
        elif init_flag == 1:
            self.manager.initialize()
        elif status_flag == 1:
            self.show_status()
        elif diag_flag == 1:
            self.run_diag()
        elif recover_flag == 1:
            self.recover()
        else:
            self.gcode.respond_info("PN5180 command usage:")
            self.gcode.respond_info(
                "  PN5180 NAME=%s READ=1   - start periodic reading" % (
                    self.name,))
            self.gcode.respond_info(
                "  PN5180 NAME=%s READ=0   - stop periodic reading" % (
                    self.name,))
            self.gcode.respond_info(
                "  PN5180 NAME=%s SCAN=1   - read once" % (self.name,))
            self.gcode.respond_info(
                "  PN5180 NAME=%s ISO15693_BLOCKS_PER_READ=8 SCAN=1   - override ISO15693 blocks per read" % (
                    self.name,))
            self.gcode.respond_info(
                "  PN5180 NAME=%s STATUS=1 - show status" % (self.name,))
            self.gcode.respond_info(
                "  PN5180 NAME=%s DIAG=1   - read diagnostic registers" % (
                    self.name,))
            self.gcode.respond_info(
                "  PN5180 NAME=%s RECOVER=1 - hardware reset PN5180" % (
                    self.name,))
            self.gcode.respond_info(
                "  PN5180 NAME=%s DEBUG=1  - enable per-scan debug output" % (
                    self.name,))
            self.gcode.respond_info(
                "  PN5180 NAME=%s DEBUG=0  - disable per-scan debug output" % (
                    self.name,))
            self.gcode.respond_info(
                "  PN5180 NAME=%s HAPPYHARE=1 - enable MMU_GATE_MAP dispatch" % (
                    self.name,))
            self.gcode.respond_info(
                "  PN5180 NAME=%s HAPPYHARE=0 - disable MMU_GATE_MAP dispatch" % (
                    self.name,))


def load_config_prefix(config):
    return PN5180(config)
