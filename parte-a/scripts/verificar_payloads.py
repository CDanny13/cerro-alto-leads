# Prueba del flujo completo por HTTP contra el servidor en ejecución.
# Envía los diez archivos originales, espera sus estados y verifica las recepciones.
# No simula pacientes ni LLM: debe haber integraciones configuradas en el servidor.
# Hay diez archivos pero nueve event_id distintos: el 02 reenvía el 01 y el 03
# es un duplicado comercial con otro ID. La consulta por desde permite comprobar
# las diez recepciones sin confundirlas con los nueve eventos canónicos.
"""Envía los diez payloads originales y comprueba resultados por HTTP. Requiere servicios reales configurados."""
import argparse
from collections import Counter
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import time

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from cerro_alto.config import ROOT, Settings


# Envía las diez entradas y exige ACK 202 en menos de diez segundos por envío.
# Consulta estados hasta terminar o agotar timeout; después comprueba rutas
# y diez recepciones. Repetir sobre la misma base conserva la idempotencia.
def verify(base_url, timeout=300):
    files = sorted((ROOT / "payloads").glob("*.json"))
    if len(files) != 10:
        raise AssertionError("Se requieren los diez payloads originales")
    payloads = [json.loads(path.read_text(encoding="utf-8")) for path in files]
    # Se captura la hora antes de enviar para consultar después todas las
    # recepciones de esta ejecución, incluidos los reenvíos de un ID existente.
    since = datetime.now(timezone.utc).isoformat()
    token = Settings.from_env().admin_token.get_secret_value()
    with httpx.Client(base_url=base_url.rstrip("/"), timeout=10, follow_redirects=False) as client:
        for path in files:
            started = time.monotonic()
            response = client.post("/webhook", content=path.read_bytes(), headers={"Content-Type": "application/json"})
            assert response.status_code == 202 and time.monotonic() - started < 10, f"Recepción fallida: {path.name}"
        deadline = time.monotonic() + timeout
        states = {}
        # El ACK solo confirma guardado. Este sondeo espera el procesamiento externo
        # y la entrega de alertas antes de afirmar que la comprobación terminó.
        while True:
            for event_id in dict.fromkeys(p["event_id"] for p in payloads):
                response = client.get("/leads/estado", params={"event_id": event_id})
                assert response.status_code == 200, "Contrato de estado no disponible"
                states[event_id] = response.json()
            failed = [s for s in states.values() if s["estado"] in {"fallido", "rechazado"}]
            assert not failed, "Procesamiento detenido: " + ", ".join(s["motivo"] or s["estado"] for s in failed)
            if all(s["estado"] != "pendiente" for s in states.values()):
                break
            assert time.monotonic() < deadline, "Timeout esperando procesamiento"
            time.sleep(0.5)
        for index, payload in enumerate(payloads):
            state = states[payload["event_id"]]
            # El archivo 02 sí fue enviado: se omite únicamente su impresión como evento
            # canónico porque comparte event_id con 01. Su recepción se comprueba con desde.
            if index == 1:
                continue  # Comparte ID con el original; la recepción duplicada se comprueba abajo.
            if index == 2:
                assert state["estado"] == "duplicado" and state["duplicado_de"] == payloads[0]["event_id"]
            else:
                assert state["estado"] == "procesado", f"Estado inesperado: {files[index].name}"
                if index in (0, 6):
                    assert state["ruta"] == "caliente"
                elif index == 7:
                    assert state["ruta"] == "basura" and state["motivo"]
                else:
                    assert state["ruta"] in {"tibio", "caliente"}
                    if state["ruta"] == "caliente":
                        assert token, "ADMIN_TOKEN es necesario para verificar el enriquecimiento que elevó un lead a caliente"
                        detail = client.get("/admin/leads/" + state["lead_id"], headers={"Authorization": "Bearer " + token})
                        assert detail.status_code == 200
                        assert detail.json()["resultado"]["motivo_ruta"] == "paciente_con_tratamiento_pendiente", "Un interés informativo no debe elevarse sin contexto confirmado"
            print(f"{files[index].name}: {state['estado']} / {state['ruta'] or '-'}")
        response = client.get("/leads/estado", params={"desde": since})
        assert response.status_code == 200
        # La consulta por fecha debe contener diez recepciones aunque solo haya
        # nueve IDs distintos; una de las del primer ID debe figurar duplicada.
        receipts = response.json()
        counts = Counter(r["event_id"] for r in receipts)
        assert counts[payloads[0]["event_id"]] >= 2
        assert any(r["event_id"] == payloads[0]["event_id"] and r["estado"] == "duplicado" for r in receipts)
        assert len(receipts) >= 10
    return states


# Lee URL opcional y tiempo máximo de espera, ejecuta verify y fija la salida
# de consola. Un assert fallido significa que la comprobación no se completó.
def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url", nargs="?", default="http://127.0.0.1:8000")
    parser.add_argument("--timeout", type=float, default=300)
    args = parser.parse_args()
    try:
        verify(args.url, args.timeout)
        print("Verificación HTTP completada. Los mocks de Pytest son una prueba separada.")
        return 0
    except (AssertionError, httpx.RequestError) as error:
        print(str(error) if isinstance(error, AssertionError) else "Error de conexión HTTP", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
