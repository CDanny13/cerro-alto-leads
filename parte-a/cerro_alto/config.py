# Configuración central, validada con Pydantic y leída del entorno o de .env.
# Settings concentra URLs, secretos y límites para evitar valores dispersos.
# SecretStr oculta secretos en la representación del modelo; al enviar solicitudes
# se extraen explícitamente. No debe imprimirse el resultado de esa extracción.
# Los valores por defecto permiten iniciar la recepción local, pero no sustituyen
# credenciales: las integraciones sin configurar fallan de forma explícita.
from pathlib import Path
import os
from typing import Literal

from dotenv import load_dotenv
from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator

ROOT = Path(__file__).resolve().parents[2]


# Modelo único de configuración: Pydantic convierte los textos del entorno
# a booleanos/números y comprueba límites antes de iniciar la aplicación.
class Settings(BaseModel):
    model_config = ConfigDict(hide_input_in_errors=True)
    # Persistencia local: cambiar DB_PATH permite una prueba limpia sin borrar
    # la base anterior; las rutas relativas dependen del directorio de ejecución.
    db_path: str = Field(default=str(ROOT / "data" / "leads.sqlite3"), min_length=1)
    api_pacientes_url: str = ""
    api_pacientes_token: SecretStr = SecretStr("")
    # Proveedor, clave y modelo son independientes y configurables.
    # El modo JSON solicita estructura, pero la validación final sigue siendo local.
    llm_api_url: str = ""
    llm_api_key: SecretStr = SecretStr("")
    llm_model: str = ""
    llm_json_mode: bool = True
    llm_token_parameter: Literal["auto", "max_tokens", "max_completion_tokens"] = "auto"
    alert_webhook_url: SecretStr = SecretStr("")
    alert_webhook_token: SecretStr = SecretStr("")
    admin_token: SecretStr = SecretStr("")
    # Controles del procesamiento: frecuencia, timeout de cada petición, intentos
    # y duración de reservas. Una lease evita tomar enseguida un trabajo en curso.
    worker_enabled: bool = True
    poll_seconds: float = Field(default=0.5, gt=0)
    request_timeout: float = Field(default=15, gt=0, le=45)
    max_attempts: int = Field(default=3, ge=1, le=5)
    retry_seconds: float = Field(default=5, ge=0)
    lease_seconds: int = Field(default=240, ge=60)
    max_body_bytes: int = Field(default=262144, ge=8192)
    max_message_chars: int = Field(default=30000, ge=5000)
    dedupe_hours: int = Field(default=24, ge=1)
    # El calendario debe confirmarlo el operador; si está incompleto, los textos
    # de urgencia evitan asegurar que alguna sede esté abierta.
    national_holidays: str = ""
    holidays_complete: bool = False

    # Rechaza URLs incompletas o con usuario, contraseña, query o fragmento.
    # Exige HTTPS salvo localhost; la ruta secreta de Slack sí puede formar parte
    # de la URL, que por ello se conserva como SecretStr y no debe registrarse.
    @model_validator(mode="after")
    def secure_endpoints(self):
        from urllib.parse import urlparse
        for url in (self.api_pacientes_url, self.llm_api_url,
                    self.alert_webhook_url.get_secret_value()):
            if not url:
                continue
            parsed = urlparse(url)
            if not parsed.hostname:
                raise ValueError("La URL debe incluir un servidor válido")
            if parsed.username or parsed.password or parsed.query or parsed.fragment:
                raise ValueError("Las URLs no deben contener credenciales, query ni fragmento")
            if parsed.scheme != "https" and not (
                parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "localhost", "::1"}
            ):
                raise ValueError("Usa HTTPS; HTTP solo está permitido en localhost")
        return self

    # Carga el .env de la raíz sin reemplazar variables ya presentes en el sistema.
    # Relaciona nombres en mayúsculas, como DB_PATH, con campos como db_path.
    @classmethod
    def from_env(cls):
        load_dotenv(ROOT / ".env", override=False)
        values = {name: os.environ[name.upper()] for name in cls.model_fields
                  if name.upper() in os.environ}
        return cls(**values)

    # Resume si están presentes los datos mínimos de cada integración.
    # Comprueba presencia de valores, no su validez contra el servicio remoto.
    def readiness(self) -> dict[str, bool]:
        return {
            "pacientes": bool(self.api_pacientes_url and self.api_pacientes_token.get_secret_value()),
            "llm": bool(self.llm_api_url and self.llm_api_key.get_secret_value() and self.llm_model),
            "alertas": bool(self.alert_webhook_url.get_secret_value()),
            "administracion": bool(self.admin_token.get_secret_value()),
        }
