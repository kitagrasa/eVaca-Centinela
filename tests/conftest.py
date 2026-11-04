"""Configuración común de los tests.

El monitor no lleva la dirección del sitio en el código (el repositorio es
público): la lee de `EVACA_DESTINO_BASE`, que en GitHub se rellena con el
secreto `DESTINO_BASE`. Aquí se pone una de mentira para que los tests no
dependan de la configuración de la máquina ni de la red.

Los tests que necesitan comprobar qué pasa SIN destino quitan la variable con
`monkeypatch.delenv("EVACA_DESTINO_BASE")` dentro del propio test.

También viven aquí el instante fijo y los payloads de ejemplo de las series:
los mismos que usaba eVaca-PP, para que las reglas de cálculo se prueben con
las mismas respuestas.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

DESTINO_DE_PRUEBA = "https://destino.example"

# Instante fijo de referencia: 2 de noviembre de 2026, 00:30 UTC.
HOY = date(2026, 11, 2)
AHORA = datetime(2026, 11, 2, 0, 30, tzinfo=UTC)

CARPETA_FIXTURES = Path(__file__).resolve().parent / "fixtures"


@pytest.fixture(autouse=True)
def destino_de_prueba(monkeypatch):
    """Todos los tests ven un destino válido, salvo los que lo quitan."""
    monkeypatch.setenv("EVACA_DESTINO_BASE", DESTINO_DE_PRUEBA)


@pytest.fixture
def hoy() -> date:
    """Fecha fija de trabajo para las series."""
    return HOY


@pytest.fixture
def ahora() -> datetime:
    """Instante fijo de trabajo."""
    return AHORA


def cargar_fixture(nombre: str) -> dict:
    """Carga un JSON de tests/fixtures."""
    return json.loads((CARPETA_FIXTURES / f"{nombre}.json").read_text(encoding="utf-8"))


@pytest.fixture
def payload_simple() -> dict:
    """Cuatro meses: uno parcial (alta) y tres completos."""
    return cargar_fixture("payload_simple")


@pytest.fixture
def payload_no_data() -> dict:
    """Perfil inexistente: customerId -1 y sin meses."""
    return cargar_fixture("payload_no_data")


@pytest.fixture
def payload_simulation() -> dict:
    """Meses marcados como simulación que deben descartarse."""
    return cargar_fixture("payload_simulation")
