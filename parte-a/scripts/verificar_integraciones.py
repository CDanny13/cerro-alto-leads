# Punto de entrada de la comprobación externa: no necesita Uvicorn iniciado.
# Carga .env, ejecuta check_integrations y comunica éxito o un código de fallo.
# Solo --alerta solicita una notificación real; las demás llamadas siempre usan
# los servicios configurados. Un código de salida distinto de cero indica fallo.
"""Llama servicios REALES. No usa mocks ni imprime datos de pacientes o credenciales."""
import argparse
import asyncio
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cerro_alto.config import Settings
from cerro_alto.errors import ServiceError
from cerro_alto.live_checks import check_integrations


# Interpreta --alerta y ejecuta la función async desde una CLI síncrona.
# Solo muestra resumen o componente/código de fallo, sin credenciales externas.
def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--alerta", action="store_true", help="Envía además una notificación real de prueba")
    args = parser.parse_args()
    try:
        result = asyncio.run(check_integrations(Settings.from_env(), send_alert=args.alerta))
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except ServiceError as error:
        print(f"FALLO REAL: {error.component}/{error.code}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
