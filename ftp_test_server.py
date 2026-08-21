"""Minimal FTP server for testing the scope's "Save -> FTP -> Send" feature.

Why this exists
----------------
The scope's SAVE dialog has an "FTP" send option, but using it requires the
scope to have somewhere to send the file *to* -- an FTP server, running
somewhere reachable on the network the scope is connected to. This script
turns this PC into that FTP server for testing purposes, so you can point
the scope at it and see whether a saved waveform file actually shows up.

The scope reported "NO WIFI NETWORK" when trying to use FTP, even though
its wired LAN connection (used for SCPI/LXI) was working fine. This is a
common pattern on Android-based instruments: the FTP-send feature checks
specifically for an active WiFi radio connection, not just "is there a
network route" -- so a wired-only connection doesn't satisfy it even though
it's perfectly good network connectivity. If you connect the scope's WiFi
module to the same network your PC is on (same router/subnet as the wired
LAN, ideally), the FTP-send feature should stop complaining.

How to use this
----------------
1. Install the one dependency (only needed once):
       pip install pyftpdlib

2. Run this script on the PC you want the scope to send files to:
       python ftp_test_server.py

   It will print this PC's LAN IP address and the folder it's serving.
   Windows will likely pop up a firewall prompt the first time -- choose
   "Allow access" (at least for Private networks).

3. On the scope: Save -> FTP -> Send, and point it at:
       Host / IP:  <the IP this script printed>
       Port:       21  (or whatever PORT is set to below)
       Username:   scope
       Password:   scope123
       Directory:  / (root of what this script shares)

4. Watch this script's console -- every connection, login, and file
   upload is logged as it happens, and the file will appear in the
   ftp_incoming folder next to this script.

This is a throwaway test server (plain FTP, no TLS, one fixed
username/password) meant to answer the immediate question "does Send-to-FTP
actually work, and where does it go" -- not something to leave running
permanently. Stop it with Ctrl+C.
"""

import socket
from pathlib import Path

from pyftpdlib.authorizers import DummyAuthorizer
from pyftpdlib.handlers import FTPHandler
from pyftpdlib.servers import FTPServer

# ---------------------------------------------------------------------------
# Settings -- change these if you like, then re-run the script.
# ---------------------------------------------------------------------------
USERNAME = "scope"
PASSWORD = "scope123"
PORT = 21  # standard FTP port; try 2121 instead if 21 is blocked/in use
INCOMING_DIR = Path(__file__).parent / "ftp_incoming"


def _local_ip() -> str:
    """Best-effort guess at this PC's LAN IP (the one on the same network
    as the scope), without needing any extra dependency."""
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        # Doesn't actually send anything -- just makes the OS pick the
        # outbound interface/IP it would use to reach that address.
        probe.connect(("192.168.10.108", 3000))  # the scope's known address
        return probe.getsockname()[0]
    except OSError:
        return "<could not determine -- check with ipconfig>"
    finally:
        probe.close()


def main() -> None:
    INCOMING_DIR.mkdir(exist_ok=True)

    authorizer = DummyAuthorizer()
    authorizer.add_user(USERNAME, PASSWORD, str(INCOMING_DIR), perm="elradfmw")

    handler = FTPHandler
    handler.authorizer = authorizer
    handler.banner = "Test FTP server for OWON ADS824A Save-to-FTP testing."

    # Passive mode port range, in case the scope's FTP client needs it
    # (and so a firewall rule can be scoped to a small port range instead
    # of "allow everything").
    handler.passive_ports = range(60000, 60010)

    server = FTPServer(("0.0.0.0", PORT), handler)

    print("=" * 70)
    print("Test FTP server running.")
    print(f"  Host/IP for the scope to use : {_local_ip()}")
    print(f"  Port                         : {PORT}")
    print(f"  Username                     : {USERNAME}")
    print(f"  Password                     : {PASSWORD}")
    print(f"  Files will land in           : {INCOMING_DIR}")
    print("  Press Ctrl+C to stop.")
    print("=" * 70)

    server.serve_forever()


if __name__ == "__main__":
    main()
