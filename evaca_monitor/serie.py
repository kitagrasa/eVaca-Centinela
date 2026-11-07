"""Cálculo puro de la serie tipo fondo con índice base 100.

Funciones sin acceso a disco ni a red: reciben la respuesta ya descargada y
devuelven las filas ordenadas. Así son fáciles de probar.

Hay dos fuentes:

* la rentabilidad **mensual** del perfil: es el cierre de cada mes cerrado y
  manda sobre el chart;
* el **chart diario** público de los dos últimos años: da los días. Los meses
  cerrados que caben en el chart se calibran para que su producto acabe
  exactamente en la rentabilidad mensual; el mes en curso se publica tal cual.

El JSON guarda los datos reales: la base, el ancla del día 1 de cada mes, el
cierre exacto de cada mes, los días de chart calibrados, los del mes en curso
y los meses antiguos que solo tienen rentabilidad mensual. Encima, para el
HTML, `daily_rows` rellena los huecos de los meses que no tienen días de chart.

El ancla del día 1 existe para los meses que no tienen chart: Portfolio
Performance empieza a dibujar cada serie en su primera cotización del periodo,
así que sin un punto el día 1, el primer mes de cualquier comparativa (YTD, 1
año, 3 años) salía vacío hasta el cierre.

Ningún dato de este módulo lleva el nombre del trader: todo lo que se publica
se nombra con `serie_id(usuario)`, un identificador opaco y estable.
"""

from __future__ import annotations

import calendar
import hashlib
import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

# Tipos de fila admitidos en el histórico.
KIND_BASE = "base"
KIND_MONTH = "month"
KIND_DAY = "day"
KIND_ANCHOR = "anchor"
# Día de un mes cerrado calibrado con el cierre mensual. Se marca aparte de
# `day` para que un fallo del chart no lo confunda con una muestra del mes en
# curso y para que `daily_rows` sepa que ese mes ya tiene rejilla propia.
KIND_CALIBRATED = "c"
KINDS = (KIND_BASE, KIND_MONTH, KIND_DAY, KIND_ANCHOR, KIND_CALIBRATED)

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

# Periodos que admite el chart público.
CHART_PERIODS = frozenset(
    {
        "currMonth",
        "oneMonthAgo",
        "twoMonthsAgo",
        "currQuarter",
        "threeMonthsAgo",
        "sixMonthsAgo",
        "currYear",
        "oneYearAgo",
        "lastYear",
        "lastTwoYears",
    }
)
CHART_WINDOW = "lastTwoYears"


class SeriesError(Exception):
    """El histórico o el chart no permiten construir una serie válida."""


@dataclass(frozen=True, slots=True)
class MonthGain:
    """Rentabilidad de un mes según la respuesta del endpoint.

    `start` es el día de inicio del mes (el primero, salvo el mes de alta) y
    `gain` la rentabilidad de ese mes en porcentaje. Es el cambio de equity del
    mes natural con ingresos y retiros compensados, así que vale como cierre
    mensual. El `yearly` de la respuesta no se usa: es el compuesto de estos
    mensuales, no una fuente independiente.
    """

    start: date
    gain: float
    is_simulation: bool = False


@dataclass(frozen=True, slots=True)
class ChartPoint:
    """Un punto del chart diario: fecha y nivel de la cartera simulada."""

    date: date
    equity: float


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


@dataclass(slots=True)
class SeriesBuild:
    """Resultado de construir una serie: filas y auditoría de los meses."""

    rows: list[PriceRow]
    months: dict[str, dict[str, Any]]


def serie_id(usuario: str) -> str:
    """Identificador opaco y estable de un trader: 12 hex de sha256.

    `serie_id(u) = sha256("evaca:" + u.strip().lower()).hexdigest()[:12]`.

    Todo lo que se publica (`data/series/<id>.json`, `data/prices/<id>` y el
    resumen de `data/state.json`) se nombra con este id: el nombre del trader
    solo existe en la lista privada y en el mapa privado (`PP-ids.md`).

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


# ---------------------------------------------------------------------- #
# Chart diario
# ---------------------------------------------------------------------- #
def parse_chart(payload: Any, window: str = CHART_WINDOW) -> list[ChartPoint]:
    """Extrae los puntos diarios de la respuesta del chart.

    La respuesta trae la ventana pedida dentro de `simulation`. Se rechaza el
    chart entero si falta la estructura, si no hay ningún punto, si una fecha
    no crece, si se repite o si un nivel no es positivo: publicar medio chart
    sería peor que no publicar ninguno.
    """
    if window not in CHART_PERIODS:
        raise SeriesError(f"periodo de chart no válido: {window!r}")
    if not isinstance(payload, dict):
        raise SeriesError("la respuesta del chart no es un objeto JSON")
    simulacion = payload.get("simulation")
    if not isinstance(simulacion, dict):
        raise SeriesError("la respuesta del chart no trae 'simulation'")
    ventana = simulacion.get(window)
    if not isinstance(ventana, dict):
        raise SeriesError(f"la respuesta del chart no trae la ventana {window!r}")
    chart = ventana.get("chart")
    if not isinstance(chart, list):
        raise SeriesError("la ventana del chart no trae la lista 'chart'")

    pares: list[tuple[str, float]] = []
    for entrada in chart:
        if not isinstance(entrada, dict):
            raise SeriesError("hay un punto del chart que no es un objeto")
        fecha = _parse_timestamp(entrada.get("timestamp"))
        equity = _parse_equity(entrada.get("equity"))
        if fecha is None or equity is None:
            raise SeriesError("hay un punto del chart incompleto")
        pares.append((fecha, equity))
    if not pares:
        raise SeriesError("el chart no tiene ningún punto")
    return _validar_puntos(pares)


def parse_chart_points(raw: Any) -> list[ChartPoint]:
    """Valida los puntos guardados en JSON: [["2024-01-01", 10009.0], ...]."""
    if not isinstance(raw, list):
        raise SeriesError("los puntos del chart guardado no son una lista")
    pares: list[tuple[str, float]] = []
    for entrada in raw:
        if not isinstance(entrada, (list, tuple)) or len(entrada) < 2:
            raise SeriesError("hay un punto guardado del chart incompleto")
        fecha = _parse_start(entrada[0])
        equity = _parse_equity(entrada[1])
        if fecha is None or equity is None:
            raise SeriesError("hay un punto guardado del chart incompleto")
        pares.append((fecha.isoformat(), equity))
    if not pares:
        raise SeriesError("el chart guardado no tiene ningún punto")
    return _validar_puntos(pares)


def monthly_returns(points: Sequence[ChartPoint]) -> dict[tuple[int, int], float]:
    """Retorno de cada mes del chart por cierre-contra-cierre.

    El retorno de un mes es `P(último día del mes) / P(último día del mes
    anterior) - 1`. El primer mes del chart no aparece: no hay cierre del mes
    anterior dentro de la ventana y el nivel inicial no se usa.
    """
    ultimo: dict[tuple[int, int], float] = {}
    for punto in points:
        ultimo[(punto.date.year, punto.date.month)] = punto.equity
    claves = sorted(ultimo)
    retornos: dict[tuple[int, int], float] = {}
    for anterior, actual in zip(claves, claves[1:], strict=False):
        if _next_month(anterior) != actual:
            continue
        base = ultimo[anterior]
        if base <= 0:
            continue
        retornos[actual] = (ultimo[actual] / base - 1) * 100
    return retornos


# ---------------------------------------------------------------------- #
# Construcción de la serie
# ---------------------------------------------------------------------- #
def build_series(
    monthly: Sequence[MonthGain],
    today: date,
    history: Iterable[PriceRow] = (),
    chart: Iterable[ChartPoint] | None = None,
    *,
    checked_at: str = "",
) -> list[PriceRow]:
    """Construye la serie completa, ordenada y sin fechas duplicadas.

    Es el envoltorio de `build_series_full` para quien solo necesita las
    filas (los tests y cualquier uso futuro).
    """
    return build_series_full(
        monthly,
        today,
        history=history,
        chart=chart,
        checked_at=checked_at,
    ).rows


def build_series_full(
    monthly: Sequence[MonthGain],
    today: date,
    history: Iterable[PriceRow] = (),
    chart: Iterable[ChartPoint] | None = None,
    *,
    checked_at: str = "",
) -> SeriesBuild:
    """Construye las filas y la auditoría de los meses calibrados.

    monthly: meses de la respuesta (se limpian aquí dentro).
    today: fecha de hoy en Europe/Madrid.
    history: filas ya guardadas; se conservan los días de chart reales (`C` y
    `D`) de los meses que no se reconstruyen en esta pasada.
    chart: puntos diarios del chart público, o None si no hay chart disponible.

    Reglas de los meses cerrados con chart:

    * el cierre `M` es siempre el `gain` exacto de la rentabilidad mensual;
    * los días con movimiento se multiplican por un factor constante para que
      el producto del mes acabe en ese cierre, aunque el chart se desvíe o
      vaya en sentido contrario;
    * los días con retorno 0 se quedan en 0;
    * solo se marca `rejected` si no hay forma matemática de repartir el
      cierre (ningún día con movimiento y un mes que no es plano, o un factor
      inválido). En ese caso se publica `M` + relleno `A`.

    El umbral de aviso (`alert_pp`) no cambia el cálculo: el aviso por
    Telegram lo decide `serie_run` a partir de la auditoría que devuelve esta
    función. `cap_pp` tampoco impide calibrar: en `serie_run` solo marca el
    chart como poco fiable para el aviso y la recomendación.
    """
    meses = clean_monthly(monthly)
    clave_actual = (today.year, today.month)
    stats = {(mes.start.year, mes.start.month): mes.gain for mes in meses}
    historial = list(history)

    puntos_chart = [
        punto for punto in _normalizar_chart(chart) if punto.date <= today
    ]
    por_mes, ultimo_mes = _chart_by_month(puntos_chart)

    filas: dict[date, PriceRow] = {}

    # Fila base: un día antes del primer mes completo, con valor 100.
    fecha_base = meses[0].start - timedelta(days=1)
    filas[fecha_base] = PriceRow(date=fecha_base, close=BASE_VALUE, kind=KIND_BASE)
    cierre_previo = BASE_VALUE

    # Meses cuyos días de chart se han decidido en esta pasada. Los que no
    # estén aquí conservan sus `C`/`D` guardados: si el chart falla o una
    # ventana antigua ya no lo cubre, lo publicado no se borra.
    decididos: set[tuple[int, int]] = set()
    auditoria: dict[str, dict[str, Any]] = {}

    for mes in meses:
        clave = (mes.start.year, mes.start.month)
        if clave >= clave_actual:
            continue

        ganancia = stats.get(clave)
        if ganancia is None:  # pragma: no cover - clean_monthly lo garantiza
            continue
        ultimo_dia = _last_day(mes.start)
        dias_chart = por_mes.get(clave)

        if not dias_chart:
            cierre_previo = _add_plain_month(
                filas, mes, cierre_previo, ganancia
            )
            continue

        gc, retornos, _frontera = _chart_month(
            dias_chart, ultimo_mes.get(_prev_month(clave))
        )
        n = sum(1 for retorno in retornos if retorno != 0.0)
        if n == 0 and abs(ganancia) <= 1e-12:
            # Un mes plano puede quedar plano: no hay nada que repartir.
            factor: float | None = 1.0
            rechazado = False
        else:
            # La rentabilidad mensual manda siempre. Si el chart se desvía
            # (o incluso va en sentido contrario), se calibran sus días para
            # que el mes acabe exactamente en stats. Solo se rechaza si no
            # hay forma matemática de repartir el cierre (ningún día con
            # movimiento o un factor inválido).
            factor = _factor(ganancia, gc, n)
            rechazado = (
                factor is None
                or not math.isfinite(factor)
                or factor <= 0
            )
            if rechazado:
                factor = 1.0

        auditoria[_month_key(clave)] = {
            "stats_pct": round(ganancia, 4),
            "chart_pct": round(gc, 4),
            "factor": round(factor, 6) if factor is not None else 1.0,
            "status": "rejected" if rechazado else "calibrated",
            "checked_at": checked_at,
        }
        decididos.add(clave)

        if rechazado:
            cierre_previo = _add_plain_month(
                filas, mes, cierre_previo, ganancia
            )
            continue

        # Mes calibrado: un punto por día del chart. El primer punto de una
        # ventana es el cierre del primer día (no se usa el nivel inicial del
        # chart); cuando no hay cierre del mes anterior, ese primer día se
        # deja plano y los días anteriores se rellenan con `A`.
        if dias_chart[0].date > mes.start:
            filas.setdefault(
                mes.start,
                PriceRow(date=mes.start, close=cierre_previo, kind=KIND_ANCHOR),
            )

        acumulado = cierre_previo
        cierre_anterior = cierre_previo
        for punto, retorno in zip(dias_chart, retornos, strict=False):
            ajustado = 0.0 if retorno == 0.0 else (1 + retorno) * factor - 1
            acumulado *= 1 + ajustado
            if punto.date == ultimo_dia:
                # El último día es el cierre mensual exacto; se añade abajo.
                continue
            cierre = round(acumulado, 4)
            filas[punto.date] = PriceRow(
                date=punto.date,
                close=cierre,
                kind=KIND_CALIBRATED,
                gain=_gain_from_close(cierre, cierre_anterior),
            )
            cierre_anterior = cierre

        cierre_mes = round(cierre_previo * (1 + ganancia / 100), 4)
        filas[ultimo_dia] = PriceRow(
            date=ultimo_dia,
            close=cierre_mes,
            kind=KIND_MONTH,
            gain=ganancia,
        )
        cierre_previo = cierre_mes

    # Mes en curso: días del chart sin calibrar. Si no hay chart, se conserva
    # lo guardado; sin nada guardado, se usa el MTD de la rentabilidad mensual
    # (el comportamiento antiguo, solo como respaldo).
    mes_actual = next(
        (
            mes
            for mes in meses
            if (mes.start.year, mes.start.month) == clave_actual
        ),
        None,
    )
    dias_actual = por_mes.get(clave_actual)
    inicio_actual = mes_actual.start if mes_actual else today.replace(day=1)
    # Sin el cierre del mes anterior no hay base correcta para los días del
    # mes en curso: se espera a que stats traiga el mes (día 5) antes que
    # publicar un nivel que luego habría que corregir. La excepción es el
    # primer mes de la serie, que arranca sobre la base 100.
    anterior_actual = _prev_month(clave_actual)
    hay_previo = anterior_actual in stats or (
        (meses[0].start.year, meses[0].start.month) == clave_actual
    )
    if dias_actual and hay_previo:
        # El chart del mes en curso no necesita la rentabilidad mensual: sus
        # días son `D` sin calibrar. Se publican también si stats todavía no
        # trae el mes actual.
        _, retornos, _frontera = _chart_month(
            dias_actual, ultimo_mes.get(_prev_month(clave_actual))
        )
        if dias_actual[0].date > inicio_actual:
            filas.setdefault(
                inicio_actual,
                PriceRow(
                    date=inicio_actual,
                    close=cierre_previo,
                    kind=KIND_ANCHOR,
                ),
            )
        acumulado = cierre_previo
        cierre_anterior = cierre_previo
        for punto, retorno in zip(dias_actual, retornos, strict=False):
            acumulado *= 1 + retorno
            cierre = round(acumulado, 4)
            filas[punto.date] = PriceRow(
                date=punto.date,
                close=cierre,
                kind=KIND_DAY,
                gain=_gain_from_close(cierre, cierre_anterior),
            )
            cierre_anterior = cierre
        decididos.add(clave_actual)
    elif mes_actual is not None:
        # Respaldo sin chart: ancla y MTD de la rentabilidad mensual, como
        # antes. Solo se usa si el chart no trae el mes en curso (o si falta
        # el cierre del mes anterior para encadenarlo con seguridad).
        filas.setdefault(
            inicio_actual,
            PriceRow(
                date=inicio_actual, close=cierre_previo, kind=KIND_ANCHOR
            ),
        )
        cierre_hoy = round(cierre_previo * (1 + mes_actual.gain / 100), 4)
        filas[today] = PriceRow(
            date=today,
            close=cierre_hoy,
            kind=KIND_DAY,
            gain=mes_actual.gain,
        )

    # Se conservan los días reales de chart (`C`/`D`) de los meses que esta
    # pasada no ha podido reconstruir. El cierre `M` y la base no se tocan
    # nunca; un día guardado sí manda sobre un ancla.
    for fila in historial:
        if fila.kind not in (KIND_CALIBRATED, KIND_DAY):
            continue
        if fila.date >= today:
            continue
        clave = (fila.date.year, fila.date.month)
        if clave in decididos:
            continue
        existente = filas.get(fila.date)
        if existente is not None and existente.kind in (KIND_BASE, KIND_MONTH):
            continue
        ganancia = fila.gain
        if ganancia is None:
            cierre_anterior = _previous_close(filas, fila.date)
            if cierre_anterior is not None:
                ganancia = _gain_from_close(fila.close, cierre_anterior)
        filas[fila.date] = PriceRow(
            date=fila.date, close=fila.close, kind=fila.kind, gain=ganancia
        )

    return SeriesBuild(
        rows=[filas[fecha] for fecha in sorted(filas)], months=auditoria
    )


def daily_rows(rows: Sequence[PriceRow], today: date) -> list[PriceRow]:
    """Devuelve las filas con un punto por cada día de los meses cerrados.

    Portfolio Performance empieza a dibujar cada serie en su primera
    cotización dentro del periodo. Con dos puntos por mes (día 1 y cierre),
    las ventanas que PP ancla a mitad de mes (1 año, 3 años) seguían
    empezando en el día 1 siguiente. Esta rejilla las cubre también.

    Los meses con días de chart (`C`/`D`) ya traen su propia rejilla diaria:
    no se rellenan por dentro. La única excepción es el hueco inicial de un
    mes frontera (el primero con chart): los días anteriores al primer `C` se
    rellenan con `A` hasta el ancla del día 1.

    El mes en curso no se rellena salvo ese hueco inicial: su MTD todavía
    puede cambiar y PP no reescribe fechas antiguas.

    Las filas inventadas llevan kind = "fill" y solo existen para el HTML:
    nunca se guardan en `data/series/<id>.json`.
    """
    ordenadas = sorted(rows, key=lambda fila: fila.date)
    if not ordenadas:
        return []

    clave_actual = (today.year, today.month)
    por_fecha: dict[date, PriceRow] = {fila.date: fila for fila in ordenadas}

    # Primer día calibrado (`C`) de cada mes: a partir de ahí no se interpola.
    # Los meses con solo `D` tampoco se rellenan (son muestras del mes en
    # curso o datos antiguos); la única excepción es el hueco inicial de un
    # mes frontera, que sí llega a tener `C`.
    primer_dia_c: dict[tuple[int, int], date] = {}
    meses_con_d: set[tuple[int, int]] = set()
    for fila in ordenadas:
        clave = (fila.date.year, fila.date.month)
        if fila.kind == KIND_CALIBRATED:
            anterior = primer_dia_c.get(clave)
            if anterior is None or fila.date < anterior:
                primer_dia_c[clave] = fila.date
        elif fila.kind == KIND_DAY:
            meses_con_d.add(clave)

    for anterior, siguiente in zip(ordenadas, ordenadas[1:], strict=False):
        dias = (siguiente.date - anterior.date).days
        if dias < 2:
            continue
        for paso in range(1, dias):
            fecha = anterior.date + timedelta(days=paso)
            clave = (fecha.year, fecha.month)
            if clave in primer_dia_c:
                if fecha >= primer_dia_c[clave]:
                    continue
            elif clave in meses_con_d:
                continue
            elif clave == clave_actual:
                continue
            if clave > clave_actual:
                continue
            por_fecha[fecha] = PriceRow(
                date=fecha,
                close=_interpolar(anterior.close, siguiente.close, paso, dias),
                kind=KIND_FILL,
            )

    return [por_fecha[fecha] for fecha in sorted(por_fecha)]


# ---------------------------------------------------------------------- #
# Ayudantes internos
# ---------------------------------------------------------------------- #
def _normalizar_chart(
    chart: Iterable[ChartPoint] | None,
) -> list[ChartPoint]:
    """Ordena y valida los puntos recibidos; None o vacío = sin chart."""
    if not chart:
        return []
    puntos = sorted(chart, key=lambda punto: punto.date)
    anterior: date | None = None
    for punto in puntos:
        if not punto.equity > 0:
            raise SeriesError("el chart tiene un nivel no positivo")
        if anterior is not None and punto.date <= anterior:
            raise SeriesError("el chart tiene fechas duplicadas o desordenadas")
        anterior = punto.date
    return puntos


def _chart_by_month(
    puntos: Sequence[ChartPoint],
) -> tuple[dict[tuple[int, int], list[ChartPoint]], dict[tuple[int, int], float]]:
    """Agrupa los puntos por mes y deja el último nivel de cada mes."""
    por_mes: dict[tuple[int, int], list[ChartPoint]] = {}
    for punto in puntos:
        por_mes.setdefault((punto.date.year, punto.date.month), []).append(punto)
    ultimo = {
        clave: lista[-1].equity
        for clave, lista in por_mes.items()
        if lista
    }
    return por_mes, ultimo


def _chart_month(
    dias: Sequence[ChartPoint], cierre_anterior: float | None
) -> tuple[float, list[float], bool]:
    """Retorno del mes, retornos diarios y si es un mes frontera.

    `cierre_anterior` es el último nivel del mes anterior en el chart. Si no
    existe, el primer punto de la ventana no se usa como nivel: el primer día
    se deja plano y se devuelve `frontera=True`.
    """
    if not dias:
        return 0.0, [], False
    if cierre_anterior is not None and cierre_anterior > 0:
        retornos: list[float] = []
        anterior = cierre_anterior
        for punto in dias:
            retornos.append(punto.equity / anterior - 1)
            anterior = punto.equity
        return (dias[-1].equity / cierre_anterior - 1) * 100, retornos, False

    # Frontera: el primer punto ya trae el movimiento del primer día y no hay
    # cierre del mes anterior. No se usa el nivel inicial del chart; el primer
    # día se queda plano y el resto conserva su forma.
    primera = dias[0].equity
    retornos = [0.0]
    anterior = primera
    for punto in dias[1:]:
        retornos.append(punto.equity / anterior - 1)
        anterior = punto.equity
    return (dias[-1].equity / primera - 1) * 100, retornos, True


def _factor(ganancia: float, chart_pct: float, n: int) -> float | None:
    """Factor constante para que el producto del mes acabe en `ganancia`."""
    if n <= 0:
        return None
    try:
        return ((1 + ganancia / 100) / (1 + chart_pct / 100)) ** (1 / n)
    except (OverflowError, ValueError, ZeroDivisionError):
        return None


def _add_plain_month(
    filas: dict[date, PriceRow],
    mes: MonthGain,
    cierre_previo: float,
    ganancia: float,
) -> float:
    """Añade un mes sin chart: ancla del día 1 y cierre `M` exacto."""
    filas.setdefault(
        mes.start,
        PriceRow(date=mes.start, close=cierre_previo, kind=KIND_ANCHOR),
    )
    cierre = round(cierre_previo * (1 + ganancia / 100), 4)
    filas[_last_day(mes.start)] = PriceRow(
        date=_last_day(mes.start),
        close=cierre,
        kind=KIND_MONTH,
        gain=ganancia,
    )
    return cierre


def _parse_start(valor: Any) -> date | None:
    """Convierte '2020-03-16T14:53:03.533Z' en date(2020, 3, 16)."""
    if not isinstance(valor, str) or len(valor) < 10:
        return None
    try:
        return date.fromisoformat(valor[:10])
    except ValueError:
        return None


def _parse_timestamp(valor: Any) -> str | None:
    """Devuelve la fecha ISO de un timestamp del chart, o None."""
    fecha = _parse_start(valor)
    return fecha.isoformat() if fecha is not None else None


def _parse_gain(valor: Any) -> float | None:
    """Convierte la rentabilidad a número, descartando booleanos y basura."""
    if isinstance(valor, bool) or valor is None:
        return None
    try:
        return float(valor)
    except (TypeError, ValueError):
        return None


def _parse_equity(valor: Any) -> float | None:
    """Convierte el nivel del chart a número positivo."""
    if isinstance(valor, bool) or valor is None:
        return None
    try:
        numero = float(valor)
    except (TypeError, ValueError):
        return None
    if not numero > 0 or not math.isfinite(numero):
        return None
    return numero


def _validar_puntos(pares: Sequence[tuple[str, float]]) -> list[ChartPoint]:
    """Convierte pares (fecha ISO, nivel) y exige fechas crecientes."""
    puntos: list[ChartPoint] = []
    anterior: date | None = None
    for texto, equity in pares:
        fecha = date.fromisoformat(texto)
        if anterior is not None and fecha <= anterior:
            raise SeriesError("el chart tiene fechas duplicadas o no crecientes")
        if not equity > 0:
            raise SeriesError("el chart tiene un nivel no positivo")
        puntos.append(ChartPoint(date=fecha, equity=equity))
        anterior = fecha
    return puntos


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


def _previous_close(
    filas: dict[date, PriceRow], fecha: date
) -> float | None:
    """Cierre de la fila real anterior a `fecha`, sin mirar el tipo."""
    anteriores = [fila for fila in filas.values() if fila.date < fecha]
    if not anteriores:
        return None
    return max(anteriores, key=lambda fila: fila.date).close


def _gain_from_close(close: float, previous_close: float) -> float | None:
    """Rentabilidad (%) de un cierre respecto al cierre anterior."""
    if previous_close <= 0:
        return None
    return round((close / previous_close - 1) * 100, 4)


def _last_day(mes: date) -> date:
    """Último día del mes de la fecha dada."""
    ultimo = calendar.monthrange(mes.year, mes.month)[1]
    return date(mes.year, mes.month, ultimo)


def _prev_month(clave: tuple[int, int]) -> tuple[int, int]:
    anio, mes = clave
    if mes == 1:
        return anio - 1, 12
    return anio, mes - 1


def _next_month(clave: tuple[int, int]) -> tuple[int, int]:
    anio, mes = clave
    if mes == 12:
        return anio + 1, 1
    return anio, mes + 1


def _month_key(clave: tuple[int, int]) -> str:
    return f"{clave[0]:04d}-{clave[1]:02d}"
