# Errores comunes a las integraciones y cálculo de esperas de reintento.
# ServiceError conserva componente, código seguro y política de recuperación.
# No se propagan cuerpos de respuestas externas hacia logs o alertas.
# retry_after interpreta la indicación de espera del proveedor, con un valor
# de respaldo cuando la cabecera no es válida.
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import math


# Error controlado con política de reintento y código seguro para auditoría.
class ServiceError(Exception):
    # retryable indica si merece otro intento; retry_after fija espera mínima.
    # deferred identifica una espera local por cupo que no debe consumir intentos.
    def __init__(self, component: str, code: str, *, retryable=True, retry_after=0.0, deferred=False):
        # Solo códigos controlados: jamás incluir cuerpos, URLs ni excepciones de terceros.
        super().__init__(code)
        self.component = component
        self.code = code
        self.retryable = retryable
        self.retry_after = retry_after
        self.deferred = deferred


# Acepta Retry-After como segundos o como fecha HTTP futura.
# Evita esperas no finitas y asegura al menos un segundo para valores interpretables.
def retry_after(headers, default=5.0) -> float:
    value = headers.get("Retry-After", "")
    try:
        seconds = float(value)
        return max(1.0, seconds) if math.isfinite(seconds) else default
    except ValueError:
        try:
            return max(1.0, (parsedate_to_datetime(value) - datetime.now(timezone.utc)).total_seconds())
        except (ValueError, TypeError, OverflowError):
            return default
