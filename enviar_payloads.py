"""Envía los payloads de ejemplo a tu webhook, en orden, como lo haría el conector de leads.

Uso:
    python3 enviar_payloads.py https://tu-sistema.example.com/webhook
    python3 enviar_payloads.py http://localhost:5678/webhook/leads --carpeta payloads --pausa 1

No necesita dependencias: solo Python 3.10 o superior.
"""
import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path


def enviar(url: str, cuerpo: bytes) -> tuple[int | None, str]:
    req = urllib.request.Request(url, data=cuerpo, method="POST", headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:  # el conector espera como máximo 10 segundos
            return r.status, r.read(300).decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read(300).decode("utf-8", "replace")
    except (urllib.error.URLError, TimeoutError) as e:
        return None, str(e)


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("url", help="URL de tu webhook")
    p.add_argument("--carpeta", default=str(Path(__file__).with_name("payloads")))
    p.add_argument("--pausa", type=float, default=1.0, help="segundos entre un evento y el siguiente")
    a = p.parse_args()
    archivos = sorted(Path(a.carpeta).glob("*.json"))
    if not archivos:
        sys.exit(f"No hay archivos .json en {a.carpeta}")
    for f in archivos:
        cuerpo = f.read_bytes()
        estado, respuesta = enviar(a.url, cuerpo)
        event_id = json.loads(cuerpo).get("event_id", "(sin event_id)")
        print(f"{f.name}  {event_id}  ->  {estado if estado else 'sin respuesta'}  {respuesta[:120]}")
        time.sleep(a.pausa)


if __name__ == "__main__":
    main()
