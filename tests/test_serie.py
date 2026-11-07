"""Tests del cálculo de la serie (sin red y sin disco).

Portados de eVaca-PP: las reglas de la serie son las mismas y no se reescriben.
"""

from __future__ import annotations

import math
import statistics
from datetime import date, timedelta

import pytest
from evaca_monitor.serie import (
    KIND_ANCHOR,
    KIND_BASE,
    KIND_CALIBRATED,
    KIND_DAY,
    KIND_FILL,
    KIND_MONTH,
    ChartPoint,
    MonthGain,
    PriceRow,
    SeriesError,
    build_series,
    build_series_full,
    clean_monthly,
    daily_rows,
    monthly_returns,
    parse_chart,
    parse_chart_points,
    parse_monthly,
)


def mes(anio: int, numero: int, ganancia: float, simulacion: bool = False) -> MonthGain:
    """Crea un mes de prueba empezando el día 1."""
    return MonthGain(date(anio, numero, 1), ganancia, simulacion)


def fila(anio: int, numero: int, dia: int, cierre: float, tipo: str) -> PriceRow:
    """Crea una fila de histórico de prueba."""
    return PriceRow(date(anio, numero, dia), cierre, tipo)


def payload_chart(
    puntos: list[tuple[date, float]], ventana: str = "lastTwoYears"
) -> dict:
    """Respuesta sintética del chart con la forma real."""
    return {
        "simulation": {
            ventana: {
                "chart": [
                    {
                        "timestamp": f"{dia.isoformat()}T00:00:00Z",
                        "equity": nivel,
                    }
                    for dia, nivel in puntos
                ]
            }
        }
    }


def serie_diaria(
    desde: date, hasta: date, movimientos: dict[date, float] | None = None
) -> list[ChartPoint]:
    """Puntos diarios desde `desde` hasta `hasta`, con niveles acumulados."""
    movimientos = movimientos or {}
    puntos: list[ChartPoint] = []
    nivel = 100.0
    dia = desde
    while dia <= hasta:
        nivel *= movimientos.get(dia, 1.0)
        puntos.append(ChartPoint(dia, nivel))
        dia += timedelta(days=1)
    return puntos


def como_tuplas(filas: list[PriceRow]) -> list[tuple[date, float, str]]:
    """Resume las filas para compararlas de forma legible."""
    return [(f.date, f.close, f.kind) for f in filas]


# ----------------------------------------------------------------------
# Limpieza
# ----------------------------------------------------------------------
def test_descarta_el_primer_mes_parcial() -> None:
    puntos = [
        MonthGain(date(2025, 3, 16), 10.79),
        mes(2025, 4, 14.90),
    ]
    limpios = clean_monthly(puntos)

    assert [p.start for p in limpios] == [date(2025, 4, 1)]


def test_descarta_simulaciones() -> None:
    puntos = [mes(2025, 4, 1.0, simulacion=True), mes(2025, 5, 2.0)]
    limpios = clean_monthly(puntos)

    assert [p.start for p in limpios] == [date(2025, 5, 1)]


def test_ordena_y_quita_duplicados_de_mes() -> None:
    puntos = [
        mes(2025, 5, 2.0),
        mes(2025, 4, 1.0),
        mes(2025, 4, 9.0),  # mismo mes: gana el último
    ]
    limpios = clean_monthly(puntos)

    assert [p.start for p in limpios] == [date(2025, 4, 1), date(2025, 5, 1)]
    assert limpios[0].gain == 9.0


def test_sin_meses_completos_es_error() -> None:
    with pytest.raises(SeriesError):
        clean_monthly([MonthGain(date(2025, 3, 16), 1.0)])


def test_sin_meses_es_error() -> None:
    with pytest.raises(SeriesError):
        build_series([], today=date(2026, 11, 2))


# ----------------------------------------------------------------------
# Base e histórico mensual
# ----------------------------------------------------------------------
def test_fila_base_en_cien(hoy: date) -> None:
    filas = build_series([mes(2025, 4, 10.0)], today=hoy)

    assert como_tuplas(filas)[0] == (date(2025, 3, 31), 100.0, KIND_BASE)


def test_encadenado_mensual(hoy: date) -> None:
    puntos = [mes(2025, 4, 14.90), mes(2025, 5, -3.0), mes(2025, 6, 2.0)]

    filas = build_series(puntos, today=hoy)

    # Cada mes: ancla el día 1 con el cierre anterior y cierre real al final.
    assert como_tuplas(filas) == [
        (date(2025, 3, 31), 100.0, KIND_BASE),
        (date(2025, 4, 1), 100.0, KIND_ANCHOR),
        (date(2025, 4, 30), 114.9, KIND_MONTH),
        (date(2025, 5, 1), 114.9, KIND_ANCHOR),
        (date(2025, 5, 31), 111.453, KIND_MONTH),
        (date(2025, 6, 1), 111.453, KIND_ANCHOR),
        (date(2025, 6, 30), 113.6821, KIND_MONTH),
    ]


def test_cierre_de_mes_en_el_ultimo_dia(hoy: date) -> None:
    filas = build_series([mes(2025, 2, 1.0)], today=hoy)

    # 2025 no es bisiesto: febrero termina el día 28.
    assert filas[-1] == PriceRow(date(2025, 2, 28), 101.0, KIND_MONTH)


def test_guarda_la_rentabilidad_original_de_meses_y_dia_actual(hoy: date) -> None:
    puntos = [
        mes(2025, 4, 14.9),
        mes(2025, 5, -3.0),
        mes(2025, 6, 2.0),
        mes(2026, 11, 2.51),
    ]

    filas = {f.date: f for f in build_series(puntos, today=hoy)}

    assert filas[date(2025, 4, 30)].gain == 14.9
    assert filas[date(2025, 5, 31)].gain == -3.0
    assert filas[date(2025, 6, 30)].gain == 2.0
    assert filas[date(2026, 11, 2)].gain == 2.51
    assert filas[date(2025, 4, 1)].gain is None  # ancla
    assert filas[date(2025, 3, 31)].gain is None  # base


def test_reconstruye_la_rentabilidad_de_un_dia_guardado(hoy: date) -> None:
    puntos = [mes(2025, 4, 10.0), mes(2026, 10, 1.0)]
    historial = [fila(2026, 10, 15, 111.1, KIND_DAY)]

    filas = {f.date: f for f in build_series(puntos, today=hoy, history=historial)}

    # 111.1 sobre el cierre anterior (110.0) es un 1 %.
    assert filas[date(2026, 10, 15)].gain == 1.0


def test_reconstruye_la_rentabilidad_de_un_dia_1_guardado() -> None:
    puntos = [mes(2025, 4, 10.0), mes(2026, 11, 1.0)]
    historial = [fila(2026, 11, 1, 111.1, KIND_DAY)]

    filas = {
        f.date: f
        for f in build_series(puntos, today=date(2026, 11, 2), history=historial)
    }

    assert filas[date(2026, 11, 1)].kind == KIND_DAY
    assert filas[date(2026, 11, 1)].gain == 1.0


def test_mes_sin_cerrar_no_genera_fila_diaria(hoy: date) -> None:
    filas = build_series([mes(2025, 4, 10.0)], today=hoy)

    assert all(f.kind != KIND_DAY for f in filas)


# ----------------------------------------------------------------------
# Mes en curso
# ----------------------------------------------------------------------
def test_dia_actual_con_mtd(hoy: date) -> None:
    puntos = [mes(2025, 4, 10.0), mes(2026, 11, 2.51)]

    filas = build_series(puntos, today=hoy)

    assert filas[-1] == PriceRow(date(2026, 11, 2), 112.761, KIND_DAY)


def test_mismo_dia_actualiza_y_no_duplica(hoy: date) -> None:
    puntos = [mes(2025, 4, 10.0), mes(2026, 11, 2.51)]
    historial = [fila(2026, 11, 2, 999.0, KIND_DAY)]

    filas = build_series(puntos, today=hoy, history=historial)

    del_dia = [f for f in filas if f.date == hoy]
    assert len(del_dia) == 1
    assert del_dia[0].close == 112.761


def test_conserva_los_dias_anteriores_del_mes(hoy: date) -> None:
    puntos = [mes(2025, 4, 10.0), mes(2026, 11, 2.51)]
    historial = [fila(2026, 11, 1, 111.5, KIND_DAY)]

    filas = build_series(puntos, today=hoy, history=historial)

    assert fila(2026, 11, 1, 111.5, KIND_DAY) in filas


def test_conserva_los_dias_reales_de_meses_cerrados(hoy: date) -> None:
    puntos = [mes(2025, 4, 10.0), mes(2026, 10, 1.0)]
    historial = [
        fila(2026, 10, 15, 555.0, KIND_DAY),
        fila(2026, 10, 31, 999.0, KIND_DAY),
    ]

    filas = build_series(puntos, today=hoy, history=historial)

    # El día real se conserva; en el último día manda el cierre del mes.
    assert como_tuplas(filas) == [
        (date(2025, 3, 31), 100.0, KIND_BASE),
        (date(2025, 4, 1), 100.0, KIND_ANCHOR),
        (date(2025, 4, 30), 110.0, KIND_MONTH),
        (date(2026, 10, 1), 110.0, KIND_ANCHOR),
        (date(2026, 10, 15), 555.0, KIND_DAY),
        (date(2026, 10, 31), 111.1, KIND_MONTH),
    ]


# ----------------------------------------------------------------------
# Ancla del día 1: PP empieza a dibujar en el primer punto del periodo
# ----------------------------------------------------------------------
def test_ancla_el_dia_1_de_cada_mes_cerrado(hoy: date) -> None:
    puntos = [mes(2025, 4, 14.90), mes(2025, 5, -3.0)]

    filas = build_series(puntos, today=hoy)
    anclas = [(f.date, f.close) for f in filas if f.kind == KIND_ANCHOR]

    assert anclas == [
        (date(2025, 4, 1), 100.0),
        (date(2025, 5, 1), 114.9),
    ]


def test_el_ancla_lleva_el_cierre_del_mes_anterior(hoy: date) -> None:
    puntos = [mes(2025, 4, 14.90), mes(2025, 5, -3.0)]

    filas = {f.date: f for f in build_series(puntos, today=hoy)}

    assert filas[date(2025, 5, 1)].close == filas[date(2025, 4, 30)].close


def test_ancla_del_mes_en_curso(hoy: date) -> None:
    puntos = [mes(2025, 4, 10.0), mes(2026, 11, 2.51)]

    filas = {f.date: f for f in build_series(puntos, today=hoy)}

    # Día 1 con el cierre del mes anterior; después el punto real de hoy.
    assert filas[date(2026, 11, 1)] == PriceRow(
        date(2026, 11, 1), 110.0, KIND_ANCHOR
    )
    assert filas[date(2026, 11, 2)].kind == KIND_DAY


def test_la_primera_ancla_valen_cien_como_la_base(hoy: date) -> None:
    filas = build_series([mes(2025, 4, 10.0)], today=hoy)

    assert como_tuplas(filas) == [
        (date(2025, 3, 31), 100.0, KIND_BASE),
        (date(2025, 4, 1), 100.0, KIND_ANCHOR),
        (date(2025, 4, 30), 110.0, KIND_MONTH),
    ]


def test_un_dia_capturado_manda_sobre_el_ancla(hoy: date) -> None:
    puntos = [mes(2025, 4, 10.0), mes(2026, 11, 2.51)]
    historial = [fila(2026, 11, 1, 111.5, KIND_DAY)]

    filas = build_series(puntos, today=hoy, history=historial)
    por_fecha = {f.date: f for f in filas}

    assert por_fecha[date(2026, 11, 1)] == fila(2026, 11, 1, 111.5, KIND_DAY)


def test_si_hoy_es_dia_1_manda_el_dato_real(hoy: date) -> None:
    puntos = [mes(2025, 4, 10.0), mes(2026, 11, 2.51)]

    filas = build_series(puntos, today=date(2026, 11, 1))
    dia_uno = {f.date: f for f in filas}[date(2026, 11, 1)]

    assert dia_uno.kind == KIND_DAY
    assert dia_uno.close == 112.761


def test_el_ancla_no_duplica_fechas(hoy: date) -> None:
    puntos = [mes(2025, 4, 10.0), mes(2025, 5, 1.0), mes(2026, 11, 1.0)]

    filas = build_series(puntos, today=hoy)
    fechas = [f.date for f in filas]

    assert fechas == sorted(fechas)
    assert len(fechas) == len(set(fechas))


# ----------------------------------------------------------------------
# Rejilla diaria para el HTML (nunca para el JSON)
# ----------------------------------------------------------------------
def test_rejilla_diaria_cubre_todos_los_dias_del_mes_cerrado(hoy: date) -> None:
    filas = build_series([mes(2025, 4, 10.0)], today=hoy)

    diarias = daily_rows(filas, today=hoy)
    fechas = [f.date for f in diarias]

    assert fechas[0] == date(2025, 3, 31)
    assert fechas[-1] == date(2025, 4, 30)
    assert len(fechas) == 31
    assert fechas == sorted(set(fechas))


def test_rejilla_diaria_empieza_y_acaba_en_los_extremos_reales(hoy: date) -> None:
    filas = build_series([mes(2025, 4, 10.0)], today=hoy)

    diarias = daily_rows(filas, today=hoy)

    assert diarias[0] == PriceRow(date(2025, 3, 31), 100.0, KIND_BASE)
    assert diarias[-1] == PriceRow(date(2025, 4, 30), 110.0, KIND_MONTH)


def test_rejilla_diaria_respeta_el_ancla(hoy: date) -> None:
    filas = build_series([mes(2025, 4, 10.0)], today=hoy)

    por_fecha = {f.date: f for f in daily_rows(filas, today=hoy)}

    # El día 1 sigue siendo el ancla real (100), no un punto interpolado.
    assert por_fecha[date(2025, 4, 1)] == PriceRow(
        date(2025, 4, 1), 100.0, KIND_ANCHOR
    )
    assert por_fecha[date(2025, 4, 2)].kind == KIND_FILL


def test_rejilla_diaria_reparte_la_rentabilidad_con_tasa_constante(
    hoy: date,
) -> None:
    filas = build_series([mes(2025, 4, 10.0)], today=hoy)

    por_fecha = {f.date: f for f in daily_rows(filas, today=hoy)}

    # Del 1 de abril (100) al 30 (110) hay 29 días.
    assert por_fecha[date(2025, 4, 2)].close == round(100.0 * 1.1 ** (1 / 29), 4)
    assert por_fecha[date(2025, 4, 16)].close == round(100.0 * 1.1 ** (15 / 29), 4)


def test_rejilla_diaria_no_rellena_un_mes_con_dias_capturados(hoy: date) -> None:
    puntos = [mes(2025, 4, 10.0)]
    historial = [fila(2025, 4, 15, 150.0, KIND_DAY)]

    filas = build_series(puntos, today=hoy, history=historial)
    por_fecha = {f.date: f for f in daily_rows(filas, today=hoy)}

    # El día capturado se queda con su valor y su tipo...
    assert por_fecha[date(2025, 4, 15)] == fila(2025, 4, 15, 150.0, KIND_DAY)
    # ...y el resto del mes no se rellena: sus huecos se quedan como huecos.
    assert date(2025, 4, 14) not in por_fecha
    assert date(2025, 4, 16) not in por_fecha
    assert all(f.kind != KIND_FILL for f in por_fecha.values())

    # El ancla del día 1 y el cierre del mes siguen ahí (son filas reales).
    assert por_fecha[date(2025, 4, 1)].kind == KIND_ANCHOR
    assert por_fecha[date(2025, 4, 30)].kind == KIND_MONTH


def test_el_mes_con_dias_no_impide_rellenar_los_demas(hoy: date) -> None:
    puntos = [mes(2025, 4, 10.0), mes(2025, 5, 2.0)]
    historial = [fila(2025, 4, 15, 150.0, KIND_DAY)]

    diarias = daily_rows(build_series(puntos, today=hoy, history=historial), today=hoy)
    por_fecha = {f.date: f for f in diarias}

    # Abril tiene un D: no se rellena.
    assert date(2025, 4, 2) not in por_fecha
    # Mayo no tiene ningún D: se rellena como siempre.
    assert por_fecha[date(2025, 5, 15)].kind == KIND_FILL


def test_rejilla_diaria_mantiene_los_cierres_de_mes(hoy: date) -> None:
    puntos = [mes(2025, 4, 14.90), mes(2025, 5, -3.0)]

    filas = build_series(puntos, today=hoy)
    diarias = {f.date: f.close for f in daily_rows(filas, today=hoy)}

    assert diarias[date(2025, 4, 30)] == 114.9
    assert diarias[date(2025, 5, 31)] == 111.453


def test_rejilla_diaria_no_inventa_el_mes_en_curso(hoy: date) -> None:
    puntos = [mes(2025, 4, 10.0), mes(2026, 11, 2.51)]

    filas = build_series(puntos, today=hoy)
    noviembre = [
        f
        for f in daily_rows(filas, today=hoy)
        if (f.date.year, f.date.month) == (2026, 11)
    ]

    # Solo el ancla del día 1 y el dato real de hoy, sin relleno.
    assert [f.date for f in noviembre] == [date(2026, 11, 1), date(2026, 11, 2)]
    assert all(f.kind != KIND_FILL for f in noviembre)


def test_rejilla_diaria_ordenada_sin_duplicados_y_4_decimales(hoy: date) -> None:
    puntos = [mes(2025, 4, 14.90), mes(2025, 5, -3.0), mes(2025, 6, 2.0)]

    diarias = daily_rows(build_series(puntos, today=hoy), today=hoy)
    fechas = [f.date for f in diarias]

    assert fechas == sorted(fechas)
    assert len(fechas) == len(set(fechas))
    assert all(f.close == round(f.close, 4) for f in diarias)
    # Del 31 de marzo al 30 de junio, todos los días.
    assert len(diarias) == 92
    assert fechas[0] == date(2025, 3, 31)
    assert fechas[-1] == date(2025, 6, 30)


def test_rejilla_diaria_sin_filas() -> None:
    assert daily_rows([], today=date(2026, 11, 2)) == []


def test_rejilla_diaria_de_una_sola_fila_no_inventa_nada() -> None:
    unica = [fila(2026, 10, 31, 111.1, KIND_MONTH)]

    assert daily_rows(unica, today=date(2026, 11, 2)) == unica


def test_rejilla_diaria_cubre_los_huecos_entre_meses(hoy: date) -> None:
    # Sin datos de septiembre: el tramo va del cierre de agosto al ancla de
    # octubre, que vale lo mismo, así que septiembre queda plano.
    filas = build_series([mes(2025, 8, 1.0), mes(2025, 10, 1.0)], today=hoy)

    diarias = daily_rows(filas, today=hoy)
    por_fecha = {f.date: f.close for f in diarias}

    assert por_fecha[date(2025, 9, 15)] == 101.0
    assert por_fecha[date(2025, 10, 3)] > por_fecha[date(2025, 10, 1)]
    # Del 31 de julio (base) al 31 de octubre, sin huecos.
    assert len(diarias) == 93


# ----------------------------------------------------------------------
# Reglas generales
# ----------------------------------------------------------------------
def test_sin_fechas_futuras(hoy: date) -> None:
    puntos = [
        mes(2025, 4, 10.0),
        mes(2026, 11, 2.51),
        mes(2026, 12, 5.0),  # mes futuro: se ignora
    ]

    filas = build_series(puntos, today=hoy)

    assert all(f.date <= hoy for f in filas)
    assert all(f.date.month != 12 or f.date.year != 2026 for f in filas)


def test_orden_ascendente_y_sin_duplicados(hoy: date) -> None:
    puntos = [
        mes(2025, 6, 2.0),
        mes(2025, 4, 14.90),
        mes(2025, 5, -3.0),
        mes(2026, 11, 1.0),
    ]

    filas = build_series(puntos, today=hoy)
    fechas = [f.date for f in filas]

    assert fechas == sorted(fechas)
    assert len(fechas) == len(set(fechas))


def test_cuatro_decimales(hoy: date) -> None:
    puntos = [mes(2025, 4, 13.3333), mes(2025, 5, -1.23456)]

    filas = build_series(puntos, today=hoy)

    assert all(f.close == round(f.close, 4) for f in filas)


def test_acepta_datos_de_payload_reales(
    payload_simple: dict, payload_simulation: dict, hoy: date
) -> None:
    filas = build_series(parse_monthly(payload_simple), today=hoy)
    por_fecha = {f.date: f.close for f in filas}

    assert por_fecha[date(2025, 3, 31)] == 100.0
    assert por_fecha[date(2025, 4, 30)] == 114.9

    simuladas = parse_monthly(payload_simulation)
    assert [p.start for p in clean_monthly(simuladas)] == [date(2025, 5, 1)]


# ----------------------------------------------------------------------
# Lectura del JSON del endpoint
# ----------------------------------------------------------------------
def test_parse_monthly_tolera_entradas_rotas() -> None:
    payload = {
        "monthly": [
            {"start": "2025-04-01T00:00:00Z", "gain": "2.5"},
            {"start": "2025-05-01T00:00:00Z", "gain": None},
            {"start": "no es una fecha", "gain": 1.0},
            "ni siquiera es un objeto",
            {"start": "2025-06-01T00:00:00Z", "gain": True},
        ]
    }

    puntos = parse_monthly(payload)

    assert len(puntos) == 1
    assert puntos[0].gain == 2.5


def test_parse_monthly_sin_lista_es_error() -> None:
    with pytest.raises(SeriesError):
        parse_monthly({"customerId": 1})

    with pytest.raises(SeriesError):
        parse_monthly([])


# ----------------------------------------------------------------------
# Chart: parseo de la respuesta
# ----------------------------------------------------------------------
def test_parse_chart_valido() -> None:
    payload = payload_chart(
        [(date(2025, 3, 31), 100.0), (date(2025, 4, 1), 101.0)]
    )

    puntos = parse_chart(payload)

    assert puntos == [
        ChartPoint(date(2025, 3, 31), 100.0),
        ChartPoint(date(2025, 4, 1), 101.0),
    ]


def test_parse_chart_vacio_es_error() -> None:
    with pytest.raises(SeriesError):
        parse_chart(payload_chart([]))


def test_parse_chart_duplicado_es_error() -> None:
    with pytest.raises(SeriesError):
        parse_chart(
            payload_chart(
                [(date(2025, 4, 1), 100.0), (date(2025, 4, 1), 101.0)]
            )
        )


def test_parse_chart_no_creciente_es_error() -> None:
    with pytest.raises(SeriesError):
        parse_chart(
            payload_chart(
                [(date(2025, 4, 2), 100.0), (date(2025, 4, 1), 101.0)]
            )
        )


@pytest.mark.parametrize("nivel", [0.0, -1.0])
def test_parse_chart_nivel_no_positivo_es_error(nivel: float) -> None:
    with pytest.raises(SeriesError):
        parse_chart(
            payload_chart(
                [(date(2025, 4, 1), 100.0), (date(2025, 4, 2), nivel)]
            )
        )


def test_parse_chart_estructura_rota_es_error() -> None:
    with pytest.raises(SeriesError):
        parse_chart({"simulation": {}})
    with pytest.raises(SeriesError):
        parse_chart({"simulation": {"lastTwoYears": {}}})
    with pytest.raises(SeriesError):
        parse_chart({"simulation": {"lastTwoYears": {"chart": {}}}})
    with pytest.raises(SeriesError):
        parse_chart(payload_chart([(date(2025, 4, 1), 100.0)]), window="otra")


def test_parse_chart_points_guardados() -> None:
    puntos = parse_chart_points(
        [["2025-04-01", 100.0], ["2025-04-02", 101.0]]
    )

    assert puntos == [
        ChartPoint(date(2025, 4, 1), 100.0),
        ChartPoint(date(2025, 4, 2), 101.0),
    ]


# ----------------------------------------------------------------------
# Retorno mensual cierre-contra-cierre
# ----------------------------------------------------------------------
def test_retorno_mensual_28_30_31_y_cambio_de_anio() -> None:
    puntos = [
        ChartPoint(date(2025, 1, 31), 100.0),
        ChartPoint(date(2025, 2, 28), 105.0),
        ChartPoint(date(2025, 3, 31), 110.0),
        ChartPoint(date(2025, 4, 30), 99.0),
        ChartPoint(date(2025, 5, 31), 100.0),
        ChartPoint(date(2025, 12, 31), 120.0),
        ChartPoint(date(2026, 1, 31), 132.0),
    ]

    retornos = monthly_returns(puntos)

    assert retornos[(2025, 2)] == pytest.approx(5.0)
    assert retornos[(2025, 4)] == pytest.approx(-10.0)
    assert retornos[(2025, 5)] == pytest.approx(100 / 99 * 100 - 100)
    assert retornos[(2026, 1)] == pytest.approx(10.0)
    # El primer mes del chart no tiene cierre anterior dentro de la ventana.
    assert (2025, 1) not in retornos


# ----------------------------------------------------------------------
# Calibración exacta
# ----------------------------------------------------------------------
def test_calibracion_producto_exacto_y_cierre_m_exacto(hoy: date) -> None:
    estadistica = [mes(2025, 4, 4.5)]
    chart = serie_diaria(
        date(2025, 3, 31),
        date(2025, 4, 30),
        {date(2025, 4, 10): 1.02, date(2025, 4, 20): 1.02},
    )

    resultado = build_series_full(estadistica, today=hoy, chart=chart)
    abril = [
        fila
        for fila in resultado.rows
        if (fila.date.year, fila.date.month) == (2025, 4)
    ]

    assert resultado.months["2025-04"]["status"] == "calibrated"
    assert resultado.months["2025-04"]["stats_pct"] == 4.5
    assert resultado.months["2025-04"]["chart_pct"] == pytest.approx(4.04)
    assert all(fila.kind == KIND_CALIBRATED for fila in abril[:-1])
    assert abril[-1].kind == KIND_MONTH
    assert abril[-1].close == 104.5
    assert abril[-1].gain == 4.5
    producto = 1.0
    for anterior, actual in zip(abril, abril[1:], strict=False):
        producto *= actual.close / anterior.close
    assert producto == pytest.approx(1.045, abs=1e-4)


def test_calibracion_no_mueve_los_dias_de_retorno_cero(hoy: date) -> None:
    estadistica = [mes(2025, 4, 4.5)]
    chart = serie_diaria(
        date(2025, 3, 31),
        date(2025, 4, 30),
        {date(2025, 4, 10): 1.02, date(2025, 4, 20): 1.02},
    )

    resultado = build_series_full(estadistica, today=hoy, chart=chart)
    filas = {fila.date: fila for fila in resultado.rows}

    assert filas[date(2025, 4, 2)].close == filas[date(2025, 4, 1)].close
    assert filas[date(2025, 4, 2)].gain == 0.0
    # El día con movimiento sí cambia.
    assert filas[date(2025, 4, 10)].close > filas[date(2025, 4, 9)].close


def test_calibracion_no_cambia_la_dispersion_de_los_log_retornos(
    hoy: date,
) -> None:
    originales = {
        date(2025, 4, 5): 1.01,
        date(2025, 4, 12): 1.02,
        date(2025, 4, 19): 0.995,
    }
    chart = serie_diaria(date(2025, 3, 31), date(2025, 4, 30), originales)
    chart_pct = ((1 + 0.01) * (1 + 0.02) * (1 - 0.005) - 1) * 100
    estadistica = [mes(2025, 4, chart_pct + 0.1)]

    resultado = build_series_full(estadistica, today=hoy, chart=chart)
    filas = {fila.date: fila for fila in resultado.rows}

    logs_originales = [math.log(1.01), math.log(1.02), math.log(0.995)]
    logs_calibrados = [
        math.log(filas[dia].close / filas[dia - timedelta(days=1)].close)
        for dia in originales
    ]

    assert resultado.months["2025-04"]["status"] == "calibrated"
    assert statistics.pstdev(logs_calibrados) == pytest.approx(
        statistics.pstdev(logs_originales), abs=1e-4
    )


# ----------------------------------------------------------------------
# Stats manda: se calibra aunque el chart se aleje
# ----------------------------------------------------------------------
def test_calibra_aunque_se_aleje_mas_del_cap(hoy: date) -> None:
    estadistica = [mes(2025, 4, 5.0)]
    chart = serie_diaria(
        date(2025, 3, 31),
        date(2025, 4, 30),
        {date(2025, 4, 10): 1.02, date(2025, 4, 20): 1.02},
    )

    resultado = build_series_full(estadistica, today=hoy, chart=chart)
    abril = [
        fila
        for fila in resultado.rows
        if (fila.date.year, fila.date.month) == (2025, 4)
    ]

    assert resultado.months["2025-04"]["status"] == "calibrated"
    assert any(fila.kind == KIND_CALIBRATED for fila in abril)
    assert abril[-1].kind == KIND_MONTH
    assert abril[-1].close == 105.0
    producto = 1.0
    for anterior, actual in zip(abril, abril[1:], strict=False):
        producto *= actual.close / anterior.close
    assert producto == pytest.approx(1.05, abs=1e-4)


def test_calibra_signos_opuestos(hoy: date) -> None:
    estadistica = [mes(2025, 4, 0.2)]
    chart = serie_diaria(
        date(2025, 3, 31),
        date(2025, 4, 30),
        {date(2025, 4, 10): 0.999},
    )

    resultado = build_series_full(estadistica, today=hoy, chart=chart)

    assert resultado.months["2025-04"]["status"] == "calibrated"
    assert any(
        fila.kind == KIND_CALIBRATED
        for fila in resultado.rows
        if (fila.date.year, fila.date.month) == (2025, 4)
    )


def test_rechaza_solo_si_no_hay_dias_con_movimiento(hoy: date) -> None:
    """Sin ningún día con retorno no hay forma de repartir el cierre."""
    estadistica = [mes(2025, 4, 2.0)]
    chart = serie_diaria(date(2025, 3, 31), date(2025, 4, 30))

    resultado = build_series_full(estadistica, today=hoy, chart=chart)

    assert resultado.months["2025-04"]["status"] == "rejected"
    assert all(
        fila.kind != KIND_CALIBRATED
        for fila in resultado.rows
        if (fila.date.year, fila.date.month) == (2025, 4)
    )


# ----------------------------------------------------------------------
# Rejilla diaria con meses de chart
# ----------------------------------------------------------------------
def test_daily_rows_no_rellena_un_mes_con_c(hoy: date) -> None:
    estadistica = [mes(2025, 4, 2.1)]
    chart = serie_diaria(
        date(2025, 3, 31),
        date(2025, 4, 30),
        {date(2025, 4, 10): 1.02},
    )

    filas = build_series_full(estadistica, today=hoy, chart=chart).rows
    diarias = daily_rows(filas, today=hoy)

    assert all(fila.kind != KIND_FILL for fila in diarias)
    assert len(diarias) == 31  # del 31 de marzo al 30 de abril


def test_daily_rows_rellena_el_hueco_inicial_del_mes_frontera(
    hoy: date,
) -> None:
    estadistica = [mes(2025, 4, 0.0)]
    chart = serie_diaria(date(2025, 4, 15), date(2025, 4, 30))

    filas = build_series_full(estadistica, today=hoy, chart=chart).rows
    diarias = {fila.date: fila for fila in daily_rows(filas, today=hoy)}

    assert diarias[date(2025, 4, 1)].kind == KIND_ANCHOR
    assert diarias[date(2025, 4, 14)].kind == KIND_FILL
    assert diarias[date(2025, 4, 15)].kind == KIND_CALIBRATED
    assert diarias[date(2025, 4, 16)].kind == KIND_CALIBRATED
    # Después del primer C no se inventa nada: ya hay un punto por día.
    assert all(
        fila.kind != KIND_FILL
        for dia, fila in diarias.items()
        if dia >= date(2025, 4, 15)
    )
