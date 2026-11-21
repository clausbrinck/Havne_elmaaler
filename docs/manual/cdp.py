"""Minimal styring af Microsoft Edge (Chrome DevTools Protocol) uden ekstra pakker.

Bruges af skaermbilleder.py til at logge ind, markere elementer og gemme skærmbilleder.
"""
import base64
import json
import os
import socket
import struct
import subprocess
import time
import urllib.request

EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"


class WebSocket:
    def __init__(self, url):
        rest = url.split("://", 1)[1]
        hostport, sti = rest.split("/", 1)
        host, port = hostport.split(":")
        self.s = socket.create_connection((host, int(port)))
        noegle = base64.b64encode(os.urandom(16)).decode()
        self.s.sendall((f"GET /{sti} HTTP/1.1\r\nHost: {hostport}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                        f"Sec-WebSocket-Key: {noegle}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())
        svar = b""
        while b"\r\n\r\n" not in svar:
            svar += self.s.recv(4096)
        self.buf = svar.split(b"\r\n\r\n", 1)[1]

    def _laes(self, n):
        while len(self.buf) < n:
            d = self.s.recv(1 << 20)
            if not d:
                raise ConnectionError("forbindelsen lukket")
            self.buf += d
        ud, self.buf = self.buf[:n], self.buf[n:]
        return ud

    def send(self, tekst):
        data = tekst.encode()
        hoved = bytearray([0x81])
        n = len(data)
        if n < 126:
            hoved.append(0x80 | n)
        elif n < 65536:
            hoved += bytes([0x80 | 126]) + struct.pack(">H", n)
        else:
            hoved += bytes([0x80 | 127]) + struct.pack(">Q", n)
        maske = os.urandom(4)
        self.s.sendall(bytes(hoved) + maske + bytes(b ^ maske[i % 4] for i, b in enumerate(data)))

    def modtag(self):
        dele = []
        while True:
            b1, b2 = self._laes(2)
            n = b2 & 0x7F
            if n == 126:
                n = struct.unpack(">H", self._laes(2))[0]
            elif n == 127:
                n = struct.unpack(">Q", self._laes(8))[0]
            data = self._laes(n)
            dele.append(data)
            if b1 & 0x80:
                return b"".join(dele).decode()


class Edge:
    def __init__(self, profil, port=9333):
        self.p = subprocess.Popen([EDGE, "--headless=new", f"--remote-debugging-port={port}",
                                   f"--user-data-dir={profil}", "--hide-scrollbars", "--no-first-run",
                                   "--disable-extensions", "about:blank"],
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(50):
            try:
                sider = json.load(urllib.request.urlopen(f"http://127.0.0.1:{port}/json"))
                side = next(s for s in sider if s["type"] == "page")
                break
            except Exception:  # noqa: BLE001
                time.sleep(0.2)
        self.ws = WebSocket(side["webSocketDebuggerUrl"])
        self.nr = 0
        self.kald("Page.enable")
        self.kald("Runtime.enable")

    def kald(self, metode, **params):
        self.nr += 1
        mit = self.nr
        self.ws.send(json.dumps({"id": mit, "method": metode, "params": params}))
        while True:
            svar = json.loads(self.ws.modtag())
            if svar.get("id") == mit:
                if "error" in svar:
                    raise RuntimeError(f"{metode}: {svar['error']}")
                return svar.get("result", {})

    def js(self, kode):
        r = self.kald("Runtime.evaluate", expression=kode, awaitPromise=True, returnByValue=True)
        if "exceptionDetails" in r:
            raise RuntimeError(r["exceptionDetails"].get("exception", {}).get("description", r["exceptionDetails"]))
        return r.get("result", {}).get("value")

    def størrelse(self, bredde, hoejde, mobil=False):
        self.kald("Emulation.setDeviceMetricsOverride", width=bredde, height=hoejde,
                  deviceScaleFactor=2, mobile=mobil)

    def gaa(self, url, vent=1.5):
        self.kald("Page.navigate", url=url)
        time.sleep(vent)
        for _ in range(50):
            if self.js("document.readyState") == "complete":
                break
            time.sleep(0.1)

    def billede(self, fil, hel_side=True):
        if hel_side:
            h = self.js("Math.ceil(document.documentElement.scrollHeight)")
            w = self.js("document.documentElement.clientWidth")
            r = self.kald("Page.captureScreenshot", format="png", captureBeyondViewport=True,
                          clip={"x": 0, "y": 0, "width": w, "height": h, "scale": 1})
        else:
            r = self.kald("Page.captureScreenshot", format="png")
        with open(fil, "wb") as f:
            f.write(base64.b64decode(r["data"]))

    def luk(self):
        try:
            self.kald("Browser.close")
        except Exception:  # noqa: BLE001
            pass
        self.p.wait(timeout=10)
