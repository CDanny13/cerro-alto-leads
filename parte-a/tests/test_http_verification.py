# Verificación del script contra un servidor Uvicorn real en localhost.
# El transporte HTTP local es real; pacientes, LLM y Slack siguen siendo mocks.
# El puerto libre y la base temporal permiten ejecutar el caso sin tocar una
# instancia de trabajo. El bloque finally apaga el servidor aunque falle un assert.
"""HTTP local real; pacientes, LLM y avisos son mocks EXPLÍCITOS de test."""
import runpy
import socket
import threading
import time
from pathlib import Path

import httpx
import uvicorn

from cerro_alto.app import create_app
from test_worker import configured, fixtures_handler


# Arranca Uvicorn en un puerto temporal, ejecuta el verificador original
# contra localhost y comprueba nueve IDs y tres alertas simuladas.
# Finalmente apaga servidor y cierra socket aunque la prueba falle.
def test_verification_script_over_real_http(settings, monkeypatch):
    alerts = []
    settings = configured(settings).model_copy(update={"worker_enabled": True, "poll_seconds": 0.01})
    app = create_app(settings, transport=httpx.MockTransport(fixtures_handler(alerts)))
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    config = uvicorn.Config(app, log_level="error", access_log=False)
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 5
        while not server.started:
            assert time.monotonic() < deadline, "Uvicorn no arrancó"
            time.sleep(0.01)
        monkeypatch.setenv("ADMIN_TOKEN", "test-admin")
        script = Path(__file__).resolve().parents[1] / "scripts/verificar_payloads.py"
        verify = runpy.run_path(str(script))["verify"]
        states = verify(f"http://127.0.0.1:{port}", timeout=10)
        assert len(states) == 9  # Diez recepciones, nueve event_id distintos.
        assert len(alerts) == 3
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        listener.close()
        assert not thread.is_alive()
