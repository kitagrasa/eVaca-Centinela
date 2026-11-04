"""Cálculo puro de la serie tipo fondo con índice base 100.

Funciones sin acceso a disco ni a red: reciben la respuesta ya descargada y
devuelven las filas ordenadas. Así son fáciles de probar.

El JSON guarda los datos reales: la base, el ancla del día 1 de cada mes, el
cierre real de cada mes y los días que se capturaron. Encima, para el HTML,
`daily_rows` añade un punto por cada día de los meses cerrados (interpolado),
sin tocar nunca un día real.

El ancla del día 1 existe porque Portfolio Performance empieza a dibujar cada
serie en su primera cotización del periodo: sin un punto el día 1, el primer
mes de cualquier comparativa (YTD, 1 año, 3 años) salía vacío hasta el cierre.

Ningún dato de este módulo lleva el nombre del trader: todo lo que se publica
se nombra con `serie_id(usuario)`, un identificador opaco y estable.
"""

from __future__ import annotations

import calendar
import hashlib
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

# Tipos de fila admitidos en el histórico.
KIND_BASE = "base"
KIND_MONTH = "month"
KIND_DAY = "day"
KIND_ANCHOR = "anchor"
KINDS = (KIND_BASE, KIND_MONTH, KIND_DAY, KIND_ANCHOR)

# Tipo transitorio: punto diario interpolado de un mes ya cerrado. Solo existe
# mientras se prepara el HTML; nunca se guarda en el histórico (por eso no está
# en KINDS y PriceRow.from_dict lo rechaza).
KIND_FILL = "fill"

# Cien: valor inicial del índice.
BASE_VALUE = 100.0

# Prefijo del hash. No es decorativo: separa estos ids de cualquier otro hash
# que se pudiera calcular con el mismo nombre de usuario.
PREFIJO_ID = "evaca:"
LONGITUD_ID = 12


class SeriesError(Exception):
    """El histórico no permite construir una serie válida."""


@dataclass(frozen=True, slots=True)
class MonthGain:
    """Rentabilidad de un mes según la respuesta del endpoint.

    `start` es el día de inicio del mes (el primero, salvo el mes de alta) y
    `gain` la rentabilidad de ese mes en porcentaje.
    """

    start: date
    gain: float
    is_simulation: bool = False


@dataclass(frozen=True, slots=True)
class PriceRow:
    """Una fila del histórico: fecha, índice de cierre y tipo de fila.

    `gain` es la rentabilidad original (%) que originó la fila. Solo se usa
    para el HTML (atributo `rentab`) y no se guarda en JSON: las filas que
    vienen de un archivo antiguo lo reconstruyen a partir del cierre.
    """

    date: date
    close: float
    kind: str
    gain: float | None = field(default=None, compare=False, repr=False)

    def to_dict(self) -> dict[str, Any]:
        """Convierte la fila al formato que se guarda en JSON."""
        return {
            "date": self.date.isoformat(),
            "close": round(self.close, 4),
            "kind": self.kind,
        }

    @classmethod
    def from_dict(cls, raw: Any) -> PriceRow | None:
        """Crea una fila desde JSON; devuelve None si el dato está roto."""
        if not isinstance(raw, dict):
            return None
        try:
            fecha = date.fromisoformat(str(raw["date"]))
            cierre = float(raw["close"])
            tipo = str(raw["kind"])
        except (KeyError, TypeError, ValueError):
            return None
        if tipo not in KINDS or not cierre > 0:
            return None
        return cls(date=fecha, close=round(cierre, 4), kind=tipo)


def serie_id(usuario: str) -> str:
    """Identificador opaco y estable de un trader: 12 hex de sha256.

    `serie_id(u) = sha256("evaca:" + u.strip().lower()).hexdigest()[:12]`.

    Todo lo que se publica (`data/series/<id>.json`, `data/prices/<id>` y el
    resumen de `data/state.json`) se nombra con este id: el nombre del trader
    solo existe en la lista privada y en el mapa privado (`series-ids.md`).

    Es determinista a propósito: el mismo usuario da siempre el mismo id, así
    que su serie no se pierde entre pasadas. Y no se puede leer al revés: con
    el id solo no se puede saber de quién es.
    """
    limpio = usuario.strip().lower()
    return hashlib.sha256(f"{PREFIJO_ID}{limpio}".encode("utf-8")).hexdigest()[
        :LONGITUD_ID
    ]


def parse_monthly(payload: Any) -> list[MonthGain]:
    """Extrae los meses de la respuesta del endpoint.

    Descarta las entradas incompletas en lugar de fallar: una respuesta con
    un mes roto sigue sirviendo para el resto.
    """
    if not isinstance(payload, dict):
        raise SeriesError("la respuesta del endpoint no es un objeto JSON")
    monthly = payload.get("monthly")
    if not isinstance(monthly, list):
        raise SeriesError("la respuesta del endpoint no trae la lista 'monthly'")

    puntos: list[MonthGain] = []
    for entrada in monthly:
        if not isinstance(entrada, dict):
            continue
        inicio = _parse_start(entrada.get("start"))
        ganancia = _parse_gain(entrada.get("gain"))
        if inicio is None or ganancia is None:
            continue
        puntos.append(
            MonthGain(
                start=inicio,
                gain=ganancia,
                is_simulation=bool(entrada.get("isSimulation")),
            )
        )
    return puntos


def clean_monthly(monthly: Sequence[MonthGain]) -> list[MonthGain]:
    """Filtra simulaciones, ordena, quita duplicados y el mes parcial.

    Devuelve solo meses completos, del más antiguo al más reciente.
    """
    puntos = [punto for punto in monthly if not punto.is_simulation]
    puntos.sort(key=lambda punto: punto.start)

    # Un solo punto por mes: si hay varios, gana el de fecha de inicio mayor.
    por_mes: dict[tuple[int, int], MonthGain] = {}
    for punto in puntos:
        por_mes[(punto.start.year, punto.start.month)] = punto
    puntos = [por_mes[clave] for clave in sorted(por_mes)]

    # El primer mes puede ser el mes de alta: si no empieza el día 1, se tira.
    if puntos and puntos[0].start.day != 1:
        puntos = puntos[1:]

    if not puntos:
        raise SeriesError("la respuesta no tiene ningún mes completo")
    return puntos


def build_series(
    monthly: Sequence[MonthGain],
    today: date,
    history: Iterable[PriceRow] = (),
) -> list[PriceRow]:
    """Construye la serie completa, ordenada y sin fechas duplicadas.

    monthly: meses de la respuesta (se limpian aquí dentro).
    today: fecha de hoy en Europe/Madrid.
    history: filas ya guardadas; se conservan todos los días capturados de
    verdad (de cualquier mes) que no sean de hoy en adelante.
    """
    meses = clean_monthly(monthly)
    clave_actual = (today.year, today.month)

    filas: dict[date, PriceRow] = {}

    # Fila base: un día antes del primer mes completo, con valor 100.
    fecha_base = meses[0].start - timedelta(days=1)
    filas[fecha_base] = PriceRow(date=fecha_base, close=BASE_VALUE, kind=KIND_BASE)
    cierre_previo = BASE_VALUE

    # Cada mes cerrado deja dos filas: el ancla del día 1, con el cierre del
    # mes anterior, y el cierre real del último día.
    for mes in meses:
        if (mes.start.year, mes.start.month) >= clave_actual:
            continue
        filas.setdefault(
            mes.start,
            PriceRow(date=mes.start, close=cierre_previo, kind=KIND_ANCHOR),
        )
        cierre_previo = round(cierre_previo * (1 + mes.gain / 100), 4)
        fin = _last_day(mes.start)
        filas[fin] = PriceRow(
            date=fin, close=cierre_previo, kind=KIND_MONTH, gain=mes.gain
        )

    # Mes en curso: ancla del día 1 con el cierre del último mes cerrado y un
    # punto con la rentabilidad acumulada (MTD). No se encadena día a día.
    mes_actual = next(
        (
            mes
            for mes in meses
            if (mes.start.year, mes.start.month) == clave_actual
        ),
        None,
    )
    if mes_actual is not None:
        filas.setdefault(
            mes_actual.start,
            PriceRow(date=mes_actual.start, close=cierre_previo, kind=KIND_ANCHOR),
        )
        cierre_hoy = round(cierre_previo * (1 + mes_actual.gain / 100), 4)
        filas[today] = PriceRow(
            date=today, close=cierre_hoy, kind=KIND_DAY, gain=mes_actual.gain
        )

    # Se conservan todos los días ya capturados, también los de meses
    # cerrados: son datos reales. Un día capturado manda sobre el ancla de ese
    # mismo día; el cierre de mes y la base no se tocan nunca.
    for fila in history:
        if fila.kind != KIND_DAY:
            continue
        if fila.date >= today:
            continue
        existente = filas.get(fila.date)
        if existente is not None and existente.kind in (KIND_BASE, KIND_MONTH):
            continue
        ganancia = fila.gain
        if ganancia is None:
            cierre_mes_anterior = _previous_month_close(filas, fila.date)
            if cierre_mes_anterior is not None:
                ganancia = _gain_from_close(fila.close, cierre_mes_anterior)
        filas[fila.date] = PriceRow(
            date=fila.date, close=fila.close, kind=fila.kind, gain=ganancia
        )

    return [filas[fecha] for fecha in sorted(filas)]


def daily_rows(rows: Sequence[PriceRow], today: date) -> list[PriceRow]:
    """Devuelve las filas con un punto por cada día de los meses cerrados.

    Portfolio Performance empieza a dibujar cada serie en su primera
    cotización dentro del periodo. Con dos puntos por mes (día 1 y cierre),
    las ventanas que PP ancla a mitad de mes (1 año, 3 años) seguían
    empezando en el día 1 siguiente. Esta rejilla las cubre también.

    Como la fuente solo da la rentabilidad de cada mes, el hueco se reparte
    con una tasa diaria constante entre las dos filas reales que lo rodean.
    Cada tramo va de una fila real a la siguiente, así que la línea pasa
    exactamente por todos los datos de verdad: nunca se sustituye uno.

    El mes en curso no se rellena: su MTD todavía puede cambiar y PP no
    reescribe fechas antiguas, así que esos puntos se quedarían congelados.

    Las filas inventadas llevan kind = "fill" y solo existen para el HTML:
    nunca se guardan en `data/series/<id>.json`.
    """
    ordenadas = sorted(rows, key=lambda fila: fila.date)
    if not ordenadas:
        return []

    inicio_mes_actual = today.replace(day=1)
    por_fecha: dict[date, PriceRow] = {fila.date: fila for fila in ordenadas}

    for anterior, siguiente in zip(ordenadas, ordenadas[1:], strict=False):
        if siguiente.date >= inicio_mes_actual:
            continue
        dias = (siguiente.date - anterior.date).days
        if dias < 2:
            continue
        for paso in range(1, dias):
            fecha = anterior.date + timedelta(days=paso)
            por_fecha[fecha] = PriceRow(
                date=fecha,
                close=_interpolar(anterior.close, siguiente.close, paso, dias),
                kind=KIND_FILL,
            )

    return [por_fecha[fecha] for fecha in sorted(por_fecha)]


def _parse_start(valor: Any) -> date | None:
    """Convierte '2020-03-16T14:53:03.533Z' en date(2020, 3, 16)."""
    if not isinstance(valor, str) or len(valor) < 10:
        return None
    try:
        return date.fromisoformat(valor[:10])
    except ValueError:
        return None


def _parse_gain(valor: Any) -> float | None:
    """Convierte la rentabilidad a número, descartando booleanos y basura."""
    if isinstance(valor, bool) or valor is None:
        return None
    try:
        return float(valor)
    except (TypeError, ValueError):
        return None


def _interpolar(inicio: float, fin: float, paso: int, dias: int) -> float:
    """Valor del día `paso` de un tramo de `dias`, con tasa diaria constante.

    Se interpola en logaritmo (rentabilidad compuesta diaria) para que el
    reparto sea el mismo tanto si el mes sube como si baja, y para que los
    extremos coincidan exactamente con los cierres reales.
    """
    if dias <= 0:
        return round(inicio, 4)
    if inicio > 0 and fin > 0:
        valor = inicio * (fin / inicio) ** (paso / dias)
    else:  # respaldo lineal; el modelo no debería admitir cierres <= 0
        valor = inicio + (fin - inicio) * paso / dias
    return round(valor, 4)


def _previous_month_close(
    filas: dict[date, PriceRow], fecha: date
) -> float | None:
    """Cierre del último mes cerrado antes del mes de la fecha dada.

    Sirve para reconstruir el MTD de un día capturado que ya estaba guardado
    y que no trae la rentabilidad original. Se busca el último cierre de mes
    (o la base) anterior al día 1 del mes de `fecha`.
    """
    inicio = fecha.replace(day=1)
    anteriores = [
        fila
        for fila in filas.values()
        if fila.date < inicio and fila.kind in (KIND_BASE, KIND_MONTH)
    ]
    if not anteriores:
        return None
    return max(anteriores, key=lambda fila: fila.date).close


def _gain_from_close(close: float, previous_close: float) -> float | None:
    """Rentabilidad (%) de un cierre respecto al cierre del mes anterior."""
    if previous_close <= 0:
        return None
    return round((close / previous_close - 1) * 100, 4)


def _last_day(mes: date) -> date:
    """Último día del mes de la fecha dada."""
    ultimo = calendar.monthrange(mes.year, mes.month)[1]
    return date(mes.year, mes.month, ultimo)
