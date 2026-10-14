"""Unit tests for Server Log Filter.

The suite has two layers, and both run against a **real** MCDR:

1. Behaviour tests exercising the filter directly: matching, counter and reload
   semantics, config defaults, and the error-tolerance guarantees documented in
   the README (a broken regex must not take down the working rules).
2. Contract tests asserting the safety property the whole plugin design rests
   on: a hidden line keeps ``InfoActionFlag.process`` (events keep flowing)
   while losing ``InfoActionFlag.echo_to_console`` (console stays clean), and
   critical lifecycle lines are never matched at all.

MCDR is a hard requirement, not an optional extra: the plugin subclasses
``mcdreforged.api.utils.Serializable`` for its config, so importing
``server_log_filter`` at all needs MCDR present. See tests/README.md for setup.

Run:  python -m pytest tests -v
"""

import ast
import importlib
import json
import os
import pathlib
import re
import shutil
import string
import subprocess
import sys
import tempfile
import time
import zipfile

import pytest
from mcdreforged.api.types import PermissionLevel

import server_log_filter as slf
from server_log_filter import i18n


_PLUGIN_META = json.loads(
    (pathlib.Path(__file__).resolve().parent.parent / "mcdreforged.plugin.json").read_text(
        encoding="utf-8")
)


class _SelfMetadata:
    """The slice of plugin metadata the help title reads.

    Read from the real ``mcdreforged.plugin.json`` rather than hardcoded: a literal drifts
    the moment the version is bumped, and then the title-bar test passes against the stale
    value instead of the shipped one. (It had: the fake said 1.4.0 while the metadata said
    1.4.1.)
    """

    name = _PLUGIN_META["name"]
    version = _PLUGIN_META["version"]


# ---------------------------------------------------------------------------
#  helpers
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def pinned_language(monkeypatch):
    """Pin the module-level language so message assertions cannot leak across tests.

    The plugin resolves its language once per load and keeps it in a module global, so a
    test that loads an ``en_us`` config would otherwise change what the *next* test sees —
    an order-dependent suite. Pinning it to ``zh_cn`` (the language these assertions are
    written in, and what the plugin printed before it had any language support at all)
    makes every test independent. The language tests below set it explicitly, or go
    through ``on_load``, which sets it from the config.
    """
    monkeypatch.setattr(slf, "_language", "zh_cn")


class RecordingLogger:
    """Captures logger calls so tests can assert on warnings / info messages."""

    def __init__(self):
        self.infos = []
        self.warnings = []
        self.errors = []
        self.exceptions = []

    def info(self, msg, *a, **k):
        self.infos.append(str(msg))

    def warning(self, msg, *a, **k):
        self.warnings.append(str(msg))

    def error(self, msg, *a, **k):
        self.errors.append(str(msg))

    def exception(self, msg, *a, **k):
        """MCDR's logger has it and prints the current traceback.

        The fake records the message plus the live exception so a test can assert on the
        diagnostic content without depending on MCDR's formatter; ``sys.exc_info`` is the
        same traceback the real logger would render.
        """
        detail = sys.exc_info()[1]
        rendered = str(msg) if detail is None else "{} ({!r})".format(msg, detail)
        self.exceptions.append(rendered)
        self.errors.append(rendered)


def make_rules(patterns):
    """Compile patterns the same way on_load does, via the real helper."""
    server = FakePluginServer(logger=RecordingLogger())
    return slf._build_rules(server, patterns), server.logger


def make_filter(patterns=None, log_matched=False):
    rules = [slf.Rule(p) for p in (patterns or [slf.DEFAULT_PATTERN])]
    return slf.ServerLogFilter(rules, RecordingLogger(), log_matched)


class FakeInfo:
    """Minimal stand-in for mcdreforged Info (pure-logic layer)."""

    def __init__(self, content, raw_content=None):
        self.content = content
        self.raw_content = raw_content if raw_content is not None else content
        self.action_flag = "UNTOUCHED"


class FakeSource:
    def __init__(self):
        self.replies = []

    def reply(self, msg):
        self.replies.append(msg)

    def has_permission(self, level):
        return True


class FakePluginServer:
    def __init__(
        self,
        logger=None,
        config_data=None,
        state_data=None,
        data_folder=None,
        mcdr_language="zh_cn",
    ):
        self.logger = logger or RecordingLogger()
        self.info_filters = []
        self.help_messages = []
        self.commands = []
        self.config = None
        self.config_data = config_data
        self.state_data = state_data
        self.saved = {}
        self.load_kwargs = {}
        self.data_folder = data_folder
        # Mirrors ``ServerInterface.get_mcdr_language()``. The default is zh_cn because
        # that is the language this suite's message assertions are written in; the
        # language tests override it to prove ``auto`` really follows MCDR.
        self.mcdr_language = mcdr_language

    def get_mcdr_language(self):
        return self.mcdr_language

    def get_self_metadata(self):
        """Mirrors ``ServerInterface.get_self_metadata`` — the help title reads it."""
        return _SelfMetadata()

    def get_data_folder(self):
        """A throwaway folder by default, so no real config file is ever touched."""
        if self.data_folder is None:
            self.data_folder = tempfile.mkdtemp(prefix="slf_cfg_")
        return self.data_folder

    def register_info_filter(self, f):
        self.info_filters.append(f)

    def register_help_message(self, prefix, msg):
        self.help_messages.append((prefix, msg))

    def register_command(self, cmd):
        self.commands.append(cmd)

    def load_config_simple(self, file_name=None, target_class=None, **kwargs):
        if file_name == slf.STATE_FILE_NAME:
            return target_class.deserialize(self.state_data or {})

        self.load_kwargs = kwargs
        # Mirror MCDR: the data_processor runs against the raw dict read from the
        # file, its return value decides whether the file gets written back, and
        # the (possibly mutated) dict is what gets deserialized.
        data = dict(self.config_data) if self.config_data is not None else None
        processor = kwargs.get("data_processor")
        needs_save = False
        if processor is not None and data is not None:
            needs_save = bool(processor(data))

        self.config = target_class.deserialize(data if data is not None else {})
        if needs_save:
            self.saved["config.json"] = self.config.serialize()
        return self.config

    def save_config_simple(self, config, file_name=None, **kwargs):
        data = config.serialize()
        self.saved[file_name] = data
        if file_name == slf.STATE_FILE_NAME:
            # Mirror MCDR writing the file to disk: a later on_load in the same
            # test reads it back, exactly like a plugin reload would.
            self.state_data = data


# The set of noisy lines this plugin exists to suppress (server side, MC 26.3).
NOISY_LINES = [
    "Player Steve standing on air - force-sending blocks below",
    "Player Notch standing on air - force-sending blocks below",
    "Player a_very_long_name_123 standing on air - force-sending blocks below",
]

# Lines that must NEVER be hidden: MCDR relies on them for lifecycle detection
# and general observability.
CRITICAL_LINES = [
    'Done (0.648s)! For help, type "help"',
    "Steve joined the game",
    "Steve left the game",
    "Stopping the server",
    "Stopping server",
    "Steve moved too quickly! 1.2,0.0,3.4",
    "Steve moved wrongly! 0.5",
    "Rejecting UseItemOnPacket from Steve",
    "Steve has too many items, dropping items too fast",
    "Ignoring chat session from Steve due to missing Services public key",
    "Steve lost connection: Disconnected",
    "Starting minecraft server version 1.21.8",
    "[Server thread/INFO]: Preparing level \"world\"",
    "Steve was slain by Zombie",
    "Saving and pausing game...",
]


# ---------------------------------------------------------------------------
#  1. filtering behaviour and the core safety property
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("line", NOISY_LINES)
def test_noisy_lines_are_matched(line):
    assert make_filter().match(line) is not None


@pytest.mark.parametrize("line", CRITICAL_LINES)
def test_critical_lines_are_never_matched(line):
    """The regression guard: 15 realistic lines must not be touched."""
    assert make_filter().match(line) is None


def test_matching_is_substring_not_anchored():
    f = make_filter(["standing on air"])
    assert f.match("prefix Player Steve standing on air - force-sending blocks below suffix")


def test_dot_is_a_regex_wildcard_as_documented():
    """README warns that '.' is a wildcard; verify the documented sharp edge."""
    f = make_filter(["a.c"])
    assert f.match("abc") is not None      # '.' matched 'b'
    f2 = make_filter([r"a\.c"])
    assert f2.match("abc") is None
    assert f2.match("a.c") is not None


def test_near_miss_lines_do_not_match():
    """Proof the plugin is not a blunt 'filter everything'."""
    f = make_filter()
    for line in [
        "Player Steve standing on ground - force-sending blocks below",
        "Player Steve standing on air - sending blocks below",
        "standing on air",
        "",
    ]:
        assert f.match(line) is None, line


def test_hidden_line_keeps_process_flag():
    """THE safety property: hidden, not discarded."""
    try:
        from mcdreforged.api.types import InfoActionFlag
    except ImportError:
        pytest.skip("mcdreforged not installed")
    f = make_filter()
    info = FakeInfo("Player Steve standing on air - force-sending blocks below")
    f.filter_server_info(info)
    assert info.action_flag is InfoActionFlag.hidden()
    assert InfoActionFlag.process in info.action_flag, "events must keep flowing"
    assert InfoActionFlag.echo_to_console not in info.action_flag, "console must be clean"
    assert info.action_flag != InfoActionFlag.discarded(), "must never discard the info"


def test_unmatched_line_is_left_untouched():
    f = make_filter()
    info = FakeInfo('Done (0.648s)! For help, type "help"')
    f.filter_server_info(info)
    assert info.action_flag == "UNTOUCHED", "filter must not mutate unrelated info"


@pytest.mark.parametrize("content", ["", None, "   ", "\t", "\n"])
def test_degenerate_content_does_not_crash(content):
    info = FakeInfo(content)
    make_filter().filter_server_info(info)  # must not raise


def test_falls_back_to_raw_content_when_content_is_none():
    """MCDR may hand over info whose parsed content is empty."""
    f = make_filter()
    info = FakeInfo(None, raw_content="Player Steve standing on air - force-sending blocks below")
    f.filter_server_info(info)
    assert info.action_flag != "UNTOUCHED"


def test_player_name_variation_all_match():
    f = make_filter()
    for name in ["Steve", "玩家A", "x" * 16, "Name With Spaces"]:
        assert f.match("Player {} standing on air - force-sending blocks below".format(name))


# ---------------------------------------------------------------------------
#  2. rule compilation and error tolerance
# ---------------------------------------------------------------------------

def test_invalid_regex_is_skipped_with_warning_and_others_survive():
    rules, logger = make_rules(["[unclosed", slf.DEFAULT_PATTERN])
    assert len(rules) == 1
    assert logger.warnings, "a skipped rule must warn"
    assert rules[0].pattern == slf.DEFAULT_PATTERN


def test_blank_and_whitespace_patterns_are_dropped_silently():
    rules, logger = make_rules(["", "   ", "\t", slf.DEFAULT_PATTERN])
    assert len(rules) == 1
    assert logger.warnings == []


def test_patterns_are_stripped():
    rules, _ = make_rules(["  " + slf.DEFAULT_PATTERN + "  "])
    assert rules[0].pattern == slf.DEFAULT_PATTERN


def test_empty_pattern_list_is_tolerated():
    f = make_filter([])
    assert f.match("anything") is None
    f.filter_server_info(FakeInfo("anything"))  # no crash, nothing hidden


# ---------------------------------------------------------------------------
#  3. counters, reload and reset semantics
# ---------------------------------------------------------------------------

def test_counters_increment_per_rule_independently():
    f = make_filter(["alpha", "beta"])
    for line in ["alpha 1", "beta 1", "alpha 2", "beta 2", "beta 3"]:
        f.filter_server_info(FakeInfo(line))
    assert f.rules[0].count == 2
    assert f.rules[1].count == 3
    assert f.total == 5


def test_unmatched_lines_do_not_increment_counters():
    f = make_filter()
    f.filter_server_info(FakeInfo("nothing to see here"))
    assert f.total == 0


def test_reload_swaps_the_rules_and_drops_removed_ones():
    """规则被换掉后：删掉的规则计数归零，留下的规则接着数（见下面那两条）。"""
    f = make_filter(["alpha"])
    f.filter_server_info(FakeInfo("alpha"))
    assert f.total == 1

    f.reload_rules([slf.Rule("gamma")], False)

    assert f.rules[0].pattern == "gamma"
    assert f.total == 0, "被删掉的规则不该把它的计数留给别人"
    assert f.match("alpha") is None
    assert f.match("gamma") is not None


def test_a_reload_keeps_the_counts_of_rules_that_survive():
    """同一轮的计数必须接着数，否则周期会被切成两段。"""
    f = make_filter(["alpha", "beta"])
    for _ in range(5):
        f.filter_server_info(FakeInfo("alpha"))
    f.filter_server_info(FakeInfo("beta"))

    f.reload_rules([slf.Rule("alpha"), slf.Rule("beta"), slf.Rule("gamma")], False)

    assert [r.count for r in f.rules] == [5, 1, 0], [r.count for r in f.rules]
    assert f.total == 6, "本次已隐去不该被重载清零"


def test_reset_counters_zeroes_total_and_each_rule():
    f = make_filter(["alpha", "beta"])
    f.filter_server_info(FakeInfo("alpha"))
    f.filter_server_info(FakeInfo("beta"))
    f.reset_counters()
    assert f.total == 0
    assert all(r.count == 0 for r in f.rules)


def test_every_matching_rule_is_counted_not_just_the_first():
    """SLF-02 改的正是这条旧语义。

    旧断言把"只给第一条计数"钉成了期望行为（``rules[1].count == 0``），于是被遮蔽的
    规则统计永远是 0、``zero_streak`` 一路涨，插件会建议管理员删掉一条**正在正常
    工作**的规则。现在一行命中几条就记几条；"隐去"仍然只发生一次。
    """
    f = make_filter(["alpha", "alpha beta"])
    f.filter_server_info(FakeInfo("alpha beta"))

    assert f.rules[0].count == 1
    assert f.rules[1].count == 1, "被第一条遮蔽的规则也在工作，必须一起计数"
    assert f.total == 2, "两处计数各自加一（total 记的是命中次数，不是被隐去的行数）"


def test_a_rule_that_really_does_not_match_still_stays_at_zero():
    """放宽到"全部命中都计数"不能变成"全都计数"。"""
    f = make_filter(["alpha", "gamma"])
    f.filter_server_info(FakeInfo("alpha beta"))

    assert f.rules[0].count == 1
    assert f.rules[1].count == 0
    assert f.total == 1


def test_log_matched_lines_flag_writes_to_logger():
    logger = RecordingLogger()
    f = slf.ServerLogFilter([slf.Rule(slf.DEFAULT_PATTERN)], logger, True)
    f.filter_server_info(FakeInfo("Player Steve standing on air - force-sending blocks below"))
    assert any("已隐去" in m for m in logger.infos)


def test_log_matched_lines_disabled_stays_quiet():
    logger = RecordingLogger()
    f = slf.ServerLogFilter([slf.Rule(slf.DEFAULT_PATTERN)], logger, False)
    f.filter_server_info(FakeInfo("Player Steve standing on air - force-sending blocks below"))
    assert logger.infos == []


# ---------------------------------------------------------------------------
#  4. configuration object
# ---------------------------------------------------------------------------

def test_config_defaults_match_documented_json():
    cfg = slf.Config()
    data = cfg.serialize()
    assert data["patterns"] == [slf.DEFAULT_PATTERN]
    assert data["log_matched_lines"] is False
    assert data["report_on_server_stop"] is True
    assert data["warn_about_stale_rules"] is True
    assert data["stale_rule_threshold"] == 3
    assert data["validate_patterns"] is True
    assert data["pattern_probe_timeout_ms"] == 25
    assert data["announce_config_upgrade"] is True
    assert data["announce_broken_config"] is True


def test_config_deserializes_from_json_like_dict():
    cfg = slf.Config.deserialize(
        {
            "patterns": ["standing on air", r"Ignoring chat session from .*"],
            "log_matched_lines": True,
            "report_on_server_stop": False,
        }
    )
    assert len(cfg.patterns) == 2
    assert cfg.log_matched_lines is True
    assert cfg.report_on_server_stop is False


def test_config_round_trip_is_stable():
    cfg = slf.Config.deserialize({"patterns": ["a", "b"], "log_matched_lines": True})
    again = slf.Config.deserialize(cfg.serialize())
    assert again.serialize() == cfg.serialize()


# ---------------------------------------------------------------------------
#  5. the MCDR contract (skipped when MCDR is unavailable)
# ---------------------------------------------------------------------------

def test_info_action_flag_contract_documented_in_readme():
    """README claims hidden() keeps distribution and drops console echo."""
    flag = pytest.importorskip("mcdreforged.api.types").InfoActionFlag
    hidden = flag.hidden()
    assert flag.process in hidden
    assert flag.echo_to_console not in hidden
    assert flag.send_to_server in hidden
    assert hidden != flag.discarded()
    assert flag.discarded() == flag(0)


def test_info_filter_accepts_action_flag_edit():
    """The plugin relies on InfoFilter letting it mutate info.action_flag."""
    info_mod = pytest.importorskip("mcdreforged.info_reactor.info")
    types_mod = pytest.importorskip("mcdreforged.api.types")
    info = info_mod.Info(info_mod.InfoSource.SERVER, "Player Steve standing on air - force-sending blocks below")
    info.content = "Player Steve standing on air - force-sending blocks below"
    f = make_filter()
    f.filter_server_info(info)
    assert types_mod.InfoActionFlag.process in info.action_flag
    assert types_mod.InfoActionFlag.echo_to_console not in info.action_flag


# ---------------------------------------------------------------------------
#  6. command surface
# ---------------------------------------------------------------------------

def test_on_load_registers_filter_help_and_command():
    slf.on_load(FakePluginServer(), None)
    # registration happened on the fake server we just built
    # (rebuild so we can assert on the same instance)
    fake = FakePluginServer()
    slf.on_load(fake, None)
    assert len(fake.info_filters) == 1
    assert [prefix for prefix, _ in fake.help_messages] == ["!!logfilter", "!!lf"]
    assert len(fake.commands) == 1


def test_show_status_reports_rules_and_counts():
    fake = FakePluginServer()
    slf.on_load(fake, None)
    slf._log_filter.filter_server_info(FakeInfo("Player Steve standing on air - force-sending blocks below"))
    source = FakeSource()
    slf._show_status(source)
    text = "".join(str(x) for x in source.replies)
    assert "1" in text  # one rule
    assert "已隐去" in text


def test_show_status_without_rules_warns_user():
    fake = FakePluginServer()
    fake.load_config_simple = lambda target_class=None, **kw: _empty_config(target_class)
    slf.on_load(fake, None)
    source = FakeSource()
    slf._show_status(source)
    text = "".join(str(x) for x in source.replies)
    assert "无规则" in text or "patterns" in text


def _empty_config(target_class):
    cfg = target_class()
    cfg.patterns = []
    return cfg


def test_test_command_reports_hit_and_miss():
    fake = FakePluginServer()
    slf.on_load(fake, None)

    hit = FakeSource()
    slf._test_line(hit, {"text": "Player Steve standing on air - force-sending blocks below"})
    assert "会被隐去" in "".join(str(x) for x in hit.replies)

    miss = FakeSource()
    slf._test_line(miss, {"text": 'Done (0.648s)! For help, type "help"'})
    assert "不会被隐去" in "".join(str(x) for x in miss.replies)


def test_reload_rereads_config_and_reports_count():
    fake = FakePluginServer()
    slf.on_load(fake, None)
    source = FakeSource()
    slf._reload(source)
    text = "".join(str(x) for x in source.replies)
    assert "已重载" in text


class _RecordingSource:
    """A command source that only records replies, for driving MCDR's real parser."""

    def __init__(self, level):
        self.level = level
        self.replies = []

    def has_permission(self, required):
        return self.level >= required

    def get_permission_level(self):
        return self.level

    def reply(self, message, **kwargs):
        self.replies.append(str(message))


def _entry_execute(command, level):
    """Drive MCDR's real parse + requirement check, returning (rejected, executions).

    ``_entry_execute`` is the same entry point ``CommandManager`` uses. Deliberately not
    ``_execute_command``: MCDR *collects* executions there and runs them later, so that
    call proves nothing about whether the callback would fire.
    """
    from mcdreforged.command.builder.exception import RequirementNotMet

    fake = FakePluginServer()
    slf.on_load(fake, None)
    source = _RecordingSource(level)
    try:
        executions = list(fake.commands[0]._entry_execute(source, command))
    except RequirementNotMet:
        return True, []
    return False, executions


@pytest.mark.parametrize("command", [
    "!!logfilter", "!!lf", "!!logfilter help", "!!logfilter list",
    "!!logfilter test hello", "!!logfilter reload", "!!logfilter reset",
])
def test_no_part_of_the_command_surface_is_open_to_players(command):
    """整个命令面都限管理员。

    状态页会印出你写的规则、`test` 会验证日志内容——这些不该让普通玩家看到。
    权限要求只写在根节点上，所以这条同时也在证明 MCDR 会把父节点的要求应用到子命令
    （用 MCDR 自己的 `_entry_execute`，不是对着我们自己的 `_requirements` 断言）。
    """
    rejected, _ = _entry_execute(command, PermissionLevel.USER)
    assert rejected, "{} 不该对普通玩家开放".format(command)


@pytest.mark.parametrize("command", ["!!logfilter", "!!lf", "!!logfilter list"])
def test_an_admin_can_run_them(command):
    """门开着：管理员不但不被拒，而且命令真的排上了执行。"""
    rejected, executions = _entry_execute(command, PermissionLevel.ADMIN)
    assert not rejected, "管理员不该被拒绝"
    assert executions, "命令应该真的被接受了"


def test_the_denial_explains_that_mcdr_permission_is_not_vanilla_op():
    """玩家是 OP 却用不了，是最容易让人困惑的一种失败。

    MCDR 的权限只来自 permission.yml（默认 user），完全不读游戏内的 OP 状态，
    所以默认那句「权限不足」必须补上「怎么办」。
    """
    from mcdreforged.command.builder.exception import RequirementNotMet

    fake = FakePluginServer()
    slf.on_load(fake, None)
    source = _RecordingSource(PermissionLevel.USER)
    try:
        list(fake.commands[0]._entry_execute(source, "!!lf list"))
    except RequirementNotMet as error:
        reason = str(error.get_reason())
    else:
        raise AssertionError("普通玩家不该被放行")

    assert "permission.yml" in reason, reason
    assert "!!MCDR permission set" in reason, reason
    assert "OP" in reason, "要点明与游戏内 OP 无关: {}".format(reason)


def test_the_alias_is_gated_exactly_like_the_long_form():
    """两条字面量在同一个节点上，所以权限不可能只盖住其中一个。"""
    for command in ("!!logfilter list", "!!lf list"):
        rejected, _ = _entry_execute(command, PermissionLevel.USER)
        assert rejected, command


def _click_events(reply):
    """Every (action, value) pair in the serialized reply."""
    from mcdreforged.minecraft.rtext.text import RTextBase

    found = []

    def walk(node):
        if isinstance(node, dict):
            event = node.get("clickEvent")
            if isinstance(event, dict):
                found.append((event.get("action"), event.get("value")))
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(RTextBase.from_any(reply).to_json_object())
    return found


def test_the_help_lines_are_clickable():
    """点一下 = 敲那行命令。"""
    source = FakeSource()
    slf._show_help(source)
    events = _click_events(source.replies[0])

    for command in ("!!lf list", "!!lf reload", "!!lf reset"):
        assert ("run_command", command) in events, (command, events)
    # test 需要跟一段日志文本：直接执行没有意义，只把它填进输入框
    assert ("suggest_command", "!!lf test ") in events, events


def test_the_help_text_stays_usable_as_plain_text():
    """控制台渲染会丢弃点击事件，所以那几行必须**本身就是**可复制的命令。

    这条不是在测 MCDR：它是在钉住「不加按钮也能用」这个前提——万一哪天做成纯按钮，
    控制台里的管理员就什么都看不到了。
    """
    source = FakeSource()
    slf._show_help(source)
    plain = str(source.replies[0])

    for needle in ("!!lf list", "!!lf test", "!!lf reload", "!!lf reset"):
        assert needle in plain, "帮助行里必须直接写着命令: {}".format(needle)
    assert "!!lf" in plain, "别名要能从帮助页发现"


def _find_literal(node, name):
    """Depth-first search of an MCDR command tree for a Literal named `name`.

    MCDR stores sub-literals in ``_children_literal``, a defaultdict mapping the
    literal name to a *list* of candidate nodes.
    """
    children = getattr(node, "_children_literal", None)
    if not isinstance(children, dict):
        return None
    for child in children.get(name, []):
        return child
    for nodes in children.values():
        for child in nodes:
            found = _find_literal(child, name)
            if found is not None:
                return found
    return None


def test_min_mcdr_version_matches_plugin_metadata():
    meta = json.loads((pathlib.Path(__file__).resolve().parent.parent / "mcdreforged.plugin.json").read_text("utf-8"))
    assert meta["dependencies"]["mcdreforged"] == ">=" + slf.MIN_MCDR_VERSION


def test_plugin_metadata_matches_repo_conventions():
    meta = json.loads((pathlib.Path(__file__).resolve().parent.parent / "mcdreforged.plugin.json").read_text("utf-8"))
    assert meta["id"] == "server_log_filter"
    assert meta["license"] == "MIT"
    assert set(meta["description"]) == {"en_us", "zh_cn"}
    assert meta["authors"][0]["name"] == "Pau1am"


# ---------------------------------------------------------------------------
#  7. release packaging
# ---------------------------------------------------------------------------

class _LegalityZip:
    """Stands in for PackedPlugin: feeds MCDR's own legality check a real archive.

    ``_check_dir_legality`` only reads the zip and calls ``get_id``, so this is
    enough to run MCDR's genuine validation without booting an MCDR instance.
    ``_ILLEGAL_ROOT_PY_FILE_STEM`` is copied onto the class by the tests below.
    """

    def __init__(self, path, plugin_id):
        self._zip = zipfile.ZipFile(path)
        self._plugin_id = plugin_id

    def get_id(self):
        return self._plugin_id

    @property
    def _PackedPlugin__zip_file(self):
        return self._zip


def _build_package(tmp_path):
    """Run the shipped packer and return (archive path, member names)."""
    pack = importlib.import_module("pack")
    out = tmp_path / "ServerLogFilter-test.mcdr"
    pack.build(out)
    names = zipfile.ZipFile(out).namelist()
    return out, names


def test_packaged_artifact_is_loadable(tmp_path):
    """The built .mcdr must pass MCDR's own root-entry validation.

    Regression guard: a root-level module (the test suite's own conftest.py, a
    setup.py, ...) makes MCDR refuse the package with IllegalPluginStructure.
    """
    packed = pytest.importorskip("mcdreforged.plugin.type.packed_plugin")
    meta = json.loads(
        (pathlib.Path(__file__).resolve().parent.parent / "mcdreforged.plugin.json").read_text("utf-8")
    )
    out, _ = _build_package(tmp_path)

    checker = _LegalityZip(out, meta["id"])
    type(checker)._ILLEGAL_ROOT_PY_FILE_STEM = packed.PackedPlugin._ILLEGAL_ROOT_PY_FILE_STEM
    # must not raise
    packed.PackedPlugin._check_dir_legality(checker)


def test_packager_excludes_repo_infrastructure(tmp_path):
    """Sanity check on the shipped artifact's contents."""
    _, names = _build_package(tmp_path)

    assert "mcdreforged.plugin.json" in names
    assert "server_log_filter/__init__.py" in names

    for unwanted in ("conftest.py", "pack.py", "tests/test_plugin.py"):
        assert unwanted not in names, unwanted
    assert not [n for n in names if n.startswith(".testlibs/") or n.startswith("tests/")]
    assert not [n for n in names if "__pycache__" in n or n.endswith(".pyc")]


def test_packager_ships_exactly_the_allowlist(tmp_path):
    """Pin the shipped file set so the allowlist cannot drift unnoticed.

    A packaging change that nobody announces shows up to users as "the plugin got
    much bigger for no reason". Listing the exact contents here makes any change
    to that list a deliberate, visible edit.

    The READMEs are deliberately absent: MCDR never reads them, and together they
    accounted for over half the artifact (28 KB -> 14 KB without them).
    """
    _, names = _build_package(tmp_path)

    assert sorted(names) == [
        "CHANGELOG.md",
        "LICENSE",
        "mcdreforged.plugin.json",
        "server_log_filter/__init__.py",
        "server_log_filter/i18n.py",
        "server_log_filter/lang/en_us.json",
        "server_log_filter/lang/zh_cn.json",
    ], names

    for excluded in (
        "README.md",
        "README_en.md",
        # the translator guide: a README by another name, so it stays out too
        "server_log_filter/lang/README.md",
    ):
        assert excluded not in names, "{} must not be shipped".format(excluded)


def test_changelog_keeps_only_the_latest_release():
    """The shipped changelog must not accumulate one section per release.

    CHANGELOG.md travels inside the artifact, so every past entry keeps costing
    users bytes forever -- 1.2.0 -> 1.2.1 was +10.3%, all of it the changelog
    growing. Only the newest entry belongs there; the full history stays on the
    Releases page, where it has already been published.

    Asserting the single heading *equals the current version* is what makes this
    a real constraint: "at most one heading" is satisfied by an empty file too.
    """
    root = pathlib.Path(__file__).resolve().parent.parent
    changelog = (root / "CHANGELOG.md").read_text(encoding="utf-8")
    meta = json.loads((root / "mcdreforged.plugin.json").read_text(encoding="utf-8"))

    headings = re.findall(r"^## \[([^\]]+)\]", changelog, re.M)
    assert headings == [meta["version"]], (
        "CHANGELOG.md should document exactly the shipped version ({}), found {}".format(
            meta["version"], headings
        )
    )

    # no leftover link-reference definitions for the removed versions either
    leftovers = re.findall(r"^\[\d+\.\d+\.\d+\]:", changelog, re.M)
    assert leftovers == [], "stale version links left behind: {}".format(leftovers)


def test_packager_keeps_artifact_small(tmp_path):
    """A stray .testlibs/ once blew this up to 1362 files / 7.11 MB."""
    out, names = _build_package(tmp_path)
    assert len(names) < 20, f"unexpectedly many files packed: {names}"
    assert out.stat().st_size < 200 * 1024


def test_packager_ships_submodules_recursively(tmp_path, monkeypatch):
    """Submodules of the plugin package must not be silently dropped.

    Regression guard: an earlier allowlist used ``rel.parent == Path(PACKAGE_NAME)``,
    which only matches *direct* children. Any ``server_log_filter/sub/helper.py``
    would have been omitted from the release without any error — a silently
    broken artifact. The package is only ``__init__.py`` today, so this test
    plants a submodule in a temporary copy to prove the packer keeps up.
    """
    pack = importlib.import_module("pack")
    src = pathlib.Path(__file__).resolve().parent.parent

    staged = tmp_path / "staged"
    shutil.copytree(
        src, staged, ignore=shutil.ignore_patterns(".git", ".testlibs", "__pycache__", "*.pyc")
    )
    sub = staged / "server_log_filter" / "sub"
    sub.mkdir()
    (sub / "__init__.py").write_text("", encoding="utf-8")
    (sub / "helper.py").write_text("VALUE = 1\n", encoding="utf-8")

    monkeypatch.setattr(pack, "SRC", staged)
    out = tmp_path / "nested.mcdr"
    pack.build(out)
    names = zipfile.ZipFile(out).namelist()

    assert "server_log_filter/sub/__init__.py" in names, names
    assert "server_log_filter/sub/helper.py" in names, names


def test_packager_root_entries_would_be_illegal_if_denylisted():
    """Documents *why* the allowlist exists, using a real denylist build.

    The old README recipe (rglob + tiny skip set) produces an archive that MCDR
    rejects. This test asserts the failure mode is real, so nobody 'simplifies'
    pack.py back into a denylist.
    """
    packed = pytest.importorskip("mcdreforged.plugin.type.packed_plugin")
    meta = json.loads(
        (pathlib.Path(__file__).resolve().parent.parent / "mcdreforged.plugin.json").read_text("utf-8")
    )
    src = pathlib.Path(__file__).resolve().parent.parent

    denylist_skip = {".git", "__pycache__"}
    files = [
        p
        for p in src.rglob("*")
        if p.is_file() and not (denylist_skip & set(p.parts)) and p.suffix != ".pyc"
    ]
    assert any(p.name == "conftest.py" for p in files), "conftest.py must exist for this guard to matter"

    tmp = tempfile.mkdtemp()
    archive = pathlib.Path(tmp) / "denylist.mcdr"
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zf:
        for p in files:
            zf.write(p, p.relative_to(src).as_posix())

    checker = _LegalityZip(archive, meta["id"])
    type(checker)._ILLEGAL_ROOT_PY_FILE_STEM = packed.PackedPlugin._ILLEGAL_ROOT_PY_FILE_STEM
    with pytest.raises(packed.IllegalPluginStructure):
        packed.PackedPlugin._check_dir_legality(checker)


# ---------------------------------------------------------------------------
#  8. stale-rule detection
# ---------------------------------------------------------------------------

IDLE = "a-rule-that-never-matches"
LIVE = slf.DEFAULT_PATTERN


def start_plugin(config=None, state=None, mcdr_language="zh_cn"):
    """Boot the plugin against a fake server and return (server, state_module)."""
    server = FakePluginServer(
        config_data=config, state_data=state, mcdr_language=mcdr_language
    )
    slf.on_load(server, None)
    return server


def run_session(server, hits=None, reach_startup=True):
    """Simulate one full server session: start -> startup -> stop.

    ``hits`` maps pattern -> how many lines that rule matched during the session.
    """
    slf.on_server_start(server)
    if reach_startup:
        slf.on_server_startup(server)
    hits = hits or {}
    for rule in slf._log_filter.rules:
        rule.count = hits.get(rule.pattern, 0)
    slf.on_server_stop(server, 0)


def warnings_of(server):
    return "\n".join(server.logger.warnings)


def test_idle_rule_is_reported_once_the_streak_reaches_the_threshold():
    server = start_plugin(
        config={"patterns": [LIVE, IDLE], "stale_rule_threshold": 2}
    )
    # session 1 and 2: the idle rule never matches -> streak grows to 2
    run_session(server, hits={LIVE: 5})
    assert IDLE not in warnings_of(server), "must not warn before the threshold is reached"
    run_session(server, hits={LIVE: 5})
    assert IDLE not in warnings_of(server), "the warning belongs to the NEXT startup"

    # session 3: the startup now sees streak == 2 and speaks up
    slf.on_server_start(server)
    slf.on_server_startup(server)
    text = warnings_of(server)
    assert IDLE in text
    assert "有 1 条过滤规则已经连续 2 个开服周期一次都没命中过" in text
    # the rule that is working is not mentioned
    assert LIVE not in text


def test_stale_warning_does_not_repeat_the_streak_for_every_rule():
    """Two idle rules must not produce two identical "已连续 N 次…" lines.

    The count is already stated in the header; repeating it per rule pushes the
    useful part (which rules, and what to do) off the screen.
    """
    server = start_plugin(
        config={"patterns": [IDLE, "another-idle-rule", LIVE], "stale_rule_threshold": 1}
    )
    run_session(server, hits={LIVE: 3})
    slf.on_server_start(server)
    slf.on_server_startup(server)

    text = warnings_of(server)
    assert "有 2 条过滤规则已经连续 1 个开服周期一次都没命中过" in text
    # both patterns are listed
    assert "· " + IDLE in text
    assert "· another-idle-rule" in text
    # ...but the per-rule streak sentence appears exactly zero times
    assert "（已连续" not in text
    assert "都没有命中" not in text
    assert text.count("一次都没命中过") == 1, "the header must be stated once"


def test_a_rule_idle_longer_than_the_threshold_still_says_so():
    """Information the header cannot carry is still worth showing.

    The header only promises "threshold or more"; a rule idle far longer than that
    is a stronger signal (probably obsolete rather than mistyped).
    """
    server = start_plugin(config={"patterns": [IDLE], "stale_rule_threshold": 1})
    for _ in range(4):
        run_session(server)                      # never matches -> streak climbs
    slf.on_server_start(server)
    slf.on_server_startup(server)

    text = warnings_of(server)
    assert "连续 1 个开服周期一次都没命中过" in text
    assert "（已连续 4 个周期没命中）" in text, "a longer streak must not be hidden"


def test_a_rule_at_exactly_the_threshold_gets_no_annotation():
    server = start_plugin(config={"patterns": [IDLE], "stale_rule_threshold": 3})
    for _ in range(3):
        run_session(server)
    slf.on_server_start(server)
    slf.on_server_startup(server)

    text = warnings_of(server)
    assert "（已连续" not in text, "nothing to add beyond the header, so add nothing"


def test_a_matching_rule_never_becomes_stale():
    server = start_plugin(config={"patterns": [LIVE], "stale_rule_threshold": 1})
    for _ in range(5):
        run_session(server, hits={LIVE: 3})
        slf.on_server_start(server)
        slf.on_server_startup(server)
        assert "一次都没命中过" not in warnings_of(server)


def test_a_single_hit_resets_the_streak():
    server = start_plugin(config={"patterns": [IDLE], "stale_rule_threshold": 2})

    def streak():
        return slf._state.rules[IDLE].zero_streak

    run_session(server)
    run_session(server)
    assert streak() == 2

    run_session(server, hits={IDLE: 1})  # rule fires once
    assert streak() == 0

    run_session(server)
    assert streak() == 1


def test_a_session_that_never_reached_startup_is_not_counted():
    """A server that fails to boot must not be able to fake an 'idle rule'.

    Otherwise three consecutive crashes would make every rule look unused and
    produce a bogus warning.
    """
    server = start_plugin(config={"patterns": [IDLE], "stale_rule_threshold": 2})
    for _ in range(5):
        run_session(server, reach_startup=False)
    assert slf._state.session_index == 0, "failed startups must not advance the history"
    assert IDLE not in slf._state.rules

    slf.on_server_start(server)
    slf.on_server_startup(server)
    assert "一次都没命中过" not in warnings_of(server)


def test_warning_can_be_switched_off():
    server = start_plugin(
        config={"patterns": [IDLE], "warn_about_stale_rules": False, "stale_rule_threshold": 1}
    )
    run_session(server)
    slf.on_server_start(server)
    slf.on_server_startup(server)
    assert IDLE not in warnings_of(server)
    # ...but the statistics are still collected, just not announced
    assert slf._state.rules[IDLE].zero_streak == 1


def test_threshold_zero_disables_the_warning():
    server = start_plugin(config={"patterns": [IDLE], "stale_rule_threshold": 0})
    for _ in range(4):
        run_session(server)
        slf.on_server_start(server)
        slf.on_server_startup(server)
    assert "一次都没命中过" not in warnings_of(server)


def test_state_forgets_patterns_that_left_the_config():
    server = start_plugin(config={"patterns": [LIVE, IDLE], "stale_rule_threshold": 1})
    run_session(server)
    assert set(slf._state.rules) == {LIVE, IDLE}

    # the admin removes the idle rule
    server.config_data = {"patterns": [LIVE], "stale_rule_threshold": 1}
    slf._apply_config(server)
    run_session(server)
    assert set(slf._state.rules) == {LIVE}, "removed patterns must be pruned from the state file"


def state_rules_of(server):
    return set(server.saved[slf.STATE_FILE_NAME]["rules"])


def test_deleting_a_rule_prunes_its_state_immediately_on_plugin_reload():
    """The admin should not have to wait for a whole server session.

    Pruning used to happen only in on_server_stop, so a rule deleted and the
    plugin reloaded stayed in state.json until the next successful shutdown.
    """
    server = start_plugin(config={"patterns": [LIVE, IDLE], "stale_rule_threshold": 1})
    run_session(server)
    assert state_rules_of(server) == {LIVE, IDLE}

    server.config_data = {"patterns": [LIVE], "stale_rule_threshold": 1}
    slf.on_load(server, None)          # !!MCDR reload plugin

    assert state_rules_of(server) == {LIVE}, "the deleted rule must be gone right away"
    assert IDLE not in slf._state.rules


def test_deleting_a_rule_prunes_its_state_on_the_reload_command():
    server = start_plugin(config={"patterns": [LIVE, IDLE], "stale_rule_threshold": 1})
    run_session(server)

    server.config_data = {"patterns": [LIVE], "stale_rule_threshold": 1}
    source = FakeSource()
    slf._reload(source)

    assert state_rules_of(server) == {LIVE}
    assert "已清除 1 条已删除规则的统计" in "".join(str(x) for x in source.replies)


def test_pruning_does_not_touch_the_file_when_nothing_was_deleted():
    """No change, no write: state.json must not be rewritten on every reload."""
    server = start_plugin(config={"patterns": [LIVE, IDLE], "stale_rule_threshold": 1})
    run_session(server)

    server.saved.clear()
    slf.on_load(server, None)

    assert server.saved == {}, "an unchanged state must not be written back"


def test_a_rule_rejected_by_the_safety_probe_keeps_its_history():
    """Only rules whose text has actually left ``patterns`` may be forgotten.

    Pruning must compare against the *configured* patterns, not the compiled
    rules: a rule that is still written down but gets skipped at load time
    (here, rejected by the catastrophic-backtracking guard after an upgrade)
    is not a deletion, and starting its statistics over would be a regression.
    """
    danger = r"(a+)+$"
    server = start_plugin(
        config={
            "patterns": [LIVE, danger],
            "stale_rule_threshold": 1,
            "validate_patterns": False,
        }
    )
    run_session(server)
    assert danger in slf._state.rules

    # the same two patterns, but the probe is on now, so the dangerous one is skipped
    server.config_data = {
        "patterns": [LIVE, danger],
        "stale_rule_threshold": 1,
        "validate_patterns": True,
    }
    slf._apply_config(server)

    assert all(rule.pattern != danger for rule in slf._log_filter.rules), "it is skipped"
    assert danger in slf._state.rules, "still configured, so still remembered"


def test_a_reset_config_does_not_wipe_the_rule_history(tmp_path):
    """A syntax error must not cost the admin every rule's history.

    When config.json is broken it is quarantined and regenerated with defaults, so
    the patterns seen at that moment are *not* what the user configured. Pruning
    against them would throw away statistics that are recoverable once the file is
    restored by hand.
    """
    server, folder = server_with_config_file(tmp_path, BROKEN_JSON)
    server.state_data = {
        "session_index": 4,
        "rules": {
            LIVE: {"hits_last_session": 2, "zero_streak": 0, "total_hits": 9, "last_hit_session": 4},
            IDLE: {"hits_last_session": 0, "zero_streak": 3, "total_hits": 0, "last_hit_session": 0},
        },
    }

    slf.on_load(server, None)

    assert set(slf._state.rules) == {LIVE, IDLE}, "the history must survive the reset"
    assert slf.STATE_FILE_NAME not in server.saved, "nothing may be written back either"

    # ...and the session-end cleanup must not quietly finish the job either
    run_session(server, hits={LIVE: 1})
    assert set(slf._state.rules) == {LIVE, IDLE}, "the history must survive the session too"


def test_pruning_announces_what_it_removed():
    server = start_plugin(config={"patterns": [LIVE, IDLE], "stale_rule_threshold": 1})
    run_session(server)
    server.logger.infos.clear()

    server.config_data = {"patterns": [LIVE], "stale_rule_threshold": 1}
    slf.on_load(server, None)

    text = "\n".join(server.logger.infos)
    assert "已从 state.json 清除 1 条规则统计" in text
    assert IDLE in text, "the admin needs to know which rule was forgotten"


def test_state_file_round_trips_through_json():
    server = start_plugin(config={"patterns": [IDLE], "stale_rule_threshold": 1})
    run_session(server)
    saved = server.saved[slf.STATE_FILE_NAME]

    import json as _json

    again = slf.State.deserialize(_json.loads(_json.dumps(saved)))
    assert again.serialize() == saved


def test_history_is_kept_between_runs():
    first = start_plugin(config={"patterns": [IDLE], "stale_rule_threshold": 2})
    run_session(first)
    carried = first.saved[slf.STATE_FILE_NAME]

    # a fresh process picks up the file the previous one wrote
    second = start_plugin(config={"patterns": [IDLE], "stale_rule_threshold": 2}, state=carried)
    assert slf._state.session_index == 1
    run_session(second)
    assert second.saved[slf.STATE_FILE_NAME]["rules"][IDLE]["zero_streak"] == 2


def test_reload_does_not_fake_an_idle_session():
    """A plugin reload mid-session must not restart the counters from zero.

    If counts were dropped, a rule that had actually matched would look idle for
    that session and could push an innocent rule over the threshold.
    """
    server = start_plugin(config={"patterns": [LIVE], "stale_rule_threshold": 1})
    slf.on_server_start(server)
    slf.on_server_startup(server)
    for _ in range(4):
        slf._log_filter.filter_server_info(
            FakeInfo("Player Steve standing on air - force-sending blocks below")
        )
    assert slf._log_filter.total == 4

    # !!MCDR reload plugin -> on_load runs again with the PREVIOUS module.
    # MCDR hands the old module object in; simulate that faithfully, because the
    # new module overwrites the globals before carry_over_from() is called.
    old_filter = slf._log_filter

    class PreviousModule:
        _log_filter = old_filter

    slf.on_load(server, PreviousModule)
    assert slf._log_filter is not old_filter, "reload must build a fresh filter"
    assert slf._log_filter.total == 4, "counters must survive a plugin reload"

    slf.on_server_stop(server, 0)
    assert slf._state.rules[LIVE].zero_streak == 0
    assert slf._state.rules[LIVE].hits_last_session == 4


def test_reload_carries_the_running_session_over_to_the_new_module():
    """A real reload builds a *fresh* module, so the 'startup happened' flag resets.

    If the flag were dropped, the reloaded plugin would skip recording the session
    entirely and the server's uptime would silently disappear from the statistics.
    """
    server = start_plugin(config={"patterns": [LIVE], "stale_rule_threshold": 1})
    slf.on_server_start(server)
    slf.on_server_startup(server)
    old_filter = slf._log_filter

    class PreviousModule:
        _log_filter = old_filter
        _session_reached_startup = True

    # simulate the brand-new module MCDR hands to on_load
    slf._session_reached_startup = False
    slf.on_load(server, PreviousModule)
    assert slf._session_reached_startup is True, "the running session must survive a reload"

    slf.on_server_stop(server, 0)
    assert slf._state.session_index == 1, "the session must still be recorded"


def test_status_command_surfaces_the_streak():
    server = start_plugin(config={"patterns": [IDLE], "stale_rule_threshold": 2})
    run_session(server)
    run_session(server)

    source = FakeSource()
    slf._show_status(source)
    text = "".join(str(x) for x in source.replies)
    assert "连续 2 次开服零命中" in text
    assert "开服周期: 2 个" in text


def test_reset_command_clears_the_streak():
    server = start_plugin(config={"patterns": [IDLE], "stale_rule_threshold": 1})
    run_session(server)
    assert slf._state.rules[IDLE].zero_streak == 1

    source = FakeSource()
    slf._reset_streaks(source)
    assert slf._state.rules[IDLE].zero_streak == 0
    assert "已重置" in "".join(str(x) for x in source.replies)
    assert server.saved[slf.STATE_FILE_NAME]["rules"][IDLE]["zero_streak"] == 0


# ---------------------------------------------------------------------------
#  9. catastrophic-backtracking guard
# ---------------------------------------------------------------------------

# Patterns a user could plausibly write, all of which must survive the probe.
REALISTIC_PATTERNS = [
    r"standing on air - force-sending blocks below",
    r"Player \w+ .*",
    r"joined the game|left the game",
    r"moved too quickly! [\d\.,]+",
    r"^\S+ has too many items",
    r"(?:Rejecting|Ignoring) \w+",
    r"Ignoring chat session from \S+ due to missing Services public key",
    r"Preparing spawn area: \d+%",
]


@pytest.mark.parametrize("pattern", REALISTIC_PATTERNS)
def test_realistic_patterns_pass_the_probe(pattern):
    server = FakePluginServer()
    rules = slf._build_rules(server, [pattern])
    assert [r.pattern for r in rules] == [pattern]
    assert server.logger.errors == []


@pytest.mark.parametrize(
    "pattern",
    [r"(a+)+$", r"^(a|a)*$", r"(\w+\s?)*$", r"(a*)*b"],
)
def test_catastrophic_patterns_are_refused(pattern):
    """These freeze MCDR's main thread for hundreds of ms per line, so they must go."""
    server = FakePluginServer()
    rules = slf._build_rules(server, [pattern])
    assert rules == []
    assert any("灾难性回溯" in msg for msg in server.logger.errors)


def test_one_bad_pattern_does_not_take_down_the_others():
    server = FakePluginServer()
    rules = slf._build_rules(server, [LIVE, r"(a+)+$", "joined the game"])
    assert [r.pattern for r in rules] == [LIVE, "joined the game"]


def test_probe_can_be_turned_off():
    server = FakePluginServer()
    rules = slf._build_rules(server, [r"(a+)+$"], validate=False)
    assert [r.pattern for r in rules] == [r"(a+)+$"]


def test_on_load_actually_applies_the_probe():
    """The guard must be wired into the real load path, not merely callable.

    Without this, disabling `validate_patterns` inside ``on_load`` would go
    unnoticed while every direct ``_build_rules`` test kept passing.
    """
    server = start_plugin(config={"patterns": [LIVE, r"(a+)+$"]})
    assert [r.pattern for r in slf._log_filter.rules] == [LIVE]
    assert any("灾难性回溯" in msg for msg in server.logger.errors)


def test_on_load_respects_validate_patterns_false():
    server = start_plugin(config={"patterns": [r"(a+)+$"], "validate_patterns": False})
    assert [r.pattern for r in slf._log_filter.rules] == [r"(a+)+$"]
    assert server.logger.errors == []


def test_on_load_honours_the_timeout_setting():
    """Wiring guard: the configured budget is what the probe actually receives."""
    seen = []
    original = slf._probe_pattern

    def spy(regex, budget_ms):
        seen.append(budget_ms)
        return original(regex, budget_ms)

    slf._probe_pattern = spy
    try:
        start_plugin(config={"patterns": [LIVE], "pattern_probe_timeout_ms": 7})
    finally:
        slf._probe_pattern = original

    assert seen == [7]


def test_a_nonsense_probe_budget_does_not_reject_every_rule():
    """A budget of 0 ms means "every cost is too much", so it used to refuse *all* rules.

    Worse, each refusal blamed catastrophic backtracking — for a pattern that is a plain
    literal. The filter then silently hid nothing while the log pointed at the wrong
    cause. The budget is floored at 1 ms, which still separates microseconds (real rules)
    from hundreds of milliseconds (dangerous ones).

    这里同时钉住**下限真的被传下去**：只看"规则被保留"是不够的——``_probe_pattern``
    内部还会把长探测串的预算夹到至少 1 ms，所以把外面的下限去掉，这条断言仍然会过
    （变异检查里一度就是这样报"存活"）。所以要直接检查传给探测的预算。
    """
    for budget in (0, -5):
        server = FakePluginServer()
        seen = []
        original = slf._probe_pattern

        def spy(regex, budget_ms, _seen=seen, _original=original):
            _seen.append(budget_ms)
            return _original(regex, budget_ms)

        slf._probe_pattern = spy
        try:
            rules = slf._build_rules(server, [LIVE], probe_budget_ms=budget)
        finally:
            slf._probe_pattern = original

        assert [r.pattern for r in rules] == [LIVE], "rule lost at budget={}".format(budget)
        assert server.logger.errors == [], server.logger.errors
        assert seen and min(seen) >= slf.MIN_PROBE_BUDGET_MS, (
            "预算没有被夹到下限，探测拿到的是 {}".format(seen)
        )


def test_probing_realistic_patterns_stays_cheap():
    """Load-time validation must not turn into a startup delay.

    8 realistic patterns cost microseconds in practice; the bound is deliberately
    loose so this stays a regression guard rather than a flaky benchmark.
    """
    server = FakePluginServer()
    start = time.perf_counter()
    rules = slf._build_rules(server, REALISTIC_PATTERNS)
    elapsed = time.perf_counter() - start

    assert len(rules) == len(REALISTIC_PATTERNS)
    assert server.logger.errors == []
    assert elapsed < 0.1, "pattern validation became expensive: {:.3f}s".format(elapsed)


def test_per_line_filtering_stays_in_the_microsecond_range():
    """A blunt but real guard: filtering must not cost milliseconds per line.

    1000 lines with 5 rules should be well under 20 ms (measured: ~1 ms).
    """
    patterns = [LIVE] + ["noise-marker-{}".format(i) for i in range(4)]
    f = slf.ServerLogFilter([slf.Rule(p) for p in patterns], RecordingLogger(), False)
    lines = ["Player Steve standing on air - force-sending blocks below",
             "Steve joined the game"] * 500
    infos = [FakeInfo(line) for line in lines]

    start = time.perf_counter()
    for info in infos:
        f.filter_server_info(info)
    elapsed = time.perf_counter() - start

    per_line_us = elapsed / len(infos) * 1e6
    assert elapsed < 0.02, "filtering {:.1f} µs/line is too slow".format(per_line_us)


def test_probe_accepts_a_compiled_regex_without_recompiling():
    rx = re.compile(LIVE)
    rule = slf.Rule(LIVE, rx)
    assert rule.regex is rx


def test_rule_pattern_is_stripped_and_blank_ones_skipped():
    server = FakePluginServer()
    rules = slf._build_rules(server, ["  " + LIVE + "  ", "", None, "   "])
    assert [r.pattern for r in rules] == [LIVE]


# ---------------------------------------------------------------------------
#  10. lightweight invariants (the hot path must stay cheap)
# ---------------------------------------------------------------------------


def test_hidden_flag_is_built_once_and_reused():
    """InfoActionFlag.hidden() allocates a new flag; the hot path must not call it."""
    f = make_filter()
    assert f._hidden_flag is not None
    assert f._hidden_flag is f._hidden_flag

    info = FakeInfo("Player Steve standing on air - force-sending blocks below")
    f.filter_server_info(info)
    assert info.action_flag is f._hidden_flag, "each hit must reuse the cached flag"


def test_rules_are_stored_immutably():
    f = make_filter([LIVE, "joined the game"])
    assert isinstance(f.rules, tuple)


def test_no_rules_short_circuits_before_reading_the_line():
    """With nothing to filter the plugin must not even touch the info object."""

    class ProbeInfo:
        def __init__(self):
            self.action_flag = "UNTOUCHED"
            self.content_reads = 0

        @property
        def content(self):
            self.content_reads += 1
            return "Player Steve standing on air - force-sending blocks below"

        @property
        def raw_content(self):
            self.content_reads += 1
            return "Player Steve standing on air - force-sending blocks below"

    info = ProbeInfo()
    f = slf.ServerLogFilter([], RecordingLogger(), False)
    f.filter_server_info(info)
    assert info.content_reads == 0
    assert info.action_flag == "UNTOUCHED"


def test_non_matching_line_leaves_the_action_flag_alone():
    f = make_filter()
    info = FakeInfo("Steve joined the game")
    f.filter_server_info(info)
    assert info.action_flag == "UNTOUCHED"


def test_matching_no_longer_short_circuits_but_still_reports_order():
    """热路径不再"遇到第一条就停"——但"第一条命中"的语义仍由 ``match()`` 保留。

    旧用例 ``test_matching_stops_at_the_first_rule`` 断言后面的规则"根本不被求值"，
    那正是 SLF-02 要改掉的：不求值就等于不计数。这里把它反过来钉住。
    """
    later = slf.Rule(r"standing on air - force-sending blocks below")
    f = slf.ServerLogFilter([slf.Rule(LIVE), later], RecordingLogger(), False)
    info = FakeInfo("Player Steve standing on air - force-sending blocks below")
    f.filter_server_info(info)

    assert f.rules[0].count == 1
    assert later.count == 1, "被前面规则遮蔽的规则也必须被求值并计数"
    assert f.match("Player Steve standing on air - force-sending blocks below") is f.rules[0], (
        "哪一条最先命中仍然如实回答"
    )


def test_state_tracking_adds_no_work_to_the_per_line_path():
    """The new feature must be free per line: all bookkeeping happens at session edges.

    Proven structurally — filter_server_info never touches the State object.
    """
    server = start_plugin(config={"patterns": [LIVE], "stale_rule_threshold": 1})
    slf.on_server_start(server)
    slf.on_server_startup(server)

    touched = []
    original_save = server.save_config_simple

    def spy(config, file_name=None, **kwargs):
        touched.append(file_name)
        return original_save(config, file_name=file_name, **kwargs)

    server.save_config_simple = spy
    for _ in range(50):
        slf._log_filter.filter_server_info(
            FakeInfo("Player Steve standing on air - force-sending blocks below")
        )
    assert touched == [], "per-line filtering must not write the state file"
    assert slf._log_filter.total == 50


# ---------------------------------------------------------------------------
#  11. upgrade notification
# ---------------------------------------------------------------------------

def config_options():
    """The real, user-facing configuration options."""
    return set(slf.Config.get_field_annotations())


def test_every_config_option_has_a_description():
    """Coverage invariant for the notes shown when a config gains new options.

    Adding an option without recording its version would make the upgrade notice
    announce a blank entry, so this fails until the option is described — in *every*
    shipped language, since the sentence itself now comes from the catalogues.
    """
    documented = set(slf.CONFIG_DOC)
    assert config_options() == documented, (
        "undocumented options: {}; stale entries: {}".format(
            config_options() - documented, documented - config_options()
        )
    )
    for name, (since, doc_key) in slf.CONFIG_DOC.items():
        assert re.match(r"^\d+\.\d+\.\d+$", since), "{}: bad version {!r}".format(name, since)
        for language in i18n.available_languages():
            text = i18n.translate(doc_key, language)
            assert text != doc_key, "{}: no {} description for {}".format(
                name, language, doc_key
            )
            assert text.strip(), "{}: empty description in {}".format(name, language)


def test_the_generated_config_has_no_comment_fields():
    """The config must stay plain — no bookkeeping keys mixed in with user settings.

    An earlier revision injected ``#option`` fields to imitate comments; it made the
    file look complicated, so it was removed. Nothing but real options may ship.
    """
    data = slf.Config().serialize()
    assert set(data) == config_options()
    assert not [k for k in data if k.startswith("#")]


def test_migrator_reports_the_options_that_were_missing():
    slf._newly_added_options = []
    raw = {"patterns": ["x"]}                      # a 1.0.x era config
    slf._config_migrator(raw)
    assert slf._newly_added_options == [
        "language", "log_matched_lines", "report_on_server_stop",
        "warn_about_stale_rules", "stale_rule_threshold", "validate_patterns",
        "pattern_probe_timeout_ms", "announce_config_upgrade", "announce_broken_config",
    ]


def test_migrator_says_nothing_when_the_config_is_complete():
    slf._newly_added_options = ["stale"]
    assert slf._config_migrator(slf.Config().serialize()) is False
    assert slf._newly_added_options == []


def test_migrator_never_asks_for_a_write():
    """Filling in the missing options is MCDR's job; the hook only observes."""
    slf._newly_added_options = []
    assert slf._config_migrator({"patterns": ["x"]}) is False


def test_migrator_tolerates_a_non_dict_config():
    """A malformed file must not crash the loader before MCDR can regenerate it."""
    assert slf._config_migrator(["not", "a", "dict"]) is False


def test_on_load_wires_the_migrator_into_config_loading():
    """Guard the wiring, not just the function — a correct function that is not
    connected does nothing."""
    server = FakePluginServer(config_data={"patterns": [LIVE]})
    slf.on_load(server, None)
    assert server.load_kwargs.get("data_processor") is slf._config_migrator


def test_reload_command_also_migrates():
    server = start_plugin(config={"patterns": [LIVE]})
    server.load_kwargs = {}
    server.config_data = {"patterns": [LIVE]}       # a stale, partial config appears
    slf._apply_config(server)
    assert server.load_kwargs.get("data_processor") is slf._config_migrator


def announce_output(server):
    return "\n".join(server.logger.infos)


def test_upgrade_announcement_lists_options_with_versions():
    server = FakePluginServer(config_data={"patterns": [LIVE]})
    slf.on_load(server, None)
    text = announce_output(server)
    assert "配置已更新" in text
    for name in ("warn_about_stale_rules", "stale_rule_threshold",
                 "validate_patterns", "pattern_probe_timeout_ms"):
        assert name in text
    assert "v1.1.0 加入" in text


def test_upgrade_announcement_explains_each_new_option():
    """Naming the option is not enough — say what it does."""
    server = FakePluginServer(config_data={"patterns": [LIVE]})
    slf.on_load(server, None)
    text = announce_output(server)
    for name, (_, doc_key) in slf.CONFIG_DOC.items():
        if name in ("patterns", "log_matched_lines", "report_on_server_stop"):
            continue
        # the sentence shown must be the catalogue's, in the language in force
        expected = i18n.translate(doc_key, "zh_cn").split("。")[0]
        assert expected in text, "{}: description missing".format(name)


def test_no_announcement_when_the_config_is_already_current():
    server = start_plugin(config=slf.Config().serialize())
    server.logger.infos.clear()
    slf.on_load(server, None)
    assert "配置已更新" not in announce_output(server)


def test_announcement_is_logged_exactly_once():
    server = FakePluginServer(config_data={"patterns": [LIVE]})
    slf.on_load(server, None)
    assert announce_output(server).count("配置已更新") == 1


def test_upgrade_announcement_can_be_switched_off():
    """``announce_config_upgrade = false`` silences the notice — and nothing else.

    The switch governs the *speaking*, not the filling-in: MCDR still writes the
    missing options into the file (that part is not ours to gate), the admin just
    stops being told about it at every load.
    """
    server = FakePluginServer(
        config_data={"patterns": [LIVE], "announce_config_upgrade": False}
    )
    slf.on_load(server, None)

    assert "配置已更新" not in announce_output(server)
    # the config still got completed — only the announcement is gone
    assert set(slf._config.serialize()) == config_options()
    assert slf._newly_added_options, "the missing options were still detected"


def test_the_upgrade_switch_defaults_to_on():
    server = FakePluginServer(config_data={"patterns": [LIVE]})
    slf.on_load(server, None)
    assert slf._config.announce_config_upgrade is True


def test_the_upgrade_switch_survives_a_reload():
    """Reloading with the switch off must stay quiet the second time too."""
    config = {"patterns": [LIVE], "announce_config_upgrade": False}
    server = start_plugin(config=config)
    server.logger.infos.clear()

    server.config_data = dict(config, validate_patterns=False)
    slf._apply_config(server)

    assert "配置已更新" not in announce_output(server)
    assert slf._newly_added_options, "the reload should still have found new options"


# ---------------------------------------------------------------------------
#  12. a broken config file is preserved, not silently overwritten
# ---------------------------------------------------------------------------

# What a user typically produces by hand-adding a rule and forgetting the comma.
BROKEN_JSON = '''{
    "patterns": [
        "standing on air - force-sending blocks below"
        "my-own-rule"
    ],
    "report_on_server_stop": true
}
'''


def server_with_config_file(tmp_path, text):
    """A fake server whose data folder holds ``text`` as config.json."""
    folder = tmp_path / "cfg"
    folder.mkdir()
    (folder / "config.json").write_text(text, encoding="utf-8")
    return FakePluginServer(data_folder=str(folder)), folder


def test_parse_error_truncated():
    """"line"/"column" must survive into the reported reason: that is the part
    that tells a user where to look."""
    import json

    try:
        json.loads(BROKEN_JSON)
    except ValueError as error:
        reason = "JSON 语法错误：{}".format(error)
    else:
        raise AssertionError("the fixture is supposed to be invalid JSON")
    assert "line" in reason and "column" in reason, reason


def test_broken_config_is_backed_up_before_being_regenerated(tmp_path):
    server, folder = server_with_config_file(tmp_path, BROKEN_JSON)

    slf._quarantine_broken_config(server, slf.CONFIG_FILE_NAME)

    backup = folder / "config.json.old"
    assert backup.is_file(), "the user's file must be preserved"
    assert backup.read_text(encoding="utf-8") == BROKEN_JSON, "byte for byte"
    assert not (folder / "config.json").exists(), "the original is moved aside"


def test_backup_announcement_says_what_where_and_why(tmp_path):
    server, folder = server_with_config_file(tmp_path, BROKEN_JSON)

    slf._quarantine_broken_config(server, slf.CONFIG_FILE_NAME)

    assert len(server.logger.errors) == 1
    text = server.logger.errors[0]
    assert "配置文件无法解析" in text
    assert "已重置为默认配置" in text
    assert "config.json.old" in text, "the user must be told where the backup is"
    assert "JSON 语法错误" in text, "and why it failed"
    assert "line" in text and "column" in text, "and where in the file"
    assert "逗号" in text, "plus the likely cause for this very common mistake"


def test_a_valid_config_is_left_alone(tmp_path):
    server, folder = server_with_config_file(
        tmp_path, json.dumps({"patterns": ["x"]}, indent=4)
    )

    assert slf._quarantine_broken_config(server, slf.CONFIG_FILE_NAME) is None
    assert not (folder / "config.json.old").exists()
    assert server.logger.errors == []


def test_a_missing_config_is_not_an_error(tmp_path):
    """First run: there is nothing to back up and nothing to complain about."""
    folder = tmp_path / "empty"
    folder.mkdir()
    server = FakePluginServer(data_folder=str(folder))

    assert slf._quarantine_broken_config(server, slf.CONFIG_FILE_NAME) is None
    assert server.logger.errors == []
    assert not (folder / "config.json.old").exists()


def test_an_empty_config_file_is_treated_as_broken(tmp_path):
    """A truncated file is a real way to lose a config."""
    server, folder = server_with_config_file(tmp_path, "")

    assert slf._quarantine_broken_config(server, slf.CONFIG_FILE_NAME) is not None
    assert (folder / "config.json.old").is_file()


@pytest.mark.parametrize("text", ['["a", "b"]', '"just a string"', "42"])
def test_json_that_is_not_an_object_is_also_quarantined(tmp_path, text):
    """MCDR would reject these too, so they must not be silently replaced."""
    server, folder = server_with_config_file(tmp_path, text)

    reason = slf._quarantine_broken_config(server, slf.CONFIG_FILE_NAME)

    assert reason is not None and "顶层应为 JSON 对象" in reason
    assert (folder / "config.json.old").read_text(encoding="utf-8") == text


def test_a_second_failure_overwrites_the_previous_backup(tmp_path):
    """One ``.old`` slot: the most recent broken file is the one worth reading."""
    server, folder = server_with_config_file(tmp_path, BROKEN_JSON)
    slf._quarantine_broken_config(server, slf.CONFIG_FILE_NAME)

    (folder / "config.json").write_text("{ even more broken", encoding="utf-8")
    slf._quarantine_broken_config(server, slf.CONFIG_FILE_NAME)

    assert (folder / "config.json.old").read_text(encoding="utf-8") == "{ even more broken"


def test_on_load_checks_the_config_file():
    """Wiring guard: the check has to run on the real load path."""
    checked = []
    server = FakePluginServer(config_data={"patterns": [LIVE]})
    original = slf._quarantine_broken_config

    def spy(srv, name, data=None):
        checked.append(name)
        return original(srv, name, data)

    slf._quarantine_broken_config = spy
    try:
        slf.on_load(server, None)
    finally:
        slf._quarantine_broken_config = original
    assert checked == [slf.CONFIG_FILE_NAME]


def test_reload_command_checks_the_config_file_too():
    server = start_plugin(config={"patterns": [LIVE]})
    checked = []
    original = slf._quarantine_broken_config

    def spy(srv, name, data=None):
        checked.append(name)
        return original(srv, name, data)

    slf._quarantine_broken_config = spy
    try:
        slf._apply_config(server)
    finally:
        slf._quarantine_broken_config = original
    assert checked == [slf.CONFIG_FILE_NAME]


def test_quarantine_survives_an_unwritable_location(tmp_path, monkeypatch):
    """If even the backup fails, the plugin must still load rather than crash."""
    server, folder = server_with_config_file(tmp_path, BROKEN_JSON)

    def boom(src, dst):
        raise OSError("simulated")

    monkeypatch.setattr(slf.os, "replace", boom)
    reason = slf._quarantine_broken_config(server, slf.CONFIG_FILE_NAME)

    assert reason is not None, "the problem is still reported"
    assert any("备份" in m for m in server.logger.errors)


# ---------------------------------------------------------------------------
#  12b. the notice switches for the broken-config path
# ---------------------------------------------------------------------------

def _broken_config_mentioning(line):
    """``BROKEN_JSON`` with an extra line at the top — still unparseable.

    The switch has to end up in the *raw text*: when the file cannot be parsed,
    that text is the only place the admin's choice can be read from.
    """
    return BROKEN_JSON.replace("{\n", "{\n    " + line + "\n", 1)


def test_the_broken_config_switch_is_a_real_option():
    """Drift guard: the name the quarantine peeks for must exist on Config.

    Renaming the field without renaming the constant would leave the switch
    silently dead — it would be written to the file and never looked at.
    """
    assert slf.BROKEN_CONFIG_NOTICE_OPTION in slf.Config.get_field_annotations()


def test_broken_config_notice_can_be_switched_off(tmp_path):
    text = _broken_config_mentioning('"announce_broken_config": false,')
    server, folder = server_with_config_file(tmp_path, text)

    slf._quarantine_broken_config(server, slf.CONFIG_FILE_NAME)

    assert server.logger.errors == [], "the notice was supposed to be silenced"
    assert (folder / "config.json.old").is_file(), "the backup happens regardless"
    assert not (folder / "config.json").exists()


@pytest.mark.parametrize(
    "line, announced",
    [
        ('"announce_broken_config": false,', False),
        ('"announce_broken_config" :  false ,', False),   # layout is free-form
        ('"announce_broken_config": FALSE,', False),      # a hand-written capital
        ('"announce_broken_config": true,', True),
        ('"announce_broken_config": "false",', True),     # a string is not a boolean
        ('"announce_broken_configx": false,', True),      # a longer name is not a match
        (None, True),                                     # absent -> default (on)
    ],
)
def test_the_switch_is_read_leniently_from_the_raw_file(tmp_path, line, announced):
    text = BROKEN_JSON if line is None else _broken_config_mentioning(line)
    server, folder = server_with_config_file(tmp_path, text)

    slf._quarantine_broken_config(server, slf.CONFIG_FILE_NAME)

    assert bool(server.logger.errors) is announced, server.logger.errors
    assert (folder / "config.json.old").is_file(), "the backup always happens"


def test_raw_bool_option_finds_nothing_when_absent():
    assert slf._raw_bool_option('{"a": true}', "b") is None


def test_a_failed_backup_is_reported_even_with_the_notice_off(tmp_path, monkeypatch):
    """Silencing a notice must not silence a *data-loss* error.

    When the file cannot even be moved aside, MCDR is about to overwrite it in
    place. That is not routine chatter, so it gets through the switch on purpose.
    """
    text = _broken_config_mentioning('"announce_broken_config": false,')
    server, _ = server_with_config_file(tmp_path, text)

    def boom(src, dst):
        raise OSError("simulated")

    monkeypatch.setattr(slf.os, "replace", boom)
    slf._quarantine_broken_config(server, slf.CONFIG_FILE_NAME)

    assert any("备份" in m for m in server.logger.errors), server.logger.errors


# ---------------------------------------------------------------------------
#  12. language — the plugin speaks the admin's language
#
#  Every message lives in server_log_filter/lang/<code>.json; the ``language``
#  option picks one, defaulting to ``auto`` (follow MCDR). These tests cover the
#  resolution rules, the wiring into every surface that speaks, and the structural
#  invariants that keep the catalogues honest.
# ---------------------------------------------------------------------------

REPO = pathlib.Path(__file__).resolve().parent.parent
SOURCE_FILES = ["server_log_filter/__init__.py", "server_log_filter/i18n.py"]


def catalogue(language):
    return i18n.get_catalog(language).messages


def placeholders(template):
    """The ``{}`` field names a template uses, e.g. ``{"count"}``."""
    return {name for _, name, _, _ in string.Formatter().parse(template) if name}


def keys_referenced_in_the_code():
    """Every catalogue key the plugin's own source asks for.

    Parsed rather than grepped: a dotted literal such as ``"state.json"`` inside a
    docstring is not a message key, and only real call sites should count.
    """
    keys = set()
    for relative in SOURCE_FILES:
        tree = ast.parse((REPO / relative).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not node.args:
                continue
            target = node.func
            name = getattr(target, "id", None) or getattr(target, "attr", None)
            if name not in ("_t", "say", "translate", "translate_values"):
                continue
            first = node.args[0]
            if isinstance(first, ast.Constant) and isinstance(first.value, str):
                keys.add(first.value)
    keys |= {doc_key for _, doc_key in slf.CONFIG_DOC.values()}
    # Never a literal at a call site: i18n.resolve() hands it back as a note key.
    keys.add(i18n.UNSUPPORTED_KEY)
    # ``_probe_pattern`` returns a key *name* rather than a sentence and the caller
    # formats it later, so its two keys cannot be seen as a literal at a call site.
    keys |= set(slf.PROBE_DETAIL_KEYS)
    # Same shape for the diagnostics the language probes record: the key travels as
    # ``_note()``'s first argument, never as ``_t("...")``.
    keys |= set(slf.NOTE_KEYS)
    return keys


def test_the_language_option_is_a_real_option_defaulting_to_auto():
    assert slf.Config().language == "auto"
    assert slf.LANGUAGE_OPTION in config_options()


def test_auto_follows_mcdr():
    """The documented default: MCDR says en_us, so the plugin does too."""
    server = start_plugin(config={"patterns": [LIVE]}, mcdr_language="en_us")
    assert slf._language == "en_us"
    assert "Enabled 1 log filter rule" in announce_output(server)
    assert server.help_messages == [
        ("!!logfilter", i18n.translate("help.logfilter", "en_us")),
        ("!!lf", i18n.translate("help.logfilter", "en_us")),
    ]


def test_auto_follows_mcdr_in_the_other_direction():
    """Stated separately on purpose: a fallback that happens to be en_us would
    otherwise make ``auto`` look like it works while ignoring MCDR entirely."""
    server = start_plugin(config={"patterns": [LIVE]}, mcdr_language="zh_cn")
    assert slf._language == "zh_cn"
    assert "已启用 1 条日志过滤规则" in announce_output(server)


def test_auto_is_silent_about_an_mcdr_language_we_do_not_ship():
    """``auto`` is a deliberate deferral, so an unsupported MCDR language is not an error.

    The admin never asked for a specific language; nagging them about the one MCDR
    happens to be set to would be noise on every single load.
    """
    server = start_plugin(config={"patterns": [LIVE]}, mcdr_language="ja_jp")
    assert slf._language == i18n.FALLBACK_LANGUAGE
    assert server.logger.warnings == [], server.logger.warnings


def test_an_explicit_language_overrides_mcdr():
    server = start_plugin(
        config={"patterns": [LIVE], "language": "zh_cn"}, mcdr_language="en_us"
    )
    assert slf._language == "zh_cn"
    assert "已启用 1 条日志过滤规则" in announce_output(server)


@pytest.mark.parametrize(
    "written, expected",
    [
        ("EN_us", "en_us"),           # case is free
        ("zh-CN", "zh_cn"),           # a hyphen is as good as an underscore
        ("  en  ", "en_us"),          # surrounding space, and a bare language code
        ("zh", "zh_cn"),
        ("zh_TW", "zh_cn"),           # no zh_tw file: the sibling script is closer
    ],
)
def test_language_codes_are_matched_leniently(written, expected):
    server = start_plugin(config={"patterns": [LIVE], "language": written})
    assert slf._language == expected, written
    assert server.logger.warnings == [], "a value we understood must not be complained about"


def test_an_unknown_language_falls_back_and_says_so():
    server = start_plugin(config={"patterns": [LIVE], "language": "klingon"})
    assert slf._language == i18n.FALLBACK_LANGUAGE
    warnings = "\n".join(server.logger.warnings)
    assert "klingon" in warnings, "the admin must be told their value was ignored"
    assert "en_us" in warnings and "zh_cn" in warnings, "and what the valid values are"


def only_these_catalogues(monkeypatch, files):
    """Point the catalogue loader at an in-memory set of language files.

    ``files`` maps a language code to the *text* of its catalogue. This substitutes the
    package-resource loader rather than a directory on disk, which is the code path a
    packed ``.mcdr`` uses — MCDR imports the plugin straight out of the zip, so the
    catalogues are read through the loader there, never through ``open()``.
    """

    def reader(relative):
        language = relative.rsplit("/", 1)[-1][: -len(".json")]
        text = files.get(language)
        return None if text is None else text.encode("utf-8")

    monkeypatch.setattr(i18n, "_resource_bytes", reader)
    monkeypatch.setattr(
        i18n,
        "_list_lang_files",
        # accepts the optional diagnostics bucket the real function grew in SLF-06
        lambda notes=None: sorted("{}.json".format(k) for k in files),
    )
    i18n.clear_cache()


def test_a_broken_catalogue_falls_back_and_is_reported(monkeypatch):
    """A contributor's broken translation must not break the server.

    ``zh_cn`` is the one that fails, and ``zh_cn`` is what is selected, so every
    message falls back to the readable ``en_us`` catalogue — and the admin is told
    which file to fix.
    """
    only_these_catalogues(
        monkeypatch, {"en_us": json.dumps(catalogue("en_us")), "zh_cn": "{ not json"}
    )
    try:
        server = start_plugin(config={"patterns": [LIVE], "language": "zh_cn"})
        assert slf._language == "zh_cn"
        warnings = "\n".join(server.logger.warnings)
        assert "zh_cn.json" in warnings, warnings
        # messages fall back to the catalogue that does load, rather than showing keys
        assert "Enabled 1 log filter rule" in announce_output(server)
    finally:
        i18n.clear_cache()


def test_two_broken_catalogues_still_do_not_break_the_plugin(monkeypatch):
    """With nothing readable left, the message degrades to its key name.

    That is the deliberate last resort: the key is descriptive on purpose, and it
    beats both an exception and a blank line.
    """
    only_these_catalogues(monkeypatch, {"en_us": "{ not json", "zh_cn": "{ not json"})
    try:
        server = start_plugin(config={"patterns": [LIVE], "language": "zh_cn"})
        assert slf._log_filter is not None, "the plugin still loaded"
        assert "summary.rules_enabled" in announce_output(server)
    finally:
        i18n.clear_cache()


def test_the_packed_plugin_can_still_read_its_catalogues(tmp_path):
    """The failure mode that makes all of this worth pinning.

    MCDR imports a packed plugin **out of the zip**, so ``__file__`` points inside the
    ``.mcdr`` and anything that opens a file next to it fails. When that happens the
    plugin still starts and still filters — it just quietly shows raw keys to every
    user, which no ordinary test would notice. So: build the real artifact, import it
    through the real loader in a subprocess, and require a real sentence to come out.
    """
    pack = importlib.import_module("pack")
    artifact = tmp_path / "ServerLogFilter.mcdr"
    pack.build(artifact)

    script = (
        "import sys\n"
        "sys.path.insert(0, {path!r})\n"
        "import server_log_filter as slf\n"
        "from server_log_filter import i18n\n"
        "print('loaded from', slf.__file__)\n"
        "print('languages', i18n.available_languages())\n"
        "print(i18n.translate('summary.rules_enabled', 'zh_cn', count=2))\n"
        "print(i18n.translate('summary.rules_enabled', 'en_us', count=2))\n"
    ).format(path=str(artifact))
    proc = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env={**os.environ, "PYTHONPATH": str(REPO / ".testlibs")},
    )

    assert proc.returncode == 0, proc.stderr
    assert ".mcdr" in proc.stdout, "the artifact itself must have been imported"
    assert "['en_us', 'zh_cn']" in proc.stdout, proc.stdout
    assert "已启用 2 条日志过滤规则" in proc.stdout, proc.stdout
    assert "Enabled 2 log filter rule(s)" in proc.stdout, proc.stdout


def test_the_language_switches_on_reload_without_a_restart():
    server = start_plugin(config={"patterns": [LIVE], "language": "zh_cn"})
    assert slf._language == "zh_cn"

    server.config_data = {"patterns": [LIVE], "language": "en_us"}
    source = FakeSource()
    slf._reload(source)
    assert slf._language == "en_us"
    assert "Filter rules reloaded" in "".join(str(x) for x in source.replies)


def test_mcdr_language_is_read_from_the_config_dict_when_the_method_is_absent():
    """Belt and braces for an MCDR old enough to lack ``get_mcdr_language()``."""
    server = FakePluginServer(config_data={"patterns": [LIVE]})
    server.get_mcdr_language = None
    server.get_mcdr_config = lambda: {"language": "en_us"}
    slf.on_load(server, None)
    assert slf._language == "en_us"


def test_a_failing_mcdr_language_lookup_does_not_take_the_plugin_down():
    server = FakePluginServer(config_data={"patterns": [LIVE]})

    def boom():
        raise RuntimeError("no language for you")

    server.get_mcdr_language = boom
    server.get_mcdr_config = boom
    slf.on_load(server, None)

    assert slf._language == i18n.FALLBACK_LANGUAGE
    assert slf._log_filter is not None, "the plugin still loaded"


def test_the_broken_config_notice_uses_the_language_from_the_raw_file(tmp_path):
    """The file is unparseable, so its own text is the only source for the language.

    MCDR here is zh_cn; the admin's broken file says en_us. Reporting the reset in
    Chinese would be exactly backwards — that message is the one that has to be read.
    """
    text = _broken_config_mentioning('"language": "en_us",')
    server, _ = server_with_config_file(tmp_path, text)     # MCDR language: zh_cn
    slf._quarantine_broken_config(server, slf.CONFIG_FILE_NAME)

    assert any("cannot be parsed" in m for m in server.logger.errors), server.logger.errors
    assert not any("无法解析" in m for m in server.logger.errors)


def test_english_reaches_the_stale_rule_warning():
    server = start_plugin(
        config={"patterns": [IDLE], "stale_rule_threshold": 1, "language": "en_us"}
    )
    run_session(server)
    slf.on_server_start(server)
    slf.on_server_startup(server)

    text = warnings_of(server)
    assert "have never matched anything across 1 consecutive sessions" in text
    assert "!!logfilter test" in text, "the advice has to survive translation"


def test_english_reaches_the_commands_and_the_run_summary():
    server = start_plugin(config={"patterns": [LIVE], "language": "en_us"}, state=None)

    status = FakeSource()
    slf._show_status(status)
    assert "Rules:" in "".join(str(x) for x in status.replies)

    hit = FakeSource()
    slf._test_line(hit, {"text": "Player Steve " + LIVE})
    assert "would be hidden" in "".join(str(x) for x in hit.replies)

    miss = FakeSource()
    slf._test_line(miss, {"text": 'Done (0.648s)! For help, type "help"'})
    assert "would not be hidden" in "".join(str(x) for x in miss.replies)

    reset = FakeSource()
    slf._reset_streaks(reset)
    assert "Idle-session counters reset" in "".join(str(x) for x in reset.replies)

    # the end-of-run summary counts what the filter itself saw, so drive real
    # matches through it rather than poking the counters
    server.logger.infos.clear()
    slf.on_server_start(server)
    for _ in range(7):
        slf._log_filter.filter_server_info(FakeInfo("Player Steve " + LIVE))
    slf.on_server_stop(server, 0)
    assert any(
        "Hidden 7 server log line(s)" in m for m in server.logger.infos
    ), server.logger.infos


def test_english_reaches_the_prune_announcement_and_the_rule_errors():
    server = FakePluginServer(
        config_data={"patterns": [LIVE], "language": "en_us"},
        state_data={"session_index": 1, "rules": {"a-gone-rule": {}}},
        mcdr_language="en_us",
    )
    slf.on_load(server, None)

    assert any(
        "Cleared 1 rule statistic(s)" in m for m in server.logger.infos
    ), server.logger.infos

    bad = FakePluginServer(logger=RecordingLogger(), mcdr_language="en_us")
    slf._build_rules(bad, ["(unclosed"])
    assert any("failed to compile" in m for m in bad.logger.warnings), bad.logger.warnings

    slf._language = "en_us"
    danger = FakePluginServer(logger=RecordingLogger())
    slf._build_rules(danger, [r"(a+)+$"])
    assert any(
        "catastrophic backtracking" in m for m in danger.logger.errors
    ), danger.logger.errors


def test_english_reaches_the_upgrade_notice():
    server = FakePluginServer(
        config_data={"patterns": [LIVE], "language": "en_us"}, mcdr_language="en_us"
    )
    slf.on_load(server, None)

    text = announce_output(server)
    assert "Config updated" in text
    assert "added in v1.1.0" in text
    assert 'See the "Configuration" section' in text


# --- catalogue invariants ---------------------------------------------------

def test_every_shipped_language_file_loads():
    languages = i18n.available_languages()
    assert languages, "no language files were found"
    assert i18n.FALLBACK_LANGUAGE in languages, "the fallback must ship"
    for language in languages:
        assert i18n.get_catalog(language).error is None, language


def test_every_language_file_has_exactly_the_same_keys():
    """A language missing a key shows the key name to the user instead of a sentence."""
    languages = i18n.available_languages()
    reference = set(catalogue(i18n.FALLBACK_LANGUAGE))
    assert reference
    for language in languages:
        keys = set(catalogue(language))
        assert keys == reference, "{}: missing {}; unexpected {}".format(
            language, sorted(reference - keys), sorted(keys - reference)
        )


def test_every_translation_keeps_the_placeholders():
    """Dropping ``{count}`` turns a sentence into a lie, not just a typo."""
    reference = catalogue(i18n.FALLBACK_LANGUAGE)
    for language in i18n.available_languages():
        translated = catalogue(language)
        for key, template in reference.items():
            assert placeholders(translated[key]) == placeholders(template), (
                "{}: {} lost or gained a placeholder".format(language, key)
            )


def test_every_key_the_code_asks_for_exists_in_every_catalogue():
    keys = keys_referenced_in_the_code()
    assert keys, "the extractor found no keys at all -- it is broken, not the code"
    for language in i18n.available_languages():
        missing = sorted(k for k in keys if k not in catalogue(language))
        assert not missing, "{}: {}".format(language, missing)


def test_no_catalogue_key_is_left_unused():
    """Dead keys ship to every user and quietly stop being maintained."""
    unused = sorted(set(catalogue("en_us")) - keys_referenced_in_the_code())
    assert not unused, "unused catalogue keys: {}".format(unused)


def test_the_config_descriptions_are_exactly_the_options():
    described = {k for k in catalogue("en_us") if k.startswith("config_doc.")}
    expected = {"config_doc." + name for name in config_options()}
    assert described == expected, sorted(described ^ expected)


def test_the_english_catalogue_is_actually_english():
    """Catches a forgotten translation pasted straight back in."""
    for key, template in catalogue("en_us").items():
        cjk = [c for c in template if "\u4e00" <= c <= "\u9fff"]
        assert not cjk, "{} is not translated: {!r}".format(key, template)


# ---------------------------------------------------------------------------
#  14. 1.2.3: an undecodable config, the test-prefix trap, and !!logfilter
# ---------------------------------------------------------------------------


def server_with_raw_config(tmp_path, data):
    """A fake server whose data folder holds exactly ``data`` as config.json."""
    folder = tmp_path / "cfg"
    folder.mkdir()
    (folder / "config.json").write_bytes(data)
    return FakePluginServer(data_folder=str(folder)), folder


def test_a_config_that_is_not_utf8_is_quarantined(tmp_path):
    """ANSI/GBK is what an editor on Chinese Windows writes by default.

    MCDR cannot parse such a file either, so it belongs on the same backup-and-reset path
    as a syntax error. Before the fix it raised UnicodeDecodeError — a ValueError, so the
    ``except OSError`` there did not catch it — and the plugin failed to load at all.
    """
    payload = '{"patterns": ["中文规则"]}'.encode("gbk")
    server, folder = server_with_raw_config(tmp_path, payload)
    slf._language = "zh_cn"

    reason = slf._quarantine_broken_config(server, slf.CONFIG_FILE_NAME)

    assert reason is not None, "an undecodable file is a broken config"
    backup = folder / (slf.CONFIG_FILE_NAME + slf.CONFIG_BACKUP_SUFFIX)
    assert backup.read_bytes() == payload, "the original bytes are kept verbatim"
    assert any("UTF-8" in m for m in server.logger.errors), server.logger.errors
    assert not (folder / slf.CONFIG_FILE_NAME).exists(), "the file was moved aside"


def test_on_load_survives_a_config_that_is_not_utf8(tmp_path):
    """The decode error must not reach on_load: that is what killed the plugin."""
    payload = '{"patterns": ["中文规则"]}'.encode("gbk")
    server, folder = server_with_raw_config(tmp_path, payload)

    slf.on_load(server, None)  # must not raise

    assert slf._log_filter is not None, "the filter is still installed"
    assert (folder / (slf.CONFIG_FILE_NAME + slf.CONFIG_BACKUP_SUFFIX)).exists()
    assert len(slf._log_filter.rules) == 1, "the regenerated default config is in force"


def test_the_test_command_strips_the_console_prefix():
    """``^``-anchored rules are the trap here.

    The filter matches the body MCDR has already stripped, while admins paste the whole
    console line. Without stripping, ``!!logfilter test`` answers "no match" for a rule
    that works in practice — and the user goes off to break a rule that was correct.
    """
    start_plugin(config={"patterns": [r"^Player \w+ joined"]})
    hit = FakeSource()
    slf._test_line(
        hit, {"text": '[12:00:00] [Server thread/INFO]: Player Steve joined the game'}
    )

    text = "".join(str(x) for x in hit.replies)
    assert "命中规则" in text, text
    assert "不匹配" not in text, text
    assert "前缀" in text, "the reply must say the prefix was stripped: {}".format(text)


def test_the_test_command_stays_quiet_without_a_prefix():
    start_plugin(config={"patterns": [LIVE]})
    hit = FakeSource()
    slf._test_line(hit, {"text": "Player Steve " + LIVE})
    assert "前缀" not in "".join(str(x) for x in hit.replies)


def test_status_shows_the_language_and_where_it_came_from():
    """The 1.2.2 headline feature must be checkable without reading the config file."""
    start_plugin(config={"patterns": [LIVE], "language": "en_us"}, state=None)
    fixed = FakeSource()
    slf._show_status(fixed)
    # 标签与值分色了，但拼起来的纯文本不变
    assert "Language: en_us (set in the config)" in "".join(
        str(x) for x in fixed.replies
    )

    start_plugin(config={"patterns": [LIVE]}, state=None, mcdr_language="en_us")
    following = FakeSource()
    slf._show_status(following)
    assert "Language: en_us (following MCDR)" in "".join(
        str(x) for x in following.replies
    )


def test_status_reports_skipped_rules_and_the_last_hit():
    """Both used to be invisible: a rejected rule, and when a rule last fired."""
    start_plugin(
        config={"patterns": [LIVE, r"(a+)+$"]},
        state={"session_index": 3, "rules": {LIVE: {"last_hit_session": 2}}},
    )
    status = FakeSource()
    slf._show_status(status)

    text = "".join(str(x) for x in status.replies)
    assert "已跳过: 1 条规则" in text, text
    assert "上次命中：第 2 个开服周期" in text, text


def test_a_pattern_that_overflows_is_skipped_not_fatal():
    """``a{999999999999}`` is a fat-fingered quantifier, not a re.error.

    ``re.compile`` raises OverflowError for it, which the old ``except re.error`` missed —
    so one typo took down ``on_load`` instead of just that one rule.
    """
    server = FakePluginServer()
    rules = slf._build_rules(server, [LIVE, "a{999999999999}", "joined the game"])

    assert [r.pattern for r in rules] == [LIVE, "joined the game"], "good rules survived"
    assert len(server.logger.warnings) == 1, server.logger.warnings
    assert "999999999999" in server.logger.warnings[0]


def test_a_deeply_nested_pattern_is_skipped_not_fatal():
    """Thousands of nested groups blow the C stack while compiling, not re.error."""
    pattern = "(" * 600 + "a" + ")" * 600
    server = FakePluginServer()

    rules = slf._build_rules(server, [LIVE, pattern])

    assert [r.pattern for r in rules] == [LIVE]
    assert len(server.logger.warnings) == 1


def test_a_template_with_a_bad_attribute_is_returned_raw():
    """The "a bad translation can never raise" promise has to include ``{a.b}``."""
    import server_log_filter.i18n as i18n

    i18n._CATALOGS["xx_broken"] = i18n.Catalog({"t": "{a.b}"}, None)
    try:
        assert i18n.translate("t", "xx_broken", a=1) == "{a.b}"
    finally:
        i18n._CATALOGS.pop("xx_broken", None)


# ---------------------------------------------------------------------------
#  15. 命令面：!!lf 别名与帮助页
# ---------------------------------------------------------------------------

def test_the_root_literal_answers_to_both_spellings():
    """一个节点带两条字面量，而不是两棵会各自漂移的命令树。"""
    fake = FakePluginServer()
    slf.on_load(fake, None)
    root = fake.commands[0]
    assert root.literals == {slf.COMMAND, slf.COMMAND_ALIAS}, root.literals


def test_both_spellings_reach_mcdrs_help_list():
    """用户不该需要猜哪个拼法是「正式」的。"""
    fake = FakePluginServer()
    slf.on_load(fake, None)
    assert [prefix for prefix, _ in fake.help_messages] == list(slf.ROOT_LITERALS)
    assert all(msg for _, msg in fake.help_messages), "help text must not be empty"


def test_the_bare_command_is_wired_to_the_help_screen():
    """接线测试：函数写对了 ≠ 它真的被挂在了这个节点上。"""
    fake = FakePluginServer()
    slf.on_load(fake, None)
    root = fake.commands[0]
    assert getattr(root, "_callback", None) is slf._show_help, \
        "a bare command must land on help, not on the status screen"


def test_the_help_subcommand_is_wired_to_the_help_screen():
    fake = FakePluginServer()
    slf.on_load(fake, None)
    node = _find_literal(fake.commands[0], "help")
    assert node is not None, "there must be a `help` subcommand"
    assert getattr(node, "_callback", None) is slf._show_help


def test_list_still_shows_the_status_screen():
    """`list` 是状态页，这一点不能被帮助页顶掉。"""
    fake = FakePluginServer()
    slf.on_load(fake, None)
    node = _find_literal(fake.commands[0], "list")
    assert node is not None
    assert getattr(node, "_callback", None) is slf._show_status


def test_the_help_screen_lists_every_subcommand():
    fake = FakePluginServer()
    slf.on_load(fake, None)
    source = FakeSource()
    slf._show_help(source)
    text = "".join(str(x) for x in source.replies)

    for needle in ("help", "list", "test", "reload", "reset"):
        assert needle in text, "帮助页漏了 {}: {}".format(needle, text)
    assert slf.COMMAND_ALIAS in text, "别名必须能从帮助页发现"
    # 帮助页会**描述**状态页（所以「规则数」这个词会出现），但它不能是状态页本身：
    # 用只有状态页才有的实时字段来区分。
    assert "已隐去" not in text, "帮助页不该显示运行数据"
    assert "语言：" not in text, "帮助页不该显示运行数据"


def test_the_help_screen_is_translated():
    start_plugin(config={"patterns": [LIVE], "language": "en_us"}, state=None)
    source = FakeSource()
    slf._show_help(source)
    text = "".join(str(x) for x in source.replies)

    assert "Usage: !!logfilter <subcommand>" in text, text
    assert "(!!lf works too)" in text, text
    assert "administrator permission" in text, "帮助页必须说明需要管理员权限"


# ---------------------------------------------------------------------------
#  16. 帮助页的排版与配色
# ---------------------------------------------------------------------------

def _colours(node):
    """Every ``color`` value in a serialized RText tree, in order."""
    from mcdreforged.minecraft.rtext.text import RTextBase

    found = []

    def walk(item):
        if isinstance(item, dict):
            if "color" in item:
                found.append(item["color"])
            for value in item.values():
                walk(value)
        elif isinstance(item, list):
            for value in item:
                walk(value)

    walk(RTextBase.from_any(node).to_json_object())
    return found


def _help_text():
    start_plugin(config={"patterns": [LIVE]}, state=None)
    source = FakeSource()
    slf._show_help(source)
    return str(source.replies[0])


def test_the_title_bar_names_the_plugin_and_the_version():
    start_plugin(config={"patterns": [LIVE]}, state=None)
    plain = str(slf._title_line())

    assert plain.startswith("====="), plain
    assert plain.endswith("====="), plain
    assert "Server Log Filter" in plain, plain
    assert "v" + _PLUGIN_META["version"] in plain, "要出现真实版本号: {}".format(plain)
    # 大约一行宽（MC 聊天默认字体）
    assert 44 <= len(plain) <= 60, "顶栏宽度 {} 列不像一行: {!r}".format(len(plain), plain)


def test_the_title_uses_a_different_colour_for_the_version():
    """名称与版本要凭颜色分得开，而不是靠空格。"""
    start_plugin(config={"patterns": [LIVE]}, state=None)
    colours = _colours(slf._title_line())

    assert colours.count("gold") == 2, "两侧等号同色: {}".format(colours)
    assert "aqua" in colours, colours
    assert "yellow" in colours, colours


def test_the_title_degrades_gracefully_without_metadata():
    """读不到元数据只是少印一个版本号，不该让整条命令变成报错。"""
    class NoMetadata:
        def get_mcdr_language(self):
            return "zh_cn"

    slf._server = NoMetadata()
    try:
        plain = str(slf._title_line())
        start_plugin(config={"patterns": [LIVE]}, state=None)
    finally:
        pass

    assert "Server Log Filter" in plain, plain
    assert "None" not in plain, "版本号取不到时不能印出 None: {!r}".format(plain)


def test_help_rows_are_aligned_in_one_column():
    """说明文字要跟在同一列，不能因为子命令有长有短就左右乱跳。"""
    lines = [line for line in _help_text().splitlines() if line.startswith(slf.COMMAND_ALIAS)]
    assert len(lines) == 5, lines

    columns = {line.index("-- ") for line in lines}
    assert len(columns) == 1, "分隔符没有对齐: {}".format(sorted(columns))
    assert columns.pop() > len(slf.COMMAND_ALIAS) + 1, "命令与说明之间要有间距"


def test_the_help_page_has_no_dark_text():
    """深灰/黑色在深色终端上几乎看不见。

    ⚠️ 必须扫**原对象**而不是 ``str(...)``：转成字符串后颜色信息已经没了，
    扫字符串会永远扫不出东西——这条测试最初就是这么写的，是变异检查
    （「把说明改回 dark_gray」竟然没有被抓住）把它揪出来的。
    """
    start_plugin(config={"patterns": [LIVE]}, state=None)
    source = FakeSource()
    slf._show_help(source)

    for screen in (source.replies[0], slf._title_line()):
        bad = [c for c in _colours(screen) if c in ("dark_gray", "black")]
        assert not bad, "出现了看不清的颜色 {}: {}".format(bad, screen)


def test_the_status_screen_has_no_dark_text_either():
    start_plugin(config={"patterns": [LIVE]}, state=None)
    source = FakeSource()
    slf._show_status(source)
    bad = [c for c in _colours(source.replies[0]) if c in ("dark_gray", "black")]
    assert not bad, "状态页出现了看不清的颜色: {}".format(bad)


def test_the_help_page_does_not_repeat_the_runtime_tip():
    """这屏是命令清单；「服务端日志不受影响」那类注意事项属于状态页。"""
    text = _help_text()

    assert "不受影响" not in text, text
    assert "派发" not in text, text


def test_the_status_screen_still_has_the_runtime_tip():
    """帮助页拿掉了它，但状态页必须留着——否则这条提醒就彻底消失了。"""
    start_plugin(config={"patterns": [LIVE]}, state=None)
    source = FakeSource()
    slf._show_status(source)
    assert "不受影响" in str(source.replies[0])


def _status_text(rules=(LIVE,)):
    start_plugin(config={"patterns": list(rules)}, state=None)
    source = FakeSource()
    slf._show_status(source)
    return source.replies[0]


def _text_colours(node):
    """``(text, colour)`` for every fragment of a serialized RText tree."""
    from mcdreforged.minecraft.rtext.text import RTextBase

    pairs = []

    def walk(item):
        if isinstance(item, dict):
            if "text" in item:
                pairs.append((item["text"], item.get("color")))
            for value in item.values():
                walk(value)
        elif isinstance(item, list):
            for value in item:
                walk(value)

    walk(RTextBase.from_any(node).to_json_object())
    return pairs


def test_the_status_screen_uses_the_same_palette_as_help():
    """两个界面要像一家的：标签 aqua、值 white 或绿、备注 gray。

    ⚠️ 必须逐个标签断言，不能只查「画面里存在 aqua」——提示行也是 aqua，
    那样即使所有标签都退回 gray，断言依然通过（变异检查抓到过这一次）。
    """
    start_plugin(config={"patterns": [LIVE]}, state=None)
    source = FakeSource()
    slf._show_status(source)
    pairs = _text_colours(source.replies[0])

    for key in ("status.rule_count", "status.hidden_now",
                "status.language_label", "status.tip_label"):
        label = i18n.translate(key, "zh_cn")
        assert (label, "aqua") in pairs, "{} 的标签该是 aqua：{!r}".format(key, pairs)
    assert "white" in [c for _, c in pairs], pairs
    assert "gray" in [c for _, c in pairs], pairs


def test_status_rule_rows_have_the_same_separator_as_help_rows():
    """规则行也用 ``--``，而且落在固定列上——和帮助页一个形状。"""
    text = str(_status_text(rules=(LIVE, "joined the game", "moved too quickly")))
    rows = [line for line in text.splitlines() if line.startswith(" [")]
    assert len(rows) == 3, rows

    columns = {r.index("-- ") for r in rows}
    assert len(columns) == 1, "规则行的分隔符没对齐: {}".format(sorted(columns))


def test_both_screens_start_with_the_same_title_bar():
    """两个界面共用同一个顶栏，不各写一份。"""
    start_plugin(config={"patterns": [LIVE]}, state=None)
    help_source, status_source = FakeSource(), FakeSource()
    slf._show_help(help_source)
    slf._show_status(status_source)

    assert str(help_source.replies[0]).splitlines()[0] == str(status_source.replies[0]).splitlines()[0]


def test_the_status_screen_labels_its_language_and_tip():
    """语言与提示也走「标签 + 内容」的形式，跟其它行一致。"""
    start_plugin(config={"patterns": [LIVE], "language": "en_us"}, state=None)
    source = FakeSource()
    slf._show_status(source)          # 不要再调 _status_text()：它会重新启动插件
    text = str(source.replies[0])

    assert "Language: en_us (set in the config)" in text, text
    assert "Note: the server's own log is unaffected" in text, text


def test_a_single_digit_rule_index_has_no_padding():
    """``[ 1]`` 里那个空格是给两位数对齐用的，只有 9 条以内时它是纯噪音。"""
    rows = [line for line in str(_status_text()).splitlines() if line.startswith(" [")]
    assert len(rows) == 1, rows
    assert rows[0].startswith(" [1]"), rows[0]


def test_two_digit_rule_indices_still_line_up():
    """10 条以上才补空格——这时它真的买到了对齐。"""
    patterns = [LIVE] + ["noise-{:03d}-\\w+".format(i) for i in range(1, 12)]
    rows = [line for line in str(_status_text(rules=patterns)).splitlines() if line.startswith(" [")]
    assert len(rows) == 12, len(rows)

    assert rows[0].startswith(" [ 1]"), rows[0]
    assert rows[-1].startswith(" [12]"), rows[-1]
    assert len({r.index("]") for r in rows}) == 1, "两位数时 ] 必须对齐"

    # 「一次都没命中就删掉」的规则也要能看出编号是连续的
    assert [r.split("]")[0].strip(" [") for r in rows] == [str(i) for i in range(1, 13)]


# ---------------------------------------------------------------------------
#  17. 打包时剥掉注释（仓库里保留）
# ---------------------------------------------------------------------------

def _packaged_member(name, tmp_path):
    """The packaged bytes of one member, built through the real packer."""
    out, _ = _build_package(tmp_path)
    with zipfile.ZipFile(out) as zf:
        return zf.read(name)


def test_the_packaged_code_carries_no_comments_or_docstrings(tmp_path):
    """注释留仓库、不进包——这一条把「不进包」钉死。"""
    import io as _io
    import tokenize

    for name in ("server_log_filter/__init__.py", "server_log_filter/i18n.py"):
        source = _packaged_member(name, tmp_path).decode("utf-8")

        comments = [t for t in tokenize.generate_tokens(_io.StringIO(source).readline)
                    if t.type == tokenize.COMMENT]
        assert not comments, "{} 里还有注释: {}".format(name, comments[:2])

        tree = ast.parse(source)
        left = [n for n in ast.walk(tree)
                if isinstance(n, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
                and ast.get_docstring(n, clean=False)]
        assert not left, "{} 里还有 docstring: {}".format(name, [n.name for n in left][:3])

        compile(source, name, "exec")          # 剥完还得能编译


def test_stripping_keeps_line_numbers_so_tracebacks_still_match(tmp_path):
    """包内代码的行号必须与仓库一致，否则报错行号会对不上源码。"""
    for name in ("server_log_filter/__init__.py", "server_log_filter/i18n.py"):
        repo_lines = (pathlib.Path(__file__).resolve().parent.parent / name).read_text(
            encoding="utf-8").count("\n")
        assert _packaged_member(name, tmp_path).decode("utf-8").count("\n") == repo_lines, name


def test_stripping_does_not_eat_a_hash_inside_a_string():
    """用 tokenize 而不是正则，就是为了这个：字符串里的 # 不是注释。"""
    import pack

    source = 'x = "url#fragment"  # real comment\ny = "a # b"\n'
    stripped = pack._blank_comments(source)

    assert '"url#fragment"' in stripped, stripped
    assert '"a # b"' in stripped, stripped
    assert "real comment" not in stripped, stripped


def test_a_docstring_only_body_becomes_pass_and_still_compiles():
    """函数体只有一句 docstring 时，剥完必须补 pass，否则语法错误。"""
    import pack

    source = 'def f():\n    """only a docstring"""\n\ndef g():\n    """doc"""\n    return 1\n'
    stripped = pack._blank_docstrings(source)

    assert "only a docstring" not in stripped, stripped
    compile(stripped, "<stripped>", "exec")
    assert "pass" in stripped.split("def g")[0], stripped


def test_the_reload_command_does_not_fake_an_idle_session():
    """实机流程：服务端运行中改配置、敲 reload、然后服务端停止。

    这是管理员最常走的路径（README 也是这么推荐的）。重载若把计数清零，
    这一轮明明命中过的规则会被写进 state 成「零命中」，连续零命中计数跟着涨，
    最后弹出那条假的「这条规则从没命中过」提醒。
    """
    pattern = "joined the game"
    server = start_plugin(config={"patterns": [pattern], "stale_rule_threshold": 1})
    slf.on_server_startup(server)

    for _ in range(5):
        slf._log_filter.filter_server_info(FakeInfo("Player Steve " + pattern))

    source = FakeSource()
    slf._reload(source)

    assert slf._log_filter.rules[0].count == 5, "重载后计数不该归零"
    assert slf._log_filter.total == 5, "本次已隐去不该被重载清零"
    assert "继续保留" in "".join(str(x) for x in source.replies), "要告诉管理员计数没丢"

    slf.on_server_stop(server, 0)

    entry = slf._state.rules[pattern]
    assert entry.hits_last_session == 5, "写进 state 的必须是真实命中数"
    assert entry.zero_streak == 0, "这条规则命中了，连续零命中计数不该涨"


def test_the_reload_command_still_forgets_a_deleted_rules_counts():
    """另一半：真从配置里删掉的规则，计数就该消失。"""
    server = start_plugin(config={"patterns": ["gone", "kept"]})
    slf.on_server_startup(server)
    for _ in range(3):
        slf._log_filter.filter_server_info(FakeInfo("gone"))
    slf._log_filter.filter_server_info(FakeInfo("kept"))

    server.config_data = {"patterns": ["kept"]}   # 管理员把 gone 删了
    slf._reload(FakeSource())

    assert [r.pattern for r in slf._log_filter.rules] == ["kept"]
    assert slf._log_filter.rules[0].count == 1, "留下的规则要接着数"
    assert slf._log_filter.total == 1, "被删掉的规则不该把计数留下来"


def test_the_test_command_lists_every_matching_rule():
    """只报第一条，管理员会以为另外几条没匹配上，然后去改本来写对的规则。"""
    start_plugin(config={"patterns": ["joined", "the game", "Steve"]})

    source = FakeSource()
    slf._test_line(source, {"text": "Player Steve joined the game"})
    text = "".join(str(x) for x in source.replies)

    for pattern in ("joined", "the game", "Steve"):
        assert pattern in text, "{} 也要报出来: {}".format(pattern, text)


# ---------------------------------------------------------------------------
#  匹配一切的规则：警告但保留
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("pattern", [".", ".*", "^", "a?", "\\w*", ".+", ".{0,}"])
def test_a_pattern_that_hides_everything_is_flagged(pattern):
    """这些规则会把整个控制台变成空白，管理员会以为服务端挂了。"""
    assert slf._matches_everything(re.compile(pattern)), pattern


@pytest.mark.parametrize("pattern", [
    LIVE, IDLE, "\\w", "\\d+", "joined the game", "^\\[", "error|warn", "玩家",
])
def test_normal_patterns_are_not_flagged(pattern):
    """窄规则不可能命中所有探测串——误报会让警告变得不值得看。"""
    assert not slf._matches_everything(re.compile(pattern)), pattern


def test_a_catch_all_rule_is_kept_but_warned():
    """警告，但规则仍然生效：偷偷丢掉管理员写的规则是另一种意外。"""
    server = start_plugin(config={"patterns": [".", LIVE]})
    logger = server.logger

    assert len(slf._log_filter.rules) == 2, "规则要保留（不是被拒）"
    assert slf._warned_patterns == ["."], slf._warned_patterns
    assert slf._rejected_patterns == [], "这不是「被拒绝」，是「被警告」"
    assert any("匹配一切" in w for w in logger.warnings), logger.warnings

    # 确实仍然生效
    info = FakeInfo("Player Steve joined the game")
    slf._log_filter.filter_server_info(info)
    assert info.action_flag is not None


def test_the_status_screen_says_which_rule_hides_everything():
    start_plugin(config={"patterns": [".", LIVE]})
    source = FakeSource()
    slf._show_status(source)
    text = str(source.replies[0])

    assert "匹配一切" in text, text
    assert ". / .* / ^" in text, text          # 顺带给出常见写法错误
    assert "已跳过" not in text, "它不是被跳过的规则"


def test_a_catch_all_warning_is_not_mistaken_for_a_backtracking_rejection():
    """两条守卫职责不同，别混成一个桶：回溯是「会卡」，匹配一切是「会静音」。"""
    server = start_plugin(config={"patterns": [".", "(a+)+$"]})
    del server

    assert slf._warned_patterns == ["."], slf._warned_patterns
    assert slf._rejected_patterns == ["(a+)+$"], slf._rejected_patterns


# ---------------------------------------------------------------------------
#  重复规则
# ---------------------------------------------------------------------------

def test_the_same_rule_written_twice_becomes_one():
    server = start_plugin(config={"patterns": [LIVE, LIVE]})
    del server

    assert len(slf._log_filter.rules) == 1, "同一条写两次只该留一条"
    assert slf._duplicate_patterns == [LIVE], slf._duplicate_patterns


def test_a_duplicate_is_reported_and_does_not_poison_the_statistics():
    """重复的那条永远 0 命中，却和第一条共用 state 条目——会喂出假的「从没命中过」。"""
    server = start_plugin(config={"patterns": [LIVE, LIVE], "stale_rule_threshold": 1})
    assert any("重复" in w for w in server.logger.warnings), server.logger.warnings

    source = FakeSource()
    slf._show_status(source)
    text = str(source.replies[0])
    assert "重复" in text, text

    # 真的命中之后，这一轮不该被判成零命中
    slf.on_server_startup(server)
    for _ in range(3):
        slf._log_filter.filter_server_info(FakeInfo("Player Steve " + LIVE))
    slf.on_server_stop(server, 0)

    entry = slf._state.rules[LIVE]
    assert entry.hits_last_session == 3, entry.hits_last_session
    assert entry.zero_streak == 0, "命中了就不该涨零命中计数"


def test_deduplication_keeps_the_order_of_first_appearance():
    server = start_plugin(config={"patterns": ["b", "a", "b", "a", "c"]})
    del server
    assert [r.pattern for r in slf._log_filter.rules] == ["b", "a", "c"]


# ---------------------------------------------------------------------------
#  18. 打包可复现
# ---------------------------------------------------------------------------

def test_packing_the_same_source_twice_gives_identical_bytes(tmp_path):
    """同样源码必须打出同样字节，否则用户无法「重建后比对 sha256」来验证发布资产。"""
    import pack

    first = tmp_path / "first.mcdr"
    second = tmp_path / "second.mcdr"
    pack.build(first)

    # 让所有源文件的 mtime 都变一下：旧实现会因此改变 zip 里的时间戳
    import os
    import time

    now = time.time()
    for path in pack.collect():
        os.utime(path, (now, now))

    pack.build(second)

    assert first.read_bytes() == second.read_bytes(), "两次打包的字节不同"
    assert first.stat().st_size == second.stat().st_size


def test_every_member_carries_the_pinned_timestamp(tmp_path):
    """不是「恰好两次一样」，而是每个成员都用同一个常量时间戳。"""
    import pack

    out, names = _build_package(tmp_path)
    with zipfile.ZipFile(out) as zf:
        stamps = {i.date_time for i in zf.infolist()}
    assert stamps == {pack.FIXED_ZIP_TIMESTAMP}, stamps
    assert len(names) == 7


def test_the_changelog_has_no_duplicate_headings():
    """同一层的小节标题不该出现两次。

    这条是给「脚本反复编辑 CHANGELOG」这个流程兜底的：两次编辑各插了一个
    ``### 开发``，文件看起来仍然合法，人眼容易漏。（本项目已犯过一次。）
    """
    text = (pathlib.Path(__file__).resolve().parent.parent / "CHANGELOG.md").read_text(
        encoding="utf-8")
    headings = [line.strip() for line in text.splitlines() if line.startswith("#")]
    duplicates = {h for h in headings if headings.count(h) > 1}
    assert not duplicates, "CHANGELOG 里有重复标题: {}".format(sorted(duplicates))


# ---------------------------------------------------------------------------
#  16. 回溯探测的规模盲区（SLF-01）与配置上限（SLF-09）
#
#  **这一组里的长输入必须放进带超时的子进程。** 指数级回溯的正则一旦被真正跑起来，
#  单次 ``re.search`` 就会把测试进程钉死几分钟到几小时——纯 Python 给 C 层的
#  ``re.search`` 加不了超时，能指望的只有「在外面套一层进程超时」。
# ---------------------------------------------------------------------------

_PROBE_SUBPROCESS_TIMEOUT = 120.0


def probe_in_subprocess(pattern, budget_ms=25, timeout=_PROBE_SUBPROCESS_TIMEOUT):
    """在子进程里跑一次 ``_probe_pattern``，返回耗时秒数。

    超时就直接失败并说清是哪条模式：那意味着探测本身被拖住了，而不是"测试写得慢"。
    """
    snippet = (
        "import re, sys, time\n"
        "sys.path.insert(0, {repo!r})\n"
        "sys.path.insert(0, {libs!r})\n"
        "import server_log_filter as slf\n"
        "rx = re.compile(sys.argv[1])\n"
        "start = time.perf_counter()\n"
        "slf._probe_pattern(rx, int(sys.argv[2]))\n"
        "print(time.perf_counter() - start)\n"
    ).format(
        repo=str(REPO),
        libs=str(pathlib.Path(__file__).resolve().parent.parent / ".testlibs"),
    )
    try:
        done = subprocess.run(
            [sys.executable, "-c", snippet, pattern, str(budget_ms)],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        pytest.fail(
            "探测在 {:.0f}s 内没有返回，说明它自己被这条模式拖住了: {!r}".format(
                timeout, pattern
            )
        )
    assert done.returncode == 0, done.stderr
    return float(done.stdout.strip())


def test_the_probe_is_growth_based_not_length_fixed():
    """旧版是**固定 22 字符**探测串，于是 `(a|aa)+$` 从探测里溜过去了。

    实测（本机，见本次修复报告）：22 字符 4.7 ms、24 字符 12 ms、26 字符 33 ms、
    30 字符 234 ms——每 +2 字符约 ×2.6。22 字符那一点低于 25 ms 预算，所以旧探测
    判定"通过"；而它在真实日志行长度上会让 MCDR 主线程**每行都卡**。

    现在探测串最长的仍是 12/24 与接近真实行长的文本档，靠的是**增长率**：同一个
    模式的 t(2n)/t(n) 会远高于阈值。这条用例就是钉住"它不再靠一个固定长度下结论"。
    """
    assert len(slf.GROWTH_PROBE_LENGTHS) == 2, "增长判据需要 n 与 2n 两个长度"
    assert slf.GROWTH_PROBE_LENGTHS[1] == 2 * slf.GROWTH_PROBE_LENGTHS[0]
    # 每个字母表一对，否则 `(x+x+)+y` 这类只在特定字符上爆炸的模式会漏掉
    assert len(slf.GROWTH_PROBE_PAIRS) >= 2
    assert slf.PROBE_GROWTH_RATIO < 100, "阈值太松就抓不住指数曲线"
    # 接近真实日志行长的档必须存在（真实 MC 行普遍 60-300 字符）
    assert max(len(s) for s in slf.PROBE_SUBJECTS) >= 60


def test_the_probe_bounds_its_own_worst_case():
    """探测长度必须小到对最坏的模式也能跑完。

    `(a|aa)+$` 在 40 字符上要 31 s、200 字符上约 10^40 步。所以增长档的长度上限
    是硬约束：一旦有人把它调大（想让信号更强），守卫自己就变成了一次挂死。
    """
    assert max(slf.GROWTH_PROBE_LENGTHS) <= 32, (
        "增长档太长会让指数级模式自己把探测卡住：{}".format(slf.GROWTH_PROBE_LENGTHS)
    )


@pytest.mark.parametrize(
    "pattern",
    [
        r"(a|aa)+$",
        r"(\w+\s?)*$",
        r"(a+)+$",
        r"^(a|a)*$",
        r"(a*)*b",
        r"^(x+x+)+y",
        r"(a+){2,}$",
    ],
)
def test_an_exponential_pattern_is_refused_by_the_real_probe(pattern):
    """每一条都在**独立子进程**里跑：其中几条在长串上是无界的。"""
    elapsed = probe_in_subprocess(pattern)
    assert elapsed < 30.0, "探测本身花了 {:.1f}s: {!r}".format(elapsed, pattern)
    server = FakePluginServer()
    rules = slf._build_rules(server, [pattern])
    assert rules == [], "{!r} 应该被拦下".format(pattern)
    assert any("灾难性回溯" in msg for msg in server.logger.errors)


def test_the_pattern_the_old_probe_let_through_is_now_caught():
    """SLF-01 的直因：这条模式在旧探测（固定 22 字符）下**通过**。

    它必须现在被拒——这是整条发现的验收条件。放进子进程是因为它在真实日志行长度上
    会让 `re.search` 跑几十小时。
    """
    pattern = r"(a|aa)+$"
    elapsed = probe_in_subprocess(pattern)
    assert elapsed < 30.0
    verdict = slf._probe_pattern(re.compile(pattern), 25)
    assert verdict is not None, "旧探测放过的模式必须被新探测拦下"
    assert verdict[0] in slf.PROBE_DETAIL_KEYS, verdict


def test_a_long_probe_subject_carries_the_cost_signal():
    """接近真实日志行长的探测串必须真的在比较耗时的档上。

    只留短串的话，"只在长输入上变坏"的模式就被漏掉了；这条用例钉住那个档确实存在，
    而且对一条正常规则它仍然是免费的（微秒级）。
    """
    long_subjects = [s for s in slf.PROBE_SUBJECTS if len(s) >= 60]
    assert long_subjects, "缺少接近真实日志行长的探测档"
    rx = re.compile(r"standing on air - force-sending blocks below")
    for subject in long_subjects:
        start = time.perf_counter()
        rx.search(subject)
        assert time.perf_counter() - start < 0.01, subject


def test_probing_an_exponential_pattern_does_not_stall_the_command_thread():
    r"""最坏情况必须留在"值得付"的量级：一次加载可以花到秒级，但不能是分钟级。

    实测最坏是 `(\w+\s?)*$` 约 1.5 s（一个探测串长度的单次 search），其余在百毫秒级。
    """
    for pattern in (r"(a|aa)+$", r"(a*)*b", r"(x+x+)+y"):
        elapsed = probe_in_subprocess(pattern)
        assert elapsed < 5.0, "{!r} 的探测花了 {:.2f}s".format(pattern, elapsed)


def test_a_huge_probe_budget_is_clamped_with_a_warning():
    """SLF-09：`pattern_probe_timeout_ms` 只有下限没有上限，写大值会卡住加载期。

    上限与探测改造是一致关系：探测是在 on_load（以及 ``!!lf reload`` 的命令线程）
    上同步跑的，所以这个值必须有天花板，且钳制时要说话。
    """
    server = FakePluginServer()
    seen = []
    original = slf._probe_pattern

    def spy(regex, budget_ms):
        seen.append(budget_ms)
        return original(regex, budget_ms)

    slf._probe_pattern = spy
    try:
        rules = slf._build_rules(
            server, [LIVE], probe_budget_ms=10 ** 9
        )
    finally:
        slf._probe_pattern = original

    assert [r.pattern for r in rules] == [LIVE]
    assert seen == [slf.MAX_PROBE_BUDGET_MS], seen
    assert any("pattern_probe_timeout_ms" in m for m in server.logger.warnings)


def test_a_tiny_probe_budget_is_still_floored():
    """下限的行为不能被上限改动顺手弄丢。"""
    server = FakePluginServer()
    seen = []
    original = slf._probe_pattern

    def spy(regex, budget_ms):
        seen.append(budget_ms)
        return original(regex, budget_ms)

    slf._probe_pattern = spy
    try:
        slf._build_rules(server, [LIVE], probe_budget_ms=0)
    finally:
        slf._probe_pattern = original

    assert seen == [slf.MIN_PROBE_BUDGET_MS], seen


def test_a_budget_inside_the_range_is_not_announced():
    """没钳制过就不该说话——否则每次加载都刷一条无意义警告。"""
    server = FakePluginServer()
    rules = slf._build_rules(server, [LIVE], probe_budget_ms=17)
    assert [r.pattern for r in rules] == [LIVE]
    assert not any("pattern_probe_timeout_ms" in m for m in server.logger.warnings)


def test_the_probe_documents_that_it_is_only_a_heuristic():
    """发现里明确要求把「探测不是形式化保证」写进开发文档——它是启发式。"""
    text = (pathlib.Path(__file__).resolve().parent.parent / "docs" / "DEVELOPMENT.md").read_text(
        encoding="utf-8"
    )
    assert "启发式" in text and "形式化保证" in text


# ---------------------------------------------------------------------------
#  17. 一行命中多条规则时的统计（SLF-02 / SLF-05 / SLF-13）
# ---------------------------------------------------------------------------

BROAD = r"Player Steve standing on air"
SHADOWED = r"standing on air - force-sending blocks below"
SHADOW_LINE = "Player Steve standing on air - force-sending blocks below"


def test_a_shadowed_rule_is_not_reported_as_never_matching():
    """SLF-02 的核心：被前面规则遮蔽的规则**正在工作**，不能报它「从未命中」。

    旧实现"遇到第一条匹配就 return 并且只给它计数"，于是第二条规则
    ``count == 0 / zero_streak == 3``，控制台被告知「连续 N 次零命中」并**建议管理员
    把它从 patterns 里删掉**——而删掉后对应日志立刻重新刷屏。

    修复后一行命中的每条规则都计数，所以它的 ``zero_streak`` 一直是 0。
    （顺带说明为什么不能只改判据：被遮蔽的规则 ``count`` 确实是 0，
    "本轮 count == 0" 区分不出"被遮蔽"与"真的没命中"。）
    """
    server = start_plugin(
        config={"patterns": [BROAD, SHADOWED], "stale_rule_threshold": 2}
    )
    for _ in range(4):
        run_session(server, hits={BROAD: 3, SHADOWED: 3})
    assert slf._state.rules[SHADOWED].zero_streak == 0

    for _ in range(4):
        slf.on_server_start(server)
        slf.on_server_startup(server)          # 这里才是旧实现误报的位置
        for _ in range(3):
            slf._log_filter.filter_server_info(FakeInfo(SHADOW_LINE))
        slf.on_server_stop(server, 0)

    text = warnings_of(server)
    assert SHADOWED not in text, "被遮蔽的规则正在工作，不该被建议删除: " + text
    # 两条规则都真的在计数——这正是"不只是判据放宽"的证据
    assert [r.count for r in slf._log_filter.rules] == [3, 3]


def test_a_rule_that_matched_this_session_is_never_reported():
    """判据里「本轮 count == 0」这一条的直接验收。"""
    server = start_plugin(config={"patterns": [IDLE], "stale_rule_threshold": 1})
    for _ in range(3):
        run_session(server)                      # streak climbs to 3
    assert slf._state.rules[IDLE].zero_streak == 3
    server.logger.warnings.clear()

    slf.on_server_start(server)
    slf.on_server_startup(server)
    assert IDLE in warnings_of(server), "前置条件：这一轮它还没命中过"

    server.logger.warnings.clear()
    slf.on_server_start(server)
    for rule in slf._log_filter.rules:           # 这一轮它命中了
        rule.count = 1
    slf.on_server_startup(server)
    assert IDLE not in warnings_of(server), "本轮命中过的规则不该出现在零命中提醒里"


def test_a_stale_streak_is_not_re_announced_after_a_skipped_session():
    """SLF-05：中间有一个周期没走到 startup 时，已修好的规则不再被重复告警。

    管理员把写错的规则改对之后，若恰好有一个开服周期没走到 startup（崩溃 / 手动
    kill），``zero_streak`` 会停在阈值上。旧实现下一次成功开服时先读旧值再告警，
    于是刚修好的规则又被报一次——与之相反的正是注释里声称的防护目标。
    """
    server = start_plugin(config={"patterns": [IDLE], "stale_rule_threshold": 2})
    run_session(server)
    run_session(server)
    assert slf._state.rules[IDLE].zero_streak == 2

    # 一个没走到 startup 的周期：不结算，也不该改变 streak
    run_session(server, reach_startup=False)
    assert slf._state.rules[IDLE].zero_streak == 2

    # 下一轮它命中了 -> 不该再被报
    slf.on_server_start(server)
    for rule in slf._log_filter.rules:
        rule.count = 5
    slf.on_server_startup(server)
    assert IDLE not in warnings_of(server), "刚修好的规则又被报了一次"

    run_session(server, hits={IDLE: 5})
    assert slf._state.rules[IDLE].zero_streak == 0


def test_the_status_screen_explains_the_counting_rule():
    """SLF-13：各规则命中数之和与「本次已隐去」不可加，界面必须说明。"""
    server = start_plugin(config={"patterns": [BROAD, SHADOWED]})
    del server
    source = FakeSource()
    slf._show_status(source)
    text = "".join(str(x) for x in source.replies)
    assert "说明" in text and "只需要隐去一次" in text, text
    # 界面上仍然同时给出两处数字（说明而不是隐瞒）
    assert "本次已隐去" in text


def test_the_status_screen_get_counts_through_the_lock():
    """SLF-10：读取处统一走 ``counted_hits()``，不再裸读 ``rule.count``。

    裸读在 CPython 下不会真的坏，但它让"哪些统计加锁、哪些不加"无法从代码判断。
    这里钉住读口径只有一条路：状态页的数字来自 ``counted_hits`` 的快照。
    """
    server = start_plugin(config={"patterns": [LIVE]})
    slf._log_filter.filter_server_info(FakeInfo(SHADOW_LINE))
    del server

    calls = []
    original = slf.ServerLogFilter.counted_hits

    def spy(self, rules=None):
        calls.append(rules)
        return original(self, rules)

    slf.ServerLogFilter.counted_hits = spy
    try:
        slf._show_status(FakeSource())
    finally:
        slf.ServerLogFilter.counted_hits = original

    assert len(calls) == 1, "状态页必须通过 counted_hits 读命中数"


def test_counted_hits_matches_the_rules_it_was_given():
    f = make_filter(["alpha", "beta"])
    for line in ["alpha 1", "beta 1", "alpha 2", "alpha 3"]:
        f.filter_server_info(FakeInfo(line))
    assert f.counted_hits() == [3, 1]
    assert f.counted_hits(list(f.rules)) == [3, 1]


# ---------------------------------------------------------------------------
#  18. 提前 return 之后的生命周期（SLF-03）
# ---------------------------------------------------------------------------


@pytest.fixture
def pretend_mcdr_is_too_old(monkeypatch):
    """把 ``InfoActionFlag`` 置空，模拟 MCDR < 2.15.0。"""
    monkeypatch.setattr(slf, "InfoActionFlag", None)
    return monkeypatch


def test_a_low_version_mcdr_survives_the_whole_lifecycle(
    pretend_mcdr_is_too_old, monkeypatch
):
    """SLF-03：``on_server_startup`` 缺 ``_config`` 判空，每次开服完成都抛 AttributeError。

    低版本路径上 on_load 打一条错误就 return，``_config`` 仍是 None；而 MCDR 照常
    派发生命周期事件。原来 ``on_server_startup`` 进来第一句就是
    ``None.warn_about_stale_rules``。用户会同时看到「请升级 MCDR」和一条更吓人的
    traceback，同事件里排在后面的插件处理器还可能收不到该事件。
    """
    server = FakePluginServer(config_data={"patterns": [LIVE]})
    slf.on_load(server, None)

    assert any("MCDR" in m for m in server.logger.errors), "还是要说清原因"
    assert slf._config is None and slf._log_filter is None and slf._state is None

    # 完整生命周期：一个都不许抛
    slf.on_server_start(server)
    slf.on_server_startup(server)
    slf.on_server_stop(server, 0)
    assert server.info_filters == [], "低版本上不注册过滤器"


def test_the_low_version_path_resets_the_module_globals(monkeypatch):
    """提前 return 之前必须显式置空：模块级全局在进程内是复用的。

    否则一次"先正常加载、再降级重载"会让 on_server_startup 拿旧过滤器算统计。
    """
    server = FakePluginServer(config_data={"patterns": [LIVE]})
    slf.on_load(server, None)                       # 正常加载，三个全局都有值
    assert slf._log_filter is not None

    monkeypatch.setattr(slf, "InfoActionFlag", None)
    slf.on_load(server, None)                       # 降级加载

    assert slf._config is None
    assert slf._log_filter is None, "旧过滤器必须被丢掉"
    assert slf._state is None
    assert slf._session_reached_startup is False


def test_a_bare_startup_without_a_config_is_harmless():
    """即使三个全局同时为 None（例如手工调用），on_server_startup 也只是安静地返回。"""
    saved = (slf._config, slf._log_filter, slf._state)
    slf._config = slf._log_filter = slf._state = None
    try:
        slf.on_server_startup(FakePluginServer())
        slf.on_server_start(FakePluginServer())
        slf.on_server_stop(FakePluginServer(), 0)
    finally:
        slf._config, slf._log_filter, slf._state = saved


# ---------------------------------------------------------------------------
#  19. 类型写坏的配置也要被检疫（SLF-04）
# ---------------------------------------------------------------------------

# 合法 JSON、字段类型不对 —— 例如把阈值写成字符串。MCDR 的默认 failure_policy='regen'
# 会直接用默认值覆盖原文件，所以这一种必须和语法错走同一条备份 + 播报路径。
BAD_TYPES_JSON = """{
    "patterns": ["ZZZ-THE-ADMIN-WROTE-THIS"],
    "stale_rule_threshold": "three"
}
"""


def test_a_type_error_config_is_quarantined(tmp_path):
    """SLF-04：类型写坏的配置以前走 MCDR 的 REGEN，用户 patterns 静默消失。"""
    server, folder = server_with_config_file(tmp_path, BAD_TYPES_JSON)
    slf._language = "zh_cn"

    reason = slf._quarantine_broken_config(server, slf.CONFIG_FILE_NAME)

    assert reason is not None, "类型不符必须被识别出来"
    assert "类型" in reason, reason
    backup = folder / (slf.CONFIG_FILE_NAME + slf.CONFIG_BACKUP_SUFFIX)
    assert backup.is_file(), "类型写坏的配置也必须留下 .old"
    assert backup.read_text(encoding="utf-8") == BAD_TYPES_JSON, "原文件要逐字节保留"
    assert "ZZZ-THE-ADMIN-WROTE-THIS" in backup.read_text(encoding="utf-8")


def test_a_type_error_config_keeps_the_admin_patterns_in_the_backup(tmp_path):
    """备份里必须是用户写的那份原文——这正是这套机制要保住的东西。

    走完整的 ``on_load``：真机 MCDR 会在检疫之后用默认值重建配置，这里只断言
    "备份先发生"（重建由 ``FakePluginServer`` 不模拟）。

    用 ``write_bytes`` 落盘，测试文本里的 ``\\n`` 就不会在 Windows 上被写成 CRLF——
    "逐字节保留"这条断言必须比的是**同一串字节**。
    """
    folder = tmp_path / "cfg"
    folder.mkdir()
    raw = BAD_TYPES_JSON.encode("utf-8")
    (folder / "config.json").write_bytes(raw)
    server = FakePluginServer(data_folder=str(folder))

    try:
        slf.on_load(server, None)
    except Exception as error:         # FakePluginServer 不做 REGEN，容忍它失败
        assert "three" in str(error) or isinstance(error, TypeError), error

    backup = folder / (slf.CONFIG_FILE_NAME + slf.CONFIG_BACKUP_SUFFIX)
    assert backup.is_file(), "类型写坏的配置也必须留下 .old"
    assert backup.read_bytes() == raw, "原文件要逐字节保留"
    assert "ZZZ-THE-ADMIN-WROTE-THIS" in raw.decode("utf-8")
    assert not (folder / slf.CONFIG_FILE_NAME).exists(), "原文件被挪走"


def test_the_type_error_notice_is_announced_like_a_syntax_error(tmp_path):
    """并发播报：同一条 broken.* 提示，只是原因换成了类型不符。"""
    server, folder = server_with_config_file(tmp_path, BAD_TYPES_JSON)
    slf._language = "zh_cn"

    slf._quarantine_broken_config(server, slf.CONFIG_FILE_NAME)

    assert len(server.logger.errors) == 1
    text = server.logger.errors[0]
    assert "配置文件无法解析" in text
    assert "config.json.old" in text
    assert "类型" in text, text


@pytest.mark.parametrize(
    "text",
    [
        json.dumps({"patterns": "not-a-list"}),
        json.dumps({"stale_rule_threshold": "three"}),
        json.dumps({"log_matched_lines": "yes"}),
        json.dumps({"patterns": [1, 2, 3]}),
    ],
)
def test_every_type_mismatch_is_quarantined(tmp_path, text):
    server, folder = server_with_config_file(tmp_path, text)
    reason = slf._quarantine_broken_config(server, slf.CONFIG_FILE_NAME)
    assert reason is not None, "这些配置都应被判定为写坏: {}".format(text)
    assert (folder / (slf.CONFIG_FILE_NAME + slf.CONFIG_BACKUP_SUFFIX)).is_file()


@pytest.mark.parametrize(
    "text",
    [
        json.dumps({"patterns": ["a"]}),
        json.dumps({"patterns": [], "stale_rule_threshold": 5}),
        json.dumps({"unknown_future_option": 1, "patterns": ["a"]}),
    ],
)
def test_a_sound_config_is_not_touched_by_the_type_check(tmp_path, text):
    """不能把"未来版本多出来的键"也当成类型错误——那会让升级变成一次假警报。"""
    server, folder = server_with_config_file(tmp_path, text)
    assert slf._quarantine_broken_config(server, slf.CONFIG_FILE_NAME) is None
    assert server.logger.errors == []
    assert not (folder / (slf.CONFIG_FILE_NAME + slf.CONFIG_BACKUP_SUFFIX)).exists()


@pytest.mark.parametrize(
    "text,expected",
    [
        ("{ not json", "JSON 语法错误"),
        ('["a", "b"]', "顶层应为 JSON 对象"),
        ("42", "顶层应为 JSON 对象"),
        ("", "JSON 语法错误"),
    ],
)
def test_the_type_check_leaves_the_other_three_alone(tmp_path, text, expected):
    """三种既有检疫行为不能被这次扩展破坏（它们各有自己的用例）。"""
    server, folder = server_with_config_file(tmp_path, text)
    slf._language = "zh_cn"

    reason = slf._quarantine_broken_config(server, slf.CONFIG_FILE_NAME)

    assert reason is not None and expected in reason, reason


def test_a_non_utf8_config_is_still_handled_by_the_decode_path(tmp_path):
    """第四种「写坏」仍然走解码那条路，不会被类型层抢走。"""
    payload = '{"patterns": ["中文规则"]}'.encode("gbk")
    server, folder = server_with_raw_config(tmp_path, payload)
    slf._language = "zh_cn"

    reason = slf._quarantine_broken_config(server, slf.CONFIG_FILE_NAME)

    assert reason is not None and "UTF-8" in reason, reason


# ---------------------------------------------------------------------------
#  20. 语言探测失败要留痕（SLF-06）
# ---------------------------------------------------------------------------


def test_a_failing_language_probe_is_reported_not_swallowed():
    """两处 ``except Exception: pass`` 静默吞掉了可诊断的失败。

    构造：``get_mcdr_language`` 与 ``get_mcdr_config`` 都抛。旧实现下
    ``language: auto`` 会安静地退到 en_us，日志里一个字都没有——中文管理员突然
    看到英文消息却无从查起。现在要有一条 warning 把 API 名与异常说清楚。
    """
    server = FakePluginServer(config_data={"patterns": [LIVE]})

    def boom():
        raise RuntimeError("no language for you")

    server.get_mcdr_language = boom
    server.get_mcdr_config = boom
    slf.on_load(server, None)

    assert slf._language == i18n.FALLBACK_LANGUAGE
    warnings = "\n".join(server.logger.warnings)
    assert "get_mcdr_language" in warnings, warnings
    assert "RuntimeError" in warnings, warnings
    assert "no language for you" in warnings, warnings


def test_a_working_language_probe_stays_silent():
    """正常路径上一个字都不该多说。"""
    server = FakePluginServer(config_data={"patterns": [LIVE]}, mcdr_language="en_us")
    slf.on_load(server, None)
    assert slf._language == "en_us"
    assert not any("MCDR" in m and "语言" in m for m in server.logger.warnings)


def test_a_diagnostic_from_one_call_does_not_leak_into_the_next():
    """诊断收集在**本次调用的上下文**里，不是模块全局。"""
    server = FakePluginServer(config_data={"patterns": [LIVE]})

    def boom():
        raise RuntimeError("first failure")

    server.get_mcdr_language = boom
    server.get_mcdr_config = boom
    slf.on_load(server, None)
    first = len(server.logger.warnings)
    assert first >= 1

    server.get_mcdr_language = lambda: "en_us"
    server.get_mcdr_config = lambda: {"language": "en_us"}
    slf.on_load(server, None)
    assert len(server.logger.warnings) == first, "第二次不该重复上一次的诊断"
    assert slf._language == "en_us"


def test_the_language_file_listing_failure_is_also_reported(monkeypatch):
    """``i18n._list_lang_files`` 的 ``except Exception: pass`` 同样要留痕。

    两条退路都堵上：loader 那条抛异常，纯文件系统那条指向一个不存在的目录
    （不直接 monkeypatch ``os.listdir``——那会连带影响 pytest 自己）。
    诊断经 ``_note`` 走上下文，所以这里显式建一个上下文，模拟插件当时的处境。
    """

    def boom(*args, **kwargs):
        raise RuntimeError("loader exploded")

    monkeypatch.setattr(i18n.importlib.resources, "files", boom)
    monkeypatch.setattr(i18n, "_filesystem_dir", lambda: str(REPO / "no-such-folder"))
    notes = {}
    token = slf._notes.set(notes)
    try:
        i18n.available_languages(notes)
    finally:
        slf._notes.reset(token)

    assert notes, "两条退路都失败时必须留下诊断"
    assert any("loader exploded" in note for note in notes.values()), notes
    assert any("os.listdir" in note for note in notes.values()), notes


def test_the_listing_failure_reaches_a_console_warning(monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("loader exploded")

    monkeypatch.setattr(i18n.importlib.resources, "files", boom)
    monkeypatch.setattr(i18n, "_filesystem_dir", lambda: str(REPO / "no-such-folder"))
    server = FakePluginServer(config_data={"patterns": [LIVE]})
    slf.on_load(server, None)
    warnings = "\n".join(server.logger.warnings)
    assert "loader exploded" in warnings, warnings
    assert "os.listdir" in warnings, warnings


def test_a_message_whose_placeholder_is_named_like_a_parameter_still_formats():
    """``language.detect_failed_summary`` 的占位符就叫 ``language``。

    而 ``translate(key, language, **kwargs)`` 的第二个参数也叫 ``language``：把它当
    关键字传进去会撞名（TypeError），不传又会让占位符原样留下。所以走
    ``translate_values`` 的映射形式。这条用例钉住"不许再退回未格式化的模板"。
    """
    text = i18n.translate_values(
        "language.detect_failed_summary",
        "en_us",
        {"count": 2, "language": "en_us", "details": "boom"},
    )
    assert "{count}" not in text and "{language}" not in text and "{details}" not in text
    assert "2 failure(s)" in text and "en_us" in text and "boom" in text


def test_no_message_reaches_the_console_with_unfilled_placeholders():
    """跨用例的守卫：探测失败那条警告必须真的格式化过。

    本用例在一次真实 ``on_load`` 之后检查控制台文案，任何 ``{name}`` 残留都算失败——
    这一类 bug 的表现正是"消息看起来没坏，只是读起来像模板"。
    """
    server = FakePluginServer(config_data={"patterns": [LIVE]})

    def boom():
        raise RuntimeError("no language for you")

    server.get_mcdr_language = boom
    server.get_mcdr_config = boom
    slf.on_load(server, None)

    for message in server.logger.warnings + server.logger.errors:
        leftover = re.findall(r"\{[a-z_][a-z_0-9]*\}", message)
        assert not leftover, "未填充的占位符 {}: {}".format(leftover, message)


# ---------------------------------------------------------------------------
#  21. state.json 的隔离与并发（SLF-08）
# ---------------------------------------------------------------------------

STATE_PAYLOAD = json.dumps(
    {
        "session_index": 3,
        "rules": {IDLE: {"hits_last_session": 0, "zero_streak": 3,
                         "total_hits": 0, "last_hit_session": 0}},
    }
)


def test_a_broken_state_file_is_backed_up_not_silently_reset(tmp_path):
    """SLF-08：只有 config.json 有备份 + 播报，state.json 损坏会被静默重置。"""
    server, folder = server_with_config_file(tmp_path, json.dumps({"patterns": [LIVE]}))
    (folder / slf.STATE_FILE_NAME).write_text("{ truncated", encoding="utf-8")
    slf._language = "zh_cn"

    reason = slf._quarantine_broken_state(server, slf.STATE_FILE_NAME)

    assert reason is not None
    backup = folder / (slf.STATE_FILE_NAME + slf.CONFIG_BACKUP_SUFFIX)
    assert backup.is_file(), "损坏的 state.json 必须留一份 .old"
    assert backup.read_text(encoding="utf-8") == "{ truncated"
    assert any("state.json" in m for m in server.logger.warnings), server.logger.warnings


def test_a_type_broken_state_file_is_backed_up_too(tmp_path):
    server, folder = server_with_config_file(tmp_path, json.dumps({"patterns": [LIVE]}))
    (folder / slf.STATE_FILE_NAME).write_text(
        json.dumps({"session_index": "three"}), encoding="utf-8"
    )
    slf._language = "zh_cn"

    reason = slf._quarantine_broken_state(server, slf.STATE_FILE_NAME)

    assert reason is not None
    assert (folder / (slf.STATE_FILE_NAME + slf.CONFIG_BACKUP_SUFFIX)).is_file()


def test_a_sound_state_file_is_left_alone(tmp_path):
    server, folder = server_with_config_file(tmp_path, json.dumps({"patterns": [LIVE]}))
    (folder / slf.STATE_FILE_NAME).write_text(STATE_PAYLOAD, encoding="utf-8")

    assert slf._quarantine_broken_state(server, slf.STATE_FILE_NAME) is None
    assert not (folder / (slf.STATE_FILE_NAME + slf.CONFIG_BACKUP_SUFFIX)).exists()
    assert server.logger.warnings == []


def test_a_missing_state_file_is_not_an_error(tmp_path):
    server, folder = server_with_config_file(tmp_path, json.dumps({"patterns": [LIVE]}))
    assert slf._quarantine_broken_state(server, slf.STATE_FILE_NAME) is None
    assert server.logger.warnings == []


def test_loading_the_state_checks_the_file_first(tmp_path):
    """接线守卫：检疫必须在真正的加载路径上被调用。"""
    server, folder = server_with_config_file(tmp_path, json.dumps({"patterns": [LIVE]}))
    (folder / slf.STATE_FILE_NAME).write_text("{ truncated", encoding="utf-8")
    seen = []
    original = slf._quarantine_broken_state

    def spy(srv, name):
        seen.append(name)
        return original(srv, name)

    slf._quarantine_broken_state = spy
    try:
        slf._load_state(server)
    finally:
        slf._quarantine_broken_state = original

    assert seen == [slf.STATE_FILE_NAME]


def test_the_state_lock_is_a_single_shared_mutex():
    """``_state`` 的读改写必须共用同一把锁（不是每次新建一把）。

    各线程如果各自拿到一把不同的锁，等于没有互斥。
    """
    seen = []
    for _ in range(12):
        seen.append(id(slf._state_mutex()))
    assert len(set(seen)) == 1, seen


def test_state_updates_take_the_module_lock():
    """写路径必须在锁里：否则主线程结算与命令线程改动能互相覆盖。

    做法：先自己占住 ``_state_mutex()``，再从**另一个线程**调用结算路径。它如果
    真的在关键区里，就会被这把锁挡住。变异检查里有对应锚点——把 ``with
    _state_mutex():`` 从 ``_update_state_after_session`` 里去掉，这条用例必须变红。
    """
    import threading as _threading

    server = start_plugin(config={"patterns": [IDLE], "stale_rule_threshold": 1})
    slf.on_server_start(server)
    slf.on_server_startup(server)

    result = []
    holder = slf._state_mutex()
    holder.acquire()
    try:
        def attempt():
            slf._update_state_after_session(server, slf._state, [], [IDLE])
            result.append("ran without the lock")

        thread = _threading.Thread(target=attempt, name="attempt")
        thread.start()
        thread.join(timeout=1.5)
        assert result == [], (
            "结算路径没等 ``_state_mutex()``，说明它不在关键区里"
        )
        assert thread.is_alive(), "线程本该被锁挡住"
    finally:
        holder.release()

    thread.join(timeout=10)
    assert result == ["ran without the lock"]
    assert slf._state.session_index == 1


def test_concurrent_sessions_through_the_real_entry_points_do_not_lose_updates():
    """SLF-17：多线程跑真实的 ``on_server_stop`` 结算路径。

    线程数 = 结算次数是这里唯一的不变式（``zero_streak`` 只增不减）。少一次就说明
    ``_update_state_after_session`` 里的读改写被并发吃掉了——也就是锁被拿掉后的表现。
    """
    import threading as _threading

    server = start_plugin(config={"patterns": [IDLE], "stale_rule_threshold": 1})
    slf.on_server_start(server)
    slf.on_server_startup(server)

    rounds = 60
    errors = []

    def settle():
        try:
            # 只有"这一轮完成过启动"才会结算；每个线程自己置位，模拟各自的一轮。
            slf._session_reached_startup = True
            slf._update_state_after_session(
                server, slf._state, list(slf._log_filter.rules), [IDLE]
            )
        except Exception as error:  # noqa: BLE001 - 记下来而不是让线程静默死掉
            errors.append(error)

    threads = [
        _threading.Thread(target=settle, name="settle-{}".format(i))
        for i in range(rounds)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert not errors, errors
    assert slf._state.session_index == rounds, (
        "并发结算丢了更新: {} != {}".format(slf._state.session_index, rounds)
    )
    assert slf._state.rules[IDLE].zero_streak == rounds


def test_concurrent_state_writers_do_not_lose_updates():
    """锁本身的压力测试：8 线程各跑 200 次读改写，一次都不能丢。"""
    import threading as _threading

    state = slf.State()
    rounds = 200
    barrier = _threading.Barrier(8)

    def settle():
        barrier.wait()
        for _ in range(rounds):
            with slf._state_mutex():
                state.session_index += 1
                entry = state.rules.setdefault(IDLE, slf.RuleState())
                entry.zero_streak += 1

    threads = [_threading.Thread(target=settle, name="settle-{}".format(i))
               for i in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    assert state.session_index == 8 * rounds
    assert state.rules[IDLE].zero_streak == 8 * rounds


def test_concurrent_filtering_and_command_reloads_keep_the_statistics_sane():
    """SLF-17：真实入口的并发小规模压测（主线程过滤 + 命令线程改动）。

    主线程逐行过滤的同时，命令线程反复 ``!!lf reload`` 与 ``!!lf list``。断言的是
    **不变式**而不是精确计数（重载会把计数按 pattern 接续，所以总数在换规则时会变）：
    规则集合始终完整、状态文件始终可反序列化、总量非负、每轮都有结算。
    """
    import threading as _threading

    server = start_plugin(config={"patterns": [LIVE, IDLE], "stale_rule_threshold": 1})
    slf.on_server_start(server)
    slf.on_server_startup(server)

    stop = _threading.Event()
    errors = []

    def filter_lines():
        try:
            while not stop.is_set():
                slf._log_filter.filter_server_info(
                    FakeInfo("Player Steve standing on air - force-sending blocks below")
                )
        except Exception as error:  # noqa: BLE001
            errors.append(error)

    def command_thread():
        try:
            while not stop.is_set():
                slf._show_status(FakeSource())
                server.config_data = {"patterns": [LIVE, IDLE], "stale_rule_threshold": 1}
                slf._apply_config(server)
        except Exception as error:  # noqa: BLE001
            errors.append(error)

    producers = [_threading.Thread(target=filter_lines, name="filter-{}".format(i))
                 for i in range(3)]
    commanders = [_threading.Thread(target=command_thread, name="cmd-{}".format(i))
                  for i in range(2)]
    for thread in producers + commanders:
        thread.start()
    _threading.Event().wait(0.4)
    stop.set()
    for thread in producers + commanders:
        thread.join(timeout=30)

    assert not errors, errors
    assert {r.pattern for r in slf._log_filter.rules} == {LIVE, IDLE}
    counts = slf._log_filter.counted_hits()
    assert len(counts) == len(slf._log_filter.rules)
    assert all(isinstance(count, int) and count >= 0 for count in counts), counts
    assert slf._log_filter.total >= 0
    # 状态文件在任何交错下都必须仍是可反序列化的合法结果
    saved = json.loads(json.dumps(slf._state.serialize()))
    assert slf.State.deserialize(saved).serialize() == saved

def test_the_status_screen_works_on_local_snapshots():
    """SLF-12：三个模块级列表在命令线程被整体重建，状态页必须读快照。

    这里在状态页执行到一半时替换掉那三个列表（模拟一次交错的 ``!!lf reload``），
    断言它印出来的仍然是开始那一刻的那一版。
    """
    server = start_plugin(config={"patterns": ["."]})       # "." 会进 _warned_patterns
    del server
    assert slf._warned_patterns, "前置条件：需要一条被警告的规则"

    real_field = slf._field
    observed = []

    def spy(label, value, value_colour=None):
        if "匹配一切" in label:
            observed.append(str(value))
            # 就在"读取"和"渲染"之间，另一个线程重建了列表
            slf._warned_patterns = ["a-later-pattern", "another"]
        return real_field(label, value) if value_colour is None else real_field(
            label, value, value_colour
        )

    slf._field = spy
    try:
        slf._show_status(FakeSource())
    finally:
        slf._field = real_field

    assert observed == ["1 条规则（会隐去所有输出，请检查是不是写成了 . / .* / ^ 之类）"], (
        "状态页读的是被换掉之后的新列表，说明没有取快照: {}".format(observed)
    )


# ---------------------------------------------------------------------------
#  22. 单引号、空行规则与 reload 兜底（SLF-14 / SLF-15）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "line,expected",
    [
        ('"language": "en_us"', "en_us"),
        ("'language': 'en_us'", "en_us"),
        ('"language":  "zh_cn" ', "zh_cn"),
        ("'language' : 'zh_tw'", "zh_tw"),
    ],
)
def test_the_raw_string_reader_accepts_both_quote_styles(line, expected):
    """SLF-14①：严格 JSON 里字符串只能用双引号，但这段原文**本来就是写坏的文件**。

    只认双引号会让手写时混进单引号的文件读不到 language，语言悄悄退回 auto。
    """
    raw = "{\n    " + line + "\n}\n"
    assert slf._raw_str_option(raw, slf.LANGUAGE_OPTION) == expected


def test_the_raw_string_reader_still_returns_none_when_absent():
    assert slf._raw_str_option("{}", slf.LANGUAGE_OPTION) is None
    assert slf._raw_str_option('{"patterns": []}', slf.LANGUAGE_OPTION) is None


def test_a_single_quoted_language_survives_a_broken_config(tmp_path):
    """端到端：写坏的配置里用单引号写的 language 也要被读到。"""
    text = "{\n    'language': 'en_us',\n    \"patterns\": [\n        \"a\"\n        \"b\"\n    ]\n}\n"
    server, folder = server_with_config_file(tmp_path, text)
    slf._quarantine_broken_config(server, slf.CONFIG_FILE_NAME)

    assert any("cannot be parsed" in m for m in server.logger.errors), (
        "播报应当用文件里写的 en_us，而不是 MCDR 的 zh_cn: {}".format(server.logger.errors)
    )


@pytest.mark.parametrize("pattern", [r"^$", r"(?m)^$", r"^$|^$"])
def test_a_rule_that_hides_every_blank_line_is_flagged(pattern):
    """SLF-14②：只匹配空行的规则（``^$``）以前不会被 catch-all 守卫标记。

    发现里建议"往样本集加一个空串"，但那个办法是错的：``_matches_everything`` 用的是
    ``all(...)``，而 `.` / `.+` / `\\S` 这些真正的 catch-all 本来就不匹配空串，
    加进去只会把它们全部漏报。所以这里用**补集**单独判：匹配空串、且一个非空样本都
    不匹配。
    """
    regex = re.compile(pattern)
    assert slf._hides_every_blank_line(regex), pattern
    assert not slf._matches_everything(regex), "它不是 catch-all，不该走那条判据"


@pytest.mark.parametrize(
    "pattern",
    [LIVE, IDLE, r"\w", r"\d+", "joined the game", r"^\[", "error|warn", "玩家"],
)
def test_the_blank_line_rule_does_not_catch_normal_patterns(pattern):
    assert not slf._hides_every_blank_line(re.compile(pattern)), pattern


def test_a_blank_line_rule_is_kept_and_warned_like_a_catch_all():
    server = start_plugin(config={"patterns": [r"^$", LIVE]})
    assert len(slf._log_filter.rules) == 2, "仍然保留（不是被拒）"
    assert slf._warned_patterns == [r"^$"], slf._warned_patterns
    assert slf._rejected_patterns == []
    assert any("匹配一切" in w for w in server.logger.warnings)


def test_the_catch_all_subject_set_still_has_no_empty_string():
    """把空串塞回样本集会让 `.` / `.+` 漏报——这条用例把它钉住。"""
    assert "" not in slf.CATCH_ALL_SUBJECTS
    assert slf._matches_everything(re.compile("."))
    assert slf._matches_everything(re.compile(".+"))


def test_the_reload_command_replies_instead_of_raising():
    """SLF-15①：``!!reload`` 没有异常兜底（同一个文件的 ``reset`` 有）。"""
    server = start_plugin(config={"patterns": [LIVE]})

    def boom(_server):
        raise RuntimeError("simulated reload failure")

    slf._apply_config = boom
    source = FakeSource()
    try:
        slf._reload(source)                     # 不允许抛出去
    finally:
        del slf._apply_config

    text = "".join(str(x) for x in source.replies)
    assert text, "管理员必须得到一条回复，而不是只有一条 traceback"
    assert "重载失败" in text, text
    assert "simulated reload failure" in text, text
    logged = "\n".join(server.logger.exceptions)
    assert "simulated reload failure" in logged, (
        "MCDR 日志里也要有完整堆栈；实际记录到的是 {!r}".format(logged)
    )


def test_the_test_command_handles_a_prefix_only_line():
    """SLF-15②：只粘贴前缀时，``sub(...) or text`` 会退回带前缀的原文。

    那时 ``^`` 锚定的规则被报成「不命中」，而真实过滤时该行会被剥掉前缀后匹配——
    结论正好相反。剥完是空串就应当按空串测。
    """
    start_plugin(config={"patterns": [r"^$"]})
    source = FakeSource()
    slf._test_line(source, {"text": "[12:00:00] [Server thread/INFO]: "})

    text = "".join(str(x) for x in source.replies)
    assert "命中规则" in text, text
    assert "不匹配" not in text, text


def test_the_test_command_still_reports_a_real_miss():
    """显式判断不能把"真正不命中"也一起说成命中。"""
    start_plugin(config={"patterns": [r"^$"]})
    source = FakeSource()
    slf._test_line(source, {"text": "Steve joined the game"})

    text = "".join(str(x) for x in source.replies)
    assert "不匹配" in text, text


def test_the_test_command_leaves_text_without_a_prefix_alone():
    start_plugin(config={"patterns": [LIVE]})
    source = FakeSource()
    slf._test_line(source, {"text": SHADOW_LINE})

    text = "".join(str(x) for x in source.replies)
    assert "命中规则" in text
    assert "前缀" not in text, "没有前缀就不该说剥掉了前缀"


# ---------------------------------------------------------------------------
#  23. 次级缺口（A1 §4）
# ---------------------------------------------------------------------------


def test_the_probe_runs_for_every_pattern_in_a_list():
    """探测必须逐条跑，不能只测第一条。"""
    server = FakePluginServer()
    rules = slf._build_rules(server, [LIVE, r"(a+)+$", "joined the game"])
    assert [r.pattern for r in rules] == [LIVE, "joined the game"]


def test_the_state_file_is_not_written_by_the_per_line_path():
    """逐行热路径仍然不碰状态文件（与 test_state_tracking_* 同一承诺的另一半）。"""
    server = start_plugin(config={"patterns": [LIVE], "stale_rule_threshold": 1})
    slf.on_server_start(server)
    slf.on_server_startup(server)
    server.saved.clear()

    for _ in range(20):
        slf._log_filter.filter_server_info(FakeInfo(SHADOW_LINE))
    assert server.saved == {}


def test_on_load_can_run_twice_without_error():
    """同一个模块上连续两次 on_load（真实重载会新建模块，测试里不会）也不该抛。"""
    server = FakePluginServer(config_data={"patterns": [LIVE]})
    slf.on_load(server, None)
    first = slf._log_filter
    slf.on_load(server, None)
    assert slf._log_filter is not first
    assert [r.pattern for r in slf._log_filter.rules] == [LIVE]


def test_a_command_run_before_on_load_replies_and_does_not_raise():
    """命令面在插件尚未初始化时必须给一句可读回复。"""
    saved = (slf._server, slf._state, slf._log_filter)
    slf._server = slf._state = slf._log_filter = None
    try:
        for command in (slf._reload, slf._reset_streaks, slf._show_status):
            source = FakeSource()
            command(source)
            assert source.replies, command
            assert "尚未初始化" in "".join(str(x) for x in source.replies)
    finally:
        slf._server, slf._state, slf._log_filter = saved





