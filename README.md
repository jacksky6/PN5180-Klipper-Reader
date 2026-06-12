# PN5180 NFC Reader for Klipper

PN5180 NFC Reader for Klipper is a Klipper extra module for reading filament spool
NFC tags through a PN5180 SPI reader. It is intended for shared-reader MMU
setups where one reader is used to identify the loaded spool.

The module can read a spool id from simple text or JSON tag data and can
optionally dispatch it to Happy Hare with:

```gcode
MMU_GATE_MAP NEXT_SPOOLID=<id>
```

## Supported Tags

- NTAG213 / NTAG215 / NTAG216
- OpenPrintTag / Prusa style ISO15693 tags, such as ICODE SLIX2

The default format is `ntag`, which is the best choice for common user-written
NTAG stickers. Use `openprinttag` for Prusa/OpenPrintTag labels. Use `auto`
only when you are testing or mixing both tag types.

## Wiring

The PN5180 must use hardware SPI, a chip-select pin, and an MCU-controlled reset
pin. The reset pin is required because some PN5180 modules can stop responding
to SPI after long runs or disturbed reads, and only a hardware reset can recover
them.

Example SLB/MMU wiring:

| PN5180 | MCU pin |
| --- | --- |
| SCK | PB13 |
| MISO | PB14 |
| MOSI | PB15 |
| NSS / CS | PA8 |
| RST | PC7 |
| 5V | 5V |
| 3.3V | 3.3V |
| GND | GND |

Many PN5180 breakout boards need both `5V` and `3.3V`: `5V` powers the RF/front
end side and `3.3V` powers the logic/SPI side. Check your module before wiring.

## Installation

Copy the Klipper extra module into your Klipper installation:

```bash
cp klippy/extras/pn5180.py ~/klipper/klippy/extras/pn5180.py
```

Then include or copy the example configuration from:

```text
config/pn5180.cfg
```

Restart Klipper after editing the configuration.

## Minimal Configuration

```ini
[pn5180 mmu_reader]
spi_bus: spi2_PB14_PB15_PB13
cs_pin: mmu:PA8
reset_pin: mmu:PC7

spi_speed: 100000
scan_period: 6.0
read_on_start: False

tag_format: ntag
happyhare_enable: True
debug_log: True
```

Use the SPI bus name provided by your Klipper MCU firmware. For STM32 builds,
the available SPI bus names are shown in the Klipper build output or MCU enum
definitions.

## Commands

Read once:

```gcode
PN5180 NAME=mmu_reader SCAN=1
```

Start periodic reading:

```gcode
PN5180 NAME=mmu_reader READ=1
```

Stop periodic reading:

```gcode
PN5180 NAME=mmu_reader READ=0
```

Run diagnostics:

```gcode
PN5180 NAME=mmu_reader DIAG=1
```

Hardware reset and reinitialize the reader:

```gcode
PN5180 NAME=mmu_reader RECOVER=1
```

## Recovery Behavior

`SCAN=1` does not reset the PN5180 before every scan. If no tag is detected, the
module performs a lightweight communication check and returns normally. If a
read fails, it first resets the RF state and retries. If SPI communication looks
invalid, for example registers or EEPROM read back as all `FF` or all `00`, it
uses the configured `reset_pin` to hardware-reset and reinitialize the PN5180.

## Troubleshooting

- If diagnostics return all `0xFFFFFFFF`, check MISO, CS, SPI bus name, power,
  ground, and reset wiring.
- If the firmware version reads as `FF FF`, the PN5180 is not returning valid
  SPI data.
- If UID reading works but tag data reading is unstable, keep the tag still and
  try increasing `rf_timeout` to `0.1`.
- For stable long-running use, prefer `tag_format: ntag` or
  `tag_format: openprinttag` instead of `auto`.
- Disable debug spam after testing with `debug_log: False`.
