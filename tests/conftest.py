"""Configuración común de los tests.

El monitor no lleva la dirección del sitio en el código (el repositorio es
público): la lee de `EVACA_DESTINO_BASE`, que en GitHub se rellena con el
secreto `DESTINO_BASE`. Aquí se pone una de mentira para que los tests no
dependan de la configuración de la máquina ni de la red.

Los tests que necesitan comprobar qué pasa SIN destino quitan la variable con
`monkeypatch.delenv("EVACA_DESTINO_BASE")` dentro del propio test.
"""

from __future__ import annotations

import pytest

DESTINO_DE_PRUEBA = "https://destino.example"


@pytest.fixture(autouse=True)
def destino_de_prueba(monkeypatch):
    """Todos los tests ven un destino válido, salvo los que lo quitan."""
    monkeypatch.setenv("EVACA_DESTINO_BASE", DESTINO_DE_PRUEBA)
