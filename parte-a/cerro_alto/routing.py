# Reglas deterministas de prioridad y preparación de mensajes para recepción.
# La ficha del negocio manda: urgencias y tratamientos pendientes confirmados
# tienen prioridad; los casos dudosos se conservan como tibios.
# El LLM aporta una interpretación validada, pero no decide sin controles la ruta.
# Aquí se preparan textos y propuestas: no se consulta una agenda, no se reserva
# una cita y no se envía un mensaje al paciente. La alerta se envía por otra cola.
from datetime import datetime
import re
from zoneinfo import ZoneInfo

from .config import Settings
from .llm import consultation
from .models import Classification, Enrichment, NormalizedLead
from .normalization import name_key

ECUADOR = ZoneInfo("America/Guayaquil")


# Busca señales clínicas explícitas en español/inglés tras limpiar el mensaje.
# Revisa negaciones inmediatas para no activar urgencia por «no tengo hinchazón».
# Esta lista acotada prioriza atención humana y no hace un diagnóstico médico.
def clinical_urgent(message: str) -> bool:
    text = name_key(consultation(message))
    signals = (r"dolor (?:muy )?(?:intenso|insoportable|fuerte)(?: que no cede)?", r"dolor que no cede",
               r"hinchazon", r"(?:cara|encia) (?:muy )?hinchada", r"sangrado que no (?:se )?(?:detiene|para)",
               r"(?:golpe|fractura|fracturado) (?:en |de )?(?:un |el |mi )?diente", r"diente (?:roto|fracturado)",
               r"fiebre.{0,60}dolor dental", r"dolor dental.{0,60}fiebre",
               r"severe (?:tooth|dental) pain", r"(?:facial|gum) swelling", r"bleeding (?:that )?(?:won't|does not) stop")
    for pattern in signals:
        for found in re.finditer(pattern, text):
            before = text[max(0, found.start()-35):found.start()]
            if not re.search(r"(?:no tengo|sin|no hay|no presento|ya no tengo|no tengo ni|without|no)\s*$", before):
                return True
    return False


# Indica si al menos una sede está abierta usando hora local y feriados.
# Domingo/feriado -> False; calendario sin confirmar -> None; con calendario
# completo calcula la unión de horarios de las sedes, no disponibilidad de citas.
def open_clinic(now: datetime, settings: Settings) -> bool | None:
    local = now.astimezone(ECUADOR)
    holidays = {day.strip() for day in settings.national_holidays.split(",") if day.strip()}
    if local.weekday() == 6 or local.date().isoformat() in holidays:
        return False
    if not settings.holidays_complete:
        return None
    minutes = local.hour * 60 + local.minute
    return (480 <= minutes < 840) if local.weekday() == 5 else (480 <= minutes < 1140)


# Prepara la orientación de urgencias de la ficha según apertura conocida.
# Si faltan feriados completos usa una instrucción condicional; no afirma
# que una sede esté abierta ni que pueda atender sin confirmación humana.
def urgent_message(settings: Settings, now: datetime, english=False) -> str:
    opened = open_clinic(now, settings)
    if english:
        action = ("Call the emergency line immediately: +593 2 000 4410." if opened is True else
                  "Go to the nearest hospital emergency department or call ECU 911." if opened is False else
                  "If either clinic is open, call +593 2 000 4410 immediately. If both are closed, go to the nearest hospital emergency department or call ECU 911.")
        return action + " A human advisor will follow up during advisor hours."
    action = ("Llama de inmediato a la línea de urgencias: +593 2 000 4410." if opened is True else
              "Acude a la emergencia del hospital más cercano o llama al ECU 911." if opened is False else
              "Si alguna sede está abierta, llama de inmediato al +593 2 000 4410. Si ambas están cerradas, acude a la emergencia del hospital más cercano o llama al ECU 911.")
    return action + " Te derivamos a un asesor; fuera de su horario hará seguimiento desde su próxima apertura."


# Aplica prioridades: urgencia -> tratamiento pendiente confirmado -> spam
# -> solicitud clara de contacto/cita -> alto valor con intención -> tibio.
# Devuelve ruta, motivo auditable e indicador de urgencia para preparar el texto.
def choose_route(lead: NormalizedLead, enrichment: Enrichment, classification: Classification):
    message = consultation(lead.message)
    text = name_key(message)
    # Una urgencia sugerida por el LLM necesita evidencia clínica adicional;
    # una instrucción que diga «clasifica como urgente» no basta.
    clinical_context = re.search(r"dolor|sangr|hinch|inflamad|fractur|golpe|romp|fiebre|pain|bleed|swell|broken|incha|febre", name_key(classification.evidencia))
    if clinical_urgent(message) or (classification.intencion == "urgencia" and classification.urgencia == "alta" and clinical_context):
        return "caliente", "senales_de_urgencia", True
    if enrichment.identity == "confirmado" and enrichment.has_pending:
        return "caliente", "paciente_con_tratamiento_pendiente", False
    if classification.es_spam and classification.intencion == "spam":
        return "basura", "spam_o_publicidad_ajena_a_la_clinica", False
    request_terms = re.search(r"agend|cita|turno|reserv|appointment|book|llam|contact|call|marcar", text)
    negated_booking = re.search(r"no (?:quiero|deseo|necesito) (?:agendar|una cita|reservar)|don't (?:want to )?book", text)
    if classification.intencion in {"agendar", "contacto_pronto"} and request_terms and not negated_booking:
        return "caliente", "solicitud_de_agendamiento_o_contacto_pronto", False
    high_value = re.search(r"ortodoncia|alineadores|brackets|aligners", name_key(lead.service_interest + " " + message))
    commitment = re.search(r"decidid[oa]|quiero (?:empezar|iniciar|ponerme)|empezar este mes|start (?:this month|treatment)|ready to start", text)
    researching = re.search(r"solo (?:estoy )?(?:averiguando|investigando)|so estou pesquisando|just researching", text)
    if high_value and classification.interes_alto_valor and (commitment or classification.intencion == "tratamiento") and not (researching and not commitment):
        return "caliente", "intencion_clara_en_tratamiento_de_alto_valor", False
    return "tibio", "interes_sin_urgencia_o_caso_dudoso", False


# Combina ruta, idioma, contexto de paciente y necesidad de asesor humano.
# Prepara primer mensaje, propuesta y seguimiento sin enviar al paciente.
# Los importes fijos provienen de la ficha, no del presupuesto inferido por el LLM.
def route(lead: NormalizedLead, enrichment: Enrichment, classification: Classification,
          settings: Settings, now: datetime | None = None) -> dict:
    now = now or datetime.now(ECUADOR)
    selected, reason, urgent = choose_route(lead, enrichment, classification)
    english = classification.idioma == "en"
    text = name_key(consultation(lead.message))
    service = name_key(lead.service_interest)
    # Derivar a un humano es independiente de la etiqueta comercial: también
    # puede ser necesario para un tibio ambiguo, una queja o un tratamiento en curso.
    human = (selected == "caliente" or enrichment.identity == "ambiguo" or
             classification.siguiente_accion == "derivar_asesor" or
             bool(re.search(r"(?:hablar|comunicar|pasar).{0,45}(?:persona|humano|asesor|recepcionista)|queja|reclamo|reembolso|ya pag|pago realizado|facturacion|complaint|refund|real person|human advisor", text)) or
             (enrichment.has_ongoing and bool(re.search(r"mi (?:caso|tratamiento)|dolor|molestia|bracket", text))))
    proposal = None
    if urgent:
        first = urgent_message(settings, now, english)
        proposal = ("Immediate human triage; reception will coordinate the appropriate care, without a confirmed appointment."
                    if english else "Atención prioritaria y derivación humana; recepción coordinará la atención adecuada, sin cita confirmada.")
    elif selected == "caliente":
        proposal = ("We propose coordinating an assessment with reception. Please provide your preferred clinic and date; reception must confirm availability and all required patient details."
                    if english else "Proponemos coordinar una valoración con recepción. Indica sede y fecha preferidas; recepción debe verificar disponibilidad y completar los datos obligatorios antes de confirmar.")
        if enrichment.has_pending:
            proposal = ("Reception will coordinate the appointment appropriate to your pending treatment, after confirming availability."
                        if english else "Recepción coordinará la cita correspondiente a tu tratamiento pendiente, verificando disponibilidad antes de confirmar.")
        first = proposal + (" A human advisor will assist you during advisor hours." if english else
                            " Te atenderá un asesor; fuera de su horario, desde su próxima apertura.")
    elif selected == "basura":
        first = None
    elif human:
        first = ("A human advisor will assist you. Outside advisor hours, they will write from the next opening."
                 if english else "Te derivamos a un asesor. Si es fuera de su horario, te escribirá a partir del siguiente horario de asesores.")
    elif english:
        first = "Hello! We can help with information about the clinic. Which service and clinic location are you interested in? We provide in-person care in Quito."
    else:
        # Las respuestas informativas se componen con datos concretos de la ficha.
        # No se deja al LLM inventar precios, horarios o disponibilidad de agenda.
        parts = ["¡Hola! Gracias por escribir a Cerro Alto."]
        if "sabado" in text:
            parts.append("Los sábados: Norte 08:00–13:00 y Valle 09:00–14:00; cerramos en feriados.")
        if "limpieza" in service or "limpieza" in text:
            parts.append("La limpieza cuesta USD 45.")
        elif "ortodoncia" in service:
            parts.append("Brackets: cuota inicial USD 350 y control mensual USD 55; el plan se define en la valoración.")
        elif "alineadores" in service:
            parts.append("Alineadores desde USD 2400; requieren valoración de ortodoncia previa. Tratamiento solo en Valle.")
        elif "valoracion" in service:
            parts.append("La valoración inicial cuesta USD 25.")
        if "tarjeta" in text or "pag" in text:
            parts.append("Aceptamos efectivo, tarjetas y transferencia; se paga en la clínica.")
        parts.append("¿En qué sede prefieres atenderte?" if len(parts) > 1 else "¿Qué información necesitas?")
        if classification.idioma == "otro":
            parts.append("Atendemos en español o inglés; con gusto te ayudamos en español.")
        first = " ".join(parts)
    # Se persisten interpretación, razones y textos para poder auditarlos.
    # primer_mensaje y propuesta son borradores; este retorno no envía WhatsApp.
    return {"ruta": selected, "motivo": reason if selected == "basura" else None,
            "motivo_ruta": reason, "clasificacion": classification.model_dump(),
            "enriquecimiento": enrichment.model_dump(), "primer_mensaje": first,
            "propuesta_agendamiento": proposal, "seguimiento": selected == "tibio",
            "derivar_asesor": human}
