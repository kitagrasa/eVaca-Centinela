"""Tests del HTML que importa Portfolio Performance.

Portados de eVaca-PP. Lo único que cambia por diseño es el nombre: el título y
el `<h1>` llevan el id opaco de la serie, no el del trader.
"""

from __future__ import annotations

import re
from datetime import date

from evaca_monitor.serie import (
    KIND_ANCHOR,
    KIND_BASE,
    KIND_CALIBRATED,
    KIND_DAY,
    KIND_FILL,
    KIND_MONTH,
    PriceRow,
)
from evaca_monitor.serie_html import render

# Un id con la forma real (12 hex); no es el de nadie.
ID = "0f1e2d3c4b5a"


def filas_de_ejemplo() -> list[PriceRow]:
    """Tres filas desordenadas a propósito."""
    return [
        PriceRow(date(2025, 5, 31), 111.453, KIND_MONTH),
        PriceRow(date(2025, 3, 31), 100.0, KIND_BASE),
        PriceRow(date(2025, 4, 30), 114.9, KIND_MONTH),
    ]


def lineas_de_datos(html: str) -> list[str]:
    """Filas de datos del HTML, sin la cabecera."""
    return [
        linea
        for linea in html.splitlines()
        if linea.strip().startswith("<tr data-kind=")
    ]


def fecha_de(linea: str) -> str:
    """Fecha ISO de una fila de datos."""
    coincidencia = re.search(r"<td>\s*(\d{4}-\d{2}-\d{2})", linea)
    assert coincidencia is not None
    return coincidencia.group(1)


def rentab_de(linea: str) -> str | None:
    """Valor de rentab de una fila, o None si no lo lleva."""
    coincidencia = re.search(r'rentab=\s+"([^"]*)"', linea)
    return coincidencia.group(1) if coincidencia else None


def test_una_sola_tabla() -> None:
    html = render(ID, filas_de_ejemplo())

    assert html.count("<table>") == 1
    assert html.count("</table>") == 1


def test_cabeceras_date_y_close() -> None:
    html = render(ID, filas_de_ejemplo())

    assert "<th>Date</th><th>Close</th>" in html


def test_idioma_ingles_para_portfolio_performance() -> None:
    html = render(ID, filas_de_ejemplo())

    assert 'lang="en"' in html
    assert "Fecha" not in html
    assert "Cotización" not in html


def test_fechas_iso_y_decimal_con_punto() -> None:
    html = render(ID, filas_de_ejemplo())

    assert "<td>   2025-03-31   </td><td>   100.0000   </td>" in html
    assert "<td>   2025-04-30   </td><td>   114.9000   </td>" in html
    assert "114,9" not in html


def test_filas_en_orden_descendente() -> None:
    html = render(ID, filas_de_ejemplo())

    posicion_mayo = html.index("2025-05-31")
    posicion_abril = html.index("2025-04-30")
    posicion_marzo = html.index("2025-03-31")

    assert posicion_mayo < posicion_abril < posicion_marzo


def test_marca_el_tipo_de_cada_fila() -> None:
    filas = [
        PriceRow(date(2025, 3, 31), 100.0, KIND_BASE),
        PriceRow(date(2025, 4, 1), 100.0, KIND_ANCHOR),
        PriceRow(date(2025, 4, 2), 100.1, KIND_FILL),
        PriceRow(date(2025, 4, 30), 110.0, KIND_MONTH),
        PriceRow(date(2025, 5, 2), 111.0, KIND_DAY),
    ]

    html = render(ID, filas)

    assert '<tr data-kind=   "A"   ><td>   2025-03-31   </td>' in html
    assert '<tr data-kind=   "A"   ><td>   2025-04-01   </td>' in html
    assert '<tr data-kind=   "A"   ><td>   2025-04-02   </td>' in html
    assert '<tr data-kind=   "M"   ><td>   2025-04-30   </td>' in html
    assert '<tr data-kind=   "D"   ><td>   2025-05-02   </td>' in html


def test_la_marca_es_un_atributo_y_no_una_columna_mas() -> None:
    html = render(ID, filas_de_ejemplo())

    filas = [
        linea
        for linea in html.splitlines()
        if linea.strip().startswith("<tr data-kind=")
    ]

    assert len(filas) == 3
    assert all(linea.count("<td>") == 2 for linea in filas)
    assert all(linea.count("</td>") == 2 for linea in filas)


def test_rentab_solo_en_las_filas_m_y_d() -> None:
    filas = [
        PriceRow(date(2025, 3, 31), 100.0, KIND_BASE),
        PriceRow(date(2025, 4, 1), 100.0, KIND_ANCHOR, gain=99.0),
        PriceRow(date(2025, 4, 2), 100.1, KIND_FILL),
        PriceRow(date(2025, 4, 30), 110.0, KIND_MONTH, gain=14.9),
        PriceRow(date(2025, 5, 2), 111.0, KIND_DAY, gain=0.76),
    ]

    html = render(ID, filas)
    por_fecha = {fecha_de(linea): linea for linea in lineas_de_datos(html)}

    assert rentab_de(por_fecha["2025-04-30"]) == "14.90"
    assert rentab_de(por_fecha["2025-05-02"]) == "0.76"
    assert rentab_de(por_fecha["2025-03-31"]) is None
    assert rentab_de(por_fecha["2025-04-01"]) is None
    assert rentab_de(por_fecha["2025-04-02"]) is None
    assert html.count("rentab=") == 2


def test_rentab_dos_decimales_sin_signo_porcentaje() -> None:
    filas = [
        PriceRow(date(2025, 4, 30), 110.0, KIND_MONTH, gain=-3.0),
        PriceRow(date(2025, 5, 2), 111.0, KIND_DAY, gain=0.0),
    ]

    html = render(ID, filas)
    por_fecha = {fecha_de(linea): linea for linea in lineas_de_datos(html)}

    assert rentab_de(por_fecha["2025-04-30"]) == "-3.00"
    assert rentab_de(por_fecha["2025-05-02"]) == "0.00"
    assert all("%" not in (rentab_de(linea) or "") for linea in por_fecha.values())


def test_columnas_alineadas_con_rentabilidades_de_ancho_distinto() -> None:
    filas = [
        PriceRow(date(2026, 11, 3), 327.1183, KIND_DAY, gain=-3.97),
        PriceRow(date(2026, 11, 2), 340.6418, KIND_DAY, gain=0.0),
        PriceRow(date(2026, 11, 1), 340.6418, KIND_ANCHOR),
        PriceRow(date(2026, 10, 31), 340.6418, KIND_MONTH, gain=-9.48),
    ]

    html = render(ID, filas)
    lineas = lineas_de_datos(html)

    assert len(lineas) == 4
    # La fecha y el inicio del cierre empiezan en la misma columna.
    assert len({linea.index("2026-") for linea in lineas}) == 1
    assert len({linea.index("<td>", linea.index("</td>")) for linea in lineas}) == 1
    # Todos los cierres tienen 8 caracteres, así que las líneas miden igual.
    assert len({len(linea) for linea in lineas}) == 1


def test_cierre_de_distinto_ancho_no_desalinea_la_fila() -> None:
    filas = [
        PriceRow(date(2026, 11, 3), 1000.1234, KIND_DAY, gain=1.0),
        PriceRow(date(2026, 11, 2), 99.5, KIND_DAY, gain=0.0),
        PriceRow(date(2026, 11, 1), 99.5, KIND_ANCHOR),
    ]

    lineas = lineas_de_datos(render("x", filas))

    assert len({len(linea) for linea in lineas}) == 1
    assert len({linea.index("<td>", linea.index("</td>")) for linea in lineas}) == 1


def test_escapa_el_identificador() -> None:
    html = render("a<b&c", filas_de_ejemplo())

    assert "a&lt;b&amp;c" in html
    assert "<title>eVaca - a<b" not in html


def test_sin_javascript_ni_css() -> None:
    html = render(ID, filas_de_ejemplo())

    assert "<script" not in html
    assert "<style" not in html


def test_acaba_en_salto_de_linea() -> None:
    assert render(ID, filas_de_ejemplo()).endswith("</html>\n")


# ----------------------------------------------------------------------
# Identidad opaca: el HTML se publica
# ----------------------------------------------------------------------
def test_el_titulo_y_el_h1_llevan_el_id_y_no_un_nombre() -> None:
    html = render(ID, filas_de_ejemplo())

    assert f"<title>eVaca - {ID}</title>" in html
    assert f"<h1>eVaca - {ID}</h1>" in html
    # Y nada de "eVaca - " con otra cosa que no sea el id entre medias.
    assert html.count(f"eVaca - {ID}") == 2


def test_cada_fila_tiene_dos_celdas_y_su_marca_m_d_o_a() -> None:
    filas = [
        PriceRow(date(2025, 4, 30), 110.0, KIND_MONTH, gain=10.0),
        PriceRow(date(2025, 5, 2), 111.0, KIND_DAY, gain=0.9),
        PriceRow(date(2025, 5, 1), 110.0, KIND_FILL),
    ]

    lineas = lineas_de_datos(render(ID, filas))

    assert len(lineas) == 3
    assert all(linea.count("<td>") == 2 for linea in lineas)
    marcas = [re.search(r'data-kind=\s+"([MDA])"', linea).group(1) for linea in lineas]
    assert set(marcas) == {"M", "D", "A"}


# ----------------------------------------------------------------------
# Meses calibrados y leyenda
# ----------------------------------------------------------------------
def test_marca_los_dias_calibrados_con_c_y_su_rentab() -> None:
    filas = [
        PriceRow(date(2025, 4, 30), 104.5, KIND_MONTH, gain=4.5),
        PriceRow(date(2025, 4, 10), 102.2252, KIND_CALIBRATED, gain=2.23),
        PriceRow(date(2025, 4, 1), 100.0, KIND_CALIBRATED, gain=0.0),
    ]

    html = render(ID, filas)
    por_fecha = {fecha_de(linea): linea for linea in lineas_de_datos(html)}

    assert 'data-kind=   "C"' in por_fecha["2025-04-10"]
    assert rentab_de(por_fecha["2025-04-10"]) == "2.23"
    assert rentab_de(por_fecha["2025-04-01"]) == "0.00"
    assert rentab_de(por_fecha["2025-04-30"]) == "4.50"


def test_leyenda_de_la_cabecera_antes_de_la_tabla() -> None:
    html = render(ID, filas_de_ejemplo())

    leyenda = (
        "M = Mes stats · D = Diario chart sin calibrar · "
        "C = diario chart Calibrado · A = relleno Automático"
    )
    assert f"<p>{leyenda}</p>" in html
    assert html.index(leyenda) < html.index("<table>")
    # La leyenda está fuera de la tabla: PP solo importa las celdas.
    assert html.count("<td>") == 3 * 2


def test_la_leyenda_no_añade_columnas() -> None:
    html = render(ID, filas_de_ejemplo())

    tabla = html[html.index("<table>") : html.index("</table>")]
    assert "M = Mes stats" not in tabla
    assert tabla.count("<th>") == 2
    assert tabla.count("<td>") == 6
