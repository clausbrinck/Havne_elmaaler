"""Start elmåler-systemet til manualens skærmbilleder.

- En lille SMTP-modtager på port 2525 gemmer alle mails i %TEMP%\\elmaaler-manual-mails (intet sendes ud).
- TVING_OFFLINE: målere der altid er offline. TVING_W: "nr:watt,..." fast effekt.
"""
import os
import socket
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "elmaaler"))
MAPPE = os.path.join(tempfile.gettempdir(), "elmaaler-manual-mails")
os.makedirs(MAPPE, exist_ok=True)


def smtp_klient(k):
    f = k.makefile("rb")
    k.sendall(b"220 test\r\n")
    data, i_data = [], False
    for linje in f:
        if i_data:
            if linje in (b".\r\n", b".\n"):
                with open(os.path.join(MAPPE, f"{time.time():.3f}.eml"), "wb") as ud:
                    ud.write(b"".join(data))
                data, i_data = [], False
                k.sendall(b"250 ok\r\n")
            else:
                data.append(linje[1:] if linje.startswith(b"..") else linje)
            continue
        cmd = linje[:4].upper()
        if cmd == b"DATA":
            i_data = True
            k.sendall(b"354 go\r\n")
        elif cmd == b"QUIT":
            k.sendall(b"221 bye\r\n")
            break
        elif cmd in (b"EHLO", b"HELO"):
            k.sendall(b"250 test\r\n")
        else:
            k.sendall(b"250 ok\r\n")
    k.close()


def smtp_server():
    s = socket.socket()
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("127.0.0.1", 2525))
    s.listen()
    while True:
        k, _ = s.accept()
        threading.Thread(target=smtp_klient, args=(k,), daemon=True).start()


threading.Thread(target=smtp_server, daemon=True).start()

from app import collector, main  # noqa: E402

OFF = {int(x) for x in os.environ.get("TVING_OFFLINE", "").split(",") if x}
W = {int(a): float(b) for a, b in (x.split(":") for x in os.environ.get("TVING_W", "").split(",") if x)}
_orig = collector.Simulator.hent


def hent(self):
    data, bro = _orig(self)
    for nr in OFF:
        data[nr]["tilg"] = False
    for nr, w in W.items():
        data[nr]["effekt"] = w
        data[nr]["tilg"] = True
    for nr in data:
        if nr not in OFF and nr not in W and self.m[nr]["offline"]:
            data[nr]["tilg"] = True          # ellers stabilt: kun de tvungne er offline
    return data, bro


collector.Simulator.hent = hent
main.main()
