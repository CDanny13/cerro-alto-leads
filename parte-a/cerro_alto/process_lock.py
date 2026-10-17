# Exclusión de procesadores en el despliegue local de un solo worker.
# El bloqueo del sistema operativo impide dos Worker sobre el mismo SQLite.
# El archivo puede permanecer en disco: lo que excluye es el bloqueo activo,
# que se libera al cerrar el descriptor o terminar el proceso.
# Este guardián complementa las reservas temporales de trabajo en storage.py.
import os


# Guardián local: una base corresponde a un único procesador activo.
class ProcessLock:
    """Evita arrancar dos procesadores sobre el mismo SQLite local."""
    # Abre un archivo contiguo a SQLite y bloquea su primer byte en Windows
    # o el archivo en Unix. El bloqueo no espera: si ya existe otro, falla el arranque.
    def __init__(self, path):
        self.handle = open(path + ".worker.lock", "a+b")
        self.handle.seek(0, os.SEEK_END)
        if self.handle.tell() == 0:
            self.handle.write(b"0")
            self.handle.flush()
        # Windows bloquea desde la posición actual del archivo; se vuelve al byte
        # cero para que todos los procesos intenten bloquear el mismo lugar.
        self.handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.handle.close()
            raise RuntimeError("Ya existe un procesador para esta base. Usa un único worker de Uvicorn.") from None

    # Cierra el descriptor para liberar el bloqueo del sistema operativo.
    def close(self):
        self.handle.close()
