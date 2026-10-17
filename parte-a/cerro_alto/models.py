# Contratos de datos internos y del endpoint exigido por INVERS.
# NormalizedLead contiene datos depurados; Enrichment resume la API de pacientes;
# Classification valida el JSON del LLM; LeadStatus define la respuesta pública.
# Los modelos separan el resultado de una interpretación del dato original recibido.
# La evidencia del LLM también se contrasta con el mensaje en llm.py: tener un JSON
# con tipos correctos no demuestra por sí mismo que su contenido esté sustentado.
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


# Representación uniforme para todas las etapas.
# warnings conserva anomalías recuperables sin rechazar automáticamente el lead.
class NormalizedLead(BaseModel):
    full_name: str = ""
    name_key: str = ""
    telefono_e164: str | None = None
    email: str | None = None
    city: str = ""
    service_interest: str = ""
    message: str = ""
    warnings: list[str] = Field(default_factory=list)


# Los ocho campos del contrato público de estado de INVERS.
# Un event_id nulo permite representar entradas sin identificador externo.
class LeadStatus(BaseModel):
    event_id: str | None
    estado: Literal["procesado", "duplicado", "rechazado", "fallido", "pendiente"]
    ruta: Literal["caliente", "tibio", "basura"] | None = None
    lead_id: str | None = None
    duplicado_de: str | None = None
    telefono_e164: str | None = None
    email: str | None = None
    motivo: str | None = None

    # Evita publicar combinaciones contradictorias de estado y ruta.
    # Solo procesado lleva ruta; rechazos, duplicados, fallos y basura requieren motivo.
    @model_validator(mode="after")
    def consistent(self):
        if (self.estado == "procesado") != (self.ruta is not None):
            raise ValueError("La ruta solo corresponde a un evento procesado")
        if (self.estado in {"duplicado", "rechazado", "fallido"} or self.ruta == "basura") and not self.motivo:
            raise ValueError("Se requiere motivo")
        return self


# Esquema estricto de salida del modelo: rechaza campos extra y tipos incorrectos.
# El presupuesto debe ser nulo o estar sustentado; no equivale a tarifas de la ficha.
class Classification(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    intencion: Literal["agendar", "contacto_pronto", "informacion", "tratamiento", "urgencia", "spam", "otro"]
    urgencia: Literal["alta", "media", "baja"]
    presupuesto_estimado: float | None = Field(ge=0, le=1000000)
    presupuesto_evidencia: str | None = Field(max_length=500)
    siguiente_accion: Literal["coordinar_valoracion", "derivar_asesor", "seguimiento", "descartar"]
    interes_alto_valor: bool
    es_spam: bool
    evidencia: str = Field(min_length=1, max_length=700)
    idioma: Literal["es", "en", "otro"] = "es"


# Resumen mínimo del paciente: identidad nueva, confirmada o ambigua.
# Los tratamientos se expresan con indicadores, sin copiar el historial completo.
class Enrichment(BaseModel):
    identity: Literal["nuevo", "confirmado", "ambiguo"]
    patient_id: str | None = None
    sede_habitual: Literal["norte", "valle"] | None = None
    ultima_visita: str | None = None
    has_pending: bool = False
    has_ongoing: bool = False
    candidate_count: int = 0
