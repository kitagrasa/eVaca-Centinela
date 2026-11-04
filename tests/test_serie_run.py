"""Tests de la pasada completa de las series.

Portados de eVaca-PP y ampliados con lo que cambia por diseño: el nombre del
trader nunca sale en los ficheros ni en el log (solo su id opaco), la lista es
la del monitor y el cliente es `EvacaClient`.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from evaca_monitor import serie_store
from evaca_monitor.client import EvacaError
from evaca_monitor.config import Config, SeriesConfig
from evaca_monitor.serie import serie_id
from evaca_monitor.serie_run import RunConfig, SerieRun

AHORA = datetime(2026, 11, 2, 0, 30, tzinfo=UTC)

REGISTRO = logging.getLogger("pruebas_evaca_series")
REGISTRO.addHandler(logging.NullHandler())

ID_UNO = serie_id("uno")
ID_DOS = serie_id("dos")


class ClienteFalso:
    """Cliente que devuelve respuestas preparadas y cuenta los pedidos."""

    def __init__(self, respuestas: dict[str, Any] | None = None) -> None:
        self.respuestas = dict(respuestas or {})
        self.pedidos: list[str] = []
        self.cerrado = False

    def get_gain_history(self, usuario: str) -> dict[str, Any]:
        self.pedidos.append(usuario)
        valor = self.respuestas.get(usuario)
        if isinstance(valor, Exception):
            raise valor
        if valor is None:
            raise AssertionError(f"petición inesperada para {usuario}")
        return valor

    def close(self) -> None:
        self.cerrado = True


class Registro:
    """Captura los mensajes de log para poder mirarlos."""

    def __init__(self) -> None:
        self.mensajes: list[str] = []
        self._logger = logging.getLogger("pruebas_captura_series")
        self._logger.handlers = []
        self._logger.propagate = False
        self._logger.setLevel(logging.INFO)
        manejador = logging.Handler()
        manejador.emit = self._emitir  # type: ignore[method-assign]
        self._logger.addHandler(manejador)

    def _emitir(self, record: logging.LogRecord) -> None:  # pragma: no cover - trivial
        self.mensajes.append(record.getMessage())

    @property
    def logger(self) -> logging.Logger:
        return self._logger


def entorno_base(tmp_path: Path) -> SimpleNamespace:
    """Carpeta de datos y watchlist aislados para cada test."""
    datos = tmp_path / "data"
    watchlist = tmp_path / "watchlist.md"
    watchlist.write_text("uno\ndos\n", encoding="utf-8")
    return SimpleNamespace(raiz=tmp_path, datos=datos, watchlist=watchlist)


def ejecutar(
    entorno: SimpleNamespace,
    cliente: ClienteFalso,
    *,
    reloj: Any = None,
    pausas: list[float] | None = None,
    usuarios: list[str] | None = None,
    logger: logging.Logger = REGISTRO,
    **opciones: Any,
) -> int:
    """Lanza la pasada con reloj y pausa controlados."""
    pausas = pausas if pausas is not None else []
    config = Config(
        users=list(usuarios) if usuarios is not None else ["uno", "dos"],
        series=SeriesConfig(pause_seconds=3.0, cache_minutes=60),
        users_source=entorno.watchlist,
    )
    run = SerieRun(
        config,
        RunConfig(data_dir=entorno.datos, **opciones),
        cliente,
        clock=reloj or (lambda: AHORA),
        pause=pausas.append,
        logger=logger,
    )
    return run.run()


def leer_json(ruta: Path) -> dict:
    return json.loads(ruta.read_text(encoding="utf-8"))


def lineas_html(html: str) -> list[str]:
    """Filas de datos del HTML, sin la cabecera."""
    return [
        linea
        for linea in html.splitlines()
        if linea.strip().startswith("<tr data-kind=")
    ]


def fila_html(html: str, fecha: str) -> str:
    """Línea del HTML que contiene la fecha indicada."""
    return next(linea for linea in lineas_html(html) if fecha in linea)


def rentab_html(linea: str) -> str | None:
    """Valor de rentab de una fila, o None si no lo lleva."""
    coincidencia = re.search(r'rentab=\s+"([^"]*)"', linea)
    return coincidencia.group(1) if coincidencia else None


# ----------------------------------------------------------------------
# Casos normales
# ----------------------------------------------------------------------
def test_trader_nuevo_crea_json_html_y_state(
    tmp_path: Path, payload_simple: dict
) -> None:
    entorno = entorno_base(tmp_path)
    cliente = ClienteFalso({"uno": payload_simple, "dos": payload_simple})

    codigo = ejecutar(entorno, cliente)

    assert codigo == 0
    assert cliente.pedidos == ["uno", "dos"]
    assert (entorno.datos / "series" / f"{ID_UNO}.json").is_file()
    assert (entorno.datos / "series" / f"{ID_DOS}.json").is_file()

    html = (entorno.datos / "prices" / ID_UNO).read_text(encoding="utf-8")
    assert "<th>Date</th><th>Close</th>" in html
    assert "2025-03-31" in html

    estado = leer_json(entorno.datos / "state.json")
    assert estado["last_run"] == "2026-11-02T00:30:00+00:00"
    assert estado["results"][ID_UNO] == {
        "status": "ok",
        "last_date": "2025-06-30",
        "error": None,
    }


def test_el_html_se_guarda_sin_extension(
    tmp_path: Path, payload_simple: dict
) -> None:
    entorno = entorno_base(tmp_path)

    ejecutar(entorno, ClienteFalso({"uno": payload_simple, "dos": payload_simple}))

    ruta = entorno.datos / "prices" / ID_UNO

    assert ruta.is_file()
    assert ruta.suffix == ""


def test_el_json_guarda_el_formato_acordado(
    tmp_path: Path, payload_simple: dict
) -> None:
    entorno = entorno_base(tmp_path)
    # La respuesta real puede traer más campos: no deben acabar en data/.
    payload = {
        **payload_simple,
        "avatarUrl": "https://ejemplo.invalido/foto",
        "monthly": [
            {**mes, "campoQueNoSeUsa": "no debe guardarse"}
            for mes in payload_simple["monthly"]
        ],
    }

    ejecutar(entorno, ClienteFalso({"uno": payload}), usuarios=["uno"])

    ruta = entorno.datos / "series" / f"{ID_UNO}.json"
    datos = leer_json(ruta)

    assert datos["version"] == 1
    assert datos["id"] == ID_UNO
    assert datos["fetched_at"] == "2026-11-02T00:30:00+00:00"
    assert datos["updated_at"] == "2026-11-02T00:30:00+00:00"
    # En data/ solo va lo que se usa: nada de identificadores ni campos extra.
    assert datos["last_payload"] == {"monthly": payload_simple["monthly"]}
    assert "customerId" not in datos["last_payload"]
    assert "avatarUrl" not in datos["last_payload"]
    assert "campoQueNoSeUsa" not in ruta.read_text(encoding="utf-8")
    assert "ejemplo.invalido" not in ruta.read_text(encoding="utf-8")
    assert [fila["kind"] for fila in datos["history"]][0] == "base"


def test_el_state_guarda_ids_y_no_nombres(tmp_path: Path, payload_simple: dict) -> None:
    entorno = entorno_base(tmp_path)

    ejecutar(entorno, ClienteFalso({"uno": payload_simple, "dos": payload_simple}))

    estado = leer_json(entorno.datos / "state.json")

    assert set(estado["results"]) == {ID_UNO, ID_DOS}
    assert "uno" not in estado["results"]
    assert "dos" not in estado["results"]


def test_pausa_entre_peticiones_reales(
    tmp_path: Path, payload_simple: dict
) -> None:
    entorno = entorno_base(tmp_path)
    pausas: list[float] = []

    ejecutar(
        entorno,
        ClienteFalso({"uno": payload_simple, "dos": payload_simple}),
        pausas=pausas,
    )

    assert pausas == [3.0]


# ----------------------------------------------------------------------
# Caché
# ----------------------------------------------------------------------
def test_segunda_ejecucion_usa_cache_y_no_pide(
    tmp_path: Path, payload_simple: dict
) -> None:
    entorno = entorno_base(tmp_path)
    ejecutar(entorno, ClienteFalso({"uno": payload_simple, "dos": payload_simple}))

    cliente = ClienteFalso({})
    codigo = ejecutar(entorno, cliente, reloj=lambda: AHORA + timedelta(minutes=30))

    assert codigo == 0
    assert cliente.pedidos == []


def test_force_ignora_la_cache(tmp_path: Path, payload_simple: dict) -> None:
    entorno = entorno_base(tmp_path)
    ejecutar(entorno, ClienteFalso({"uno": payload_simple, "dos": payload_simple}))

    cliente = ClienteFalso({"uno": payload_simple, "dos": payload_simple})
    ejecutar(entorno, cliente, force=True)

    assert cliente.pedidos == ["uno", "dos"]


def test_cache_vale_dentro_de_la_hora(
    tmp_path: Path, payload_simple: dict
) -> None:
    entorno = entorno_base(tmp_path)
    ejecutar(entorno, ClienteFalso({"uno": payload_simple, "dos": payload_simple}))

    cliente = ClienteFalso({})
    ejecutar(entorno, cliente, reloj=lambda: AHORA + timedelta(minutes=59))

    assert cliente.pedidos == []


def test_cache_caduca_pasada_la_hora(
    tmp_path: Path, payload_simple: dict
) -> None:
    entorno = entorno_base(tmp_path)
    ejecutar(entorno, ClienteFalso({"uno": payload_simple, "dos": payload_simple}))

    cliente = ClienteFalso({"uno": payload_simple, "dos": payload_simple})
    ejecutar(entorno, cliente, reloj=lambda: AHORA + timedelta(minutes=61))

    assert cliente.pedidos == ["uno", "dos"]


# ----------------------------------------------------------------------
# Fallos
# ----------------------------------------------------------------------
def test_fallo_conserva_archivos_y_no_bloquea_al_resto(
    tmp_path: Path, payload_simple: dict
) -> None:
    entorno = entorno_base(tmp_path)
    ejecutar(entorno, ClienteFalso({"uno": payload_simple, "dos": payload_simple}))
    antes = (entorno.datos / "series" / f"{ID_UNO}.json").read_text(encoding="utf-8")

    cliente = ClienteFalso(
        {"uno": EvacaError("el trader no existe"), "dos": payload_simple}
    )
    codigo = ejecutar(entorno, cliente, force=True)

    assert codigo == 1
    assert (entorno.datos / "series" / f"{ID_UNO}.json").read_text(
        encoding="utf-8"
    ) == antes

    estado = leer_json(entorno.datos / "state.json")
    assert estado["results"][ID_UNO]["status"] == "error"
    assert "no existe" in estado["results"][ID_UNO]["error"]
    assert estado["results"][ID_DOS]["status"] == "ok"


def test_trader_sin_datos_no_rompe_la_ejecucion(
    tmp_path: Path, payload_no_data: dict
) -> None:
    entorno = entorno_base(tmp_path)

    codigo = ejecutar(
        entorno, ClienteFalso({"uno": payload_no_data}), usuarios=["uno"]
    )

    assert codigo == 1
    assert not (entorno.datos / "series" / f"{ID_UNO}.json").exists()
    estado = leer_json(entorno.datos / "state.json")
    assert estado["results"][ID_UNO]["status"] == "error"


# ----------------------------------------------------------------------
# Altas y bajas en la lista
# ----------------------------------------------------------------------
def test_trader_eliminado_borra_sus_archivos(
    tmp_path: Path, payload_simple: dict
) -> None:
    entorno = entorno_base(tmp_path)
    ejecutar(entorno, ClienteFalso({"uno": payload_simple, "dos": payload_simple}))
    assert (entorno.datos / "series" / f"{ID_DOS}.json").exists()

    codigo = ejecutar(entorno, ClienteFalso({}), usuarios=["uno"])

    assert codigo == 0
    assert (entorno.datos / "series" / f"{ID_UNO}.json").exists()
    assert not (entorno.datos / "series" / f"{ID_DOS}.json").exists()
    assert not (entorno.datos / "prices" / ID_DOS).exists()
    assert ID_DOS not in leer_json(entorno.datos / "state.json")["results"]


def test_lista_vacia_no_borra_nada(tmp_path: Path, payload_simple: dict) -> None:
    entorno = entorno_base(tmp_path)
    ejecutar(entorno, ClienteFalso({"uno": payload_simple, "dos": payload_simple}))
    cliente = ClienteFalso({})

    codigo = ejecutar(entorno, cliente, usuarios=[])

    assert codigo == 1
    assert cliente.pedidos == []
    assert (entorno.datos / "series" / f"{ID_UNO}.json").exists()
    assert (entorno.datos / "series" / f"{ID_DOS}.json").exists()


def test_solo_procesa_el_trader_indicado(
    tmp_path: Path, payload_simple: dict
) -> None:
    entorno = entorno_base(tmp_path)
    ejecutar(entorno, ClienteFalso({"uno": payload_simple, "dos": payload_simple}))

    cliente = ClienteFalso({"dos": payload_simple})
    codigo = ejecutar(entorno, cliente, usuario="dos", force=True)

    assert codigo == 0
    assert cliente.pedidos == ["dos"]
    assert (entorno.datos / "series" / f"{ID_UNO}.json").exists()
    estado = leer_json(entorno.datos / "state.json")
    assert set(estado["results"]) == {ID_UNO, ID_DOS}


def test_trader_indicado_que_no_esta_activo_es_error(tmp_path: Path) -> None:
    entorno = entorno_base(tmp_path)
    cliente = ClienteFalso({})

    codigo = ejecutar(entorno, cliente, usuario="tres")

    assert codigo == 1
    assert cliente.pedidos == []


# ----------------------------------------------------------------------
# Modo ensayo
# ----------------------------------------------------------------------
def test_dry_run_no_escribe_ni_pide(tmp_path: Path) -> None:
    entorno = entorno_base(tmp_path)
    cliente = ClienteFalso({})

    codigo = ejecutar(entorno, cliente, dry_run=True)

    assert codigo == 0
    assert cliente.pedidos == []
    assert list((entorno.datos / "series").glob("*.json")) == []
    assert serie_store.load_state(entorno.datos / "state.json") == {
        "last_run": None,
        "results": {},
    }


def test_dry_run_con_datos_guardados_no_modifica_nada(
    tmp_path: Path, payload_simple: dict
) -> None:
    entorno = entorno_base(tmp_path)
    ejecutar(entorno, ClienteFalso({"uno": payload_simple, "dos": payload_simple}))
    antes = (entorno.datos / "series" / f"{ID_UNO}.json").read_text(encoding="utf-8")
    antes_html = (entorno.datos / "prices" / ID_UNO).read_text(encoding="utf-8")

    cliente = ClienteFalso({})
    codigo = ejecutar(entorno, cliente, dry_run=True, force=True)

    assert codigo == 0
    assert cliente.pedidos == []
    assert (entorno.datos / "series" / f"{ID_UNO}.json").read_text(
        encoding="utf-8"
    ) == antes
    assert (entorno.datos / "prices" / ID_UNO).read_text(
        encoding="utf-8"
    ) == antes_html


# ----------------------------------------------------------------------
# Mes en curso
# ----------------------------------------------------------------------
def test_el_mes_en_curso_escribe_el_dia_de_hoy(
    tmp_path: Path, payload_simple: dict
) -> None:
    entorno = entorno_base(tmp_path)
    payload = dict(payload_simple)
    payload["monthly"] = [
        *payload_simple["monthly"],
        {"start": "2026-11-01T00:00:00Z", "gain": 2.51, "isSimulation": False},
    ]

    codigo = ejecutar(entorno, ClienteFalso({"uno": payload}), usuarios=["uno"])

    assert codigo == 0
    html = (entorno.datos / "prices" / ID_UNO).read_text(encoding="utf-8")
    fila = fila_html(html, "2026-11-02")
    assert 'data-kind=   "D"' in fila
    assert rentab_html(fila) == "2.51"
    assert "116.5355" in fila

    datos = leer_json(entorno.datos / "series" / f"{ID_UNO}.json")
    assert datos["history"][-1] == {
        "date": "2026-11-02",
        "close": 116.5355,
        "kind": "day",
    }


# ----------------------------------------------------------------------
# Ancla del día 1: el HTML tiene un punto al empezar cada mes
# ----------------------------------------------------------------------
def test_el_html_lleva_ancla_el_dia_1_de_cada_mes(
    tmp_path: Path, payload_simple: dict
) -> None:
    entorno = entorno_base(tmp_path)

    ejecutar(entorno, ClienteFalso({"uno": payload_simple, "dos": payload_simple}))

    html = (entorno.datos / "prices" / ID_UNO).read_text(encoding="utf-8")
    datos = leer_json(entorno.datos / "series" / f"{ID_UNO}.json")

    fila_abril = fila_html(html, "2025-04-01")
    fila_mayo = fila_html(html, "2025-05-01")

    assert 'data-kind=   "A"' in fila_abril
    assert "100.0000" in fila_abril
    assert 'data-kind=   "A"' in fila_mayo
    assert "114.9000" in fila_mayo
    assert [fila["kind"] for fila in datos["history"]] == [
        "base",
        "anchor",
        "month",
        "anchor",
        "month",
        "anchor",
        "month",
    ]


def test_el_html_lleva_rejilla_diaria_y_el_json_no(
    tmp_path: Path, payload_simple: dict
) -> None:
    entorno = entorno_base(tmp_path)

    ejecutar(entorno, ClienteFalso({"uno": payload_simple, "dos": payload_simple}))

    html = (entorno.datos / "prices" / ID_UNO).read_text(encoding="utf-8")
    datos = leer_json(entorno.datos / "series" / f"{ID_UNO}.json")

    # 2025-04-15 es relleno: está en el HTML y no en el histórico.
    fila_relleno = fila_html(html, "2025-04-15")
    assert 'data-kind=   "A"' in fila_relleno
    assert all(fila["date"] != "2025-04-15" for fila in datos["history"])
    assert {fila["kind"] for fila in datos["history"]} <= {
        "base",
        "anchor",
        "month",
        "day",
    }
    # Del 31 de marzo al 30 de junio de 2025, todos los días.
    assert html.count("<tr data-kind=") == 92


def test_el_json_sigue_ascendente_y_el_html_baja(
    tmp_path: Path, payload_simple: dict
) -> None:
    entorno = entorno_base(tmp_path)

    ejecutar(entorno, ClienteFalso({"uno": payload_simple, "dos": payload_simple}))

    html = (entorno.datos / "prices" / ID_UNO).read_text(encoding="utf-8")
    datos = leer_json(entorno.datos / "series" / f"{ID_UNO}.json")

    fechas_json = [fila["date"] for fila in datos["history"]]
    assert fechas_json == sorted(fechas_json)

    fechas_html = [
        linea.split("<td>")[1].split("</td>")[0].strip()
        for linea in html.splitlines()
        if linea.strip().startswith("<tr data-kind=")
    ]
    assert fechas_html == sorted(fechas_html, reverse=True)


def test_los_dias_reales_se_conservan_al_cerrar_el_mes(
    tmp_path: Path, payload_simple: dict
) -> None:
    entorno = entorno_base(tmp_path)
    octubre_abierto = {
        **payload_simple,
        "monthly": [
            *payload_simple["monthly"],
            {"start": "2026-10-01T00:00:00Z", "gain": 2.5, "isSimulation": False},
        ],
    }
    octubre_cerrado = {
        **payload_simple,
        "monthly": [
            *payload_simple["monthly"],
            {"start": "2026-10-01T00:00:00Z", "gain": 5.0, "isSimulation": False},
            {"start": "2026-11-01T00:00:00Z", "gain": 1.0, "isSimulation": False},
        ],
    }

    ejecutar(
        entorno,
        ClienteFalso({"uno": octubre_abierto}),
        usuarios=["uno"],
        reloj=lambda: datetime(2026, 10, 15, 0, 30, tzinfo=UTC),
    )
    ejecutar(
        entorno,
        ClienteFalso({"uno": octubre_cerrado}),
        usuarios=["uno"],
        reloj=lambda: datetime(2026, 11, 2, 0, 30, tzinfo=UTC),
    )

    datos = leer_json(entorno.datos / "series" / f"{ID_UNO}.json")
    tipos = {fila["date"]: fila["kind"] for fila in datos["history"]}

    assert tipos["2026-10-15"] == "day"
    assert tipos["2026-10-31"] == "month"

    capturado = next(
        fila for fila in datos["history"] if fila["date"] == "2026-10-15"
    )
    html = (entorno.datos / "prices" / ID_UNO).read_text(encoding="utf-8")

    # El día capturado se publica tal cual: la rejilla no lo sustituye.
    cierre = f"{capturado['close']:.4f}"
    fila_dia = fila_html(html, "2026-10-15")
    assert 'data-kind=   "D"' in fila_dia
    assert rentab_html(fila_dia) == "2.50"
    assert cierre in fila_dia
    # Y el mes cerrado sigue completo, con su ancla del día 1.
    fila_ancla = fila_html(html, "2026-10-01")
    assert 'data-kind=   "A"' in fila_ancla
    fila_mes = fila_html(html, "2026-10-31")
    assert 'data-kind=   "M"' in fila_mes
    assert rentab_html(fila_mes) == "5.00"


# ----------------------------------------------------------------------
# Privacidad: el nombre del trader no sale del repositorio privado
# ----------------------------------------------------------------------
def test_ningun_fichero_generado_lleva_el_nombre(
    tmp_path: Path, payload_simple: dict
) -> None:
    entorno = entorno_base(tmp_path)

    codigo = ejecutar(
        entorno,
        ClienteFalso({"TraderSecreto": payload_simple}),
        usuarios=["TraderSecreto"],
    )

    assert codigo == 0
    for ruta in entorno.datos.rglob("*"):
        if not ruta.is_file():
            continue
        assert "tradersecreto" not in ruta.name.lower()
        assert "tradersecreto" not in ruta.read_text(encoding="utf-8").lower()


def test_el_log_no_lleva_el_nombre(
    tmp_path: Path, payload_simple: dict
) -> None:
    entorno = entorno_base(tmp_path)
    registro = Registro()

    ejecutar(
        entorno,
        ClienteFalso({"TraderSecreto": payload_simple}),
        usuarios=["TraderSecreto"],
        logger=registro.logger,
    )

    assert registro.mensajes
    assert all("tradersecreto" not in mensaje.lower() for mensaje in registro.mensajes)


def test_el_error_guardado_no_lleva_el_nombre(tmp_path: Path) -> None:
    entorno = entorno_base(tmp_path)
    fallo = EvacaError(
        "HTTP 500 en /sapi/userstats/gain/username/TraderSecreto/history"
    )

    ejecutar(entorno, ClienteFalso({"TraderSecreto": fallo}), usuarios=["TraderSecreto"])

    estado = (entorno.datos / "state.json").read_text(encoding="utf-8")
    assert "TraderSecreto" not in estado
    assert serie_id("TraderSecreto") in estado
