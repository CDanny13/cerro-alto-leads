# Clasificación real mediante una API compatible con Chat Completions.
# El prompt versionado, el esquema JSON y la ficha forman el mensaje de sistema.
# El mensaje del lead se envía como dato, con reducción de contactos y contexto
# mínimo del paciente; no se manda el historial completo ni su identidad.
# La respuesta debe pasar validación de estructura y evidencia. El cliente no
# inventa una clasificación de reemplazo si falla la red o el contenido del LLM.
# La ruta definitiva se decide luego en routing.py, mediante reglas explícitas.
import json
import re
from urllib.parse import urlparse

import httpx
from pydantic import ValidationError

from .config import ROOT, Settings
from .errors import ServiceError, retry_after
from .models import Classification, Enrichment, NormalizedLead


# Retira el bloque explícito de instrucción maliciosa conocido en los ejemplos.
# Es una defensa acotada; no un detector universal de prompt injection.
# El prompt de sistema, la evidencia y las reglas añaden controles independientes.
def consultation(message: str) -> str:
    # Defensa adicional para bloques explícitos; el prompt y las reglas son capas separadas.
    cleaned = re.sub(r"IMPORTANTE PARA EL SISTEMA:.*?Fin de la instrucción\.", "", message,
                     flags=re.IGNORECASE | re.DOTALL)
    return cleaned.strip()


# Oculta patrones comunes de teléfono y correo antes de enviar texto al LLM.
# Reduce exposición de contactos; no garantiza anonimizar todo dato personal libre.
def redact(message: str) -> str:
    message = re.sub(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", "[correo omitido]", message)
    return re.sub(r"(?<!\w)\+?\d[\d ()-]{7,}\d(?!\w)", "[contacto omitido]", message)


# Contrasta evidencia literal con mensaje/servicio y coherencia de spam.
# Para presupuesto exige una declaración explícita del usuario y un importe
# coincidente; un precio del catálogo no demuestra cuánto puede pagar el lead.
def evidence_supported(result: Classification, message: str, service: str):
    available = (message + "\n" + service).casefold()
    if not message and not service:
        available = "sin mensaje"
    if result.evidencia.casefold() not in available:
        raise ServiceError("llm", "llm_evidencia_no_sustentada")
    if result.es_spam != (result.intencion == "spam"):
        raise ServiceError("llm", "llm_spam_inconsistente")
    if result.presupuesto_estimado is None:
        if result.presupuesto_evidencia is not None:
            raise ServiceError("llm", "llm_presupuesto_inconsistente")
        return
    evidence = result.presupuesto_evidencia or ""
    if (not evidence or evidence.casefold() not in message.casefold() or not re.search(
        r"presupuesto|puedo (?:pagar|gastar)|dispongo|mi l[ií]mite|budget|can spend|or[cç]amento", evidence, re.I)):
        raise ServiceError("llm", "llm_presupuesto_no_sustentado")
    # Formatos simples de USD; las cantidades ambiguas se rechazan para revisión/reintento.
    amounts = re.findall(r"(?<!\d)\d+(?:[.,]\d{1,2})?(?!\d)", evidence)
    if not any(abs(float(amount.replace(",", ".")) - result.presupuesto_estimado) < 0.001 for amount in amounts):
        raise ServiceError("llm", "llm_presupuesto_no_sustentado")


# Adaptador del proveedor de clasificación; no contiene un clasificador simulado.
class LLMClient:
    # Carga prompt y ficha versionados y agrega el esquema JSON de Pydantic.
    # La ficha actúa como contexto de negocio del modelo y el esquema fija su salida.
    def __init__(self, settings: Settings, client: httpx.AsyncClient):
        self.settings, self.client = settings, client
        prompt = (ROOT / "parte-a/prompts/clasificar_lead.txt").read_text(encoding="utf-8")
        ficha = (ROOT / "parte-a/Contexto/ficha-negocio.md").read_text(encoding="utf-8")
        self.system = prompt + "\n\nESQUEMA JSON:\n" + json.dumps(Classification.model_json_schema(), ensure_ascii=False) + "\n\nFICHA:\n" + ficha

    # Prepara contexto mínimo, llama al modelo configurado y valida la respuesta.
    # Comprueba HTTP, sobre Chat Completions, fin de generación, JSON y evidencia.
    # Ante cualquier fallo lanza ServiceError; el worker decide reintento o revisión.
    async def classify(self, lead: NormalizedLead, enrichment: Enrichment) -> Classification:
        if not (self.settings.llm_api_url and self.settings.llm_model and self.settings.llm_api_key.get_secret_value()):
            raise ServiceError("llm", "llm_sin_configurar", retryable=False)
        message = redact(consultation(lead.message))
        service = redact(lead.service_interest)
        token_parameter = self.settings.llm_token_parameter
        # Compatibilidad de proveedores: OpenAI usa el parámetro moderno del límite;
        # otros servicios conservan max_tokens salvo configuración explícita.
        if token_parameter == "auto":
            token_parameter = ("max_completion_tokens" if urlparse(self.settings.llm_api_url).hostname == "api.openai.com"
                               else "max_tokens")
        # La ficha y reglas van en system; el texto libre va serializado en user.
        # No se incluyen nombre, teléfono, correo ni identificador del paciente.
        payload = {
            "model": self.settings.llm_model,
            "messages": [
                {"role": "system", "content": self.system},
                {"role": "user", "content": json.dumps({
                    "mensaje": message, "servicio_interes": service,
                    "contexto_confirmado": {"identidad": enrichment.identity,
                        "tratamiento_pendiente": enrichment.has_pending,
                        "tratamiento_en_curso": enrichment.has_ongoing},
                }, ensure_ascii=False)},
            ],
            token_parameter: 1200,
        }
        if self.settings.llm_json_mode:
            payload["response_format"] = {"type": "json_object"}
        try:
            response = await self.client.post(self.settings.llm_api_url, json=payload, headers={
                "Authorization": "Bearer " + self.settings.llm_api_key.get_secret_value(),
                "Accept": "application/json",
            })
        except httpx.RequestError:
            raise ServiceError("llm", "llm_error_red") from None
        if response.status_code != 200:
            raise ServiceError("llm", f"llm_http_{response.status_code}",
                retryable=response.status_code in {408, 429} or response.status_code >= 500,
                retry_after=retry_after(response.headers) if response.status_code == 429 else 0)
        try:
            if len(response.content) > 65536:
                raise ValueError("oversize")
            # No basta con HTTP 200: hay que comprobar que el sobre contiene una
            # generación textual completa y que el proveedor no respondió con un rechazo.
            choice = response.json()["choices"][0]
            if not isinstance(choice, dict) or not isinstance(choice.get("message"), dict):
                raise ValueError("invalid_envelope")
            if choice.get("finish_reason") not in (None, "stop") or choice["message"].get("refusal"):
                raise ValueError("incomplete")
            content = choice["message"]["content"]
            if not isinstance(content, str):
                raise ValueError("not_text")
            classification = Classification.model_validate_json(content)
        except (ValueError, KeyError, IndexError, TypeError, ValidationError):
            raise ServiceError("llm", "llm_respuesta_invalida") from None
        # La validación semántica se hace después de la estructural: una cifra
        # del tipo correcto también se rechaza si no la declaró el usuario.
        evidence_supported(classification, message, service)
        return classification
