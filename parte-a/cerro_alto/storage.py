# Persistencia y colas durables sobre SQLite, sin depender de colas en memoria.
# events representa eventos canónicos; receipts representa CADA recepción HTTP,
# incluidos reenvíos, conflictos de contenido y entradas sin event_id.
# notifications es la cola de salida; failures conserva códigos auditables.
# rate_requests/rate_blocks mantienen el límite de API y api_cache guarda páginas.
# Las escrituras transaccionales evitan separar el cambio de estado de su aviso.
# Una lease es una reserva temporal: si el proceso muere, el trabajo puede volver
# a tomarse al vencerla. No es una eliminación ni un procesamiento completado.
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import time
from uuid import uuid4

from .models import LeadStatus, NormalizedLead
from .routing import clinical_urgent


# Serializa JSON estable, con Unicode legible y sin números no finitos.
# Ordenar claves permite comparar la huella del contenido sin depender de su orden.
def dumps(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


# Convierte ISO 8601 con zona horaria a segundos UTC para consultar recepciones.
def timestamp(value: str) -> float:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("Incluye zona horaria en desde")
    return parsed.astimezone(timezone.utc).timestamp()


# Encapsula SQL y transacciones para que los demás módulos no gestionen tablas.
# Cada operación abre su conexión; no se comparte una conexión entre hilos de FastAPI.
class Store:
    # Crea el directorio y las tablas si aún no existen, sin borrar datos previos.
    # WAL permite lectores mientras se escribe; los índices aceleran colas e identidad.
    def __init__(self, path: str):
        self.path = path
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as db:
            db.execute("PRAGMA journal_mode=WAL")
            # Esquema de instalación idempotente: IF NOT EXISTS conserva las tablas.
            # Los identificadores externos son únicos; SQLite permite varios NULL
            # para guardar eventos distintos que llegan sin event_id.
            db.executescript("""
                CREATE TABLE IF NOT EXISTS events (
                    id TEXT PRIMARY KEY, event_id TEXT UNIQUE, received_at REAL NOT NULL,
                    fingerprint TEXT NOT NULL, raw TEXT NOT NULL, normalized TEXT NOT NULL,
                    estado TEXT NOT NULL, ruta TEXT, lead_id TEXT, duplicado_de TEXT,
                    motivo TEXT, result TEXT, attempts INTEGER NOT NULL DEFAULT 0,
                    next_attempt REAL NOT NULL DEFAULT 0, lease_until REAL,
                    name_key TEXT, phone TEXT, email TEXT
                );
                CREATE INDEX IF NOT EXISTS event_jobs ON events(estado, next_attempt, lease_until);
                CREATE INDEX IF NOT EXISTS event_identity ON events(name_key, phone, email, received_at);
                CREATE TABLE IF NOT EXISTS receipts (
                    id TEXT PRIMARY KEY, event_fk TEXT NOT NULL REFERENCES events(id),
                    event_id TEXT, received_at REAL NOT NULL, raw TEXT NOT NULL,
                    override_state TEXT, override_reason TEXT
                );
                CREATE INDEX IF NOT EXISTS receipt_date ON receipts(received_at);
                CREATE TABLE IF NOT EXISTS failures (
                    id INTEGER PRIMARY KEY, event_fk TEXT, created_at REAL NOT NULL,
                    component TEXT NOT NULL, code TEXT NOT NULL, attempt INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS notifications (
                    id TEXT PRIMARY KEY, event_fk TEXT REFERENCES events(id),
                    kind TEXT NOT NULL, payload TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pendiente',
                    attempts INTEGER NOT NULL DEFAULT 0, next_attempt REAL NOT NULL DEFAULT 0,
                    lease_until REAL, created_at REAL NOT NULL, sent_at REAL, last_error TEXT,
                    UNIQUE(event_fk, kind)
                );
                CREATE TABLE IF NOT EXISTS rate_requests (service TEXT NOT NULL, at REAL NOT NULL);
                CREATE INDEX IF NOT EXISTS rate_at ON rate_requests(service, at);
                CREATE TABLE IF NOT EXISTS rate_blocks (service TEXT PRIMARY KEY, until REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS api_cache (key TEXT PRIMARY KEY, expires REAL NOT NULL, body TEXT NOT NULL);
            """)

    # Contexto transaccional: commit al terminar, rollback ante cualquier fallo.
    # BEGIN IMMEDIATE reserva la escritura antes de leer/modificar una cola para
    # evitar que dos peticiones reclamen o creen a la vez el mismo registro.
    @contextmanager
    def connection(self, write=False):
        db = sqlite3.connect(self.path, timeout=2.5)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA synchronous=FULL")
        try:
            if write:
                db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    # Proyecta una fila al contrato público validado con LeadStatus.
    # Una recepción repetida puede mostrar un estado propio mediante override,
    # sin modificar el estado canónico del primer evento recibido.
    @staticmethod
    def status(row, override=None, reason=None) -> dict:
        return LeadStatus(
            event_id=row["event_id"], estado=override or row["estado"],
            ruta=None if override else row["ruta"], lead_id=row["lead_id"],
            duplicado_de=(row["duplicado_de"] or row["event_id"]) if override == "duplicado" else row["duplicado_de"],
            telefono_e164=row["phone"], email=row["email"],
            motivo=reason if override else row["motivo"],
        ).model_dump()

    # Guarda recepción y evento en una única transacción antes del ACK.
    # Primero resuelve idempotencia por event_id y huella; después evalúa un
    # duplicado comercial bajo condiciones conservadoras, sin unir familiares.
    def receive(self, raw: str, payload, lead: NormalizedLead, reason: str | None,
                dedupe_hours=24) -> dict:
        now = time.time()
        receipt_id, internal_id = str(uuid4()), str(uuid4())
        external = payload.get("event_id") if isinstance(payload, dict) else None
        event_id = external if isinstance(external, str) and external.strip() and len(external) <= 200 else None
        digest = hashlib.sha256((dumps(payload) if payload is not None else raw).encode()).hexdigest()
        with self.connection(write=True) as db:
            # Idempotencia técnica: mismo ID y mismo contenido es reenvío;
            # mismo ID con contenido diferente es conflicto, sin sobrescribir el original.
            original = db.execute("SELECT * FROM events WHERE event_id=?", (event_id,)).fetchone() if event_id else None
            if original:
                same = original["fingerprint"] == digest
                state = "duplicado" if same else "rechazado"
                why = "reenvio_mismo_event_id" if same else "event_id_reutilizado_con_otro_contenido"
                db.execute("INSERT INTO receipts VALUES (?,?,?,?,?,?,?)",
                           (receipt_id, original["id"], event_id, now, raw, state, why))
                return {"receipt_id": receipt_id, **self.status(original, state, why)}
            # Deduplicación comercial entre IDs distintos: exige nombre y ambos contactos,
            # ventana temporal y mensaje idéntico o declaración de reenvío. Las urgencias
            # se excluyen para que un problema clínico nuevo nunca quede oculto.
            duplicate = None
            # Dos contactos + nombre; además mismo mensaje o declaración explícita de reenvío.
            repeated = any(term in lead.message.casefold() for term in
                           ("llené otra vez", "llene otra vez", "envié otra vez", "envie otra vez", "formulario de nuevo"))
            if not reason and event_id and lead.name_key and lead.telefono_e164 and lead.email and not clinical_urgent(lead.message):
                matches = db.execute("""SELECT * FROM events WHERE name_key=? AND phone=? AND email=?
                    AND received_at>=? AND estado NOT IN ('rechazado','duplicado') AND event_id IS NOT NULL
                    ORDER BY received_at ASC""",
                    (lead.name_key, lead.telefono_e164, lead.email, now - dedupe_hours * 3600)).fetchall()
                duplicate = next((r for r in matches if repeated or
                                  json.loads(r["normalized"])["message"] == lead.message), None)
            # Se decide el estado inicial antes de insertar el evento y su recepción.
            # Un rechazado no crea lead_id; un duplicado comercial reutiliza el del original.
            state = "rechazado" if reason else "duplicado" if duplicate else "pendiente"
            why = reason or ("reenvio_comercial_identidad_y_contactos_coincidentes" if duplicate else None)
            lead_id = duplicate["lead_id"] if duplicate else (None if reason else str(uuid4()))
            db.execute("""INSERT INTO events (id,event_id,received_at,fingerprint,raw,normalized,estado,
                lead_id,duplicado_de,motivo,name_key,phone,email) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (internal_id, event_id, now, digest, raw, lead.model_dump_json(), state, lead_id,
                 duplicate["event_id"] if duplicate else None, why, lead.name_key, lead.telefono_e164, lead.email))
            db.execute("INSERT INTO receipts VALUES (?,?,?,?,?,?,?)",
                       (receipt_id, internal_id, event_id, now, raw, None, None))
            row = db.execute("SELECT * FROM events WHERE id=?", (internal_id,)).fetchone()
            return {"receipt_id": receipt_id, **self.status(row)}

    # Busca el estado canónico por event_id; None significa que nunca se recibió.
    def find(self, event_id: str):
        with self.connection() as db:
            row = db.execute("SELECT * FROM events WHERE event_id=?", (event_id,)).fetchone()
            return self.status(row) if row else None

    # Lista una entrada por recepción desde la fecha indicada, no por evento único.
    # JOIN aplica el estado particular de reenvíos/conflictos y conserva casos sin ID.
    def since(self, since: float) -> list[dict]:
        with self.connection() as db:
            rows = db.execute("""SELECT e.*, r.override_state, r.override_reason FROM receipts r
                JOIN events e ON e.id=r.event_fk WHERE r.received_at>=? ORDER BY r.received_at, r.id""", (since,)).fetchall()
            return [self.status(row, row["override_state"], row["override_reason"]) for row in rows]

    # Reserva el evento pendiente más antiguo cuya espera y lease hayan vencido.
    # result IS NULL excluye leads ya clasificados que solo esperan entregar la alerta.
    def claim_event(self, lease_seconds: int):
        now = time.time()
        with self.connection(write=True) as db:
            row = db.execute("""SELECT * FROM events WHERE estado='pendiente' AND result IS NULL
                AND next_attempt<=? AND (lease_until IS NULL OR lease_until<?)
                ORDER BY received_at LIMIT 1""", (now, now)).fetchone()
            if row:
                db.execute("UPDATE events SET lease_until=? WHERE id=?", (now + lease_seconds, row["id"]))
                return dict(row)

    # Libera reservas al cierre ordenado para que el siguiente arranque retome
    # trabajos sin esperar su vencimiento. No cambia su estado ni elimina resultados.
    def release_leases(self):
        # Solo usar al cerrar normalmente el único procesador del despliegue.
        with self.connection(write=True) as db:
            db.execute("UPDATE events SET lease_until=NULL WHERE estado='pendiente'")
            db.execute("UPDATE notifications SET lease_until=NULL WHERE status='pendiente'")

    # Inserta un aviso dentro de la transacción del llamador.
    # La restricción evento/tipo evita crear el mismo aviso en cada reintento;
    # esto no garantiza entrega exactamente una vez en un receptor como Slack.
    @staticmethod
    def enqueue(db, event_fk, kind, payload):
        db.execute("""INSERT OR IGNORE INTO notifications (id,event_fk,kind,payload,created_at)
                      VALUES (?,?,?,?,?)""", (str(uuid4()), event_fk, kind, dumps(payload), time.time()))

    # Guarda clasificación y ruta. Un tibio/basura queda procesado de inmediato;
    # un caliente queda pendiente hasta confirmar entrega de su alerta. Si ya se
    # envió una prealerta urgente, reutiliza ese estado sin crear otra notificación.
    def complete(self, internal_id, result):
        hot = result["ruta"] == "caliente"
        with self.connection(write=True) as db:
            notification = db.execute("SELECT status FROM notifications WHERE event_fk=? AND kind='caliente'", (internal_id,)).fetchone()
            state = "pendiente" if hot else "procesado"
            if hot and notification:
                state = {"enviada": "procesado", "fallida": "fallido", "pendiente": "pendiente"}[notification["status"]]
            # Resultado y aviso caliente se guardan en la misma transacción: no puede
            # quedar una clasificación confirmada sin que exista su trabajo de notificación.
            db.execute("UPDATE events SET result=?, estado=?, ruta=?, motivo=?, lease_until=NULL WHERE id=?",
                       (dumps(result), state, result["ruta"] if state == "procesado" else None,
                        "alerta_caliente_no_entregada" if state == "fallido" else result.get("motivo"), internal_id))
            if hot:
                row = db.execute("SELECT event_id,lead_id,received_at FROM events WHERE id=?", (internal_id,)).fetchone()
                self.enqueue(db, internal_id, "caliente", {
                    "tipo": "lead_caliente", "event_id": row["event_id"], "lead_id": row["lead_id"],
                    "motivo": result["motivo_ruta"], "propuesta": result["propuesta_agendamiento"],
                    "received_at": row["received_at"],
                })

    # Encola prioridad clínica antes de consultar servicios externos.
    # Comparte el tipo caliente con el aviso final para evitar un segundo aviso idéntico.
    def urgent_prealert(self, event, proposal):
        with self.connection(write=True) as db:
            self.enqueue(db, event["id"], "caliente", {"tipo": "lead_caliente",
                "event_id": event["event_id"], "lead_id": event["lead_id"],
                "motivo": "senales_de_urgencia_preclasificacion", "propuesta": proposal,
                "received_at": event["received_at"]})

    # Registra fallo, calcula pendiente/fallido y fija la siguiente ejecución.
    # Un aplazamiento local por rate limit no consume intento. También encola un
    # aviso de error con códigos seguros para que el fallo sea visible al operador.
    def fail_event(self, internal_id, component, code, max_attempts, delay, deferred=False):
        with self.connection(write=True) as db:
            row = db.execute("SELECT * FROM events WHERE id=?", (internal_id,)).fetchone()
            # deferred se usa para cuota local: posponer trabajo no debe agotar
            # los intentos reservados para errores reales de integración.
            attempts = row["attempts"] + (0 if deferred else 1)
            state = "fallido" if attempts >= max_attempts else "pendiente"
            db.execute("UPDATE events SET estado=?,motivo=?,attempts=?,next_attempt=?,lease_until=NULL WHERE id=?",
                       (state, code, attempts, time.time() + delay, internal_id))
            db.execute("INSERT INTO failures(event_fk,created_at,component,code,attempt) VALUES(?,?,?,?,?)",
                       (internal_id, time.time(), component, code, attempts))
            self.enqueue(db, internal_id, f"error:{component}:{code}:{state}", {
                "tipo": "error_procesamiento", "event_id": row["event_id"], "lead_id": row["lead_id"],
                "componente": component, "codigo": code, "estado": state,
            })

    # Reserva el próximo aviso listo para envío; los calientes tienen prioridad.
    # Su cola es independiente de la de clasificación y tiene su propia lease.
    def claim_notification(self, lease_seconds):
        now = time.time()
        with self.connection(write=True) as db:
            row = db.execute("""SELECT * FROM notifications WHERE status='pendiente' AND next_attempt<=?
                AND (lease_until IS NULL OR lease_until<?) ORDER BY
                CASE WHEN kind='caliente' THEN 0 ELSE 1 END, created_at LIMIT 1""", (now, now)).fetchone()
            if row:
                db.execute("UPDATE notifications SET lease_until=? WHERE id=?", (now + lease_seconds, row["id"]))
                return dict(row)

    # Marca el envío confirmado por respuesta HTTP exitosa.
    # Si hay resultado del lead caliente, este pasa a procesado; una prealerta sola
    # no finaliza un evento que todavía debe enriquecerse y clasificarse.
    def notification_sent(self, notification):
        with self.connection(write=True) as db:
            db.execute("UPDATE notifications SET status='enviada',sent_at=?,lease_until=NULL WHERE id=?",
                       (time.time(), notification["id"]))
            if notification["kind"] == "caliente":
                db.execute("UPDATE events SET estado='procesado',ruta='caliente',motivo=NULL WHERE id=? AND result IS NOT NULL",
                           (notification["event_fk"],))

    # Guarda fallo de entrega y decide reintento o agotamiento de intentos.
    # Un caliente cuya alerta final no se entrega queda fallido, aunque su clasificación
    # ya exista: así el contrato no informa éxito sin completar la acción requerida.
    def notification_failed(self, notification, code, max_attempts, delay):
        attempts = notification["attempts"] + 1
        state = "fallida" if attempts >= max_attempts else "pendiente"
        with self.connection(write=True) as db:
            db.execute("""UPDATE notifications SET status=?,attempts=?,next_attempt=?,lease_until=NULL,last_error=?
                          WHERE id=?""", (state, attempts, time.time() + delay, code, notification["id"]))
            db.execute("INSERT INTO failures(event_fk,created_at,component,code,attempt) VALUES(?,?,?,?,?)",
                       (notification["event_fk"], time.time(), "alertas", code, attempts))
            if notification["kind"] == "caliente" and state == "fallida":
                db.execute("UPDATE events SET estado='fallido',ruta=NULL,motivo=? WHERE id=? AND result IS NOT NULL",
                           ("alerta_caliente_no_entregada", notification["event_fk"]))

    # Reabre solo fallidos identificados por evento o recepción.
    # Restablece intentos de evento y avisos fallidos, conservando resultado previo
    # para no volver a pagar una clasificación cuando solo falló su notificación.
    def reprocess(self, event_id=None, receipt_id=None):
        with self.connection(write=True) as db:
            row = db.execute("SELECT * FROM events WHERE event_id=?", (event_id,)).fetchone() if event_id else db.execute(
                "SELECT e.* FROM events e JOIN receipts r ON e.id=r.event_fk WHERE r.id=?", (receipt_id,)).fetchone()
            if not row:
                return None
            if row["estado"] != "fallido":
                return False
            db.execute("UPDATE events SET estado='pendiente',attempts=0,next_attempt=0,lease_until=NULL,motivo=NULL WHERE id=?", (row["id"],))
            db.execute("UPDATE notifications SET status='pendiente',attempts=0,next_attempt=0,lease_until=NULL WHERE event_fk=? AND status='fallida'",
                       (row["id"],))
            return True

    # Recupera el evento principal de un lead, sus datos depurados y su resultado.
    # La API que expone este detalle exige autorización administrativa.
    def details(self, lead_id):
        with self.connection() as db:
            row = db.execute("SELECT * FROM events WHERE lead_id=? AND estado!='duplicado' ORDER BY received_at LIMIT 1", (lead_id,)).fetchone()
            if row:
                return {"estado": self.status(row), "normalizado": json.loads(row["normalized"]),
                        "resultado": json.loads(row["result"]) if row["result"] else None}

    # Resume estados, alertas, fallos y calientes con entrega posterior a cinco
    # minutos o aún sin entrega. Es información de operación, no un chequeo de red.
    def operations(self):
        with self.connection() as db:
            latest = db.execute("SELECT max(received_at) FROM receipts").fetchone()[0]
            return {
                "eventos": {r["estado"]: r["n"] for r in db.execute("SELECT estado,count(*) n FROM events GROUP BY estado")},
                "recepciones": db.execute("SELECT count(*) FROM receipts").fetchone()[0],
                "ultima_recepcion_utc": datetime.fromtimestamp(latest, timezone.utc).isoformat() if latest else None,
                "alertas": {r["status"]: r["n"] for r in db.execute("SELECT status,count(*) n FROM notifications GROUP BY status")},
                "calientes_fuera_sla": db.execute("""SELECT count(*) FROM notifications n JOIN events e ON e.id=n.event_fk
                    WHERE n.kind='caliente' AND (n.sent_at IS NULL OR n.sent_at>e.received_at+300)
                    AND e.received_at<?""", (time.time()-300,)).fetchone()[0],
                "fallos": [dict(r) for r in db.execute("SELECT created_at,component,code,attempt FROM failures ORDER BY id DESC LIMIT 30")],
            }

    # Lista hasta 200 recepciones recientes, con advertencias y filtro opcional.
    # COALESCE prioriza el estado propio del reenvío sobre el estado del evento canónico.
    def review(self, state=None):
        with self.connection() as db:
            rows = db.execute("""SELECT e.*,r.id receipt_id,r.received_at receipt_time,r.override_state,r.override_reason
                FROM receipts r JOIN events e ON e.id=r.event_fk
                WHERE (? IS NULL OR COALESCE(r.override_state,e.estado)=?) ORDER BY r.received_at DESC LIMIT 200""", (state,state)).fetchall()
            return [{"receipt_id": row["receipt_id"],
                     "received_at": datetime.fromtimestamp(row["receipt_time"], timezone.utc).isoformat(),
                     **self.status(row, row["override_state"], row["override_reason"]),
                     "advertencias": json.loads(row["normalized"])["warnings"]} for row in rows]

    # Devuelve el cuerpo recibido sin normalizar para revisión autorizada.
    # Conservarlo permite investigar rechazos sin copiar información sensible a logs.
    def receipt_detail(self, receipt_id):
        with self.connection() as db:
            row = db.execute("SELECT * FROM receipts WHERE id=?", (receipt_id,)).fetchone()
            if row:
                return {"receipt_id": receipt_id, "event_id": row["event_id"], "contenido_original": row["raw"]}

    # Aplica una ventana móvil de 30 solicitudes por 60 segundos por servicio.
    # Reserva el cupo en una transacción: devuelve 0 si se permite llamar ahora,
    # o segundos de espera si hay bloqueo/cuota agotada. Persiste tras reinicios.
    def rate_slot(self, service, limit=30, window=60, now=None):
        now = time.time() if now is None else now
        with self.connection(write=True) as db:
            db.execute("DELETE FROM rate_requests WHERE at<=?", (now-window,))
            blocked = db.execute("SELECT until FROM rate_blocks WHERE service=?", (service,)).fetchone()
            if blocked and blocked[0] > now:
                return blocked[0] - now + 0.05
            rows = db.execute("SELECT at FROM rate_requests WHERE service=? ORDER BY at", (service,)).fetchall()
            if len(rows) >= limit:
                return max(0.05, rows[0][0] + window - now + 0.05)
            db.execute("INSERT INTO rate_requests VALUES(?,?)", (service, now))
            return 0

    # Guarda la pausa indicada por el proveedor, conservando el bloqueo más largo.
    def block_rate(self, service, delay):
        with self.connection(write=True) as db:
            db.execute("INSERT INTO rate_blocks VALUES(?,?) ON CONFLICT(service) DO UPDATE SET until=max(until,excluded.until)",
                       (service, time.time() + delay))

    # Elimina entradas vencidas y devuelve una página cacheada si todavía es válida.
    def cached(self, key):
        with self.connection(write=True) as db:
            db.execute("DELETE FROM api_cache WHERE expires<?", (time.time(),))
            row = db.execute("SELECT body FROM api_cache WHERE key=?", (key,)).fetchone()
            return json.loads(row[0]) if row else None

    # Guarda una página validada durante cinco minutos para reutilizarla incluso
    # tras reiniciar y evitar repetir solicitudes de páginas anteriores en un reintento.
    def cache(self, key, value):
        with self.connection(write=True) as db:
            db.execute("INSERT OR REPLACE INTO api_cache VALUES(?,?,?)", (key, time.time()+300, dumps(value)))
