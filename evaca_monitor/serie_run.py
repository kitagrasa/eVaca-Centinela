"""Pasada completa del subcomando `series` y regeneración del mapa de ids.

Coordina la lista privada, el endpoint, el JSON, el HTML y el resumen. Las
reglas de cálculo están en `serie.py` y el formato en `serie_html.py`; aquí no
hay ni cuentas ni detalles de HTTP.

Reglas de la pasada (las mismas que tenía eVaca-PP):

* como mucho **una petición por trader y pasada**, de uno en uno y con una
  pausa entre peticiones reales;
* **cero peticiones** si la copia guardada tiene menos de `cache_minutes`;
* un fallo no bloquea al resto: se guardan los demás y la pasada acaba en rojo;
* un trader que ya no está en la lista pierde su JSON y su HTML;
* lista vacía o inválida: se aborta **sin borrar nada**;
* `--dry-run` no toca la red ni el disco.

En el log y en `data/state.json` solo aparecen ids opacos (`serie.serie_id`) y
recuentos: el nombre del trader no puede acabar en el repositorio público.
"""

from __future__ import annotations

import logging
import os
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Optional

from . import serie_store
from .client import EvacaError
from .config import Config
from .render import en_madrid
from .serie import (
    SeriesError,
    build_series,
    daily_rows,
    parse_monthly,
    serie_id,
)
from .serie_html import render as render_html

# Raíz del repositorio del código. El mapa privado nunca se escribe aquí
# (salvo dentro de .state-repo, que es el clon del repositorio de estado).
RAIZ_CODIGO = Path(__file__).resolve().parents[1]
DIR_ESTADO = ".state-repo"


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

    client: `EvacaClient` (o un doble con `get_gain_history` en los tests).
    clock: función que devuelve el instante actual; se inyecta en los tests.
    pause: espera entre peticiones reales; se inyecta en los tests.
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
    ) -> None:
        self._config = config
        self._cfg = run_config
        self._client = client
        self._clock = clock or (lambda: datetime.now(tz=UTC))
        # En una ejecución real hay que esperar de verdad entre peticiones: la
        # pausa no es decorativa.
        self._pause = pause or time.sleep
        self._log = logger or logging.getLogger(__name__)
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
                nuevos[identificador] = self._procesar(usuario, identificador, momento)
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

        estado["last_run"] = serie_store.utc_now_iso(momento)
        estado["results"] = nuevos
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
        """Regenera `series-ids.md` (id, usuario y URL) y no pide nada.

        Es acumulativo: conserva las filas de quien ya no está en la lista,
        marcadas como retiradas. Con `--dry-run` no escribe el mapa.
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
                "(por ejemplo, .state-repo/series-ids.md).",
                ruta,
            )
            return 1

        filas = serie_store.merge_ids(serie_store.load_ids_map(ruta), usuarios)
        retiradas = sum(1 for fila in filas if fila.retirado)

        if imprimir:
            _imprimir_tabla(filas)

        if self._cfg.dry_run:
            self._log.info(
                "[dry-run] no se escribe el mapa: %d fila(s), %d retirada(s)",
                len(filas),
                retiradas,
            )
            return 0

        escrito = serie_store.write_ids_map(ruta, filas)
        self._log.info(
            "Mapa de series: %d fila(s), %d retirada(s), %s",
            len(filas),
            retiradas,
            "actualizado" if escrito else "sin cambios",
        )
        return 0

    # ------------------------------------------------------------------
    # Un trader
    # ------------------------------------------------------------------
    def _procesar(
        self, usuario: str, identificador: str, momento: datetime
    ) -> dict[str, Any]:
        """Actualiza una serie y devuelve su entrada para data/state.json."""
        directorio = self._cfg.data_dir
        ruta_datos = serie_store.serie_path(directorio, identificador)
        ruta_html = serie_store.price_path(directorio, identificador)
        datos = serie_store.load_serie(ruta_datos, identificador)
        minutos = self._config.series.cache_minutes

        if self._cfg.dry_run:
            payload = datos.last_payload
            if payload is None:
                self._log.info(
                    "%s: [dry-run] no hay datos guardados; en una ejecución "
                    "real se pediría al endpoint",
                    identificador,
                )
                return {"status": "sin_datos", "last_date": None, "error": None}
            if not self._cfg.force and self._cache_valida(datos, momento, minutos):
                self._log.info(
                    "%s: [dry-run] usaría la copia guardada; 0 peticiones",
                    identificador,
                )
            else:
                self._log.info(
                    "%s: [dry-run] pediría los datos al endpoint; 0 peticiones",
                    identificador,
                )
            origen = "dry-run"
        elif not self._cfg.force and self._cache_valida(datos, momento, minutos):
            payload = datos.last_payload
            self._log.info(
                "%s: copia guardada de hace menos de %d min; 0 peticiones",
                identificador,
                minutos,
            )
            origen = "caché"
        else:
            if self._peticiones:
                self._pause(self._config.series.pause_seconds)
            payload = self._client.get_gain_history(usuario)
            self._peticiones += 1
            self._log.info("%s: datos descargados (1 petición)", identificador)
            origen = "red"

        if payload is None:
            raise SeriesError(
                "no hay datos guardados y no se puede pedir al endpoint"
            )

        # A data/ solo va lo que hace falta: la lista mensual. El resto de la
        # respuesta (identificadores internos, campos que no se usan) no se
        # publica: el repositorio es público y no debe llevar nada de más.
        payload = _payload_mensual(payload)

        hoy = en_madrid(momento).date()
        meses = parse_monthly(payload)
        filas = build_series(meses, today=hoy, history=datos.history)
        # El JSON guarda los datos reales; el HTML añade, solo al vuelo, un
        # punto por cada día de los meses cerrados para que PP dibuje desde el
        # primer día de la comparativa.
        diarias = daily_rows(filas, today=hoy)

        datos.version = serie_store.DATA_FILE_VERSION
        datos.last_payload = payload
        datos.updated_at = serie_store.utc_now_iso(momento)
        if origen == "red":
            datos.fetched_at = datos.updated_at
        datos.history = filas

        if self._cfg.dry_run:
            self._log.info(
                "%s: [dry-run] guardaría %d filas (última %s) y un HTML de "
                "%d puntos",
                identificador,
                len(filas),
                filas[-1].date.isoformat(),
                len(diarias),
            )
        else:
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

        return {
            "status": "ok",
            "last_date": filas[-1].date.isoformat(),
            "error": None,
        }

    # ------------------------------------------------------------------
    # Ayudantes
    # ------------------------------------------------------------------
    def close(self) -> None:
        """Cierra la sesión HTTP del cliente, si sabe cerrarse."""
        cerrar = getattr(self._client, "close", None)
        if callable(cerrar):
            cerrar()

    def _cache_valida(
        self, datos: serie_store.SerieData, momento: datetime, minutos: int
    ) -> bool:
        """True si los datos guardados se pueden reutilizar sin pedir nada."""
        payload = datos.last_payload
        if not isinstance(payload, dict):
            return False
        if not isinstance(payload.get("monthly"), list):
            return False
        capturado = serie_store.parse_iso(datos.fetched_at)
        if capturado is None:
            return False
        if momento.tzinfo is None:
            momento = momento.replace(tzinfo=UTC)
        return momento - capturado < timedelta(minutes=max(0, minutos))

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
    if not filas:
        print("(el mapa está vacío)")
        return
    etiquetas = [
        f"{fila.usuario} {serie_store.MARCA_RETIRADO}" if fila.retirado else fila.usuario
        for fila in filas
    ]
    ancho_id = max(len("id"), *(len(fila.id) for fila in filas))
    ancho_usuario = max(len("usuario"), *(len(texto) for texto in etiquetas))
    print(f"{'id':<{ancho_id}}  {'usuario':<{ancho_usuario}}  url")
    for fila, etiqueta in zip(filas, etiquetas, strict=True):
        print(f"{fila.id:<{ancho_id}}  {etiqueta:<{ancho_usuario}}  {serie_store.feed_url(fila.id)}")
