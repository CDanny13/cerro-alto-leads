# Cerro Alto · instrucciones de trabajo

- Python >=3.11. El código de la solución, prompts y pruebas vive en `parte-a/`.
- `parte-a/Contexto/ficha-negocio.md` es la fuente de verdad comercial y clínica.
- Conservar los documentos, `payloads/` y `enviar_payloads.py` originales.
- Persistir recepciones antes del ACK; no ejecutar servicios externos en el webhook.
- No deduplicar personas solamente por un teléfono o correo compartido.
- Toda integración de producción es real y configurable. Mocks únicamente en tests.
- No registrar mensajes, historiales, contactos, tokens ni cuerpos de errores externos.
- Probar cada etapa con `python -m pytest`; documentar pruebas reales por separado.
- No hacer push ni entregar la prueba sin aprobación del usuario.
- Mantener README, `.env.example`, DECISIONES y AI_WORKFLOW coherentes con el código.
