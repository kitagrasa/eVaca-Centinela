"""Tests de lectura y escritura de los ficheros de las series.

Portados de eVaca-PP. El nombre del trader se sustituye por el id opaco: es lo
único que cambia en el formato, y es justo lo que no puede salir del
repositorio privado.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path

from evaca_monitor import serie_store
from evaca_monitor.serie import (
    KIND_BASE,
    KIND_CALIBRATED,
    KIND_DAY,
    KIND_MONTH,
    PriceRow,
)
from evaca_monitor.serie_store import SerieData


def fila(dia: int, cierre: float = 100.0, tipo: str = KIND_MONTH) -> PriceRow:
    return PriceRow(date(2026, 10, dia), cierre, tipo)


def test_escribe_texto_y_no_deja_temporales(tmp_path: Path) -> None:
    ruta = tmp_path / "salida.txt"

    serie_store.write_text_atomic(ruta, "hola\n")

    assert ruta.read_text(encoding="utf-8") == "hola\n"
    assert list(tmp_path.iterdir()) == [ruta]


def test_crea_las_carpetas_que_faltan(tmp_path: Path) -> None:
    ruta = tmp_path / "data" / "prices" / "serie"

    serie_store.write_text_atomic(ruta, "contenido")

    assert ruta.read_text(encoding="utf-8") == "contenido"


def test_sobrescribe_el_contenido_anterior(tmp_path: Path) -> None:
    ruta = tmp_path / "state.json"
    serie_store.write_json_atomic(ruta, {"a": 1})

    serie_store.write_json_atomic(ruta, {"a": 2, "b": 3})

    assert json.loads(ruta.read_text(encoding="utf-8")) == {"a": 2, "b": 3}


def test_json_legible_en_utf8(tmp_path: Path) -> None:
    ruta = tmp_path / "datos.json"
    serie_store.write_json_atomic(ruta, {"id": "José"})

    texto = ruta.read_text(encoding="utf-8")

    assert "José" in texto
    assert texto.endswith("\n")


def test_lee_json_inexistente_o_roto(tmp_path: Path) -> None:
    assert serie_store.read_json(tmp_path / "no.json", default={}) == {}

    roto = tmp_path / "roto.json"
    roto.write_text("{no es json", encoding="utf-8")
    assert serie_store.read_json(roto, default=[]) == []


def test_carga_una_serie_que_no_existe(tmp_path: Path) -> None:
    datos = serie_store.load_serie(tmp_path / "nada.json", "nada")

    assert datos.serie == "nada"
    assert datos.history == []
    assert datos.last_payload is None


def test_ignora_filas_invalidas_al_cargar(tmp_path: Path) -> None:
    ruta = tmp_path / "serie.json"
    ruta.write_text(
        json.dumps(
            {
                "version": 1,
                "history": [
                    {"date": "2026-10-31", "close": 100.0, "kind": "base"},
                    {"date": "no es fecha", "close": 1.0, "kind": "month"},
                    {"date": "2026-10-01", "close": 100.0, "kind": "otro"},
                    "no es un objeto",
                ],
            }
        ),
        encoding="utf-8",
    )

    datos = serie_store.load_serie(ruta, "serie")

    assert len(datos.history) == 1
    assert datos.history[0].date == date(2026, 10, 31)
    assert datos.history[0].kind == KIND_BASE


def test_guarda_y_recupera_una_serie(tmp_path: Path) -> None:
    ruta = tmp_path / "uno.json"
    datos = SerieData(
        serie="uno",
        updated_at="2026-11-02T00:30:00+00:00",
        fetched_at="2026-11-02T00:30:00+00:00",
        last_payload={"customerId": 1, "monthly": []},
        history=[fila(31, 100.0, KIND_BASE), fila(31, 101.0)],
    )

    serie_store.save_serie(ruta, datos)
    recuperado = serie_store.load_serie(ruta, "uno")

    assert recuperado.serie == "uno"
    assert recuperado.last_payload == {"customerId": 1, "monthly": []}
    assert recuperado.fetched_at == "2026-11-02T00:30:00+00:00"
    assert len(recuperado.history) == 1  # misma fecha: sin duplicados al guardar


def test_el_json_guardado_lleva_el_id_y_no_el_nombre(tmp_path: Path) -> None:
    ruta = tmp_path / "serie.json"
    serie_store.save_serie(ruta, SerieData(serie="0f1e2d3c4b5a"))

    guardado = json.loads(ruta.read_text(encoding="utf-8"))

    assert guardado["id"] == "0f1e2d3c4b5a"
    assert "trader" not in guardado
    assert "usuario" not in guardado


def test_state_inexistente_devuelve_estructura_minima(tmp_path: Path) -> None:
    estado = serie_store.load_state(tmp_path / "state.json")

    assert estado == {"last_run": None, "results": {}}


def test_state_corrupto_no_revienta(tmp_path: Path) -> None:
    ruta = tmp_path / "state.json"
    ruta.write_text("[]", encoding="utf-8")

    assert serie_store.load_state(ruta) == {"last_run": None, "results": {}}


def test_state_ida_y_vuelta(tmp_path: Path) -> None:
    ruta = tmp_path / "state.json"
    estado = {
        "last_run": "2026-11-02T00:30:00+00:00",
        "results": {"uno": {"status": "ok", "last_date": None, "error": None}},
    }

    serie_store.save_state(ruta, estado)

    assert serie_store.load_state(ruta) == estado


def test_upsert_reemplaza_por_fecha_y_ordena() -> None:
    antiguas = [
        fila(31, 100.0, KIND_BASE),
        PriceRow(date(2026, 11, 1), 150.0, KIND_DAY),
    ]
    nuevas = [
        PriceRow(date(2026, 11, 1), 160.0, KIND_DAY),
        PriceRow(date(2026, 10, 31), 110.0, KIND_MONTH),
    ]

    resultado = serie_store.upsert_rows(antiguas, nuevas)

    assert [f.date for f in resultado] == [date(2026, 10, 31), date(2026, 11, 1)]
    assert resultado[0].close == 110.0
    assert resultado[1].close == 160.0


def test_utc_now_iso_tiene_el_formato_del_proyecto() -> None:
    momento = datetime(2026, 11, 2, 0, 30, tzinfo=UTC)

    assert serie_store.utc_now_iso(momento) == "2026-11-02T00:30:00+00:00"


def test_parse_iso_admite_z_sin_zona_y_basura() -> None:
    assert serie_store.parse_iso("2026-11-02T00:30:00Z") == datetime(
        2026, 11, 2, 0, 30, tzinfo=UTC
    )
    assert serie_store.parse_iso("2026-11-02T00:30:00").tzinfo is not None
    assert serie_store.parse_iso("ayer") is None
    assert serie_store.parse_iso(None) is None


def test_el_archivo_guardado_no_ordena_mal_las_filas(tmp_path: Path) -> None:
    ruta = tmp_path / "uno.json"
    datos = SerieData(
        serie="uno",
        history=[fila(31, 100.0), fila(30, 99.0)],
    )

    serie_store.save_serie(ruta, datos)

    guardado = json.loads(ruta.read_text(encoding="utf-8"))
    fechas = [fila["date"] for fila in guardado["history"]]
    assert fechas == ["2026-10-30", "2026-10-31"]


# ----------------------------------------------------------------------
# Versión 2: chart, meses y días calibrados
# ----------------------------------------------------------------------
def test_las_filas_calibradas_sobreviven_al_ida_y_vuelta(
    tmp_path: Path,
) -> None:
    ruta = tmp_path / "uno.json"
    datos = SerieData(
        serie="uno",
        history=[
            PriceRow(date(2026, 10, 1), 100.0, KIND_CALIBRATED, gain=0.0),
            PriceRow(date(2026, 10, 2), 101.0, KIND_CALIBRATED, gain=1.0),
            PriceRow(date(2026, 10, 31), 110.0, KIND_MONTH, gain=10.0),
        ],
    )

    serie_store.save_serie(ruta, datos)
    recuperado = serie_store.load_serie(ruta, "uno")

    assert [fila.kind for fila in recuperado.history] == [
        KIND_CALIBRATED,
        KIND_CALIBRATED,
        KIND_MONTH,
    ]
    assert recuperado.history[0].date == date(2026, 10, 1)


def test_guarda_y_recupera_chart_y_auditoria(tmp_path: Path) -> None:
    ruta = tmp_path / "uno.json"
    datos = SerieData(
        serie="uno",
        fetched_at="2026-11-05T00:30:00+00:00",
        chart_fetched_at="2026-11-07T00:30:00+00:00",
        chart={
            "window": "lastTwoYears",
            "fetched_at": "2026-11-07T00:30:00+00:00",
            "points": [["2026-10-01", 100.0], ["2026-10-02", 101.0]],
        },
        months={
            "2026-10": {
                "stats_pct": 2.4,
                "chart_pct": 2.4,
                "factor": 1.0,
                "status": "calibrated",
                "checked_at": "2026-11-07T00:30:00+00:00",
            }
        },
    )

    serie_store.save_serie(ruta, datos)
    recuperado = serie_store.load_serie(ruta, "uno")

    assert recuperado.version == 2
    assert recuperado.fetched_at == "2026-11-05T00:30:00+00:00"
    assert recuperado.chart_fetched_at == "2026-11-07T00:30:00+00:00"
    assert recuperado.chart == datos.chart
    assert recuperado.months == datos.months


def test_un_archivo_v1_sin_chart_se_carga(tmp_path: Path) -> None:
    ruta = tmp_path / "uno.json"
    ruta.write_text(
        json.dumps(
            {
                "version": 1,
                "id": "uno",
                "fetched_at": "2025-04-01T00:00:00+00:00",
                "last_payload": {"monthly": []},
                "history": [
                    {"date": "2025-04-30", "close": 110.0, "kind": "month"}
                ],
            }
        ),
        encoding="utf-8",
    )

    datos = serie_store.load_serie(ruta, "uno")

    assert datos.version == 1
    assert datos.chart is None
    assert datos.months == {}
    assert datos.chart_fetched_at is None
    assert len(datos.history) == 1


def test_el_state_guarda_el_ultimo_mes_avisado_por_id(tmp_path: Path) -> None:
    ruta = tmp_path / "state.json"
    estado = {
        "last_run": "2026-11-02T00:30:00+00:00",
        "results": {},
        "alerts": {"0f1e2d3c4b5a": {"month": "2026-10", "stats_pct": 2.4}},
    }

    serie_store.save_state(ruta, estado)

    assert serie_store.load_state(ruta) == estado
