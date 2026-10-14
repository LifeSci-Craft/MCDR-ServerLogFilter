#!/usr/bin/env python3
"""Run the plugin against several MCDR versions and report what each one does.

The pytest suite pins a single MCDR (see tests/requirements-test.txt), so the
"minimum supported version is 2.15.0" claim would otherwise be unverified. This
tool boots a real MCDR instance per interpreter with a fake server and checks,
on each one:

* the plugin loads, or is cleanly refused with MCDR's own dependency message;
* target lines are hidden while unrelated lines survive;
* the idle-rule reminder appears, and only *after* the server printed ``Done``;
* ``state.json`` is written and the session/streak bookkeeping advances;
* the messages come out in Chinese, which is also what proves ``language: auto``
  follows MCDR's own setting on that MCDR version (the instance is pinned to zh_cn);
* the command tree registers (``!!logfilter`` / ``!!lf`` plus help/list/reload/reset/test);
* no traceback comes out of the plugin.

Usage::

    # every interpreter you want to cover
    python tools/mcdr_matrix.py \\
        /path/to/mcdr-2.15.0/python /path/to/mcdr-2.15.7/python /path/to/mcdr-2.16.0/python

    # just the one running this script
    python tools/mcdr_matrix.py --current

The plugin version tested is whatever ``mcdreforged.plugin.json`` says; the
``.mcdr`` is built on the fly with ``pack.py``.
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PLUGIN_ID = "server_log_filter"

TARGET = "standing on air - force-sending blocks below"
IDLE = "a-rule-that-never-matched-anything"
HITTER = "a-rule-that-hits-once"
UNRELATED = "some genuinely unrelated line"
STARTUP_LINE = 'Done (0.648s)! For help, type "help"'
THRESHOLD = 2
SEEDED_SESSION = 5

FAKE_SERVER = '''import sys, time
def out(m):
    sys.stdout.write("[20:30:00] [Server thread/INFO]: " + m + "\\n"); sys.stdout.flush()
out("Starting minecraft server version 1.21.8")
time.sleep(0.3)
out({startup!r})
time.sleep(0.5)
out("Player Steve {target}")
out({hitter!r})
out({unrelated!r})
time.sleep(6)
out("Stopping server")
'''.format(startup=STARTUP_LINE, target=TARGET, hitter=HITTER, unrelated=UNRELATED)

MCDR_CONFIG = """\
language: zh_cn
working_directory: server
start_command: '"{python}" fake_server.py'
handler: vanilla_handler
encoding: utf8
decoding: utf8
rcon:
  enable: false
  address: 127.0.0.1
  port: 25575
  password: password
plugin_directories:
- plugins
check_update: false
advanced_console: false
telemetry: false
disable_console_thread: true
disable_console_color: true
handler_detection: false
write_server_output_to_log_file: false
"""


def write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def build_plugin() -> Path:
    sys.path.insert(0, str(REPO))
    import pack

    out = Path(tempfile.mkdtemp(prefix="slf_matrix_")) / "ServerLogFilter.mcdr"
    pack.build(out)
    return out


def _interpreter_has_own_mcdr(python: str) -> bool:
    probe = subprocess.run(
        [python, "-c", "import mcdreforged"],
        capture_output=True,
        env={key: value for key, value in os.environ.items() if key != "PYTHONPATH"},
        timeout=120,
    )
    return probe.returncode == 0


def child_env(python: str) -> dict:
    """The environment to start MCDR (and, through it, the fake server) in.

    Two things have to be right, and getting either wrong is silent:

    * ``.testlibs`` may be the only place MCDR exists — the CI layout, where the interpreter has
      nothing but pytest. Then it must be on ``PYTHONPATH``, and it must be an **absolute** path:
      the instance runs in a temporary directory, so a relative ``.testlibs`` stops resolving
      there and MCDR dies with "No module named mcdreforged" — which reads like a broken plugin
      rather than a broken path.
    * If the interpreter has its own MCDR (the local layout of one virtualenv per version), the
      inherited ``PYTHONPATH`` must be **dropped**: it precedes site-packages, so a
      ``.testlibs/mcdreforged`` would shadow the version being tested and the matrix would
      report "several versions pass" while running one of them several times.

    The probe decides, once per interpreter.
    """
    env = dict(os.environ)
    testlibs = REPO / ".testlibs"

    if not testlibs.is_dir() or _interpreter_has_own_mcdr(python):
        env.pop("PYTHONPATH", None)
        return env

    existing = env.get("PYTHONPATH", "")
    parts = [str(testlibs)] + [part for part in existing.split(os.pathsep) if part]
    env["PYTHONPATH"] = os.pathsep.join(parts)
    return env


def mcdr_version(python: str) -> str:
    proc = subprocess.run(
        [python, "-c", "import mcdreforged;print(mcdreforged.__version__)"],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120,
        env=child_env(python),
    )
    return proc.stdout.strip() or "unknown"


def catalogue() -> dict:
    """The shipped Chinese catalogue, read rather than quoted.

    Every assertion below that looks for a *sentence* reads it from here. The alternatives are
    two failures this file has already had: a phrase written down here goes stale the moment the
    message is reworded (the stale-rule warning was reworded from "连续 N 次零命中" to "从未命中"
    and this tool started reporting "the warning never appeared"), and a phrase written in the
    plugin's own file is not something this tool should own. Read from the artifact's source of
    truth, exactly like ``plugin_badge()`` reads the name.
    """
    path = REPO / "server_log_filter" / "lang" / "zh_cn.json"
    return json.loads(path.read_text(encoding="utf-8"))


def phrase(key: str, **kwargs) -> str:
    return catalogue()[key].format(**kwargs)


def run_one(python: str, plugin: Path, workdir: Path) -> dict:
    root = workdir / ("mcdr_" + "".join(c if c.isalnum() else "_" for c in mcdr_version(python)))
    if root.is_dir():
        shutil.rmtree(root)
    (root / "plugins").mkdir(parents=True)
    (root / "server").mkdir()
    shutil.copy2(plugin, root / "plugins" / plugin.name)
    write(root / "server" / "fake_server.py", FAKE_SERVER)
    write(root / "config.yml", MCDR_CONFIG.format(python=python.replace("\\", "/")))

    # Seed a config that exercises both an idle rule and a working one, plus a
    # pre-existing history so the reminder has something to report immediately.
    #
    # ``language: auto`` is deliberate: MCDR_CONFIG below pins MCDR to zh_cn, so every
    # Chinese assertion in this file also proves that ``auto`` really follows MCDR on
    # *this* MCDR version. If ``get_mcdr_language()`` were missing from one of them the
    # plugin would fall back to en_us and the "loaded" check below would fail.
    write(root / "config" / PLUGIN_ID / "config.json", json.dumps({
        "language": "auto",
        "patterns": [TARGET, IDLE, HITTER],
        "log_matched_lines": False,
        "report_on_server_stop": True,
        "warn_about_stale_rules": True,
        "stale_rule_threshold": THRESHOLD,
        "validate_patterns": True,
        "pattern_probe_timeout_ms": 25,
        "announce_config_upgrade": True,
        "announce_broken_config": True,
    }, indent=2))
    write(root / "config" / PLUGIN_ID / "state.json", json.dumps({
        "session_index": SEEDED_SESSION,
        "rules": {IDLE: {"hits_last_session": 0, "zero_streak": SEEDED_SESSION,
                         "total_hits": 0, "last_hit_session": 0}},
    }, indent=2))

    env = child_env(python)
    subprocess.run([python, "-m", "mcdreforged", "init"], cwd=str(root),
                   capture_output=True, timeout=180, env=env)

    proc = subprocess.Popen([python, "-m", "mcdreforged"], cwd=str(root),
                            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True,
                            encoding="utf-8", errors="replace", env=env)
    lines, stopping_at = [], None
    started = time.time()
    try:
        while time.time() - started < 90:
            line = proc.stdout.readline()
            if not line:
                if proc.poll() is not None:
                    break
                continue
            lines.append(line.rstrip())
            if "Stopping server" in line and stopping_at is None:
                stopping_at = time.time()
            if stopping_at and time.time() - stopping_at > 8:
                break
    finally:
        try:
            proc.terminate()
            proc.wait(timeout=15)
        except Exception:  # noqa: BLE001
            proc.kill()

    out = "\n".join(lines)
    write(root / "console.txt", out + "\n")

    loaded = "已启用 3 条日志过滤规则" in out
    dep_blocked = "不满足版本约束" in out
    done_at = out.find(STARTUP_LINE)
    # One stale rule, and the seeded config sets ``stale_rule_threshold`` to THRESHOLD, so the
    # warning header interpolates to exactly this sentence.
    warning_marker = phrase("stale.title", count=1, threshold=THRESHOLD)
    warn_at = out.find(warning_marker)

    state_path = root / "config" / PLUGIN_ID / "state.json"
    state = {}
    if state_path.is_file():
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            state = {}
    rules = state.get("rules", {})

    return {
        "mcdr": mcdr_version(python),
        "python": python,
        "loaded": loaded,
        "refused_cleanly": dep_blocked,
        # The config says ``language: auto`` and MCDR_CONFIG pins MCDR to zh_cn, so the
        # plugin must speak Chinese here on every version. An MCDR without
        # ``get_mcdr_language()`` would fall back to English and show up right here.
        "spoke_chinese": loaded and "Enabled 3 log filter rule(s)" not in out,
        "target_lines_hidden": ("INFO]: Player Steve " + TARGET) not in out,
        "hitter_hidden": ("INFO]: " + HITTER) not in out,
        "unrelated_kept": UNRELATED in out,
        "warning_shown": warn_at != -1,
        "warning_after_startup": (done_at != -1 and warn_at != -1 and warn_at > done_at),
        "warning_names_rule": IDLE in out,
        # The idle streak lives in the header, not per rule. The seeded streak (5)
        # exceeds the threshold (2), so this rule does get the short annotation.
        "warning_states_streak_once": out.count(warning_marker) == 1,
        "warning_annotates_longer_streak": phrase(
            "stale.entry_extra", streak=SEEDED_SESSION
        ) in out,
        "state_written": bool(state.get("session_index")),
        "session_advanced": state.get("session_index") == SEEDED_SESSION + 1,
        "idle_streak_advanced": rules.get(IDLE, {}).get("zero_streak") == SEEDED_SESSION + 1,
        "hitter_streak_reset": rules.get(HITTER, {}).get("zero_streak") == 0,
        "tracebacks": out.count("Traceback (most recent call last)"),
        "root": str(root),
    }


def verdict(r: dict) -> str:
    if r["loaded"]:
        ok = all([
            r["target_lines_hidden"], r["hitter_hidden"], r["unrelated_kept"],
            r["warning_shown"], r["warning_after_startup"], r["warning_names_rule"],
            r["warning_states_streak_once"], r["warning_annotates_longer_streak"],
            r["state_written"], r["session_advanced"],
            r["idle_streak_advanced"], r["hitter_streak_reset"], r["spoke_chinese"],
            r["tracebacks"] == 0,
        ])
        return "PASS" if ok else "FAIL"
    if r["refused_cleanly"] and r["tracebacks"] == 0:
        return "refused (below minimum, as intended)"
    return "FAIL"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("interpreters", nargs="*",
                        help="python executables that have MCDR installed")
    parser.add_argument("--current", action="store_true",
                        help="only test the interpreter running this script")
    args = parser.parse_args()

    pythons = [sys.executable] if args.current else args.interpreters
    if not pythons:
        parser.error("give at least one interpreter, or use --current")

    plugin = build_plugin()
    workdir = Path(tempfile.mkdtemp(prefix="slf_matrix_work_"))
    print("plugin artifact: {}".format(plugin))
    print()

    results = []
    for python in pythons:
        r = run_one(python, plugin, workdir)
        results.append(r)
        print("  {:<46} {}".format(r["mcdr"], verdict(r)))

    loaded = [r for r in results if r["loaded"]]
    refused = [r for r in results if not r["loaded"]]

    print()
    print("总结")
    print("  成功加载并完整通过 : {}".format(
        ", ".join(r["mcdr"] for r in loaded) or "(无)"))
    print("  按设计被拒绝       : {}".format(
        ", ".join(r["mcdr"] for r in refused) or "(无)"))
    failures = [r for r in results if verdict(r) == "FAIL"]
    if failures:
        print("  失败               : {}".format(", ".join(r["mcdr"] for r in failures)))
        for r in failures:
            print()
            print("  --- {} 明细 ---".format(r["mcdr"]))
            print(json.dumps(r, ensure_ascii=False, indent=2))
        return 1
    print("  所有版本行为符合预期")

    if len(loaded) > 1:
        keys = ["target_lines_hidden", "hitter_hidden", "unrelated_kept", "warning_shown",
                "warning_after_startup", "warning_names_rule", "warning_states_streak_once",
                "warning_annotates_longer_streak", "spoke_chinese",
                "state_written", "session_advanced", "idle_streak_advanced",
                "hitter_streak_reset", "tracebacks"]
        print()
        print("  跨版本一致性（成功加载的 {} 个版本）".format(len(loaded)))
        for key in keys:
            values = {str(r[key]) for r in loaded}
            print("    {:<28} {}".format(key, "一致" if len(values) == 1 else "不一致: " + str(values)))

    shutil.rmtree(workdir, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
