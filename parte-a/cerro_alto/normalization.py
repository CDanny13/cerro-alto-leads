# Convierte el payload del conector en datos uniformes antes de guardarlos.
# Se distinguen advertencias recuperables de motivos de rechazo. Por ejemplo,
# un nombre vacío se acepta si queda al menos un contacto válido.
# El cuerpo original se conserva por separado en Store, incluso ante rechazos;
# normalizar no equivale a sobrescribir la entrada ni a confirmar una identidad.
# Estas funciones son locales: no consultan pacientes, DNS ni un LLM.
import re
import unicodedata
from datetime import datetime

from email_validator import EmailNotValidError, validate_email
import phonenumbers

from .models import NormalizedLead


# Crea una clave de comparación sin tildes, diferencias de mayúsculas ni
# espacios repetidos. No altera el nombre original ni demuestra identidad por sí sola.
def name_key(value: str) -> str:
    plain = unicodedata.normalize("NFKD", value.casefold())
    return " ".join("".join(c for c in plain if not unicodedata.combining(c)).split())


# Valida y transforma teléfonos a E.164 con phonenumbers.
# Acepta variantes de prefijo internacional y usa Ecuador para números nacionales;
# si el formato o el número no son válidos devuelve None, sin inventar contacto.
def phone(value) -> str | None:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        return None
    text = str(value).strip()
    if not text or re.search(r"[^\d\s+().-]", text):
        return None
    digits = re.sub(r"\D", "", text)
    if text.startswith("00"):
        text = "+" + digits[2:]
    elif not text.startswith("+") and digits.startswith("593"):
        text = "+" + digits
    try:
        parsed = phonenumbers.parse(text, "EC")
        if phonenumbers.is_valid_number(parsed):
            return phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)
    except phonenumbers.NumberParseException:
        pass
    return None


# Valida sintaxis y normaliza el correo sin consultar entregabilidad por DNS.
# Aceptación sintáctica no confirma que el buzón exista o pertenezca al lead.
def email(value) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        return validate_email(value.strip(), check_deliverability=False,
                              test_environment=True).normalized.casefold()
    except EmailNotValidError:
        return None


# Devuelve (lead_normalizado, motivo_de_rechazo_o_None).
# Revisa estructura y tipo de evento, conserva anomalías como advertencias,
# limpia campos y exige al menos teléfono o correo válido. El nombre es opcional.
def normalize(payload, max_message_chars=30000) -> tuple[NormalizedLead, str | None]:
    lead = NormalizedLead()
    if not isinstance(payload, dict) or not isinstance(payload.get("lead"), dict):
        return lead, "estructura_invalida: se requiere un objeto lead"
    if payload.get("event_type") != "lead.created":
        return lead, "event_type_no_soportado"
    data = payload["lead"]
    # Metadatos ausentes o extraños generan advertencias. La recepción puede
    # seguir siendo válida si el contacto sirve; el ID interno garantiza trazabilidad.
    external_id = payload.get("event_id")
    if not isinstance(external_id, str) or not external_id.strip() or len(external_id) > 200:
        lead.warnings.append("event_id_ausente_o_invalido")
    try:
        created = datetime.fromisoformat(payload.get("created_at", "").replace("Z", "+00:00"))
        if created.tzinfo is None:
            raise ValueError()
    except (ValueError, TypeError, AttributeError):
        lead.warnings.append("created_at_ausente_o_invalido")
    source = payload.get("source")
    if not isinstance(source, dict) or not isinstance(source.get("platform"), str) or source.get("platform") not in {"facebook", "instagram"}:
        lead.warnings.append("source_ausente_o_invalido")
    # Cada canal se valida de forma independiente: uno inválido no anula
    # al otro. Solo se rechaza por contacto si ambos quedan vacíos.
    lead.telefono_e164 = phone(data.get("phone"))
    lead.email = email(data.get("email"))
    for field, valid in (("phone", lead.telefono_e164), ("email", lead.email)):
        if data.get(field) not in (None, "") and not valid:
            lead.warnings.append(f"{field}_invalido")
    # Se normalizan únicamente transformaciones conocidas; otros tipos
    # se sustituyen por texto vacío y quedan advertidos para revisión.
    for field in ("full_name", "city", "service_interest", "message"):
        value = data.get(field, "")
        if field == "city" and isinstance(value, list) and len(value) == 1 and isinstance(value[0], str):
            value = value[0]
            lead.warnings.append("city_lista_normalizada")
        if not isinstance(value, str):
            value = ""
            lead.warnings.append(f"{field}_tipo_invalido")
        setattr(lead, field, value.strip())
    lead.name_key = name_key(lead.full_name)
    if lead.service_interest not in {"Blanqueamiento", "Combo limpieza + blanqueamiento", "Limpieza", "Valoración", "Ortodoncia", "Alineadores", "Otro", ""}:
        lead.warnings.append("service_interest_no_catalogado")
    if not lead.telefono_e164 and not lead.email:
        return lead, "sin_contacto_valido"
    if len(lead.message) > max_message_chars:
        return lead, "mensaje_excede_limite: revision_manual_requerida"
    return lead, None
