"""Test client for the scope's own FTP server (WiFi mode).

What we now know
-----------------
On this scope, WiFi and the wired LAN interface do NOT work together --
enabling WiFi effectively takes over networking, and the wired LAN (used
for SCPI control at TCPIP0::192.168.10.108::3000::SOCKET) stops responding
while WiFi is active. Once WiFi connected, the scope displayed:

    ftp://192.168.0.197:2121

This is a different address/subnet than the wired LAN's 192.168.10.108, and
the "ftp://" prefix means this is very likely the scope announcing that IT
is now running an FTP SERVER (not that it wants to send files somewhere
else) -- i.e. the roles are the opposite of the last test: the scope is the
server, and this PC should connect to it as an ordinary FTP client to
browse and download the saved-waveform files directly, no USB drive
involved.

For this to work, this PC needs to be on the same WiFi network as the
scope (same router, so both are on the 192.168.0.x range) -- a wired-only
PC connection won't be able to reach it.

How to use this
----------------
1. No extra install needed -- this only uses Python's built-in ftplib.
2. Make sure this PC is joined to the same WiFi network the scope just
   connected to.
3. Run (adjust host/port if the scope shows a different address):

       python ftp_browse_test.py --host 192.168.0.197 --port 2121

   This logs in (anonymous first; if that's rejected, pass --user/--password)
   and prints a full recursive directory listing of everything the scope's
   FTP server exposes, with file sizes -- so we can see exactly which
   folders hold the Save output (Internal vs External/USB, if both show up).

4. To actually pull one file down to this PC, add --download plus the
   remote path shown in the listing:

       python ftp_browse_test.py --host 192.168.0.197 --port 2121 ^
           --download /internal/waveform.csv --out waveform.csv

If anonymous login is rejected, try:

       python ftp_browse_test.py --host 192.168.0.197 --port 2121 --user scope --password scope123

(or whatever credentials the scope's FTP screen shows/asks for -- unlike
the last test, THIS FTP server's login is defined by the scope, not by us).
"""

import argparse
import ftplib
import sys
from pathlib import Path


def _list_recursive(ftp: ftplib.FTP, path: str = "", indent: str = "") -> None:
    """Print every file/folder under `path`, recursing into subfolders.

    Uses MLSD (structured listing) when the server supports it, and falls
    back to the older LIST format if not -- some embedded FTP servers only
    implement one or the other.
    """

    try:
        entries = list(ftp.mlsd(path or "."))
        structured = True
    except (ftplib.error_perm, AttributeError):
        structured = False
        entries = []

    if structured:
        for name, facts in entries:
            if name in (".", ".."):
                continue
            entry_path = f"{path}/{name}" if path else name
            if facts.get("type") == "dir":
                print(f"{indent}[DIR]  {name}")
                _list_recursive(ftp, entry_path, indent + "    ")
            else:
                size = facts.get("size", "?")
                print(f"{indent}       {name}  ({size} bytes)")
    else:
        lines = []
        ftp.retrlines(f"LIST {path}" if path else "LIST", lines.append)
        for line in lines:
            print(f"{indent}{line}")
        print(
            f"{indent}(server doesn't support MLSD -- showing raw LIST "
            f"output above instead of a clean recursive tree)"
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", required=True, help="Scope's FTP IP, e.g. 192.168.0.197")
    parser.add_argument("--port", type=int, default=2121)
    parser.add_argument("--user", default="anonymous")
    parser.add_argument("--password", default="anonymous@")
    parser.add_argument("--timeout", type=float, default=10.0)
    parser.add_argument(
        "--download",
        metavar="REMOTE_PATH",
        help="If given, download this remote file instead of just listing.",
    )
    parser.add_argument(
        "--out",
        metavar="LOCAL_PATH",
        help="Where to save --download's file (default: same name, current folder).",
    )
    args = parser.parse_args()

    ftp = ftplib.FTP()
    print(f"Connecting to {args.host}:{args.port} ...")
    ftp.connect(args.host, args.port, timeout=args.timeout)

    try:
        ftp.login(args.user, args.password)
        print(f"Logged in as '{args.user}'.")
    except ftplib.error_perm as error:
        print(f"Login as '{args.user}' failed ({error}).")
        print("Re-run with --user/--password matching what the scope's FTP screen shows.")
        sys.exit(1)

    ftp.set_pasv(True)  # passive mode -- friendlier to firewalls/NAT

    if args.download:
        out_path = Path(args.out) if args.out else Path(args.download).name
        print(f"Downloading {args.download} -> {out_path} ...")
        with open(out_path, "wb") as local_file:
            ftp.retrbinary(f"RETR {args.download}", local_file.write)
        print(f"Done: {out_path} ({out_path.stat().st_size} bytes)")
    else:
        print("Directory listing:")
        _list_recursive(ftp)

    ftp.quit()


if __name__ == "__main__":
    main()
