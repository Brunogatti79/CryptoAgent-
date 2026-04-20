"""
Servidor HTTP mínimo para el dashboard PWA.
Sirve dashboard.html y dashboard_state.json con los headers correctos.

Uso:
    python server.py          → puerto 8080
    python server.py 9000     → puerto custom

Acceso desde el celular:
    1. Asegurate de estar en la misma red WiFi
    2. Abrí http://<IP-de-tu-PC>:8080 en el browser del celu
    3. "Agregar a pantalla de inicio" para instalarlo como PWA

Para ver tu IP local: ipconfig (Windows) o ip a (Linux)
"""

import http.server
import json
import os
import socket
import sys


PORT    = int(sys.argv[1]) if len(sys.argv) > 1 else 8080
WEBROOT = os.path.dirname(os.path.abspath(__file__))


class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=WEBROOT, **kwargs)

    def do_GET(self):
        if self.path == "/" or self.path == "/index.html":
            self.path = "/dashboard.html"
        super().do_GET()

    def end_headers(self):
        # Headers PWA + CORS para acceso desde celular
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
        self.send_header("Access-Control-Allow-Origin", "*")
        if self.path.endswith(".json"):
            self.send_header("Content-Type", "application/json; charset=utf-8")
        super().end_headers()

    def log_message(self, fmt, *args):
        # Solo loguear errores, no cada request
        if args and str(args[1]) not in ("200", "304"):
            super().log_message(fmt, *args)


def get_local_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "localhost"


def main():
    ip = get_local_ip()
    server = http.server.HTTPServer(("0.0.0.0", PORT), Handler)

    print(f"\n{'='*48}")
    print(f"  Dashboard corriendo en:")
    print(f"  → Local:   http://localhost:{PORT}")
    print(f"  → Red:     http://{ip}:{PORT}")
    print(f"{'='*48}")
    print(f"  Desde el celu (misma WiFi):")
    print(f"  http://{ip}:{PORT}")
    print(f"\n  Ctrl+C para detener\n")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nServidor detenido.")


if __name__ == "__main__":
    main()
