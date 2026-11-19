#!/usr/bin/env python3
"""
ble_cli - a small command-line BLE central for testing devices with an
nRF52840 dongle (running Nordic connectivity firmware) via blatann.

Interactive:
    python ble_cli.py --com_port COM7 --bluetooth_address 00:11:22:33:44:55

Automated (non-interactive, returns an exit code):
    python ble_cli.py --com_port COM7 --bluetooth_address 00:11:22:33:44:55 \
        --passkey 123456 --exec "read <uuid>" --exec "write <uuid> 01ff"

    python ble_cli.py --com_port COM7 --bluetooth_address 00:11:22:33:44:55 \
        --script commands.txt

Exit codes:
    0  success
    1  one or more commands failed
    2  device not found while scanning
    3  pairing failed
    4  connection / discovery error
    130 interrupted (Ctrl+C)

Only use this on devices you own or are authorized to test.
"""
from __future__ import annotations

import argparse
import binascii
import sys
import time
import traceback

from blatann import BleDevice
from blatann.gap import smp

EXIT_OK = 0
EXIT_CMD_FAILED = 1
EXIT_NOT_FOUND = 2
EXIT_PAIRING_FAILED = 3
EXIT_CONNECT_ERROR = 4
EXIT_INTERRUPTED = 130

USAGE = (
    "Commands: list | read <uuid> | write <uuid> <hex> | "
    "pairing_status | sleep <seconds> | disconnect"
)

try:
    # Avoid UnicodeEncodeError on consoles with legacy code pages (e.g. Windows CP1252)
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def log(msg: str) -> None:
    """Print immediately (flushed) so output is real-time, also when piped."""
    print(msg, flush=True)


# --------------------------------------------------------------------------
# GATT helpers
# --------------------------------------------------------------------------
def list_characteristics(peer) -> dict:
    """Return {lowercase uuid string: characteristic} for the connected peer."""
    all_chars = {}
    for service in peer.database.services:
        for char in service.characteristics:
            all_chars[str(char.uuid).lower()] = char
    return all_chars


def char_properties(char) -> list[str]:
    """Return the real GATT properties advertised by the characteristic."""
    props = getattr(char, "properties", None)
    names = []
    for label, attr in (
        ("READ", "read"),
        ("WRITE", "write"),
        ("WRITE_NO_RESPONSE", "write_no_response"),
        ("NOTIFY", "notify"),
        ("INDICATE", "indicate"),
    ):
        if props is not None and getattr(props, attr, False):
            names.append(label)
    return names


def try_write(char, data_bytes: bytes) -> bool:
    """Write with response; fall back to write-without-response."""
    try:
        result = char.write(data_bytes).wait(5)
        if isinstance(result, tuple):
            _, event_args = result
            status = getattr(event_args, "status", result)
        else:
            status = getattr(result, "status", result)
        log(f"Wrote data (with response): {data_bytes.hex()}, status={status}")
        return True
    except Exception as e:
        log(f"Write with response failed ({e}), trying without response...")

    try:
        char.write_without_response(data_bytes)
        log(f"Wrote data (without response): {data_bytes.hex()}")
        return True
    except Exception as e2:
        log(f"Write without response also failed: {e2}")
        return False


def try_read(char, uuid: str) -> bool:
    """Read a characteristic and print the raw value."""
    try:
        result = char.read().wait(5)
        if isinstance(result, tuple):
            _, event_args = result
            value = getattr(event_args, "value", b"")
        else:
            value = result.values

        log(f"Read from {uuid}: {value.hex()} (raw={value})")
        return True
    except Exception as e:
        log(f"Failed to read from {uuid}: {e}")
        return False


# --------------------------------------------------------------------------
# Pairing helpers
# --------------------------------------------------------------------------
def check_pairing_result(pair_result, device_name: str) -> bool:
    """Parse the result of peer.security.pair().wait(). Status 0 = success."""
    try:
        if isinstance(pair_result, tuple):
            _, event_args = pair_result
            status = getattr(event_args, "status", None)
        else:
            status = getattr(pair_result, "status", None)

        log(f"[{device_name}] Pairing result status: {status}")

        if status is None:
            log(f"[{device_name}] WARNING: could not read pairing status (None).")
            return False

        if status == 0 or "success" in str(status).lower():
            log(f"[{device_name}] [OK] Pairing status: SUCCESS")
            return True

        log(f"[{device_name}] [FAIL] Pairing status: FAILED ({status})")
        return False

    except Exception as e:
        log(f"[{device_name}] Exception reading pairing result: {e}")
        return False


def check_link_encrypted(peer, device_name: str):
    """
    Check the link security level.
    Returns True (encrypted), False (not encrypted) or None (unknown).
    """
    try:
        level = peer.security.security_level
        level_str = str(level).lower()
        log(f"[{device_name}] Security level: {level}")

        if "no_security" in level_str or "no_access" in level_str:
            log(f"[{device_name}] [FAIL] Link is NOT encrypted (security_level={level}).")
            return False

        log(f"[{device_name}] [OK] Link is ENCRYPTED (security_level={level}).")
        return True

    except Exception as e:
        log(f"[{device_name}] Exception checking security level: {e}")
        return None


def do_pairing(peer, device_name: str, passkey: int) -> bool:
    """
    Passkey pairing (this tool acts as keyboard-only). Returns True only if
    pairing succeeded and the link is encrypted.
    """
    try:
        peer.security.set_security_params(
            passcode_pairing=True,
            io_capabilities=smp.IoCapabilities.KEYBOARD_ONLY,
            bond=True,
            out_of_band=False,
        )

        def on_passkey_required(_peer, event_args):
            log(f"[{device_name}] Passkey requested - sending: {passkey}")
            event_args.resolve(passkey)

        peer.security.on_passkey_required.register(on_passkey_required)

        log(f"[{device_name}] Initiating pairing (passkey={passkey})...")
        pair_result = peer.security.pair().wait(30)

        pairing_ok = check_pairing_result(pair_result, device_name)
        encrypted = check_link_encrypted(peer, device_name)

        if pairing_ok and encrypted:
            log(f"[{device_name}] [OK] Pairing verified: status=success, link=encrypted.")
            return True
        if pairing_ok and encrypted is None:
            log(f"[{device_name}] [WARN] Pairing OK but encryption state unknown. Proceeding.")
            return True
        if pairing_ok:
            log(f"[{device_name}] [FAIL] Pairing OK but link NOT encrypted. Treating as failure.")
            return False

        log(f"[{device_name}] [FAIL] Pairing failed (status_ok={pairing_ok}, encrypted={encrypted}).")
        return False

    except Exception as e:
        log(f"[{device_name}] Exception during pairing: {e}")
        traceback.print_exc()
        return False


# --------------------------------------------------------------------------
# Disconnect
# --------------------------------------------------------------------------
def disconnect(ble_device, peer, device_name: str) -> None:
    """Disconnect the peer (if connected) and close the dongle."""
    try:
        if peer is not None and peer.conn_handle is not None:
            log(f"Disconnecting {device_name} (conn_handle={peer.conn_handle})...")
            peer.disconnect().wait(5)
            time.sleep(0.5)
    except Exception as e:
        log(f"Disconnect error: {e}")
    finally:
        try:
            ble_device.close()
        except Exception as e:
            log(f"Error closing device: {e}")


# --------------------------------------------------------------------------
# Command handling
# --------------------------------------------------------------------------
class Session:
    def __init__(self, peer, characteristics: dict, device_name: str):
        self.peer = peer
        self.characteristics = characteristics
        self.device_name = device_name


def run_command(session: Session, cmd_line: str):
    """
    Execute one command line.
    Returns True (ok), False (failed) or None (request to disconnect/exit).
    """
    parts = cmd_line.split()
    if not parts:
        return True

    cmd = parts[0].lower()
    name = session.device_name

    if cmd in ("disconnect", "exit", "quit"):
        return None

    if cmd == "list" and len(parts) == 1:
        log(f"[{name}] {len(session.characteristics)} characteristics:")
        for uuid, char in session.characteristics.items():
            props = " / ".join(char_properties(char)) or "-"
            log(f"  {uuid}  {props}")
        return True

    if cmd == "read" and len(parts) == 2:
        uuid = parts[1].lower()
        char = session.characteristics.get(uuid)
        if not char:
            log(f"[{name}] Characteristic {uuid} not found")
            return False
        log(f"[{name}] Reading from {uuid}...")
        return try_read(char, uuid)

    if cmd == "write" and len(parts) == 3:
        uuid = parts[1].lower()
        try:
            data_bytes = binascii.unhexlify(parts[2])
        except (binascii.Error, ValueError):
            log("Invalid hex data format")
            return False

        char = session.characteristics.get(uuid)
        if not char:
            log(f"[{name}] Characteristic {uuid} not found")
            return False
        log(f"[{name}] Writing to {uuid}...")
        return try_write(char, data_bytes)

    if cmd == "pairing_status" and len(parts) == 1:
        return check_link_encrypted(session.peer, name) is True

    if cmd == "sleep" and len(parts) == 2:
        try:
            time.sleep(float(parts[1]))
            return True
        except ValueError:
            log("Invalid sleep duration")
            return False

    log(f"Unknown or malformed command: {cmd_line}")
    log(USAGE)
    return False


def load_script(path: str) -> list[str]:
    """Read commands from a file; blank lines and '#' comments are skipped."""
    commands = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#"):
                commands.append(line)
    return commands


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------
def find_device(ble_device, bluetooth_address: str, device_name: str | None, scan_timeout: int):
    """Scan and return the peer address of the target, or None."""
    ble_device.scanner.set_default_scan_params(timeout_seconds=scan_timeout)
    scan_reports = ble_device.scanner.start_scan().scan_reports

    target = bluetooth_address.lower()
    for report in scan_reports:
        if report.duplicate:
            continue
        if device_name and report.advertise_data.local_name != device_name:
            continue
        scanned = str(report.peer_address).split(",")[0].strip().lower()
        if scanned == target:
            log(f"Found device at {report.peer_address}")
            return report.peer_address
    return None


def main(
    serial_port: str,
    bluetooth_address: str,
    device_name: str | None = None,
    passkey: int | None = None,
    commands: list[str] | None = None,
    scan_timeout: int = 4,
) -> int:
    label = device_name or bluetooth_address

    # In-memory bond DB: no persistence, avoids duplicate-bond errors on reconnect
    ble_device = BleDevice(serial_port, bond_db_filename=":memory:")
    ble_device.open()
    log("Bond DB set to in-memory (fresh pairing each run).")

    if passkey is not None:
        log("Passkey pairing enabled.")
        ble_device.set_default_security_params(
            passcode_pairing=True,
            io_capabilities=smp.IoCapabilities.KEYBOARD_ONLY,
            bond=True,
            out_of_band=False,
            lesc_pairing=False,
        )

    peer = None
    try:
        log(f"Scanning for {bluetooth_address}"
            + (f" (name '{device_name}')" if device_name else "") + "...")
        found = find_device(ble_device, bluetooth_address, device_name, scan_timeout)
        if not found:
            log(f"Error: device not found at {bluetooth_address}.")
            return EXIT_NOT_FOUND

        log(f"Connecting to {label}...")
        try:
            peer = ble_device.connect(found).wait()
        except Exception as e:
            log(f"Connection failed: {e}")
            return EXIT_CONNECT_ERROR
        log(f"Connected to {label}.")
        time.sleep(0.5)

        if passkey is not None and not do_pairing(peer, label, passkey):
            log(f"[{label}] [FAIL] Pairing failed - disconnecting.")
            return EXIT_PAIRING_FAILED

        try:
            peer.discover_services().wait(20)
        except Exception as e:
            log(f"Service discovery failed: {e}")
            return EXIT_CONNECT_ERROR

        characteristics = list_characteristics(peer)
        log(f"{label} - services: {len(peer.database.services)}, "
            f"characteristics: {len(characteristics)}")

        session = Session(peer, characteristics, label)

        # ---- Non-interactive mode ----
        if commands:
            failures = 0
            for cmd_line in commands:
                log(f">>> {cmd_line}")
                result = run_command(session, cmd_line)
                if result is None:
                    break
                if result is False:
                    failures += 1
            log(f"Done: {failures} command(s) failed." if failures else "Done: all commands succeeded.")
            return EXIT_CMD_FAILED if failures else EXIT_OK

        # ---- Interactive mode ----
        log(USAGE)
        while True:
            try:
                cmd_line = input(">>> ").strip()
            except EOFError:
                return EXIT_OK
            if not cmd_line:
                continue
            try:
                if run_command(session, cmd_line) is None:
                    return EXIT_OK
            except Exception:
                log("Unexpected error:")
                traceback.print_exc()

    except KeyboardInterrupt:
        log("\nInterrupted by user.")
        return EXIT_INTERRUPTED
    finally:
        disconnect(ble_device, peer, label)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="BLE test CLI for an nRF52840 dongle (blatann). "
                    "Interactive by default; use --exec/--script for automation."
    )
    parser.add_argument("--com_port", required=True,
                        help="Serial port of the dongle (e.g. COM7 or /dev/ttyACM0)")
    parser.add_argument("--bluetooth_address", required=True,
                        help="Target BLE address (e.g. 00:11:22:33:44:55)")
    parser.add_argument("--device_name", default=None,
                        help="Optional advertised name; if given, it must match as well as the address")
    parser.add_argument("--passkey", type=int, default=None,
                        help="6-digit passkey for pairing (e.g. 123456). Omit to skip pairing")
    parser.add_argument("--exec", dest="exec_cmds", action="append", default=[],
                        metavar="CMD", help="Command to run non-interactively (repeatable)")
    parser.add_argument("--script", default=None,
                        help="File with one command per line ('#' for comments)")
    parser.add_argument("--scan_timeout", type=int, default=4,
                        help="Scan duration in seconds (default: 4)")
    return parser


if __name__ == "__main__":
    args = build_parser().parse_args()

    commands = list(args.exec_cmds)
    if args.script:
        try:
            commands = load_script(args.script) + commands
        except OSError as e:
            log(f"Cannot read script file: {e}")
            sys.exit(EXIT_CMD_FAILED)

    sys.exit(
        main(
            args.com_port,
            args.bluetooth_address,
            device_name=args.device_name,
            passkey=args.passkey,
            commands=commands or None,
            scan_timeout=args.scan_timeout,
        )
    )