# CoolerLCD Linux

`vevor-cooler-lcd-linux` (repo) — the open-source native Linux driver.

Native Linux driver for CPU-cooler LCD screens built on the **USB HID `5131:2007`**
controller — the little display on many AIO and air coolers sold under **Vevor**,
**HT**, and various rebrands. These ship with a Windows-only app (`PC Monitoring.exe`
/ "PC Monitor All"); this project drives the screen natively on Linux instead.

No Windows, no Wine, no proprietary driver. Just Python + hidraw.

> Status: working. CPU/GPU temp, usage, power, clock, fan, clock + date/time are
> pushed to the screen in real time. Reverse-engineered protocol fully documented
> in [`PROTOCOL.md`](PROTOCOL.md).

## Supported hardware

| | |
|---|---|
| USB ID | `5131:2007` (often enumerates as "MSR / Mini HID") |
| Transport | USB HID output reports |
| Sold as | Vevor AIO/air cooler LCD, HT "PC Monitor All", assorted rebrands |
| Tested model | **YC-01-SS-002** (Vevor) |

Run `lsusb` — if you see `ID 5131:2007`, this driver should work for you.

**Confirmed:** Vevor `YC-01-SS-002`. Other `YC-01-SS-*` variants (e.g. `-001`) are *likely* compatible **if** they expose the same `5131:2007` ID and use the "PC Monitor All" software — please open an issue to confirm your model so the list can grow.

## Features

- Real-time CPU temp / usage / power / frequency / voltage
- Real-time GPU temp / usage / power / frequency (NVIDIA via NVML)
- Fan + pump RPM, RAM usage, on-screen date/time
- Celsius / Fahrenheit toggle
- Auto-reconnect on unplug
- systemd service for boot persistence

## Requirements

- Linux with `hidraw` (any modern kernel)
- Python 3.8+
- `pip install hidapi psutil pynvml`
- `sudo apt install libhidapi-hidraw0` (or your distro's equivalent)
- For CPU temp/fan: `sudo sensors-detect` once (lm-sensors)
- NVIDIA GPU metrics need the proprietary driver (NVML); the driver runs fine
  without a GPU, those fields just stay 0.

## Install

```bash
# 1. dependencies
pip install hidapi psutil pynvml
sudo apt install libhidapi-hidraw0

# 2. driver
curl -O https://raw.githubusercontent.com/coldwelderx/cooler-lcd-linux/main/vevor_lcd_linux.py

# 3. non-root device access
sudo cp udev/99-cooler-lcd.rules /etc/udev/rules.d/
sudo udevadm control --reload-rules && sudo udevadm trigger
# replug the cooler USB cable

# 4. run
python3 vevor_lcd_linux.py
```

## Run at boot (systemd user service)

```bash
mkdir -p ~/.config/systemd/user
cp systemd/cooler-lcd.service ~/.config/systemd/user/
cp vevor_lcd_linux.py ~/
systemctl --user daemon-reload
systemctl --user enable --now cooler-lcd.service
sudo loginctl enable-linger "$USER"   # start before login, at boot
```

Check it: `systemctl --user status cooler-lcd.service` → `active (running)`.
Logs: `journalctl --user -u cooler-lcd.service -f`.

## Configuration

Edit the constants near the top of `vevor_lcd_linux.py`:

| Constant | Purpose |
|---|---|
| `USE_FAHRENHEIT` | `False` = °C, `True` = °F |
| `REFRESH_S` | update interval (default 0.2s) |
| `PUMP_RPM_HWMON` | pin a `hwmon` path for pump RPM if your board exposes it |
| `CPU_VOLT_HWMON` | pin a `hwmon` path for Vcore (millivolts) |

Find hwmon paths with `sensors` and `ls /sys/class/hwmon/*/`.

## How it works

The screen has its own firmware and renders everything itself. The host just
streams a 64-byte HID output report every 200 ms containing the sensor values,
decimal-split into single bytes. Full byte map and frame format in
[`PROTOCOL.md`](PROTOCOL.md).

This was reverse-engineered from the Windows .NET app by decompiling it and
reading the `SendData2()` / `SendValueArray` logic — no packet captures needed.

## Troubleshooting

- **`Cannot open HID`** → check `lsusb` shows `5131:2007`, and the udev rule is
  installed + you replugged the cable. Or run once with `sudo` to confirm it's a
  permissions issue.
- **Screen frozen / wrong values** → confirm the service is running and only one
  instance is writing to the device.
- **Different report size** → if writes silently fail, dump the HID descriptor
  (`sudo usbhid-dump -d 5131:2007`) and adjust `REPORT_ID` / `REPORT_LEN`.

## Contributing

Got the same screen under a different brand, or a variant with a different report
layout? Open an issue with your `lsusb` line and `usbhid-dump` output — happy to
extend support.

## License

MIT. This is an interoperability driver for hardware you own; it contains no
proprietary code from the original vendor software.

---

**Keywords:** vevor cooler lcd linux, ht pc monitor all linux, 5131:2007 linux
driver, cpu cooler screen linux, aio lcd linux, usb hid cooler display, vevor aio
linux driver, YC-01-SS-002, YC-01-SS linux.
