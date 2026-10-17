# Herramienta administrativa de terminal para una aplicación ya iniciada.
# Lee ADMIN_TOKEN de la configuración y lo adjunta como Authorization: Bearer.
# Sin opciones muestra un resumen; con opciones permite revisión o reprocesamiento.
# Las consultas de detalle pueden mostrar datos personales en la terminal del
# operador autorizado. El token no se imprime ni se pasa como argumento de consola.
"""Revisión y recuperación contra la aplicación en ejecución. Lee ADMIN_TOKEN de .env."""
import argparse
import json
from pathlib import Path
import sys

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from cerro_alto.config import Settings


# Traduce las opciones de consola a una consulta administrativa o reprocesamiento.
# Las acciones son mutuamente excluyentes. Retorna 0 ante éxito y 1 ante fallo;
# HTTPX envía Bearer automáticamente desde el encabezado del cliente.
def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8000")
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument("--estado", choices=["fallido", "pendiente", "rechazado", "duplicado", "procesado"])
    actions.add_argument("--receipt-id", help="Muestra contenido original: puede contener datos personales")
    actions.add_argument("--lead-id", help="Muestra resultado y mensajes preparados")
    actions.add_argument("--reprocesar", metavar="EVENT_ID")
    actions.add_argument("--reprocesar-recepcion", metavar="RECEIPT_ID")
    args = parser.parse_args()
    token = Settings.from_env().admin_token.get_secret_value()
    if not token:
        parser.error("Configura ADMIN_TOKEN en .env")
    from urllib.parse import quote
    # El secreto viaja en el encabezado, no en la URL. Esta herramienta solo
    # consulta o solicita recuperación; el servidor valida el permiso en cada ruta.
    with httpx.Client(base_url=args.url, headers={"Authorization": "Bearer " + token}, timeout=10) as client:
        try:
            if args.estado:
                response = client.get("/admin/revision", params={"estado": args.estado})
            elif args.receipt_id:
                response = client.get("/admin/recepciones/" + quote(args.receipt_id, safe=""))
            elif args.lead_id:
                response = client.get("/admin/leads/" + quote(args.lead_id, safe=""))
            elif args.reprocesar or args.reprocesar_recepcion:
                response = client.post("/leads/reprocesar", json={"event_id": args.reprocesar, "receipt_id": args.reprocesar_recepcion})
            else:
                response = client.get("/admin/operacion")
            if response.is_error:
                print(f"Operación rechazada: HTTP {response.status_code}", file=sys.stderr)
                return 1
            print(json.dumps(response.json(), ensure_ascii=False, indent=2))
            return 0
        except httpx.RequestError:
            print("No se pudo conectar con la aplicación", file=sys.stderr)
            return 1


if __name__ == "__main__":
    raise SystemExit(main())
