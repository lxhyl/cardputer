"""Local HTTPS server for the Morse decoder page (LAN access from phone).

Self-signed cert; phone will warn — tap "advanced → proceed". Camera permission
requires a secure context, so plain HTTP on a LAN IP won't work.
"""
import http.server
import ssl
import socket
import sys
from pathlib import Path

PORT = 8443
HERE = Path(__file__).resolve().parent


def lan_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    finally:
        s.close()


class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=str(HERE), **kw)


def main():
    addr = ("0.0.0.0", PORT)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(certfile=str(HERE / "cert.pem"),
                        keyfile=str(HERE / "key.pem"))
    httpd = http.server.ThreadingHTTPServer(addr, Handler)
    httpd.socket = ctx.wrap_socket(httpd.socket, server_side=True)
    ip = lan_ip()
    print(f"Serving https://{ip}:{PORT}/decoder.html")
    print("(self-signed cert — accept the warning on your phone)")
    sys.stdout.flush()
    httpd.serve_forever()


if __name__ == "__main__":
    main()
