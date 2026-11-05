"""Lectura y escritura de los ficheros de las series.

Todas las escrituras son atómicas: primero un temporal y luego `os.replace`.
Si el programa muere a mitad, nunca queda un JSON a medio escribir.

Hay tres sitios dentro de `data/`, y solo estos:

* `data/series/<id>.json` -> datos internos de cada serie (payload mensual,
  `fetched_at` e histórico). Se publica: lleva **solo el id**, nunca el nombre.
* `data/prices/<id>`      -> tabla HTML que descarga Portfolio Performance.
* `data/state.json`       -> resumen de la última pasada, con ids.

El mapa `PP-ids.md` (id, usuario y URL del feed) no se escribe aquí: es
**privado** y vive en el repositorio de estado, junto al `watchlist.md`.
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from .serie import PriceRow, serie_id

# Versión del formato de data/series/<id>.json.
DATA_FILE_VERSION = 1

SERIES_DIRNAME = "series"
PRICES_DIRNAME = "prices"
STATE_FILENAME = "state.json"
IDS_FILENAME = "PP-ids.md"

# Repositorio público del código. La URL del feed apunta al fichero HTML que
# publica el workflow, no a ninguna página del sitio que se vigila.
REPO_PUBLICO = "eVaca-Centinela"
RAMA_FEED = "main"
FEED_URL_TEMPLATE = (
    "https://raw.githubusercontent.com/{owner}/{repo}/{rama}/data/prices/{id}"
)

# El dueño y el repositorio se pueden forzar con EVACA_FEED_OWNER y
# EVACA_FEED_REPO. Si no, en GitHub Actions salen de GITHUB_REPOSITORY.
OWNER_POR_DEFECTO = "kitagrasa"
VARIABLE_OWNER = "EVACA_FEED_OWNER"
VARIABLE_REPO = "EVACA_FEED_REPO"

MARCA_RETIRADO = "(retirado)"


# ---------------------------------------------------------------------- #
# Tiempo
# ---------------------------------------------------------------------- #
def utc_now_iso(momento: datetime) -> str:
    """Convierte un instante en texto UTC ISO 8601, como en los ejemplos."""
    return momento.astimezone(UTC).isoformat(timespec="seconds")


def parse_iso(valor: Any) -> datetime | None:
    """Convierte texto ISO 8601 en datetime con zona; None si no vale."""
    if not isinstance(valor, str) or not valor:
        return None
    try:
        momento = datetime.fromisoformat(valor.replace("Z", "+00:00"))
    except ValueError:
        return None
    if momento.tzinfo is None:
        momento = momento.replace(tzinfo=UTC)
    return momento


# ---------------------------------------------------------------------- #
# Lectura y escritura
# ---------------------------------------------------------------------- #
def read_json(ruta: Path, default: Any = None) -> Any:
    """Lee un JSON; si no existe o está roto, devuelve el valor por defecto."""
    try:
        texto = ruta.read_text(encoding="utf-8")
    except FileNotFoundError:
        return default
    try:
        return json.loads(texto)
    except json.JSONDecodeError:
        return default


def write_text_atomic(ruta: Path, texto: str) -> None:
    """Escribe texto con temporal + os.replace en el mismo directorio."""
    ruta.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporal = tempfile.mkstemp(
        dir=ruta.parent, prefix=f".{ruta.name}.", suffix=".tmp"
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as manejador:
            manejador.write(texto)
            manejador.flush()
            os.fsync(manejador.fileno())
        os.replace(temporal, ruta)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(temporal)
        raise


def write_json_atomic(ruta: Path, datos: Any) -> None:
    """Escribe un JSON legible con escritura atómica."""
    texto = json.dumps(datos, ensure_ascii=False, indent=2) + "\n"
    write_text_atomic(ruta, texto)


# ---------------------------------------------------------------------- #
# Rutas de data/
# ---------------------------------------------------------------------- #
def series_dir(data_dir: str | os.PathLike[str]) -> Path:
    return Path(data_dir) / SERIES_DIRNAME


def prices_dir(data_dir: str | os.PathLike[str]) -> Path:
    return Path(data_dir) / PRICES_DIRNAME


def serie_path(data_dir: str | os.PathLike[str], serie: str) -> Path:
    return series_dir(data_dir) / f"{serie}.json"


def price_path(data_dir: str | os.PathLike[str], serie: str) -> Path:
    return prices_dir(data_dir) / serie


def state_path(data_dir: str | os.PathLike[str]) -> Path:
    return Path(data_dir) / STATE_FILENAME


# ---------------------------------------------------------------------- #
# Una serie
# ---------------------------------------------------------------------- #
class SerieData:
    """Contenido completo de `data/series/<id>.json`.

    `serie` es el id opaco. No hay ningún campo con el nombre del trader: si
    lo hubiera, se publicaría con el repositorio.
    """

    def __init__(
        self,
        serie: str,
        version: int = DATA_FILE_VERSION,
        updated_at: str | None = None,
        fetched_at: str | None = None,
        last_payload: dict[str, Any] | None = None,
        history: list[PriceRow] | None = None,
    ) -> None:
        self.serie = serie
        self.version = version
        self.updated_at = updated_at
        self.fetched_at = fetched_at
        self.last_payload = last_payload
        self.history = list(history or [])

    def to_dict(self) -> dict[str, Any]:
        """Serializa el archivo completo de la serie.

        Además de ordenar, elimina duplicados por fecha: el histórico nunca
        debe guardarse con dos filas del mismo día (gana la última).
        """
        por_fecha = {fila.date: fila for fila in self.history}
        filas = [por_fecha[fecha] for fecha in sorted(por_fecha)]
        return {
            "version": self.version,
            "id": self.serie,
            "updated_at": self.updated_at,
            "fetched_at": self.fetched_at,
            "last_payload": self.last_payload,
            "history": [fila.to_dict() for fila in filas],
        }

    @classmethod
    def from_dict(cls, raw: Any, serie: str) -> "SerieData":
        """Reconstruye el archivo de la serie tolerando datos incompletos."""
        datos = cls(serie=serie)
        if not isinstance(raw, dict):
            return datos
        datos.version = int(raw.get("version") or DATA_FILE_VERSION)
        datos.updated_at = _texto(raw.get("updated_at"))
        datos.fetched_at = _texto(raw.get("fetched_at"))
        payload = raw.get("last_payload")
        datos.last_payload = payload if isinstance(payload, dict) else None
        historial = raw.get("history")
        if isinstance(historial, list):
            for fila in historial:
                convertida = PriceRow.from_dict(fila)
                if convertida is not None:
                    datos.history.append(convertida)
        return datos


def load_serie(ruta: Path, serie: str) -> SerieData:
    """Carga data/series/<id>.json; si no existe, devuelve un archivo vacío."""
    return SerieData.from_dict(read_json(ruta, default={}), serie)


def save_serie(ruta: Path, datos: SerieData) -> None:
    """Guarda data/series/<id>.json en el formato acordado."""
    write_json_atomic(ruta, datos.to_dict())


# ---------------------------------------------------------------------- #
# El resumen de la pasada
# ---------------------------------------------------------------------- #
def load_state(ruta: Path) -> dict[str, Any]:
    """Carga data/state.json con una estructura mínima garantizada."""
    datos = read_json(ruta, default={})
    if not isinstance(datos, dict):
        datos = {}
    resultados = datos.get("results")
    return {
        "last_run": datos.get("last_run"),
        "results": resultados if isinstance(resultados, dict) else {},
    }


def save_state(ruta: Path, estado: dict[str, Any]) -> None:
    """Guarda data/state.json."""
    write_json_atomic(ruta, estado)


def upsert_rows(
    existentes: list[PriceRow], nuevas: list[PriceRow]
) -> list[PriceRow]:
    """Fusiona filas por fecha: las nuevas sustituyen a las antiguas.

    Devuelve la lista ordenada y sin fechas duplicadas.
    """
    por_fecha: dict[date, PriceRow] = {fila.date: fila for fila in existentes}
    for fila in nuevas:
        por_fecha[fila.date] = fila
    return [por_fecha[fecha] for fecha in sorted(por_fecha)]


# ---------------------------------------------------------------------- #
# El mapa privado: id -> usuario -> URL del feed
# ---------------------------------------------------------------------- #
class IdRow:
    """Una fila del mapa privado.

    Se guarda la marca de retirado para no perder la fila cuando alguien sale
    de la lista: así el histórico del mapa es acumulativo y se puede volver a
    usar la misma URL si esa persona regresa.
    """

    __slots__ = ("id", "usuario", "retirado")

    def __init__(self, id: str, usuario: str, retirado: bool = False) -> None:
        self.id = id
        self.usuario = usuario
        self.retirado = retirado

    def __eq__(self, otro: object) -> bool:
        if not isinstance(otro, IdRow):
            return NotImplemented
        return (self.id, self.usuario, self.retirado) == (
            otro.id,
            otro.usuario,
            otro.retirado,
        )

    def __repr__(self) -> str:  # pragma: no cover - solo para depurar
        return f"IdRow({self.id!r}, {self.usuario!r}, retirado={self.retirado})"


def feed_owner() -> str:
    """El dueño del repositorio público, para componer la URL del feed.

    Orden: `EVACA_FEED_OWNER` (explícito), `GITHUB_REPOSITORY` (lo pone GitHub
    Actions) y, en local sin nada configurado, el dueño real del repositorio.
    """
    explicito = os.environ.get(VARIABLE_OWNER, "").strip()
    if explicito:
        return explicito
    repositorio = os.environ.get("GITHUB_REPOSITORY", "").strip()
    if "/" in repositorio:
        duenio = repositorio.split("/", 1)[0].strip()
        if duenio:
            return duenio
    return OWNER_POR_DEFECTO


def feed_repo() -> str:
    """El repositorio público de los feeds.

    Se puede forzar con `EVACA_FEED_REPO`. Si no, en GitHub Actions se usa el
    nombre del repositorio de `GITHUB_REPOSITORY`; en local, el valor por
    defecto. Así el feed sigue funcionando si el repositorio se renombra.
    """
    explicito = os.environ.get(VARIABLE_REPO, "").strip()
    if explicito:
        return explicito
    repositorio = os.environ.get("GITHUB_REPOSITORY", "").strip()
    if "/" in repositorio:
        nombre = repositorio.split("/", 1)[1].strip()
        if nombre:
            return nombre
    return REPO_PUBLICO


def feed_url(serie: str, owner: str | None = None) -> str:
    """La URL completa que se pega en Portfolio Performance."""
    return FEED_URL_TEMPLATE.format(
        owner=owner or feed_owner(),
        repo=feed_repo(),
        rama=RAMA_FEED,
        id=serie,
    )


def parse_ids_map(texto: str) -> list[IdRow]:
    """Lee las filas del mapa privado; ignora lo que no sea una fila válida."""
    filas: list[IdRow] = []
    for linea in texto.splitlines():
        linea = linea.strip()
        if not linea.startswith("|"):
            continue
        celdas = [celda.strip() for celda in linea.strip("|").split("|")]
        if len(celdas) < 3:
            continue
        identificador, usuario = celdas[0], celdas[1]
        if not identificador or identificador.lower() == "id":
            continue
        if set(identificador) <= {"-", ":", " "}:
            continue
        retirado = usuario.lower().endswith(MARCA_RETIRADO)
        if retirado:
            usuario = usuario[: -len(MARCA_RETIRADO)].strip()
        if not usuario:
            continue
        filas.append(IdRow(identificador, usuario, retirado))
    return filas


def merge_ids(previas: list[IdRow], usuarios: list[str]) -> list[IdRow]:
    """Fusiona el mapa anterior con la lista actual, sin perder filas.

    - Las filas antiguas se conservan en su orden; si su id ya no está en la
      lista, se marcan como retiradas (y su URL sigue disponible).
    - Las personas nuevas se añaden al final, en el orden de la lista.
    - Si alguien vuelve a la lista, su fila deja de estar retirada.
    """
    actuales = {serie_id(usuario): usuario for usuario in usuarios}
    filas: list[IdRow] = []
    vistos: set[str] = set()

    for fila in previas:
        if fila.id in vistos:
            continue
        vistos.add(fila.id)
        if fila.id in actuales:
            filas.append(IdRow(fila.id, actuales[fila.id], retirado=False))
        else:
            filas.append(IdRow(fila.id, fila.usuario, retirado=True))

    for usuario in usuarios:
        identificador = serie_id(usuario)
        if identificador in vistos:
            continue
        vistos.add(identificador)
        filas.append(IdRow(identificador, usuario, retirado=False))

    return filas


def render_ids_map(filas: list[IdRow], owner: str | None = None) -> str:
    """Devuelve el Markdown del mapa privado (tres columnas)."""
    lineas = [
        "# Series de eVaca: id, usuario y URL del feed",
        "",
        "> Fichero **privado**: no se sube al repositorio del código, ni al log",
        "> del workflow, ni a los datos publicados. Lo genera",
        "> `python -m evaca_monitor series --ids`; no se edita a mano.",
        "> Las filas de quien ya no está en la lista se quedan marcadas como",
        "> retiradas, con su URL, por si vuelve.",
        "",
        "| id | usuario | url |",
        "| --- | --- | --- |",
    ]
    for fila in filas:
        usuario = f"{fila.usuario} {MARCA_RETIRADO}" if fila.retirado else fila.usuario
        lineas.append(f"| {fila.id} | {usuario} | {feed_url(fila.id, owner)} |")
    return "\n".join(lineas) + "\n"


def write_ids_map(ruta: Path, filas: list[IdRow], owner: str | None = None) -> bool:
    """Escribe el mapa si ha cambiado. Devuelve True si lo ha escrito."""
    texto = render_ids_map(filas, owner)
    try:
        if ruta.read_text(encoding="utf-8") == texto:
            return False
    except FileNotFoundError:
        pass
    write_text_atomic(ruta, texto)
    return True


def load_ids_map(ruta: Path) -> list[IdRow]:
    """Lee el mapa privado; si no existe o no se puede leer, no hay filas."""
    try:
        texto = ruta.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError):
        return []
    return parse_ids_map(texto)


def _texto(valor: Any) -> str | None:
    """Devuelve el valor como texto, o None si no sirve."""
    return valor if isinstance(valor, str) and valor else None
