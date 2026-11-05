"""Dónde está el sitio que se vigila.

La dirección NO vive en este repositorio, que es público: llega en la variable
de entorno `EVACA_DESTINO_BASE`, que en GitHub Actions se rellena con el
secreto `DESTINO_BASE`. Sin ella no hay a dónde pedir ni qué enlazar, y el
monitor lo dice con un mensaje que se entiende en vez de intentar una
dirección relativa (`/sapi/...`, que `requests` rechaza con un error opaco).

La variable lleva la raíz, sin barra final: `https://…`. En local se pone en la
sesión de PowerShell antes de usar el monitor:

    $env:EVACA_DESTINO_BASE = "https://..."

Ningún fichero del repositorio debe contenerla: es lo único que separa este
código de "saber qué sitio lee". Por eso también el enlace del aviso se
construye aquí y no con una constante.
"""

from __future__ import annotations

import os

# Variable de entorno que la trae; el secreto de GitHub se llama como `SECRETO`.
VARIABLE = "EVACA_DESTINO_BASE"
SECRETO = "DESTINO_BASE"


def base() -> str:
    """La raíz del sitio (sin barra final), o "" si no está configurada."""
    return os.environ.get(VARIABLE, "").strip().rstrip("/")


def configurada() -> bool:
    """¿Sabemos ya a qué sitio pedir?"""
    return bool(base())


def perfil(slug: str) -> str:
    """La URL pública de la cartera de `slug`, o "" si no hay destino.

    Devolver "" (en vez de una URL a medias) permite que los avisos salgan sin
    enlace cuando el destino no está configurado, en lugar de con uno roto.
    """
    raiz = base()
    if not raiz:
        return ""
    return f"{raiz}/people/{slug}/portfolio"


def ayuda() -> str:
    """El texto que se enseña cuando falta, en los errores y en la documentación."""
    return (
        f"Falta el destino del monitor: define el secreto {SECRETO} en GitHub "
        f"(Settings -> Secrets and variables -> Actions -> New repository "
        f"secret) o, en local, la variable de entorno {VARIABLE}. Su valor es "
        f"la raíz del sitio, sin barra final."
    )
