"""Tests de la pasada completa de las series.

Portados de eVaca-PP y ampliados con lo que cambia por diseño: el nombre del
trader nunca sale en los ficheros ni en el log (solo su id opaco), la lista es
la del monitor y el cliente es `EvacaClient`.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import UTC, date, datetime, timedelta
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

    def __init__(
        self,
        respuestas: dict[str, Any] | None = None,
        charts: dict[str, Any] | None = None,
    ) -> None:
        self.respuestas = dict(respuestas or {})
        self.charts = dict(charts or {})
        self.pedidos: list[str] = []
        self.chart_pedidos: list[tuple[str, str]] = []
        self.cerrado = False

    def get_gain_history(self, usuario: str) -> dict[str, Any]:
        self.pedidos.append(usuario)
        valor = self.respuestas.get(usuario)
        if isinstance(valor, Exception):
            raise valor
        if valor is None:
            raise AssertionError(f"petición inesperada para {usuario}")
        return valor

    def get_chart(self, usuario: str, window: str = "lastTwoYears") -> Any:
        self.chart_pedidos.append((usuario, window))
        valor = self.charts.get(usuario)
        if isinstance(valor, Exception):
            raise valor
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
    series: SeriesConfig | None = None,
    notifier: Any = None,
    **opciones: Any,
) -> int:
    """Lanza la pasada con reloj y pausa controlados."""
    pausas = pausas if pausas is not None else []
    config = Config(
        users=list(usuarios) if usuarios is not None else ["uno", "dos"],
        series=series or SeriesConfig(pause_seconds=3.0, cache_minutes=60),
        users_source=entorno.watchlist,
    )
    run = SerieRun(
        config,
        RunConfig(data_dir=entorno.datos, **opciones),
        cliente,
        clock=reloj or (lambda: AHORA),
        pause=pausas.append,
        logger=logger,
        notifier=notifier,
    )
    return run.run()


class NotificadorFalso:
    """Registra los avisos en vez de enviarlos."""

    configured = True

    def __init__(self) -> None:
        self.mensajes: list[str] = []

    def send(self, texto: str) -> bool:
        self.mensajes.append(texto)
        return True


def payload_chart(
    puntos: list[tuple[Any, float]], ventana: str = "lastTwoYears"
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


def puntos_diarios(
    desde: Any, hasta: Any, movimientos: dict[Any, float] | None = None
) -> list[tuple[Any, float]]:
    """Puntos diarios con niveles acumulados desde 100."""
    movimientos = movimientos or {}
    puntos: list[tuple[Any, float]] = []
    nivel = 100.0
    dia = desde
    while dia <= hasta:
        nivel *= movimientos.get(dia, 1.0)
        puntos.append((dia, nivel))
        dia += timedelta(days=1)
    return puntos


def payload_meses(ganancias: dict[str, float]) -> dict:
    """Payload mensual a partir de {'2025-04': 4.5, ...}."""
    monthly = []
    for clave, ganancia in ganancias.items():
        anio, mes = (int(parte) for parte in clave.split("-", 1))
        monthly.append(
            {
                "start": f"{anio:04d}-{mes:02d}-01T00:00:00Z",
                "gain": ganancia,
                "isSimulation": False,
            }
        )
    return {"monthly": monthly}


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

    assert datos["version"] == 2
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

    # Primera pasada: stats + chart de cada trader (4 peticiones reales).
    assert pausas == [3.0, 3.0, 3.0]


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


def test_la_rentabilidad_no_se_repite_pasada_la_hora(
    tmp_path: Path, payload_simple: dict
) -> None:
    """La rentabilidad es mensual: no se vuelve a pedir por pasar una hora.

    El chart sí se pide en cada pasada; la rentabilidad solo en los días 5 y
    12 (o con --force).
    """
    entorno = entorno_base(tmp_path)
    ejecutar(entorno, ClienteFalso({"uno": payload_simple, "dos": payload_simple}))

    cliente = ClienteFalso({"uno": payload_simple, "dos": payload_simple})
    ejecutar(entorno, cliente, reloj=lambda: AHORA + timedelta(minutes=61))

    assert cliente.pedidos == []
    assert cliente.chart_pedidos == [("uno", "lastTwoYears"), ("dos", "lastTwoYears")]


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
        force=True,
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


# ----------------------------------------------------------------------
# Chart diario: días 5/12 y calibración
# ----------------------------------------------------------------------
def instante(anio: int, mes: int, dia: int) -> datetime:
    return datetime(anio, mes, dia, 0, 30, tzinfo=UTC)


def test_la_rentabilidad_no_se_pide_un_dia_normal(
    tmp_path: Path, payload_simple: dict
) -> None:
    entorno = entorno_base(tmp_path)
    ejecutar(
        entorno,
        ClienteFalso({"uno": payload_simple}),
        usuarios=["uno"],
        reloj=lambda: instante(2025, 10, 2),
    )

    cliente = ClienteFalso({})
    ejecutar(
        entorno,
        cliente,
        usuarios=["uno"],
        reloj=lambda: instante(2025, 10, 3),
    )

    assert cliente.pedidos == []
    assert cliente.chart_pedidos == [("uno", "lastTwoYears")]


def test_el_dia_5_se_lee_aunque_haya_datos(
    tmp_path: Path, payload_simple: dict
) -> None:
    entorno = entorno_base(tmp_path)
    ejecutar(
        entorno,
        ClienteFalso({"uno": payload_simple}),
        usuarios=["uno"],
        reloj=lambda: instante(2025, 10, 2),
    )

    cliente = ClienteFalso({"uno": payload_simple})
    ejecutar(
        entorno,
        cliente,
        usuarios=["uno"],
        reloj=lambda: instante(2025, 10, 5),
    )

    assert cliente.pedidos == ["uno"]


def test_solo_hay_un_reintento_el_dia_6(
    tmp_path: Path,
) -> None:
    """Si el día 6 sigue sin salir el mes anterior, no se insiste el 7."""
    entorno = entorno_base(tmp_path)
    sin_septiembre = payload_meses({"2025-01": 1.0, "2025-02": 2.0})

    ejecutar(
        entorno,
        ClienteFalso({"uno": sin_septiembre}),
        usuarios=["uno"],
        reloj=lambda: instante(2025, 10, 5),
    )
    cliente = ClienteFalso({"uno": sin_septiembre})
    ejecutar(
        entorno,
        cliente,
        usuarios=["uno"],
        reloj=lambda: instante(2025, 10, 6),
    )
    assert cliente.pedidos == ["uno"]

    cliente = ClienteFalso({})
    ejecutar(
        entorno,
        cliente,
        usuarios=["uno"],
        reloj=lambda: instante(2025, 10, 7),
    )
    assert cliente.pedidos == []


def test_los_reintentos_no_aceptan_un_cierre_parcial_como_bueno(
    tmp_path: Path,
) -> None:
    """El mes que era actual cuando se leyó stats no cuenta como cierre."""
    entorno = entorno_base(tmp_path)
    parcial = payload_meses(
        {
            "2025-01": 1.0,
            "2025-02": 2.0,
            "2025-03": 3.0,
            "2025-04": 2.0,
        }
    )
    ejecutar(
        entorno,
        ClienteFalso({"uno": parcial}),
        usuarios=["uno"],
        reloj=lambda: instante(2025, 4, 15),
    )

    final = payload_meses(
        {
            "2025-01": 1.0,
            "2025-02": 2.0,
            "2025-03": 3.0,
            "2025-04": 4.5,
        }
    )
    cliente = ClienteFalso({"uno": final})
    ejecutar(
        entorno,
        cliente,
        usuarios=["uno"],
        reloj=lambda: instante(2025, 5, 6),
    )
    assert cliente.pedidos == ["uno"]

    cliente = ClienteFalso({})
    ejecutar(
        entorno,
        cliente,
        usuarios=["uno"],
        reloj=lambda: instante(2025, 5, 7),
    )
    assert cliente.pedidos == []


def test_la_lectura_de_stats_revisa_y_actualiza_los_tres_meses_anteriores(
    tmp_path: Path,
) -> None:
    """El 5 de noviembre se revisan octubre, septiembre, agosto y julio."""
    entorno = entorno_base(tmp_path)
    inicial = payload_meses(
        {
            "2025-07": 7.0,
            "2025-08": 8.0,
            "2025-09": 9.0,
            "2025-10": 10.0,
        }
    )
    ejecutar(
        entorno,
        ClienteFalso({"uno": inicial}),
        usuarios=["uno"],
        reloj=lambda: instante(2025, 10, 2),
    )
    ruta = entorno.datos / "series" / f"{ID_UNO}.json"
    antes = leer_json(ruta)
    cierre_septiembre_antes = next(
        fila["close"]
        for fila in antes["history"]
        if fila["date"] == "2025-09-30"
    )

    registro = Registro()
    corregido = payload_meses(
        {
            "2025-07": 7.0,
            "2025-08": 8.0,
            "2025-09": 9.5,
            "2025-10": 10.0,
        }
    )
    cliente = ClienteFalso({"uno": corregido})
    ejecutar(
        entorno,
        cliente,
        usuarios=["uno"],
        reloj=lambda: instante(2025, 11, 5),
        logger=registro.logger,
    )

    assert cliente.pedidos == ["uno"]
    texto = "\n".join(registro.mensajes)
    assert "revisión de stats" in texto
    assert "2025-09" in texto
    assert "cambio" in texto

    despues = leer_json(ruta)
    cierre_septiembre_despues = next(
        fila["close"]
        for fila in despues["history"]
        if fila["date"] == "2025-09-30"
    )
    assert cierre_septiembre_despues != cierre_septiembre_antes


def test_el_dia_12_revisa_tambien_el_mes_que_se_cierra(
    tmp_path: Path,
) -> None:
    """La segunda lectura sí compara el mes que se cerró el día 5."""
    entorno = entorno_base(tmp_path)
    inicial = payload_meses(
        {
            "2025-07": 1.0,
            "2025-08": 1.0,
            "2025-09": 1.0,
            "2025-10": 10.0,
            "2025-11": 2.0,
        }
    )
    ejecutar(
        entorno,
        ClienteFalso({"uno": inicial}),
        usuarios=["uno"],
        reloj=lambda: instante(2025, 11, 5),
    )
    ruta = entorno.datos / "series" / f"{ID_UNO}.json"
    antes = leer_json(ruta)
    cierre_octubre_antes = next(
        fila["close"]
        for fila in antes["history"]
        if fila["date"] == "2025-10-31"
    )

    registro = Registro()
    corregido = payload_meses(
        {
            "2025-07": 1.0,
            "2025-08": 1.0,
            "2025-09": 1.0,
            "2025-10": 10.5,
            "2025-11": 2.0,
        }
    )
    ejecutar(
        entorno,
        ClienteFalso({"uno": corregido}),
        usuarios=["uno"],
        reloj=lambda: instante(2025, 11, 12),
        logger=registro.logger,
    )

    texto = "\n".join(registro.mensajes)
    assert "2025-10" in texto
    assert "cambio" in texto
    despues = leer_json(ruta)
    cierre_octubre_despues = next(
        fila["close"]
        for fila in despues["history"]
        if fila["date"] == "2025-10-31"
    )
    assert cierre_octubre_despues != cierre_octubre_antes


def test_la_revision_no_compara_el_mes_en_curso(tmp_path: Path) -> None:
    """El MTD del mes en curso no es un cierre comparable."""
    entorno = entorno_base(tmp_path)
    inicial = payload_meses(
        {
            "2025-07": 1.0,
            "2025-08": 1.0,
            "2025-09": 1.0,
            "2025-10": 1.0,
            "2025-11": 2.0,
        }
    )
    ejecutar(
        entorno,
        ClienteFalso({"uno": inicial}),
        usuarios=["uno"],
        reloj=lambda: instante(2025, 11, 5),
    )

    registro = Registro()
    actualizado = payload_meses(
        {
            "2025-07": 1.0,
            "2025-08": 1.0,
            "2025-09": 1.0,
            "2025-10": 1.0,
            "2025-11": 3.0,
        }
    )
    ejecutar(
        entorno,
        ClienteFalso({"uno": actualizado}),
        usuarios=["uno"],
        reloj=lambda: instante(2025, 11, 12),
        logger=registro.logger,
    )

    texto = "\n".join(registro.mensajes)
    assert "coinciden" in texto
    assert "cambio" not in texto


def test_el_dia_12_se_lee_siempre(tmp_path: Path) -> None:
    entorno = entorno_base(tmp_path)
    payload = payload_meses(
        {"2025-01": 1.0, "2025-02": 2.0, "2025-09": 3.0}
    )
    ejecutar(
        entorno,
        ClienteFalso({"uno": payload}),
        usuarios=["uno"],
        reloj=lambda: instante(2025, 10, 5),
    )

    cliente = ClienteFalso({"uno": payload})
    ejecutar(
        entorno,
        cliente,
        usuarios=["uno"],
        reloj=lambda: instante(2025, 10, 12),
    )
    assert cliente.pedidos == ["uno"]

    cliente = ClienteFalso({})
    ejecutar(
        entorno,
        cliente,
        usuarios=["uno"],
        reloj=lambda: instante(2025, 10, 13),
    )
    assert cliente.pedidos == []


def test_force_lee_la_rentabilidad_cualquier_dia(
    tmp_path: Path, payload_simple: dict
) -> None:
    entorno = entorno_base(tmp_path)
    ejecutar(
        entorno,
        ClienteFalso({"uno": payload_simple}),
        usuarios=["uno"],
        reloj=lambda: instante(2025, 10, 2),
    )

    cliente = ClienteFalso({"uno": payload_simple})
    ejecutar(
        entorno,
        cliente,
        usuarios=["uno"],
        reloj=lambda: instante(2025, 10, 3),
        force=True,
    )
    assert cliente.pedidos == ["uno"]


def test_dos_pasadas_el_mismo_dia_no_repiten_stats(
    tmp_path: Path, payload_simple: dict
) -> None:
    entorno = entorno_base(tmp_path)
    ejecutar(
        entorno,
        ClienteFalso({"uno": payload_simple}),
        usuarios=["uno"],
        reloj=lambda: instante(2025, 10, 5),
    )

    cliente = ClienteFalso({})
    ejecutar(
        entorno,
        cliente,
        usuarios=["uno"],
        reloj=lambda: instante(2025, 10, 5),
    )
    assert cliente.pedidos == []


def test_un_mes_cerrado_con_chart_sale_calibrado(
    tmp_path: Path,
) -> None:
    entorno = entorno_base(tmp_path)
    payload = payload_meses({"2025-04": 4.5})
    chart = payload_chart(
        puntos_diarios(
            date(2025, 3, 31),
            date(2025, 4, 30),
            {date(2025, 4, 10): 1.02, date(2025, 4, 20): 1.02},
        )
    )
    cliente = ClienteFalso({"uno": payload}, charts={"uno": chart})

    codigo = ejecutar(
        entorno,
        cliente,
        usuarios=["uno"],
        reloj=lambda: instante(2025, 5, 2),
    )

    assert codigo == 0
    datos = leer_json(entorno.datos / "series" / f"{ID_UNO}.json")
    assert datos["version"] == 2
    assert datos["chart"]["window"] == "lastTwoYears"
    assert datos["chart_fetched_at"] == "2025-05-02T00:30:00+00:00"
    assert datos["months"]["2025-04"]["status"] == "calibrated"
    tipos = {fila["date"]: fila["kind"] for fila in datos["history"]}
    assert tipos["2025-04-01"] == "c"
    assert tipos["2025-04-29"] == "c"
    assert tipos["2025-04-30"] == "month"
    cierre = next(
        fila for fila in datos["history"] if fila["date"] == "2025-04-30"
    )
    assert cierre["close"] == 104.5

    html = (entorno.datos / "prices" / ID_UNO).read_text(encoding="utf-8")
    assert "M = Mes stats · D = Diario chart · A = Automático · C = Calibrado" in html
    assert 'data-kind=   "C"' in html
    assert 'data-kind=   "M"' in html


def test_el_mes_actual_sale_del_chart_aunque_stats_no_lo_traiga(
    tmp_path: Path,
) -> None:
    entorno = entorno_base(tmp_path)
    payload = payload_meses({"2025-04": 4.5})
    chart = payload_chart(
        puntos_diarios(
            date(2025, 3, 31),
            date(2025, 5, 1),
            {
                date(2025, 4, 10): 1.02,
                date(2025, 4, 20): 1.02,
                date(2025, 5, 1): 1.01,
            },
        )
    )

    ejecutar(
        entorno,
        ClienteFalso({"uno": payload}, charts={"uno": chart}),
        usuarios=["uno"],
        reloj=lambda: instante(2025, 5, 2),
    )

    datos = leer_json(entorno.datos / "series" / f"{ID_UNO}.json")
    tipos = {fila["date"]: fila["kind"] for fila in datos["history"]}
    assert tipos["2025-05-01"] == "day"
    # El mes en curso no se calibra ni entra en la auditoría.
    assert "2025-05" not in datos["months"]


def test_un_mes_ya_calibrado_se_conserva_cuando_el_chart_avanza(
    tmp_path: Path,
) -> None:
    """El chart avanza de mes, pero lo ya calibrado no se revierte."""
    entorno = entorno_base(tmp_path)
    payload = payload_meses(
        {
            "2025-01": 0.0,
            "2025-02": 0.0,
            "2025-03": 0.0,
            "2025-04": 0.0,
        }
    )
    completo = payload_chart(
        puntos_diarios(date(2024, 12, 31), date(2025, 4, 30))
    )
    ejecutar(
        entorno,
        ClienteFalso({"uno": payload}, charts={"uno": completo}),
        usuarios=["uno"],
        reloj=lambda: instante(2025, 5, 2),
    )
    datos = leer_json(entorno.datos / "series" / f"{ID_UNO}.json")
    assert any(
        fila["date"].startswith("2025-01") and fila["kind"] == "c"
        for fila in datos["history"]
    )
    assert "2025-01" in datos["months"]

    # El chart de la siguiente pasada ya solo llega a marzo.
    parcial = payload_chart(
        puntos_diarios(date(2025, 3, 1), date(2025, 4, 30))
    )
    ejecutar(
        entorno,
        ClienteFalso({}, charts={"uno": parcial}),
        usuarios=["uno"],
        reloj=lambda: instante(2025, 5, 3),
    )

    datos = leer_json(entorno.datos / "series" / f"{ID_UNO}.json")
    enero = [
        fila for fila in datos["history"] if fila["date"].startswith("2025-01")
    ]
    assert enero
    assert any(fila["kind"] == "c" for fila in enero)
    assert "2025-01" in datos["months"]
    assert "2025-02" in datos["months"]


def test_un_fallo_del_chart_conserva_los_dias_calibrados(
    tmp_path: Path,
) -> None:
    entorno = entorno_base(tmp_path)
    payload = payload_meses({"2025-04": 4.5})
    chart = payload_chart(
        puntos_diarios(
            date(2025, 3, 31),
            date(2025, 4, 30),
            {date(2025, 4, 10): 1.02, date(2025, 4, 20): 1.02},
        )
    )
    ejecutar(
        entorno,
        ClienteFalso({"uno": payload}, charts={"uno": chart}),
        usuarios=["uno"],
        reloj=lambda: instante(2025, 5, 2),
    )
    ruta = entorno.datos / "series" / f"{ID_UNO}.json"
    antes = leer_json(ruta)
    cuantos_c = sum(1 for fila in antes["history"] if fila["kind"] == "c")
    assert cuantos_c > 0

    # Se simula que falta el chart guardado (archivo v1) y que la red falla.
    antes["chart"] = None
    antes["chart_fetched_at"] = None
    ruta.write_text(json.dumps(antes), encoding="utf-8")

    cliente = ClienteFalso({}, charts={"uno": EvacaError("chart caído")})
    codigo = ejecutar(
        entorno,
        cliente,
        usuarios=["uno"],
        reloj=lambda: instante(2025, 5, 3),
    )

    assert codigo == 0
    despues = leer_json(ruta)
    assert sum(1 for fila in despues["history"] if fila["kind"] == "c") == cuantos_c
    assert any(fila["date"] == "2025-04-30" and fila["kind"] == "month"
               for fila in despues["history"])
    html = (entorno.datos / "prices" / ID_UNO).read_text(encoding="utf-8")
    assert 'data-kind=   "C"' in html


def test_los_dias_del_mes_en_curso_se_sustituyen_por_los_calibrados(
    tmp_path: Path,
) -> None:
    """Migración: los `D` del MTD viejo se van al llegar el chart del mes."""
    entorno = entorno_base(tmp_path)
    parcial = payload_meses(
        {
            "2025-01": 1.0,
            "2025-02": 2.0,
            "2025-03": 3.0,
            "2025-04": 2.0,
        }
    )
    ejecutar(
        entorno,
        ClienteFalso({"uno": parcial}),
        usuarios=["uno"],
        reloj=lambda: instante(2025, 4, 15),
    )
    antes = leer_json(entorno.datos / "series" / f"{ID_UNO}.json")
    assert any(
        fila["date"] == "2025-04-15" and fila["kind"] == "day"
        for fila in antes["history"]
    )

    final = payload_meses(
        {
            "2025-01": 1.0,
            "2025-02": 2.0,
            "2025-03": 3.0,
            "2025-04": 4.5,
        }
    )
    chart = payload_chart(
        puntos_diarios(
            date(2025, 3, 31),
            date(2025, 4, 30),
            {date(2025, 4, 10): 1.02, date(2025, 4, 20): 1.02},
        )
    )
    ejecutar(
        entorno,
        ClienteFalso({"uno": final}, charts={"uno": chart}),
        usuarios=["uno"],
        reloj=lambda: instante(2025, 5, 2),
        force=True,
    )

    despues = leer_json(entorno.datos / "series" / f"{ID_UNO}.json")
    abril = [
        fila for fila in despues["history"] if fila["date"].startswith("2025-04")
    ]
    assert all(fila["kind"] != "day" for fila in abril)
    assert any(fila["kind"] == "c" for fila in abril)
    assert any(
        fila["date"] == "2025-04-30" and fila["kind"] == "month"
        for fila in abril
    )


def test_antes_del_dia_5_no_se_usa_el_mes_parcial_como_cierre(
    tmp_path: Path,
) -> None:
    """El mes que era actual al leer stats no vale como cierre al cambiar de mes."""
    entorno = entorno_base(tmp_path)
    parcial = payload_meses(
        {
            "2025-01": 1.0,
            "2025-02": 2.0,
            "2025-03": 3.0,
            "2025-04": 2.0,
        }
    )
    ejecutar(
        entorno,
        ClienteFalso({"uno": parcial}),
        usuarios=["uno"],
        reloj=lambda: instante(2025, 4, 15),
    )

    # 2 de mayo: todavía no toca leer stats. No debe aparecer un cierre de
    # abril con el 2 % parcial del día 15.
    ejecutar(
        entorno,
        ClienteFalso({}),
        usuarios=["uno"],
        reloj=lambda: instante(2025, 5, 2),
    )

    datos = leer_json(entorno.datos / "series" / f"{ID_UNO}.json")
    assert not any(
        fila["date"] == "2025-04-30" and fila["kind"] == "month"
        for fila in datos["history"]
    )
    # El día capturado se conserva como dato real, sin inventar el cierre.
    assert any(
        fila["date"] == "2025-04-15" and fila["kind"] == "day"
        for fila in datos["history"]
    )


# ----------------------------------------------------------------------
# Avisos
# ----------------------------------------------------------------------
def _entorno_aviso(
    tmp_path: Path,
) -> tuple[SimpleNamespace, dict, dict]:
    """Payload y chart con un abril rechazado (stats 5, chart 4,04)."""
    entorno = entorno_base(tmp_path)
    payload = payload_meses({"2025-04": 5.0})
    chart = payload_chart(
        puntos_diarios(
            date(2025, 3, 31),
            date(2025, 4, 30),
            {date(2025, 4, 10): 1.02, date(2025, 4, 20): 1.02},
        )
    )
    return entorno, payload, chart


def test_el_aviso_sale_una_vez_y_se_guarda_por_id(
    tmp_path: Path,
) -> None:
    entorno, payload, chart = _entorno_aviso(tmp_path)
    notifier = NotificadorFalso()

    ejecutar(
        entorno,
        ClienteFalso({"uno": payload}, charts={"uno": chart}),
        usuarios=["uno"],
        reloj=lambda: instante(2025, 5, 2),
        notifier=notifier,
    )

    assert len(notifier.mensajes) == 1
    mensaje = notifier.mensajes[0]
    assert "2025-04" in mensaje
    assert "uno" in mensaje
    assert "5.00" in mensaje
    assert "4.04" in mensaje
    assert "0.96" in mensaje
    assert "Borra y vuelve a añadir el instrumento en PP" in mensaje
    assert ID_UNO in mensaje
    assert serie_store.feed_url(ID_UNO) in mensaje

    estado = leer_json(entorno.datos / "state.json")
    assert estado["alerts"][ID_UNO]["month"] == "2025-04"

    # Segunda pasada el mismo día: ni stats nuevas ni segundo aviso.
    antes = (entorno.datos / "series" / f"{ID_UNO}.json").read_text(
        encoding="utf-8"
    )
    ejecutar(
        entorno,
        ClienteFalso({}, charts={"uno": chart}),
        usuarios=["uno"],
        reloj=lambda: instante(2025, 5, 2),
        notifier=notifier,
    )
    assert len(notifier.mensajes) == 1
    despues = leer_json(entorno.datos / "series" / f"{ID_UNO}.json")
    assert len(despues["history"]) == len(json.loads(antes)["history"])


def test_el_dia_12_reavisa_si_cambia_la_rentabilidad(
    tmp_path: Path,
) -> None:
    entorno, payload, chart = _entorno_aviso(tmp_path)
    notifier = NotificadorFalso()
    ejecutar(
        entorno,
        ClienteFalso({"uno": payload}, charts={"uno": chart}),
        usuarios=["uno"],
        reloj=lambda: instante(2025, 5, 2),
        notifier=notifier,
    )
    assert len(notifier.mensajes) == 1

    # La segunda lectura del día 12 cambia el cierre de abril en 0,3 pp.
    corregido = payload_meses({"2025-04": 5.3})
    ejecutar(
        entorno,
        ClienteFalso({"uno": corregido}, charts={"uno": chart}),
        usuarios=["uno"],
        reloj=lambda: instante(2025, 5, 12),
        notifier=notifier,
    )

    assert len(notifier.mensajes) == 2
    assert "5.30" in notifier.mensajes[1]
    estado = leer_json(entorno.datos / "state.json")
    assert estado["alerts"][ID_UNO]["stats_pct"] == 5.3


def test_el_dia_12_no_reavisa_si_la_rentabilidad_no_cambia(
    tmp_path: Path,
) -> None:
    entorno, payload, chart = _entorno_aviso(tmp_path)
    notifier = NotificadorFalso()
    ejecutar(
        entorno,
        ClienteFalso({"uno": payload}, charts={"uno": chart}),
        usuarios=["uno"],
        reloj=lambda: instante(2025, 5, 2),
        notifier=notifier,
    )

    ejecutar(
        entorno,
        ClienteFalso({"uno": payload}, charts={"uno": chart}),
        usuarios=["uno"],
        reloj=lambda: instante(2025, 5, 12),
        notifier=notifier,
    )

    assert len(notifier.mensajes) == 1


def test_tres_meses_poco_fiables_añaden_la_recomendacion(
    tmp_path: Path,
) -> None:
    entorno = entorno_base(tmp_path)
    payload = payload_meses(
        {"2025-01": 5.0, "2025-02": 5.0, "2025-03": 5.0, "2025-04": 5.0}
    )
    movimientos = {}
    for mes in (1, 2, 3, 4):
        movimientos[date(2025, mes, 10)] = 1.02
        movimientos[date(2025, mes, 20)] = 1.02
    chart = payload_chart(
        puntos_diarios(date(2024, 12, 31), date(2025, 4, 30), movimientos)
    )
    notifier = NotificadorFalso()

    ejecutar(
        entorno,
        ClienteFalso({"uno": payload}, charts={"uno": chart}),
        usuarios=["uno"],
        reloj=lambda: instante(2025, 5, 2),
        notifier=notifier,
    )

    assert len(notifier.mensajes) == 1
    assert "no usar el chart" in notifier.mensajes[0].lower()
    datos = leer_json(entorno.datos / "series" / f"{ID_UNO}.json")
    poco_fiables = [
        clave
        for clave, info in datos["months"].items()
        if abs(info["stats_pct"] - info["chart_pct"]) > 0.5
    ]
    assert len(poco_fiables) >= 3
    assert all(
        info["status"] == "calibrated"
        for info in datos["months"].values()
    )


def test_dry_run_no_envia_avisos_ni_escribe(
    tmp_path: Path,
) -> None:
    entorno, payload, chart = _entorno_aviso(tmp_path)
    ejecutar(
        entorno,
        ClienteFalso({"uno": payload}, charts={"uno": chart}),
        usuarios=["uno"],
        reloj=lambda: instante(2025, 5, 2),
    )
    ruta = entorno.datos / "series" / f"{ID_UNO}.json"
    antes = ruta.read_text(encoding="utf-8")
    estado_antes = (entorno.datos / "state.json").read_text(encoding="utf-8")

    notifier = NotificadorFalso()
    cliente = ClienteFalso({})
    ejecutar(
        entorno,
        cliente,
        usuarios=["uno"],
        reloj=lambda: instante(2025, 5, 2),
        dry_run=True,
        force=True,
        notifier=notifier,
    )

    assert cliente.pedidos == []
    assert cliente.chart_pedidos == []
    assert notifier.mensajes == []
    assert ruta.read_text(encoding="utf-8") == antes
    assert (entorno.datos / "state.json").read_text(encoding="utf-8") == estado_antes


# ----------------------------------------------------------------------
# Privacidad de la nueva fuente
# ----------------------------------------------------------------------
def test_el_chart_no_publica_el_nombre_ni_la_url_del_feed(
    tmp_path: Path,
) -> None:
    entorno = entorno_base(tmp_path)
    payload = payload_meses({"2025-04": 4.5})
    chart = payload_chart(
        puntos_diarios(
            date(2025, 3, 31),
            date(2025, 4, 30),
            {date(2025, 4, 10): 1.02},
        )
    )

    ejecutar(
        entorno,
        ClienteFalso({"TraderSecreto": payload}, charts={"TraderSecreto": chart}),
        usuarios=["TraderSecreto"],
    )

    feed = serie_store.feed_url(serie_id("TraderSecreto"))
    for ruta in entorno.datos.rglob("*"):
        if not ruta.is_file():
            continue
        texto = ruta.read_text(encoding="utf-8").lower()
        assert "tradersecreto" not in texto
        assert feed.lower() not in texto
