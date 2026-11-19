# ble-test-cli

A small command-line BLE central for testing Bluetooth LE devices with an **nRF52840 dongle**. It can be used interactively or scripted for automated tests, with exit codes that work in CI or test harnesses.

> **Status:** early and experimental. Only use it on devices you own or are authorized to test.

## What it does

- Scans for a device by BLE address (and optionally advertised name)
- Pairs with a passkey (keyboard-only IO capability) and verifies the link is encrypted
- Discovers services and lists characteristics with their real GATT properties
- Reads and writes characteristics (write with response, falling back to write without response)
- Runs interactively, from repeated `--exec` arguments, or from a `--script` file

## Requirements

- **Python 3.10** (this is the only Python version blatann works with, so other versions will fail to install or run)
- An nRF52840 dongle flashed with Nordic's connectivity firmware that [blatann](https://github.com/ThomasGerstenberg/blatann) supports (see the blatann docs for the exact firmware and SoftDevice version)
- Nordic's `pc-ble-driver-py` prerequisites, installed with blatann

```
pip install -r requirements.txt
```

## Usage

Interactive:

```
python ble_cli.py --com_port COM7 --bluetooth_address 00:11:22:33:44:55
```

With pairing:

```
python ble_cli.py --com_port COM7 --bluetooth_address 00:11:22:33:44:55 --passkey 123456
```

Automated:

```
python ble_cli.py --com_port COM7 --bluetooth_address 00:11:22:33:44:55 --passkey 123456 \
    --exec "list" \
    --exec "read 00002a00-0000-1000-8000-00805f9b34fb" \
    --exec "write 12345678-1234-1234-1234-123456789012 01ff"
```

From a file:

```
python ble_cli.py --com_port COM7 --bluetooth_address 00:11:22:33:44:55 --script commands.txt
```

`commands.txt`:

```
# one command per line
list
read 00002a00-0000-1000-8000-00805f9b34fb
sleep 1
write 12345678-1234-1234-1234-123456789012 01ff
```

### Commands

| Command | Description |
|---|---|
| `list` | List characteristics and their properties |
| `read <uuid>` | Read a characteristic |
| `write <uuid> <hex>` | Write hex bytes to a characteristic |
| `pairing_status` | Check whether the link is encrypted |
| `sleep <seconds>` | Wait (useful in scripts) |
| `disconnect` | Disconnect and exit |

### Options

| Option | Description |
|---|---|
| `--com_port` | Serial port of the dongle (required) |
| `--bluetooth_address` | Target BLE address (required) |
| `--device_name` | Optional advertised name to match in addition to the address |
| `--passkey` | 6-digit passkey; omit to skip pairing |
| `--exec CMD` | Run a command non-interactively (repeatable) |
| `--script FILE` | Run commands from a file |
| `--scan_timeout` | Scan duration in seconds (default 4) |

### Exit codes

| Code | Meaning |
|---|---|
| 0 | Success |
| 1 | One or more commands failed |
| 2 | Device not found |
| 3 | Pairing failed |
| 4 | Connection or service discovery error |
| 130 | Interrupted (Ctrl+C) |

## Notes

- The bond database is kept in memory, so every run pairs fresh.
- Tested with: Python 3.10, blatann 0.6.0, nRF52840 dongle _(fill in OS and dongle firmware version)_

## License

MIT. See [LICENSE](LICENSE).
