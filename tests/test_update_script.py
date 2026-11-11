# tests/test_update_script.py
"""scripts/update.sh: перезапуск nginx, когда изменился nginx.conf или proxy_params.conf.

Эти два файла смонтированы в контейнер отдельными файлами: после git pull контейнер продолжает видеть старый
(11.11.2026 из-за этого новое сжатие JS не включилось, хотя «nginx -s reload» выполнялся). Скрипт теперь сравнивает
то, что видит контейнер, с файлами в репозитории и при отличии проверяет новый конфиг и перезапускает nginx.

Проверка идёт «вхолостую»: настоящий git (локальный «сервер» origin), а docker подменён маленьким скриптом, который
пишет вызовы в журнал и притворяется контейнером. Нужен bash (на Windows — из Git for Windows); нет bash — тесты пропускаются."""
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
UPDATE_SH = ROOT / "scripts" / "update.sh"

FAKE_DOCKER = r"""#!/usr/bin/env bash
state="$FAKE_STATE"
log() { echo "$*" >> "$state/calls.log"; }
[ "$1" = "compose" ] && shift
rc=0
case "$1" in
  up) log "up" ;;
  run) log "run-test"; [ "${FAKE_NGINX_TEST:-ok}" = ok ] || rc=1 ;;
  restart)
    log "restart $2"
    if [ "${FAKE_RESTART_FAIL:-0}" = 1 ]; then rc=1; else
      for f in nginx.conf proxy_params.conf; do cp "nginx/$f" "$state/container/$f"; done
    fi ;;
  exec)
    shift; [ "$1" = "-T" ] && shift; shift          # exec, -T, имя сервиса
    case "$1" in
      cat) if [ "${FAKE_NGINX_DOWN:-0}" = 1 ]; then rc=1; else cat "$state/container/$(basename "$2")"; fi ;;
      nginx) shift; case "$1" in -s) log "reload" ;; -t) log "exec-test" ;; esac ;;
    esac ;;
  ps) if [ "$2" = "--format" ]; then echo "app healthy"; else echo "NAME  STATUS"; echo "app  healthy"; fi ;;
  image) : ;;
esac
exit $rc
"""


def _find_bash():
    candidates = [r"C:\Program Files\Git\bin\bash.exe", r"C:\Program Files\Git\usr\bin\bash.exe", shutil.which("bash")]
    for c in candidates:
        if not c or not Path(c).exists():
            continue
        try:
            r = subprocess.run([c, "-c", "echo ok; command -v git cmp awk >/dev/null && echo tools"],
                               capture_output=True, text=True, timeout=30)
        except (OSError, subprocess.SubprocessError):
            continue
        if r.returncode == 0 and "ok" in r.stdout and "tools" in r.stdout:
            return c
    return None


BASH = _find_bash()
pytestmark = pytest.mark.skipif(BASH is None, reason="нет bash с git/cmp/awk")


def _git(args, cwd, env):
    subprocess.run(["git", *args], cwd=cwd, env=env, check=True, capture_output=True, text=True, encoding="utf-8")


class Sandbox:
    """Локальный «GitHub» (origin), рабочая копия «сервера» (repo) и поддельный docker."""

    def __init__(self, tmp: Path):
        self.tmp = tmp
        self.env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.test",
                    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.test"}
        origin, self.work, self.repo = tmp / "origin.git", tmp / "work", tmp / "repo"
        _git(["init", "--bare", str(origin)], tmp, self.env)
        _git(["--git-dir", str(origin), "symbolic-ref", "HEAD", "refs/heads/main"], tmp, self.env)
        _git(["clone", str(origin), str(self.work)], tmp, self.env)
        (self.work / "scripts").mkdir()
        (self.work / "nginx").mkdir()
        shutil.copy(UPDATE_SH, self.work / "scripts" / "update.sh")
        (self.work / "scripts" / "backup.sh").write_text("#!/usr/bin/env bash\necho backup-ok\n")
        (self.work / "nginx" / "nginx.conf").write_text("worker_processes 1;\n")
        (self.work / "nginx" / "proxy_params.conf").write_text("proxy_http_version 1.1;\n")
        _git(["add", "-A"], self.work, self.env)
        _git(["commit", "-m", "start"], self.work, self.env)
        _git(["push", "-u", "origin", "HEAD:main"], self.work, self.env)
        _git(["clone", "-b", "main", str(origin), str(self.repo)], tmp, self.env)
        (self.repo / ".env").write_text("APP_URL=https://example.test\n")
        # что «видит» контейнер сейчас: старые файлы
        self.state = tmp / "fake"
        (self.state / "container").mkdir(parents=True)
        for f in ("nginx.conf", "proxy_params.conf"):
            shutil.copy(self.repo / "nginx" / f, self.state / "container" / f)
        bindir = tmp / "bin"
        bindir.mkdir()
        (bindir / "docker").write_text(FAKE_DOCKER, newline="\n")
        self.bindir = bindir

    def change_nginx_conf_on_github(self):
        (self.work / "nginx" / "nginx.conf").write_text("worker_processes 1;\ngzip_vary on;\n")
        _git(["commit", "-am", "gzip"], self.work, self.env)
        _git(["push", "origin", "HEAD:main"], self.work, self.env)

    def run(self, **extra):
        env = {**self.env, "PATH": f"{self.bindir}{os.pathsep}{os.environ['PATH']}", "FAKE_STATE": self.state.as_posix(),
               **{k: str(v) for k, v in extra.items()}}
        return subprocess.run([BASH, "scripts/update.sh"], cwd=self.repo, env=env, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=120)

    def calls(self) -> list[str]:
        log = self.state / "calls.log"
        return log.read_text(encoding="utf-8").split("\n") if log.exists() else []

    def container_conf(self) -> str:
        return (self.state / "container" / "nginx.conf").read_text()


@pytest.fixture
def box(tmp_path):
    return Sandbox(tmp_path)


def test_script_has_valid_bash_syntax():
    r = subprocess.run([BASH, "-n", UPDATE_SH.as_posix()], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def test_unchanged_config_only_reloads_nginx(box):
    r = box.run()
    assert r.returncode == 0, r.stdout + r.stderr
    calls = box.calls()
    assert "reload" in calls and "run-test" not in calls and not any(c.startswith("restart") for c in calls)
    assert "Обновлено до" in r.stdout


def test_changed_nginx_conf_is_checked_and_nginx_restarted(box):
    box.change_nginx_conf_on_github()
    r = box.run()
    assert r.returncode == 0, r.stdout + r.stderr
    calls = box.calls()
    assert calls.index("run-test") < calls.index("restart nginx")        # сначала проверка, потом перезапуск
    assert "reload" not in calls
    assert "gzip_vary on" in box.container_conf()                          # контейнер теперь видит новый файл
    assert "nginx перезапущен" in r.stdout


def test_broken_new_config_keeps_nginx_on_old_settings_and_fails_loudly(box):
    box.change_nginx_conf_on_github()
    r = box.run(FAKE_NGINX_TEST="fail")
    assert r.returncode == 1
    calls = box.calls()
    assert "run-test" in calls and not any(c.startswith("restart") for c in calls)
    assert "gzip_vary on" not in box.container_conf()                      # старый конфиг остался
    assert "не прошёл проверку" in r.stderr


def test_failed_restart_is_reported(box):
    box.change_nginx_conf_on_github()
    r = box.run(FAKE_RESTART_FAIL=1)
    assert r.returncode == 1 and "Не удалось перезапустить nginx" in r.stderr


def test_stopped_nginx_container_is_restarted_too(box):
    """Контейнер не отвечает на «cat» — это тоже «не совпадает»: после проверки конфига его поднимают."""
    r = box.run(FAKE_NGINX_DOWN=1)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "restart nginx" in box.calls()
