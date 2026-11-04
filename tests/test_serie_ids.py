"""Tests de la identidad opaca y del mapa privado de ids.

Aquí se comprueba lo que hace privado el repositorio del código: los ids son
estables, no se pueden leer al revés, el mapa acumula filas (con los traders
retirados marcados) y nunca se escribe dentro del repositorio público.
"""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path

from evaca_monitor import serie_store
from evaca_monitor.config import Config, SeriesConfig
from evaca_monitor.serie import serie_id
from evaca_monitor.serie_run import (
    RAIZ_CODIGO,
    RunConfig,
    SerieRun,
    en_github_actions,
)

OWNER = "duenio-de-prueba"
ID_UNO = serie_id("uno")
REGISTRO = logging.getLogger("pruebas_evaca_ids")
REGISTRO.addHandler(logging.NullHandler())


def filas_del_texto(texto: str) -> list[list[str]]:
    """Celdas de las filas de datos de un Markdown con tabla."""
    celdas: list[list[str]] = []
    for linea in texto.splitlines():
        if not linea.startswith("|"):
            continue
        partes = [celda.strip() for celda in linea.strip("|").split("|")]
        if len(partes) < 3 or partes[0].lower() == "id":
            continue
        if set(partes[0]) <= {"-", ":", " "}:
            continue
        celdas.append(partes)
    return celdas


# ---------------------------------------------------------------------- #
# serie_id
# ---------------------------------------------------------------------- #
def test_el_id_se_calcula_con_la_formula_acordada() -> None:
    esperado = hashlib.sha256(b"evaca:usuario.ejemplo").hexdigest()[:12]

    assert serie_id("Usuario.Ejemplo") == esperado


def test_el_id_es_estable_sin_espacios_ni_mayusculas() -> None:
    assert serie_id("  Usuario.Ejemplo  ") == serie_id("usuario.ejemplo")
    assert serie_id("uno") == serie_id("UNO")


def test_el_id_es_opaco() -> None:
    identificador = serie_id("un_nombre_larguisimo")

    assert len(identificador) == 12
    assert int(identificador, 16) >= 0  # solo dígitos hexadecimales
    assert "nombre" not in identificador
    assert identificador != serie_id("otro_nombre")


# ---------------------------------------------------------------------- #
# La URL del feed
# ---------------------------------------------------------------------- #
def test_la_url_del_feed_usa_la_plantilla_y_el_id() -> None:
    url = serie_store.feed_url(ID_UNO, OWNER)

    assert url == (
        "https://raw.githubusercontent.com/"
        f"{OWNER}/eVaca-Centinela/main/data/prices/{ID_UNO}"
    )


def test_la_url_del_feed_no_lleva_el_nombre() -> None:
    url = serie_store.feed_url(serie_id("TraderSecreto"), OWNER)

    assert "tradersecreto" not in url.lower()
    assert "watchlist" not in url


def test_el_duenio_sale_del_entorno_o_del_repositorio(monkeypatch) -> None:
    monkeypatch.delenv("EVACA_FEED_OWNER", raising=False)
    monkeypatch.setenv("GITHUB_REPOSITORY", "alguien/eVaca-Centinela")
    assert serie_store.feed_owner() == "alguien"

    monkeypatch.setenv("EVACA_FEED_OWNER", "otro-duenio")
    assert serie_store.feed_owner() == "otro-duenio"


# ---------------------------------------------------------------------- #
# El mapa: tres columnas, acumulativo y con retirados
# ---------------------------------------------------------------------- #
def test_el_mapa_tiene_tres_columnas() -> None:
    filas = [serie_store.IdRow(ID_UNO, "uno")]

    texto = serie_store.render_ids_map(filas, OWNER)
    celdas = filas_del_texto(texto)

    assert len(celdas) == 1
    assert celdas[0] == [ID_UNO, "uno", serie_store.feed_url(ID_UNO, OWNER)]


def test_el_mapa_es_acumulativo_y_marca_los_retirados() -> None:
    id_viejo = serie_id("viejo")
    previas = [serie_store.IdRow(id_viejo, "viejo")]

    filas = serie_store.merge_ids(previas, ["uno"])

    assert [fila.id for fila in filas] == [id_viejo, ID_UNO]
    assert filas[0].retirado is True
    assert filas[0].usuario == "viejo"
    assert filas[1].retirado is False

    texto = serie_store.render_ids_map(filas, OWNER)
    assert "viejo (retirado)" in texto
    assert "uno (retirado)" not in texto


def test_si_alguien_vuelve_deja_de_estar_retirado() -> None:
    id_viejo = serie_id("viejo")
    previas = [serie_store.IdRow(id_viejo, "viejo", retirado=True)]

    filas = serie_store.merge_ids(previas, ["viejo"])

    assert len(filas) == 1
    assert filas[0].retirado is False


def test_las_personas_nuevas_se_anaden_al_final_y_sin_duplicados() -> None:
    previas = [serie_store.IdRow(ID_UNO, "uno")]

    filas = serie_store.merge_ids(previas, ["dos", "uno", "Dos"])

    assert [fila.usuario for fila in filas] == ["uno", "dos"]


def test_el_mapa_se_guarda_y_se_vuelve_a_leer(tmp_path: Path) -> None:
    ruta = tmp_path / serie_store.IDS_FILENAME
    filas = [
        serie_store.IdRow(ID_UNO, "uno"),
        serie_store.IdRow(serie_id("viejo"), "viejo", retirado=True),
    ]

    assert serie_store.write_ids_map(ruta, filas, OWNER) is True
    assert serie_store.write_ids_map(ruta, filas, OWNER) is False  # sin cambios
    assert serie_store.load_ids_map(ruta) == filas


def test_sin_mapa_anterior_no_hay_filas(tmp_path: Path) -> None:
    assert serie_store.load_ids_map(tmp_path / "no-existe.md") == []
    assert serie_store.parse_ids_map("") == []
    assert serie_store.parse_ids_map("# solo comentarios\n") == []


# ---------------------------------------------------------------------- #
# --ids: no toca la red y no escribe donde no debe
# ---------------------------------------------------------------------- #
def _run(tmp_path: Path, **opciones) -> SerieRun:
    config = Config(
        users=opciones.pop("usuarios", ["uno"]),
        series=SeriesConfig(),
        users_source=tmp_path / "watchlist.md",
    )
    return SerieRun(
        config,
        RunConfig(ids_path=opciones.pop("ids_path", tmp_path / serie_store.IDS_FILENAME), **opciones),
        None,
        logger=REGISTRO,
    )


def test_ids_escribe_el_mapa_sin_pedir_nada(tmp_path: Path) -> None:
    run = _run(tmp_path)

    assert run.run_ids(imprimir=False) == 0

    filas = serie_store.load_ids_map(tmp_path / serie_store.IDS_FILENAME)
    assert [fila.id for fila in filas] == [ID_UNO]


def test_ids_no_escribe_en_dry_run(tmp_path: Path) -> None:
    run = _run(tmp_path, dry_run=True)

    assert run.run_ids(imprimir=False) == 0
    assert not (tmp_path / serie_store.IDS_FILENAME).exists()


def test_ids_sin_lista_no_escribe_el_mapa(tmp_path: Path) -> None:
    run = _run(tmp_path, usuarios=[])

    assert run.run_ids(imprimir=False) == 1
    assert not (tmp_path / serie_store.IDS_FILENAME).exists()


def test_el_mapa_no_se_escribe_dentro_del_repositorio_publico(tmp_path: Path) -> None:
    """El mapa lleva nombres: escribirlo en el repo público sería una fuga."""
    ruta = RAIZ_CODIGO / "_mapa_que_no_debe_crearse.md"
    assert not ruta.exists()
    run = _run(tmp_path, ids_path=ruta)
    try:
        assert run.run_ids(imprimir=False) == 1
    finally:
        assert not ruta.exists()


def test_en_github_actions_lee_la_variable(monkeypatch) -> None:
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    assert en_github_actions() is False

    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    assert en_github_actions() is True

    monkeypatch.setenv("GITHUB_ACTIONS", "false")
    assert en_github_actions() is False
