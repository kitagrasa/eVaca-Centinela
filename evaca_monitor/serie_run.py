"""Pasada completa del subcomando `series` y regeneración del mapa de ids.

Coordina la lista privada, las dos fuentes, el JSON, el HTML y el resumen. Las
reglas de cálculo están en `serie.py` y el formato en `serie_html.py`; aquí no
hay ni cuentas ni detalles de HTTP.

Diseño de la pasada:

* **diaria**: una petición al chart por trader y ninguna a la rentabilidad
  mensual (salvo que todavía no haya datos guardados). Reconstruye el JSON y
  el HTML y recalibra los meses cerrados con el `gain` que ya está guardado;
* **mensual**: la rentabilidad se lee el día 5 (con **un** reintento el día 6
  si aún no ha salido) y otra vez el día 12, para corregir el cierre del mes
  anterior. `--force` fuerza la lectura cualquier día. Cada lectura compara
  los stats guardados con los de la web para los tres meses anteriores; la
  del día 12 también para el mes que se cierra, y reconstruye con los valores
  nuevos;
* un fallo del chart no borra lo publicado: se conservan los días `C` y `D`
  guardados; un fallo de la rentabilidad mensual no toca los ficheros de ese
  trader y se reintenta en la siguiente pasada;
* `--dry-run` no toca la red ni el disco y no envía avisos.

En el log y en `data/state.json` solo aparecen ids opacos (`serie.serie_id`) y
recuentos: el nombre del trader no puede acabar en el repositorio público.
"""

from __future__ import annotations

import html
import logging
import os
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, Optional

from . import serie_store
from .client import EvacaError
from .config import Config
from .render import en_madrid
from .serie import (
    ChartPoint,
    SeriesError,
    build_series_full,
    daily_rows,
    parse_chart,
    parse_chart_points,
    parse_monthly,
    serie_id,
)
from .serie_html import render as render_html

# Raíz del repositorio del código. El mapa privado nunca se escribe aquí
# (salvo dentro de .state-repo, que es el clon del repositorio de estado).
RAIZ_CODIGO = Path(__file__).resolve().parents[1]
DIR_ESTADO = ".state-repo"

# Cada lectura de stats revisa el mes que se cierra y los tres anteriores.
MESES_REVISION = 3


@dataclass(slots=True)
class RunConfig:
    """Dónde y cómo trabajar en una ejecución concreta."""

    data_dir: Path = Path("data")
    ids_path: Optional[Path] = None
    force: bool = False
    usuario: Optional[str] = None
    dry_run: bool = False


class SerieRun:
    """Ejecuta una actualización completa de las series.

    client: `EvacaClient` (o un doble con `get_gain_history` y `get_chart`).
    clock: función que devuelve el instante actual; se inyecta en los tests.
    pause: espera entre peticiones reales; se inyecta en los tests.
    notifier: objeto con `send(texto)`; si falta, no se envían avisos.
    """

    def __init__(
        self,
        config: Config,
        run_config: RunConfig,
        client: Any,
        *,
        clock: Optional[Callable[[], datetime]] = None,
        pause: Optional[Callable[[float], None]] = None,
        logger: Optional[logging.Logger] = None,
        notifier: Any = None,
    ) -> None:
        self._config = config
        self._cfg = run_config
        self._client = client
        self._clock = clock or (lambda: datetime.now(tz=UTC))
        # En una ejecución real hay que esperar de verdad entre peticiones: la
        # pausa no es decorativa.
        self._pause = pause or time.sleep
        self._log = logger or logging.getLogger(__name__)
        self._notifier = notifier
        self._peticiones = 0

    # ------------------------------------------------------------------
    # Ejecución completa
    # ------------------------------------------------------------------
    def run(self) -> int:
        """Procesa la lista y devuelve el código de salida (0 o 1)."""
        momento = self._clock()
        usuarios = list(self._config.users)
        if not usuarios:
            self._log.error(
                "La lista de seguimiento no tiene ningún trader activo; "
                "no se borra nada"
            )
            return 1

        activos = self._activos(usuarios)
        if activos is None:
            return 1

        directorio = self._cfg.data_dir
        estado = serie_store.load_state(serie_store.state_path(directorio))
        previos: dict[str, Any] = dict(estado.get("results") or {})
        avisos: dict[str, Any] = dict(estado.get("alerts") or {})
        borrar = self._a_borrar(activos, previos)

        # Con --usuario se conservan los resultados de los demás traders.
        nuevos: dict[str, Any] = {}
        if self._cfg.usuario is not None:
            nuevos.update(
                {
                    clave: valor
                    for clave, valor in previos.items()
                    if isinstance(valor, dict)
                }
            )

        fallos = 0
        for usuario in activos:
            identificador = serie_id(usuario)
            try:
                nuevos[identificador] = self._procesar(
                    usuario, identificador, momento, avisos
                )
            except (EvacaError, SeriesError) as exc:
                mensaje = _sin_nombres(str(exc), usuario, identificador)
                self._log.error("%s: %s", identificador, mensaje)
                nuevos[identificador] = {
                    "status": "error",
                    "last_date": None,
                    "error": mensaje,
                }
                fallos += 1

        if self._cfg.dry_run:
            self._log.info(
                "[dry-run] no se ha escrito nada: %d serie(s) revisadas, "
                "%d fallo(s), %d petición(es)",
                len(activos),
                fallos,
                self._peticiones,
            )
            return 0 if fallos == 0 else 1

        for identificador in borrar:
            self._borrar(identificador)
            avisos.pop(identificador, None)

        estado["last_run"] = serie_store.utc_now_iso(momento)
        estado["results"] = nuevos
        estado["alerts"] = avisos
        serie_store.save_state(serie_store.state_path(directorio), estado)
        self._log.info(
            "Resumen: %d serie(s), %d petición(es), %d fallo(s)",
            len(activos),
            self._peticiones,
            fallos,
        )
        return 0 if fallos == 0 else 1

    # ------------------------------------------------------------------
    # El mapa privado de ids
    # ------------------------------------------------------------------
    def run_ids(self, *, imprimir: bool = True) -> int:
        """Regenera `PP-ids.md` (id, usuario y URL) y no pide nada.

        Solo incluye a quien está activo en la lista: al borrar un trader, su
        fila desaparece del mapa. Con `--dry-run` no escribe el mapa.
        """
        usuarios = list(self._config.users)
        if not usuarios:
            self._log.error(
                "La lista de seguimiento no tiene ningún trader activo; "
                "no se escribe el mapa"
            )
            return 1

        ruta = self._ruta_ids()
        if _dentro_del_repositorio(ruta):
            self._log.error(
                "El mapa de ids es privado y no se puede escribir dentro del "
                "repositorio público (%s). Ponlo en el repositorio de estado "
                "(por ejemplo, .state-repo/PP-ids.md).",
                ruta,
            )
            return 1

        filas = serie_store.merge_ids(serie_store.load_ids_map(ruta), usuarios)

        if imprimir:
            _imprimir_tabla(filas)

        if self._cfg.dry_run:
            self._log.info(
                "[dry-run] no se escribe el mapa: %d fila(s)",
                len(filas),
            )
            return 0

        escrito = serie_store.write_ids_map(ruta, filas)
        self._log.info(
            "Mapa de series: %d fila(s), %s",
            len(filas),
            "actualizado" if escrito else "sin cambios",
        )
        return 0

    # ------------------------------------------------------------------
    # Un trader
    # ------------------------------------------------------------------
    def _procesar(
        self,
        usuario: str,
        identificador: str,
        momento: datetime,
        avisos: dict[str, Any],
    ) -> dict[str, Any]:
        """Actualiza una serie y devuelve su entrada para data/state.json."""
        directorio = self._cfg.data_dir
        ruta_datos = serie_store.serie_path(directorio, identificador)
        ruta_html = serie_store.price_path(directorio, identificador)
        datos = serie_store.load_serie(ruta_datos, identificador)
        hoy = en_madrid(momento).date()
        marca = serie_store.utc_now_iso(momento)

        payload = datos.last_payload
        payload_anterior = datos.last_payload
        stats_pedidas = False
        chart_ok = False
        chart_points: list[ChartPoint] | None = None

        if self._cfg.dry_run:
            if not _payload_valido(payload):
                self._log.info(
                    "%s: [dry-run] no hay datos guardados; en una ejecución "
                    "real se pediría la rentabilidad mensual y el chart",
                    identificador,
                )
                return {"status": "sin_datos", "last_date": None, "error": None}
            chart_points = _chart_from_stored(datos)
            self._log.info(
                "%s: [dry-run] usaría los datos guardados; 0 peticiones",
                identificador,
            )
            nuevo_fetched = datos.fetched_at
        else:
            if self._debe_pedir_stats(datos, hoy):
                payload = self._pedir_stats(usuario, identificador)
                stats_pedidas = True
                incluir_objetivo = (
                    hoy.day == self._config.series.stats_days[-1]
                    or self._cfg.force
                )
                self._revisar_stats(
                    identificador,
                    payload_anterior,
                    payload,
                    hoy,
                    incluir_objetivo=incluir_objetivo,
                )
            if not _payload_valido(payload):
                raise SeriesError(
                    "no hay datos guardados y no se puede pedir al endpoint"
                )

            try:
                chart_points = self._pedir_chart(usuario, identificador)
                chart_ok = True
            except (EvacaError, SeriesError) as exc:
                mensaje = _sin_nombres(str(exc), usuario, identificador)
                self._log.warning(
                    "%s: no se pudo leer el chart (%s); se conservan los "
                    "datos guardados",
                    identificador,
                    mensaje,
                )
                chart_points = _chart_from_stored(datos)
            nuevo_fetched = marca if stats_pedidas else datos.fetched_at

        if not isinstance(payload, dict):  # pragma: no cover - ya validado
            raise SeriesError("la rentabilidad mensual no es un objeto JSON")

        meses = parse_monthly(payload)
        meses = _quitar_mes_parcial(meses, nuevo_fetched, hoy)
        construccion = build_series_full(
            meses,
            today=hoy,
            history=datos.history,
            chart=chart_points,
            checked_at=marca,
        )
        filas = construccion.rows
        auditoria = dict(datos.months)
        auditoria.update(construccion.months)
        diarias = daily_rows(filas, today=hoy)

        if self._cfg.dry_run:
            self._log.info(
                "%s: [dry-run] guardaría %d filas (última %s) y un HTML de "
                "%d puntos",
                identificador,
                len(filas),
                filas[-1].date.isoformat(),
                len(diarias),
            )
            return {
                "status": "ok",
                "last_date": filas[-1].date.isoformat(),
                "error": None,
            }

        datos.version = serie_store.DATA_FILE_VERSION
        datos.last_payload = _payload_mensual(payload)
        datos.updated_at = marca
        if stats_pedidas:
            datos.fetched_at = marca
        if chart_ok:
            datos.chart = _chart_data(
                chart_points,
                marca,
                self._config.series.chart_window,
            )
            datos.chart_fetched_at = marca
        datos.months = auditoria
        datos.history = filas

        serie_store.save_serie(ruta_datos, datos)
        serie_store.write_text_atomic(
            ruta_html, render_html(identificador, diarias)
        )
        self._log.info(
            "%s: %d filas guardadas (última %s); HTML con %d puntos",
            identificador,
            len(filas),
            filas[-1].date.isoformat(),
            len(diarias),
        )

        self._avisar(
            usuario,
            identificador,
            hoy,
            auditoria,
            avisos,
            stats_pedidas=stats_pedidas,
        )
        return {
            "status": "ok",
            "last_date": filas[-1].date.isoformat(),
            "error": None,
        }

    # ------------------------------------------------------------------
    # Peticiones
    # ------------------------------------------------------------------
    def _pedir_stats(self, usuario: str, identificador: str) -> dict[str, Any]:
        """Pide la rentabilidad mensual (días 5/12, reintentos o --force)."""
        self._preparar_peticion()
        try:
            payload = self._client.get_gain_history(usuario)
        finally:
            self._peticiones += 1
        self._log.info("%s: rentabilidad mensual descargada (1 petición)", identificador)
        return payload

    def _pedir_chart(
        self, usuario: str, identificador: str
    ) -> list[ChartPoint]:
        """Pide y valida el chart diario (1 petición por trader y pasada)."""
        self._preparar_peticion()
        try:
            payload = self._client.get_chart(
                usuario, self._config.series.chart_window
            )
        finally:
            self._peticiones += 1
        self._log.info("%s: chart descargado (1 petición)", identificador)
        return parse_chart(payload, self._config.series.chart_window)

    def _preparar_peticion(self) -> None:
        """Pausa entre peticiones reales, nunca antes de la primera."""
        if self._peticiones:
            self._pause(self._config.series.pause_seconds)

    def _debe_pedir_stats(
        self, datos: serie_store.SerieData, hoy: date
    ) -> bool:
        """¿Toca leer la rentabilidad mensual en esta pasada?

        Nunca a diario. Solo el día 5 (con **un** reintento el día 6 si aún no
        ha salido el mes anterior), el día 12 (segunda lectura) o con
        `--force`. Si ya se leyó hoy, no se repite (salvo `--force`), para que
        dos pasadas seguidas sean idempotentes.
        """
        if self._cfg.force:
            return True
        if _stats_leidos_hoy(datos.fetched_at, hoy):
            return False
        payload = datos.last_payload
        if not _payload_valido(payload):
            return True

        dias = self._config.series.stats_days
        primera, segunda = dias[0], dias[-1]
        if hoy.day in (primera, segunda):
            return True
        if primera < hoy.day <= primera + 1 and hoy.day < segunda:
            anterior = _mes_anterior((hoy.year, hoy.month))
            # Si el último mes guardado era el actual cuando se leyó, es un
            # MTD parcial: cuenta como que el cierre todavía no ha salido.
            if _mes_parcial(datos.fetched_at, hoy) == anterior:
                return True
            return not _tiene_mes(payload, anterior)
        return False

    def _revisar_stats(
        self,
        identificador: str,
        anterior: Any,
        nuevo: Any,
        hoy: date,
        *,
        incluir_objetivo: bool,
    ) -> None:
        """Contrasta los stats guardados con los de la web.

        La primera lectura del mes (día 5) revisa los tres meses anteriores;
        el día 12 revisa también el mes que se cierra. No cambia el cálculo:
        el payload nuevo ya manda en la reconstrucción. Solo deja en el log
        los meses que cambian y la diferencia máxima, sin nombres ni datos
        personales.
        """
        if not isinstance(anterior, dict):
            self._log.info(
                "%s: revisión de stats: no había datos guardados que comparar",
                identificador,
            )
            return
        try:
            viejos = {
                (mes.start.year, mes.start.month): mes.gain
                for mes in parse_monthly(anterior)
            }
            nuevos = {
                (mes.start.year, mes.start.month): mes.gain
                for mes in parse_monthly(nuevo)
            }
        except SeriesError as exc:
            self._log.warning(
                "%s: revisión de stats: no se pudo comparar (%s)",
                identificador,
                _sin_nombres(str(exc), "", identificador),
            )
            return

        claves = _ventana_revision(hoy, incluir_objetivo)
        comunes = [clave for clave in claves if clave in viejos and clave in nuevos]
        solo_viejos = [clave for clave in claves if clave in viejos and clave not in nuevos]
        solo_nuevos = [clave for clave in claves if clave in nuevos and clave not in viejos]
        ausentes = [clave for clave in claves if clave not in viejos and clave not in nuevos]
        cambios = [
            (clave, viejos[clave], nuevos[clave])
            for clave in comunes
            if abs(viejos[clave] - nuevos[clave]) > 1e-4
        ]
        ventana = ", ".join(_clave_mes(clave) for clave in claves)

        if not comunes and not solo_viejos and not solo_nuevos:
            self._log.info(
                "%s: revisión de stats (%s): no hay meses comparables",
                identificador,
                ventana,
            )
            return
        if not cambios and not solo_viejos and not solo_nuevos:
            self._log.info(
                "%s: revisión de stats (%s): coinciden (%d meses)",
                identificador,
                ventana,
                len(comunes),
            )
            return
        maxima = max(
            (abs(viejo - nuevo) for _, viejo, nuevo in cambios),
            default=0.0,
        )
        self._log.warning(
            "%s: revisión de stats (%s): %d cambio(s) %s; diferencia máxima "
            "%.2f pp; solo en la web: %s; solo guardados: %s; sin datos: %s",
            identificador,
            ventana,
            len(cambios),
            ", ".join(_clave_mes(clave) for clave, _, _ in cambios) or "-",
            maxima,
            ", ".join(_clave_mes(clave) for clave in solo_nuevos) or "-",
            ", ".join(_clave_mes(clave) for clave in solo_viejos) or "-",
            ", ".join(_clave_mes(clave) for clave in ausentes) or "-",
        )

    # ------------------------------------------------------------------
    # Avisos por Telegram
    # ------------------------------------------------------------------
    def _avisar(
        self,
        usuario: str,
        identificador: str,
        hoy: date,
        auditoria: dict[str, dict[str, Any]],
        avisos: dict[str, Any],
        *,
        stats_pedidas: bool,
    ) -> None:
        """Avisa del mes anterior si se desvía más de `alert_pp`.

        Como máximo hay un aviso por mes y trader en la primera detección y
        otro el día 12 si la rentabilidad mensual ha cambiado más de
        `alert_pp`. El estado solo guarda el último mes avisado por id.
        """
        if self._cfg.dry_run:
            return

        clave = _clave_mes(_mes_anterior((hoy.year, hoy.month)))
        info = auditoria.get(clave)
        if not isinstance(info, dict):
            return
        try:
            stats_pct = float(info.get("stats_pct"))
            chart_pct = float(info.get("chart_pct"))
        except (TypeError, ValueError):
            return
        diferencia = abs(stats_pct - chart_pct)
        if diferencia <= self._config.series.alert_pp:
            return

        previo = avisos.get(identificador)
        previo = previo if isinstance(previo, dict) else {}
        primera = previo.get("month") != clave
        segunda = self._config.series.stats_days[-1]
        cambio_dia12 = False
        if stats_pedidas and hoy.day == segunda and previo.get("month") == clave:
            try:
                stats_previo = float(previo.get("stats_pct"))
            except (TypeError, ValueError):
                stats_previo = stats_pct
            cambio_dia12 = (
                abs(stats_pct - stats_previo) > self._config.series.alert_pp
            )
        if not primera and not cambio_dia12:
            return

        cap_pp = self._config.series.cap_pp
        mensaje = _mensaje_aviso(
            usuario=usuario,
            identificador=identificador,
            mes=clave,
            stats_pct=stats_pct,
            chart_pct=chart_pct,
            estado=str(info.get("status") or ""),
            poco_fiable=diferencia > cap_pp,
            cap_pp=cap_pp,
            rechazados=_rechazados_ultimos_12(auditoria, hoy, cap_pp),
        )

        if not _notificador_configurado(self._notifier):
            self._log.warning(
                "%s: hay un aviso pendiente del mes %s, pero Telegram no "
                "está configurado",
                identificador,
                clave,
            )
            return
        try:
            enviado = self._notifier.send(mensaje)
        except Exception as exc:  # noqa: BLE001 - el aviso no corta la pasada
            self._log.error(
                "%s: no se pudo enviar el aviso del mes %s (%s)",
                identificador,
                clave,
                type(exc).__name__,
            )
            return
        if enviado is False:
            self._log.error(
                "%s: no se pudo enviar el aviso del mes %s",
                identificador,
                clave,
            )
            return

        avisos[identificador] = {"month": clave, "stats_pct": stats_pct}
        self._log.info("%s: aviso del mes %s enviado", identificador, clave)

    # ------------------------------------------------------------------
    # Ayudantes
    # ------------------------------------------------------------------
    def close(self) -> None:
        """Cierra la sesión HTTP del cliente, si sabe cerrarse."""
        cerrar = getattr(self._client, "close", None)
        if callable(cerrar):
            cerrar()

    def _activos(self, usuarios: list[str]) -> Optional[list[str]]:
        """Aplica --usuario, si se indicó."""
        if self._cfg.usuario is None:
            return list(usuarios)
        buscado = self._cfg.usuario.strip().lower()
        for usuario in usuarios:
            if usuario.lower() == buscado:
                return [usuario]
        self._log.error(
            "El trader indicado en --usuario no está activo en la lista (%s)",
            serie_id(self._cfg.usuario),
        )
        return None

    def _a_borrar(
        self, activos: list[str], previos: dict[str, Any]
    ) -> list[str]:
        """Series que ya no están activas y hay que quitar del repositorio."""
        if self._cfg.usuario is not None:
            return []
        actuales = {serie_id(usuario) for usuario in activos}
        directorio = self._cfg.data_dir
        existentes = {
            ruta.stem for ruta in serie_store.series_dir(directorio).glob("*.json")
        }
        existentes.update(previos)
        for ruta in serie_store.prices_dir(directorio).glob("*"):
            if ruta.is_file() and not ruta.name.startswith("."):
                existentes.add(ruta.name)
        return sorted(
            identificador
            for identificador in existentes
            if identificador not in actuales
        )

    def _borrar(self, identificador: str) -> None:
        """Borra el JSON y el HTML de una serie retirada."""
        directorio = self._cfg.data_dir
        for ruta in (
            serie_store.serie_path(directorio, identificador),
            serie_store.price_path(directorio, identificador),
        ):
            try:
                ruta.unlink()
            except FileNotFoundError:
                continue
            self._log.info("%s: eliminado %s", identificador, ruta.name)

    def _ruta_ids(self) -> Path:
        """Dónde vive el mapa privado: junto al watchlist.md de la lista."""
        if self._cfg.ids_path is not None:
            return Path(self._cfg.ids_path)
        origen = self._config.users_source
        if origen is not None and Path(origen).suffix.lower() in (".md", ".markdown"):
            return Path(origen).parent / serie_store.IDS_FILENAME
        return Path(serie_store.IDS_FILENAME)


# ---------------------------------------------------------------------- #
# Ayudas de módulo
# ---------------------------------------------------------------------- #
def en_github_actions() -> bool:
    """¿Se ejecuta dentro de GitHub Actions? El log de allí es público."""
    return os.environ.get("GITHUB_ACTIONS", "").strip().lower() == "true"


def _payload_valido(payload: Any) -> bool:
    """¿Hay una lista mensual con algún mes guardada?"""
    return (
        isinstance(payload, dict)
        and isinstance(payload.get("monthly"), list)
        and len(payload["monthly"]) > 0
    )


def _stats_leidos_hoy(fetched_at: Any, hoy: date) -> bool:
    """¿La rentabilidad mensual se leyó ya hoy (hora de Madrid)?"""
    momento = serie_store.parse_iso(fetched_at)
    return momento is not None and en_madrid(momento).date() == hoy


def _tiene_mes(payload: dict[str, Any], clave: tuple[int, int]) -> bool:
    """¿La respuesta guardada ya trae el mes indicado?"""
    for entrada in payload.get("monthly") or []:
        if not isinstance(entrada, dict):
            continue
        inicio = entrada.get("start")
        if not isinstance(inicio, str) or len(inicio) < 10:
            continue
        try:
            fecha = date.fromisoformat(inicio[:10])
        except ValueError:
            continue
        if (fecha.year, fecha.month) == clave:
            return True
    return False


def _ventana_revision(
    hoy: date, incluir_objetivo: bool = True
) -> list[tuple[int, int]]:
    """Meses que se contrastan en una lectura de stats.

    Si hoy es 5 de noviembre (primera lectura), devuelve septiembre, agosto y
    julio: el mes que se cierra (octubre) es nuevo y no hay un cierre anterior
    con el que compararlo. El día 12 se incluye también octubre, porque ya
    hubo una primera lectura y puede haber cambiado.
    """
    clave = _mes_anterior((hoy.year, hoy.month))
    claves = [clave] if incluir_objetivo else []
    for _ in range(MESES_REVISION):
        clave = _mes_anterior(clave)
        claves.append(clave)
    return claves


def _mes_parcial(
    fetched_at: Any, hoy: date
) -> tuple[int, int] | None:
    """Mes que era el actual cuando se leyó stats y que ya ha cerrado.

    Si la última lectura fue antes de que empezara el mes en curso, su último
    mes era parcial (MTD) y no puede usarse como cierre. Se espera a la
    lectura del día 5.
    """
    momento = serie_store.parse_iso(fetched_at)
    if momento is None:
        return None
    fecha = en_madrid(momento).date()
    if fecha >= hoy.replace(day=1):
        return None
    return fecha.year, fecha.month


def _quitar_mes_parcial(
    meses: list[Any], fetched_at: Any, hoy: date
) -> list[Any]:
    """Quita el mes parcial que ya ha cerrado.

    Si al quitarlo no queda ningún mes, se conserva: es preferible una serie
    corta a una vacía.
    """
    clave = _mes_parcial(fetched_at, hoy)
    if clave is None:
        return meses
    if not any(
        (mes.start.year, mes.start.month) == clave for mes in meses
    ):
        return meses
    restantes = [
        mes for mes in meses if (mes.start.year, mes.start.month) != clave
    ]
    return restantes or meses


def _chart_from_stored(datos: serie_store.SerieData) -> list[ChartPoint] | None:
    """Recupera los puntos del chart guardado; None si no hay o están rotos."""
    chart = datos.chart
    if not isinstance(chart, dict):
        return None
    try:
        return parse_chart_points(chart.get("points"))
    except SeriesError:
        return None


def _chart_data(
    points: list[ChartPoint] | None, marca: str, window: str
) -> dict[str, Any]:
    """Lo único que se guarda del chart: ventana, fecha y puntos crudos."""
    return {
        "window": window,
        "fetched_at": marca,
        "points": [
            [punto.date.isoformat(), round(punto.equity, 4)]
            for punto in (points or [])
        ],
    }


def _payload_mensual(payload: dict[str, Any]) -> dict[str, Any]:
    """Lo único que se guarda en data/: los campos usados de la lista mensual.

    El repositorio es público y en `data/` no debe haber nada de más. De cada
    mes se guardan solo `start`, `gain` y `isSimulation`: cualquier otro campo
    de la respuesta (identificadores internos, URLs...) se tira, para que no
    pueda acabar publicado sin darse cuenta.
    """
    mensual: list[dict[str, Any]] = []
    for entrada in payload.get("monthly") or []:
        if not isinstance(entrada, dict):
            continue
        limpia: dict[str, Any] = {
            "start": entrada.get("start"),
            "gain": entrada.get("gain"),
        }
        if "isSimulation" in entrada:
            limpia["isSimulation"] = bool(entrada.get("isSimulation"))
        mensual.append(limpia)
    return {"monthly": mensual}


def _mensaje_aviso(
    *,
    usuario: str,
    identificador: str,
    mes: str,
    stats_pct: float,
    chart_pct: float,
    estado: str,
    poco_fiable: bool,
    cap_pp: float,
    rechazados: int,
) -> str:
    """Texto del aviso. Lleva el nombre y la URL: solo va a Telegram."""
    nombre = html.escape(usuario)
    identificador_seguro = html.escape(identificador)
    url = html.escape(serie_store.feed_url(identificador))
    lineas = [
        "⚠️ <b>Serie descuadrada</b>",
        f"Mes: <b>{html.escape(mes)}</b>",
        f"Usuario: <b>{nombre}</b>",
        f"Stats: {stats_pct:.2f} %",
        f"Chart: {chart_pct:.2f} %",
        f"Diferencia: {abs(stats_pct - chart_pct):.2f} pp",
    ]
    if estado == "rejected":
        lineas.append(
            "El mes no se ha calibrado: se publica como cierre mensual "
            "+ relleno."
        )
    else:
        lineas.append(
            "El mes se ha calibrado para acabar en el cierre mensual de stats."
        )
    if poco_fiable:
        lineas.append(
            f"El chart se aleja más de {cap_pp:.1f} pp: puede que no "
            "represente bien al trader."
        )
    lineas.extend(
        [
            "",
            "Borra y vuelve a añadir el instrumento en PP.",
            f"id: <code>{identificador_seguro}</code>",
            f"Feed: {url}",
        ]
    )
    if rechazados >= 3:
        lineas.extend(
            [
                "",
                f"Recomendación: no usar el chart para este trader "
                f"({rechazados} meses con desviación > {cap_pp:.1f} pp en "
                f"los últimos 12).",
            ]
        )
    return "\n".join(lineas)


def _rechazados_ultimos_12(
    auditoria: dict[str, dict[str, Any]], hoy: date, cap_pp: float
) -> int:
    """Cuenta los meses con chart poco fiable en los últimos 12 meses.

    Incluye los que no se pudieron calibrar (`rejected`) y los que sí se
    calibraron pero se alejan más de `cap_pp` de stats.
    """
    total = 0
    for clave, info in auditoria.items():
        if not isinstance(info, dict):
            continue
        try:
            anio, mes = (int(parte) for parte in clave.split("-", 1))
        except (TypeError, ValueError):
            continue
        diferencia_meses = (hoy.year - anio) * 12 + (hoy.month - mes)
        if not 0 <= diferencia_meses < 12:
            continue
        if info.get("status") == "rejected":
            total += 1
            continue
        try:
            desviacion = abs(
                float(info.get("stats_pct")) - float(info.get("chart_pct"))
            )
        except (TypeError, ValueError):
            continue
        if desviacion > cap_pp:
            total += 1
    return total


def _notificador_configurado(notifier: Any) -> bool:
    """True si hay un notificador y dice estar configurado (o no lo dice)."""
    if notifier is None:
        return False
    return bool(getattr(notifier, "configured", True))


def _clave_mes(clave: tuple[int, int]) -> str:
    return f"{clave[0]:04d}-{clave[1]:02d}"


def _mes_anterior(clave: tuple[int, int]) -> tuple[int, int]:
    anio, mes = clave
    if mes == 1:
        return anio - 1, 12
    return anio, mes - 1


def _dentro_del_repositorio(ruta: Path) -> bool:
    """True si `ruta` cae en el repositorio público (fuera de .state-repo).

    El mapa lleva nombres de usuario: escribirlo por error en `data/` o en la
    raíz lo publicaría. La única excepción es el clon del repositorio de
    estado, que está en `.state-repo/` y no se sube.
    """
    try:
        relativa = ruta.resolve().relative_to(RAIZ_CODIGO)
    except (ValueError, OSError):
        return False
    partes = relativa.parts
    return not (partes and partes[0] == DIR_ESTADO)


def _sin_nombres(texto: str, usuario: str, identificador: str) -> str:
    """Cambia el nombre del trader por su id en un mensaje de error.

    Los mensajes de `requests` pueden llevar la ruta con el nombre dentro; el
    log del workflow y `data/state.json` son públicos.
    """
    if not usuario:
        return texto
    return re.sub(re.escape(usuario), identificador, texto, flags=re.IGNORECASE)


def _imprimir_tabla(filas: list[serie_store.IdRow]) -> None:
    """Imprime el mapa por pantalla (solo en una ejecución a mano)."""
    filas = [fila for fila in filas if not fila.retirado]
    if not filas:
        print("(el mapa está vacío)")
        return
    etiquetas = [fila.usuario for fila in filas]
    ancho_id = max(len("id"), *(len(fila.id) for fila in filas))
    ancho_usuario = max(len("usuario"), *(len(texto) for texto in etiquetas))
    print(f"{'id':<{ancho_id}}  {'usuario':<{ancho_usuario}}  url")
    for fila, etiqueta in zip(filas, etiquetas, strict=True):
        print(f"{fila.id:<{ancho_id}}  {etiqueta:<{ancho_usuario}}  {serie_store.feed_url(fila.id)}")
