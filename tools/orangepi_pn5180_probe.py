#!/usr/bin/env python3
import argparse
import os
import time

import spidev


GPIO_BASE = "/sys/class/gpio"
DEFAULT_RESET_GPIO = 117  # Allwinner PD21 = port D(3) * 32 + 21


def hx(data):
    return " ".join("%02X" % (b,) for b in data)


class SysfsGPIO:
    def __init__(self, number):
        self.number = int(number)
        self.path = os.path.join(GPIO_BASE, "gpio%d" % (self.number,))

    def export(self):
        if not os.path.exists(self.path):
            with open(os.path.join(GPIO_BASE, "export"), "w") as f:
                f.write(str(self.number))
            for _ in range(100):
                if os.path.exists(self.path):
                    break
                time.sleep(0.01)

    def set_direction(self, direction):
        with open(os.path.join(self.path, "direction"), "w") as f:
            f.write(direction)

    def write(self, value):
        with open(os.path.join(self.path, "value"), "w") as f:
            f.write("1" if value else "0")

    def read(self):
        with open(os.path.join(self.path, "value"), "r") as f:
            return int(f.read().strip())


def setup_reset_gpio(gpio_number):
    rst = SysfsGPIO(gpio_number)
    rst.export()
    rst.set_direction("out")
    return rst


def reset_pn5180(gpio_number, pulse=True):
    rst = setup_reset_gpio(gpio_number)
    rst.write(1)
    time.sleep(0.05)
    if pulse:
        print("RST GPIO%d: high -> low" % (gpio_number,))
        rst.write(0)
        time.sleep(0.10)
        print("RST GPIO%d: low -> high" % (gpio_number,))
        rst.write(1)
        time.sleep(0.20)
    else:
        print("RST GPIO%d: high only, no reset pulse" % (gpio_number,))
    print("RST GPIO%d value: %d" % (gpio_number, rst.read()))


def xfer(spi, tx, speed, hold_us):
    tx_copy = list(tx)
    return spi.xfer2(tx_copy, speed, hold_us, 8)


def pn5180_cmd(spi, tx, rx_len, speed, hold_us, command_delay):
    rx0 = xfer(spi, tx, speed, hold_us)
    time.sleep(command_delay)
    rx1 = []
    if rx_len:
        rx1 = xfer(spi, [0xFF] * rx_len, speed, hold_us)
        time.sleep(command_delay)
    print("TX0:", hx(tx))
    print("RX0:", hx(rx0))
    print("RX1:", hx(rx1))
    print()
    return rx1


def main():
    parser = argparse.ArgumentParser(description="OrangePi PN5180 SPI probe")
    parser.add_argument("--bus", type=int, default=1)
    parser.add_argument("--device", type=int, default=0)
    parser.add_argument("--speed", type=int, default=50000)
    parser.add_argument("--hold-us", type=int, default=5000)
    parser.add_argument("--delay", type=float, default=0.005)
    parser.add_argument("--reset-gpio", type=int, default=DEFAULT_RESET_GPIO)
    parser.add_argument("--rst", type=int, choices=(0, 1), default=1,
                        help="1: pulse RST low/high; 0: only drive RST high")
    parser.add_argument("--no-reset", action="store_true")
    args = parser.parse_args()

    if not args.no_reset:
        reset_pn5180(args.reset_gpio, pulse=bool(args.rst))

    spi = spidev.SpiDev()
    spi.open(args.bus, args.device)
    spi.mode = 0
    spi.cshigh = False
    spi.no_cs = False
    spi.max_speed_hz = args.speed
    spi.bits_per_word = 8

    print("SPI /dev/spidev%d.%d mode=%d cshigh=%s no_cs=%s speed=%d hold_us=%d" % (
        args.bus, args.device, spi.mode, spi.cshigh, spi.no_cs,
        args.speed, args.hold_us))
    print()

    print("READ EEPROM product version 0x10 len 2")
    pn5180_cmd(spi, [0x07, 0x10, 0x02], 2, args.speed, args.hold_us, args.delay)
    print("READ EEPROM firmware version 0x12 len 2")
    pn5180_cmd(spi, [0x07, 0x12, 0x02], 2, args.speed, args.hold_us, args.delay)
    print("READ EEPROM EEPROM version 0x14 len 2")
    pn5180_cmd(spi, [0x07, 0x14, 0x02], 2, args.speed, args.hold_us, args.delay)
    print("READ SYSTEM_CONFIG")
    pn5180_cmd(spi, [0x04, 0x00], 4, args.speed, args.hold_us, args.delay)
    print("READ IRQ_STATUS")
    pn5180_cmd(spi, [0x04, 0x02], 4, args.speed, args.hold_us, args.delay)

    spi.close()


if __name__ == "__main__":
    main()
