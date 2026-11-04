"""Tests del cálculo de la serie (sin red y sin disco).

Portados de eVaca-PP: las reglas de la serie son las mismas y no se reescriben.
"""

from __future__ import annotations

from datetime import date

import pytest
from evaca_monitor.serie import (
    KIND_ANCHOR,
    KIND_BASE,
    KIND_DAY,
    KIND_FILL,
    KIND_MONTH,
    MonthGain,
    PriceRow,
    SeriesError,
    build_series,
    clean_monthly,
    daily_rows,
    parse_monthly,
)


def mes(anio: int, numero: int, ganancia: float, simulacion: bool = False) -> MonthGain:
    """Crea un mes de prueba empezando el día 1."""
    return MonthGain(date(anio, numero, 1), ganancia, simulacion)


def fila(anio: int, numero: int, dia: int, cierre: float, tipo: str) -> PriceRow:
    """Crea una fila de histórico de prueba."""
    return PriceRow(date(anio, numero, dia), cierre, tipo)


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
