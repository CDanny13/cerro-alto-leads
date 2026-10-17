# Prueba opcional que sí llama a pacientes y LLM reales.
# Exige marca integration, RUN_REAL_INTEGRATION=1 y credenciales configuradas.
# Se excluye del recorrido offline para evitar consumo externo accidental.
# Su éxito verifica esta consulta concreta; no reemplaza la prueba de los diez
# eventos ni solicita una alerta real, porque send_alert no se activa aquí.
"""Prueba explícita y opcional con API y LLM REALES. Nunca se sustituye por mocks."""
import os

import pytest

from cerro_alto.config import Settings
from cerro_alto.live_checks import check_integrations


# Ejecuta check_integrations con la configuración real bajo activación explícita.
# Exige salud y JSON válido; el mensaje «info» no puede producir presupuesto.
@pytest.mark.integration
@pytest.mark.skipif(os.getenv("RUN_REAL_INTEGRATION") != "1", reason="Requiere RUN_REAL_INTEGRATION=1 y credenciales reales")
async def test_live_patients_and_llm():
    result = await check_integrations(Settings.from_env())
    assert result["api_health"] == "ok" and result["llm_json_validado"]
    assert result["presupuesto_estimado"] is None  # El mensaje "info" no declara presupuesto.
