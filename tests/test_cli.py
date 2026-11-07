"""Interfaz de línea de comandos y configuración.

Aquí se comprueban dos cosas delicadas:

1. Que las opciones valen tanto antes como después del subcomando (argparse
   pisaba con su valor por defecto lo indicado antes del subcomando).
2. Que al podar usuarios no se borre a nadie por error.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from evaca_monitor.cli import _apply_defaults, build_parser, main  # noqa: E402
from evaca_monitor.config import load_config  # noqa: E402
from evaca_monitor.monitor import Monitor  # noqa: E402
from evaca_monitor.state import State  # noqa: E402
from evaca_monitor.models import EvacaUser  # noqa: E402
from evaca_monitor.client import EvacaClient  # noqa: E402
from evaca_monitor.serie import serie_id  # noqa: E402


def parse(argv):
    return _apply_defaults(build_parser().parse_args(argv))


# ---------------------------------------------------------------------- #
# Argumentos
# ---------------------------------------------------------------------- #
def test_el_subcomando_apunta_a_su_funcion():
    assert parse(["check"]).func.__name__ == "cmd_check"
    assert parse(["users"]).func.__name__ == "cmd_users"
    assert parse(["show", "x"]).func.__name__ == "cmd_show"


def test_usuarios_file_antes_o_despues_del_subcomando():
    """Regresión: argparse pisaba el valor puesto antes del subcomando."""
    assert parse(["--users-file", "x.md", "users"]).users_file == "x.md"
    assert parse(["users", "--users-file", "x.md"]).users_file == "x.md"
    assert parse(["--users-file", "x.md", "check"]).users_file == "x.md"
    assert parse(["check", "--users-file", "x.md"]).users_file == "x.md"


def test_valores_por_defecto():
    args = parse(["check"])
    assert args.config == "watchlist.yml"
    assert args.state == "state/state.json"
    assert args.log_level == "INFO"
    assert args.users_file is None


def test_el_estado_es_explicito_solo_si_se_indica(monkeypatch):
    monkeypatch.delenv("EVACA_STATE", raising=False)
    assert parse(["check"]).state_explicito is False
    assert parse(["check", "--state", "fuera.json"]).state_explicito is True

    monkeypatch.setenv("EVACA_STATE", "fuera.json")
    assert parse(["check"]).state_explicito is True


def test_el_check_para_si_el_estado_por_defecto_cae_en_el_repo(monkeypatch):
    """4.2: escribir el estado en el repo de código podría publicar datos."""
    monkeypatch.delenv("EVACA_STATE", raising=False)
    monkeypatch.chdir(Path(__file__).resolve().parents[1])

    assert main(["check"]) == 2


def test_variables_de_entorno(monkeypatch):
    monkeypatch.setenv("EVACA_CONFIG", "otro.yml")
    monkeypatch.setenv("EVACA_STATE", "otro.json")
    monkeypatch.setenv("EVACA_WATCHLIST_MD", "otro.md")
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")
    args = parse(["check"])
    assert args.config == "otro.yml"
    assert args.state == "otro.json"
    assert args.users_file == "otro.md"
    assert args.log_level == "DEBUG"


def test_los_argumentos_mandan_sobre_las_variables(monkeypatch):
    monkeypatch.setenv("EVACA_WATCHLIST_MD", "env.md")
    assert parse(["check", "--users-file", "argv.md"]).users_file == "argv.md"


# ---------------------------------------------------------------------- #
# Config
# ---------------------------------------------------------------------- #
def escribir(tmp_path, users_md: str | None, config_yml: str | None = None):
    yml = tmp_path / "watchlist.yml"
    yml.write_text(config_yml or "users: []\n", encoding="utf-8")
    md = tmp_path / "watchlist.md"
    if users_md is not None:
        md.write_text(users_md, encoding="utf-8")
    return yml, md


def test_lee_los_usuarios_del_markdown(tmp_path):
    yml, _ = escribir(tmp_path, "uno_ejemplo\ndos_ejemplo\n")
    config = load_config(yml)
    assert config.users == ["uno_ejemplo", "dos_ejemplo"]
    assert config.users_origin == "watchlist.md"
    assert config.describe_users() == "uno_ejemplo y dos_ejemplo"


def test_el_markdown_manda_sobre_el_yaml(tmp_path):
    yml, _ = escribir(tmp_path, "# solo este\nuno_ejemplo\n", "users:\n  - otro_ejemplo\n")
    assert load_config(yml).users == ["uno_ejemplo"]


def test_sin_markdown_usa_el_yaml(tmp_path):
    yml, _ = escribir(tmp_path, None, "users:\n  - uno_ejemplo\n")
    config = load_config(yml)
    assert config.users == ["uno_ejemplo"]
    assert config.users_origin == "watchlist.yml"


def test_sin_usuarios_en_ningun_sitio(tmp_path):
    yml, _ = escribir(tmp_path, "# todo comentado\n")
    try:
        load_config(yml)
    except ValueError as exc:
        assert "watchlist.md" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("debería haber fallado sin usuarios")


def test_ajustes_tecnicos_del_yaml(tmp_path):
    yml, _ = escribir(
        tmp_path,
        "uno_ejemplo\n",
        "http:\n  delay_seconds: 1.5\n  max_retries: 7\nalerts:\n  notify_on_baseline: false\n",
    )
    config = load_config(yml)
    assert config.http.delay_seconds == 1.5
    assert config.http.max_retries == 7
    assert config.alerts.notify_on_baseline is False
    assert config.alerts.error_cooldown_minutes == 180


def test_fichero_de_usuarios_explicito(tmp_path):
    yml, _ = escribir(tmp_path, "uno_ejemplo\n")
    otro = tmp_path / "personas.md"
    otro.write_text("dos_ejemplo\n", encoding="utf-8")
    config = load_config(yml, otro)
    assert config.users == ["dos_ejemplo"]
    assert config.users_source == otro


def test_el_del_repositorio_esta_bien_formado(monkeypatch):
    """El repo debe cargar sin incrustar a quién sigues."""
    monkeypatch.delenv("EVACA_USERS", raising=False)
    raiz = Path(__file__).resolve().parents[1]
    try:
        config = load_config(raiz / "watchlist.yml")
    except ValueError as exc:
        assert "watchlist.md" in str(exc)
        assert "EVACA_USERS" in str(exc)
    else:
        assert config.users


# ---------------------------------------------------------------------- #
# EVACA_USERS: fuera del repositorio
# ---------------------------------------------------------------------- #
def test_evaca_users_tiene_prioridad(tmp_path, monkeypatch):
    yml, _ = escribir(tmp_path, "del_fichero\n")
    monkeypatch.setenv("EVACA_USERS", "del_secreto")
    config = load_config(yml)
    assert config.users == ["del_secreto"]
    assert config.users_source is None
    assert "EVACA_USERS" in config.users_origin


def test_evaca_users_admite_varios_formatos(tmp_path, monkeypatch):
    yml, _ = escribir(tmp_path, "del_fichero\n")
    monkeypatch.setenv("EVACA_USERS", "uno, dos\ntres")
    assert load_config(yml).users == ["uno", "dos", "tres"]


def test_evaca_users_vacia_no_estorba(tmp_path, monkeypatch):
    yml, _ = escribir(tmp_path, "del_fichero\n")
    monkeypatch.setenv("EVACA_USERS", "   ")
    assert load_config(yml).users == ["del_fichero"]


def test_sin_fichero_pero_con_secreto(tmp_path, monkeypatch):
    yml, _ = escribir(tmp_path, "# (vacío a propósito)\n")
    monkeypatch.setenv("EVACA_USERS", "uno_ejemplo")
    assert load_config(yml).users == ["uno_ejemplo"]


# ---------------------------------------------------------------------- #
# Diagnóstico sin lista de usuarios
# ---------------------------------------------------------------------- #
def test_los_comandos_de_diagnostico_funcionan_sin_usuarios(tmp_path, monkeypatch):
    """`ping --username X` fallaba si el watchlist estaba vacío, que es justo
    cuando más falta hace el diagnóstico."""
    monkeypatch.delenv("EVACA_USERS", raising=False)
    yml, _ = escribir(tmp_path, "# vacío a propósito\n")
    try:
        load_config(yml)
    except ValueError:
        pass
    else:  # pragma: no cover
        raise AssertionError("debería exigir usuarios")

    assert load_config(yml, require_users=False).users == []


def test_sin_config_funciona_para_diagnostico(tmp_path, monkeypatch):
    monkeypatch.delenv("EVACA_USERS", raising=False)
    config = load_config(tmp_path / "no-existe.yml", require_users=False)
    assert config.users == []


# ---------------------------------------------------------------------- #
# La poda no debe borrar a nadie por error
# ---------------------------------------------------------------------- #
def usuario(nombre: str) -> EvacaUser:
    return EvacaUser(username=nombre, gcid=1, real_cid=2)


def test_prune_false_no_borra_a_nadie(tmp_path):
    """Con `--user X` solo se revisa a una persona: los demás deben quedarse."""
    state = State(path=tmp_path / "state.json")
    state.upsert_user(usuario("uno_ejemplo"))
    state.upsert_user(usuario("dos_ejemplo"))

    monitor = Monitor(EvacaClient(delay=0), state, notify=lambda _t: None)
    monitor._prune(["uno_ejemplo"])  # lo que haría prune=True
    assert list(state.users) == ["uno_ejemplo"]


def test_la_poda_solo_usa_la_lista_completa(tmp_path):
    """La poda se basa en la lista de seguimiento, no en lo procesado.

    Así un usuario con el nombre mal escrito (que sí está en la lista) NO se
    borra, y se sigue reintentando.
    """
    state = State(path=tmp_path / "state.json")
    state.upsert_user(usuario("con_typo_ejemplo"))
    monitor = Monitor(EvacaClient(delay=0), state, notify=lambda _t: None)
    monitor._prune(["con_typo_ejemplo", "otro_ejemplo"])
    assert list(state.users) == ["con_typo_ejemplo"]


# ---------------------------------------------------------------------- #
# El subcomando series
# ---------------------------------------------------------------------- #
def _entorno_series(tmp_path, monkeypatch, *, texto="uno_ejemplo\n"):
    """Ajustes y lista de prueba; la carpeta de datos va a tmp."""
    yml = tmp_path / "watchlist.yml"
    yml.write_text(
        "users: []\nseries:\n  pause_seconds: 0.0\n  cache_minutes: 60\n",
        encoding="utf-8",
    )
    md = tmp_path / "watchlist.md"
    md.write_text(texto, encoding="utf-8")
    monkeypatch.setenv("EVACA_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    monkeypatch.setattr("evaca_monitor.cli._setup_logging", lambda nivel: None)
    return ["--config", str(yml), "--users-file", str(md)]


def test_el_subcomando_series_apunta_a_su_funcion():
    args = parse(["series"])

    assert args.func.__name__ == "cmd_series"
    assert args.dry_run is False
    assert args.force is False
    assert args.ids is False
    assert args.usuario is None


def test_series_acepta_sus_opciones():
    args = parse(["series", "--dry-run", "--force", "--usuario", "x", "--ids"])

    assert args.dry_run is True
    assert args.force is True
    assert args.ids is True
    assert args.usuario == "x"


def test_series_dry_run_no_escribe_nada(tmp_path, monkeypatch):
    argv = _entorno_series(tmp_path, monkeypatch)

    assert main(["series", "--dry-run", *argv]) == 0
    assert not (tmp_path / "data").exists()


def test_series_sin_destino_no_hace_nada(tmp_path, monkeypatch):
    argv = _entorno_series(tmp_path, monkeypatch)
    monkeypatch.delenv("EVACA_DESTINO_BASE", raising=False)

    assert main(["series", *argv]) == 2
    assert not (tmp_path / "data").exists()


def test_series_ids_escribe_el_mapa_y_lo_imprime_a_mano(
    tmp_path, monkeypatch, capsys
):
    argv = _entorno_series(tmp_path, monkeypatch)

    assert main(["series", "--ids", *argv]) == 0

    mapa = tmp_path / "PP-ids.md"
    assert mapa.is_file()
    assert "uno_ejemplo" in mapa.read_text(encoding="utf-8")

    salida = capsys.readouterr().out
    assert "uno_ejemplo" in salida
    assert serie_id("uno_ejemplo") in salida


def test_series_ids_no_imprime_la_tabla_en_github_actions(
    tmp_path, monkeypatch, capsys
):
    argv = _entorno_series(tmp_path, monkeypatch)
    monkeypatch.setenv("GITHUB_ACTIONS", "true")

    assert main(["series", "--ids", *argv]) == 0

    assert (tmp_path / "PP-ids.md").is_file()
    assert "uno_ejemplo" not in capsys.readouterr().out


def test_series_ids_en_dry_run_no_escribe_el_mapa(tmp_path, monkeypatch):
    argv = _entorno_series(tmp_path, monkeypatch)

    assert main(["series", "--ids", "--dry-run", *argv]) == 0
    assert not (tmp_path / "PP-ids.md").exists()


def test_series_con_lista_vacia_aborta_sin_crear_nada(tmp_path, monkeypatch):
    argv = _entorno_series(tmp_path, monkeypatch, texto="# nada activo\n")

    assert main(["series", *argv]) == 2
    assert not (tmp_path / "data").exists()


def test_la_configuracion_de_series_lee_los_umbrales(tmp_path):
    yml = tmp_path / "watchlist.yml"
    yml.write_text(
        "users: [uno_ejemplo]\n"
        "series:\n"
        "  alert_pp: 0.3\n"
        "  cap_pp: 0.7\n"
        "  stats_days: [4, 11]\n"
        "  chart_window: lastYear\n",
        encoding="utf-8",
    )

    config = load_config(yml)

    assert config.series.alert_pp == 0.3
    assert config.series.cap_pp == 0.7
    assert config.series.stats_days == [4, 11]
    assert config.series.chart_window == "lastYear"
