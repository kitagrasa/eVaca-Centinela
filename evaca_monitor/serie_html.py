"""Generación del HTML que Portfolio Performance importa.

Una sola tabla, cabeceras `Date` y `Close` en inglés, fechas ISO y decimal con
punto. Las marcas de tipo y la rentabilidad van como atributos del ``<tr>``,
que Portfolio Performance ignora; los espacios decorativos son seguros porque
PP recorta el texto de las celdas antes de convertirlo.

El título y el ``<h1>`` llevan el id opaco de la serie, nunca el nombre del
trader: este fichero se publica en el repositorio del código.
"""

from __future__ import annotations

import html
from collections.abc import Iterable

from .serie import (
    KIND_ANCHOR,
    KIND_BASE,
    KIND_CALIBRATED,
    KIND_DAY,
    KIND_FILL,
    KIND_MONTH,
    PriceRow,
)

# Marca de cada fila para poder distinguir en el HTML qué es dato original y
# qué es relleno. Va como atributo del <tr>, nunca como una tercera columna:
# Portfolio Performance solo importa las celdas <td> y una columna extra haría
# que descartara la fila entera.
#   M = cierre mensual de la rentabilidad de stats (también en meses calibrados)
#   D = día del chart sin calibrar (mes en curso o mes que ya coincidía con stats)
#   C = día de un mes cerrado realmente calibrado con el cierre de stats
#   A = base o relleno (generado por el programa; las anclas del día 1 solo
#       aparecen en ficheros antiguos)
KIND_MARKERS = {
    KIND_MONTH: "M",
    KIND_DAY: "D",
    KIND_CALIBRATED: "C",
    KIND_BASE: "A",
    KIND_ANCHOR: "A",
    KIND_FILL: "A",
}

# Leyenda de la cabecera. PP ignora todo lo que no sea la tabla, así que puede
# ir como párrafo antes de ella.
LEYENDA = (
    "M = Mes stats · D = Diario chart sin calibrar · "
    "A = Automático · C = Calibrado"
)

# Ancho mínimo del valor de rentab entre comillas. Los valores habituales
# (-3.97, 0.00, -9.48...) caben aquí; si alguno es más largo, el ancho se
# amplía para todas las filas y la alineación se mantiene.
RENTAB_FIELD_MIN = 10


def render(serie: str, rows: Iterable[PriceRow]) -> str:
    """Devuelve el HTML completo de la serie, de la más reciente a la más antigua.

    `serie` es el id opaco (`serie_id`), nunca el nombre del trader. Recibe
    las filas ya preparadas para PP: la serie real más la rejilla diaria de
    los meses cerrados (`serie.daily_rows`).

    El orden descendente es solo de presentación: PP vuelve a ordenar las
    filas por fecha antes de importarlas, así que no cambia nada en el valor.

    Las filas M, C y D llevan además `rentab` con la rentabilidad calculada
    del cierre anterior en % (dos decimales, sin el signo %); las A no lo
    llevan. Las columnas se rellenan con espacios para que todos los datos
    empiecen en la misma posición vertical; PP recorta esos espacios al
    importar.
    """
    nombre = html.escape(serie)
    filas = sorted(rows, key=lambda fila: fila.date, reverse=True)
    ancho_rentab, hueco_sin_rentab, ancho_cierre = _alignment(filas)

    cuerpo = "\n".join(
        _render_row(fila, ancho_rentab, hueco_sin_rentab, ancho_cierre)
        for fila in filas
    )

    return (
        "<!DOCTYPE html>\n"
        '<html lang="en">\n'
        "<head>\n"
        '  <meta charset="utf-8">\n'
        f"  <title>eVaca - {nombre}</title>\n"
        "</head>\n"
        "<body>\n"
        f"  <h1>eVaca - {nombre}</h1>\n"
        f"  <p>{LEYENDA}</p>\n"
        "  <table>\n"
        "    <thead>\n"
        "      <tr><th>Date</th><th>Close</th></tr>\n"
        "    </thead>\n"
        "    <tbody>\n"
        f"{cuerpo}\n"
        "    </tbody>\n"
        "  </table>\n"
        "</body>\n"
        "</html>\n"
    )


def _render_row(
    fila: PriceRow, ancho_rentab: int, hueco_sin_rentab: int, ancho_cierre: int
) -> str:
    """Una fila del HTML con las columnas alineadas."""
    marca = KIND_MARKERS.get(fila.kind, "?")
    cotizacion = _quoted_gain(fila)
    if cotizacion is not None:
        atributos = (
            f'data-kind=   "{marca}"   rentab={cotizacion:>{ancho_rentab}}'
        )
        relleno_fecha = "   "
    else:
        atributos = f'data-kind=   "{marca}"'
        # Las filas sin rentab compensan dentro de la celda de fecha el hueco
        # que ocupa el atributo en las demás, para que la fecha y el cierre
        # empiecen en la misma columna.
        relleno_fecha = " " * (3 + hueco_sin_rentab)
    cierre = f"{fila.close:.4f}".ljust(ancho_cierre)
    return (
        f"      <tr {atributos}   >"
        f"<td>{relleno_fecha}{fila.date.isoformat()}   </td>"
        f"<td>   {cierre}   </td></tr>"
    )


def _quoted_gain(fila: PriceRow) -> str | None:
    """Rentabilidad entre comillas si la fila es M, C o D y tiene dato."""
    if (
        fila.kind in (KIND_MONTH, KIND_CALIBRATED, KIND_DAY)
        and fila.gain is not None
    ):
        return f'"{_format_gain(fila.gain)}"'
    return None


def _alignment(filas: list[PriceRow]) -> tuple[int, int, int]:
    """Anchos fijos para alinear rentab y cierre.

    Devuelve (ancho_rentab, hueco_sin_rentab, ancho_cierre). El primero es el
    de la rentabilidad entre comillas alineada a la derecha; el segundo es lo
    que ocupa el atributo completo (``   rentab=`` más el ancho) y que hay que
    compensar en las filas A; el tercero es el ancho del cierre para que todas
    las filas terminen en la misma columna.
    """
    cotizaciones: list[str] = []
    for fila in filas:
        cotizacion = _quoted_gain(fila)
        if cotizacion is not None:
            cotizaciones.append(cotizacion)

    ancho_cierre = max((len(f"{fila.close:.4f}") for fila in filas), default=0)

    if not cotizaciones:
        return 0, 0, ancho_cierre
    ancho_rentab = max(
        RENTAB_FIELD_MIN, max(len(cotizacion) for cotizacion in cotizaciones)
    )
    return ancho_rentab, len("   rentab=") + ancho_rentab, ancho_cierre


def _format_gain(valor: float) -> str:
    """Rentabilidad con dos decimales, sin signo % y sin -0.00."""
    texto = f"{valor:.2f}"
    return "0.00" if texto == "-0.00" else texto
