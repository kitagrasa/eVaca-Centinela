"""Tests del destino: el sitio no está en el código, llega por el entorno.

Lo que se comprueba aquí es la propiedad que permite que el repositorio sea
público: sin `EVACA_DESTINO_BASE` no se pide ni se enlaza nada, y con ella el
enlace se construye bien (sin barras duplicadas y apuntando al perfil).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from evaca_monitor import destino  # noqa: E402
from evaca_monitor.client import DestinoNoConfigurado, EvacaClient  # noqa: E402
from evaca_monitor.models import EvacaUser, Snapshot  # noqa: E402
from evaca_monitor.render import render_baseline, render_user_changes  # noqa: E402

USER = EvacaUser(username="UsuarioEjemplo", gcid=1111111, real_cid=2222222)


@pytest.fixture
def sin_destino(monkeypatch):
    monkeypatch.delenv(destino.VARIABLE, raising=False)


@pytest.fixture
def con_destino(monkeypatch):
    # Con barra final a propósito: no debe acabar en "example//people".
    monkeypatch.setenv(destino.VARIABLE, "https://otro.example/")


def test_la_variable_y_el_secreto_son_los_documentados():
    """El README manda configurar estos nombres; si cambian, cambian los dos."""
    assert destino.VARIABLE == "EVACA_DESTINO_BASE"
    assert destino.SECRETO == "DESTINO_BASE"


def test_sin_destino_no_hay_url(sin_destino):
    assert destino.base() == ""
    assert destino.configurada() is False
    assert destino.perfil("usuarioejemplo") == ""
    assert USER.portfolio_url == ""


def test_con_destino_la_url_se_construye_una_sola_vez(con_destino):
    assert destino.base() == "https://otro.example"
    assert USER.portfolio_url == (
        "https://otro.example/people/usuarioejemplo/portfolio"
    )


def test_sin_destino_el_aviso_sale_sin_enlace(sin_destino):
    """Mejor sin enlace que con uno roto: el mensaje sigue siendo útil."""
    texto = render_user_changes(
        USER, [], instrument_labels={}, timestamp="01/01/2026 00:00"
    )
    assert "Ver cartera pública" not in texto
    assert "href=" not in texto

    baseline = render_baseline(
        USER,
        Snapshot(user=USER, assets={}),
        instrument_labels={},
        timestamp="01/01/2026 00:00",
    )
    assert "Ver cartera pública" not in baseline


def test_con_destino_el_aviso_lleva_el_enlace(con_destino):
    texto = render_user_changes(
        USER, [], instrument_labels={}, timestamp="01/01/2026 00:00"
    )
    assert (
        'href="https://otro.example/people/usuarioejemplo/portfolio"' in texto
    )


def test_sin_destino_el_cliente_lo_dice_claro(sin_destino):
    """No es un fallo de red: es configuración, y el mensaje lo explica."""
    cliente = EvacaClient(delay=0.0)
    with pytest.raises(DestinoNoConfigurado) as exc:
        cliente.get_portfolio(123)
    assert "DESTINO_BASE" in str(exc.value)


def test_el_cliente_puede_recibir_el_destino_a_mano():
    """Los tests y los diagnósticos pueden fijarlo sin tocar el entorno."""
    cliente = EvacaClient(delay=0.0, base="https://otro.example/")
    assert cliente.base == "https://otro.example"


def test_el_log_no_deja_el_host_suelto():
    """`requests` mete el host en sus excepciones y GitHub no lo enmascara.

    Solo enmascara el valor exacto del secreto (la URL), así que `host='…'`
    se colaría en el log público. `_limpiar` tapa las dos formas.
    """
    cliente = EvacaClient(delay=0.0, base="https://otro.example")
    assert cliente._limpiar("GET https://otro.example/sapi/x") == (
        "GET (destino)/sapi/x"
    )
    sucio = "Max retries exceeded (host='otro.example', port=443)"
    assert "otro.example" not in cliente._limpiar(sucio)
    assert "(destino)" in cliente._limpiar(sucio)
