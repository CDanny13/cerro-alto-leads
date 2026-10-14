# Sistema de leads · Clínica Dental Cerro Alto

Repositorio base de la **Parte A** de la prueba técnica de INVERS. Haz un **fork**, trabaja en tu fork y pega su enlace en la página de la prueba.

> Clínica ficticia, creada para la prueba técnica de INVERS.

## Estructura

```
README.md        # reemplázalo por el tuyo: cómo replicar tu sistema paso a paso
.env.example     # las variables que necesita tu sistema, sin valores reales
payloads/        # los 10 eventos de ejemplo del conector
enviar_payloads.py  # los envía a tu webhook: python3 enviar_payloads.py <URL>
parte-a/         # tu sistema: workflows exportados o código, prompts y tests
```

El brief de la Parte A, en la página de la prueba, explica qué construir y cómo lo evaluamos.

## Antes de hacer push

Tu fork es **público**. Nunca subas credenciales (tokens, API keys, contraseñas): usa un archivo `.env`, que ya está en `.gitignore`. Si se te escapa una, revócala de inmediato y anótalo en `DECISIONES`.
