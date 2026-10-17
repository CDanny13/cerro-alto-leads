# Fixtures compartidas de Pytest: preparación común y aislada para las pruebas.
# Cada test recibe una base temporal, worker automático desactivado y token ficticio.
# Los payloads se leen de los originales sin modificarlos. Las copias mutables
# necesarias para casos adicionales se crean dentro de cada prueba.
# No se leen credenciales del .env para construir la configuración de estos tests.
import json
from pathlib import Path

import pytest

from cerro_alto.config import Settings

ROOT = Path(__file__).resolve().parents[2]


# Fixture con SQLite temporal por test y token artificial.
# Desactiva el worker automático para que cada caso controle cuándo se procesa.
@pytest.fixture
def settings(tmp_path):
    return Settings(db_path=str(tmp_path / "leads.sqlite3"), worker_enabled=False,
                    admin_token="test-admin", retry_seconds=0)


# Carga los diez JSON en orden de nombre y devuelve objetos para las pruebas.
# Los archivos originales nunca se escriben ni se sustituyen por datos generados.
@pytest.fixture
def payloads():
    return [json.loads(path.read_text(encoding="utf-8")) for path in sorted((ROOT / "payloads").glob("*.json"))]
