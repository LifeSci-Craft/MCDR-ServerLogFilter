"""Server Log Filter —— 在 MCDR 侧隐去服务端的刷屏日志。

按用户给的正则规则把匹配行从 MCDR 控制台隐去；服务端自己的
``server/logs/latest.log`` 完全不受影响。为什么值得这么做、以及全部实测数字，
见 README.md 与 benchmarks/bench_filter.py。

四条不能改的底线（原因与教训见 docs/DEVELOPMENT.md「实现注记」）：
1. 必须用 ``InfoActionFlag.hidden()``，不能 ``return False`` —— 后者等于
   ``discarded()``，会让 MCDR 的启动 / 停止 / 玩家进出检测失效。
2. ``filter_server_info`` 跑在 MCDR 主线程，只做「按顺序短路匹配」，别加额外开销。
3. 任何一条消息都不允许因为配置或语言文件写坏而抛异常。
4. 文案一律走 ``_t()``（见 lang/<语言代码>.json），代码里不写死面向用户的句子。
5. 界面里**不用 dark_gray / black**：管理员多半在深色终端上看，深色字看不清。

需要 MCDR >= 2.15.0：``InfoActionFlag`` 自该版本引入；更低版本打一条错误并停用。
"""

import json
import os
import re
import threading
import time
from typing import Dict, List, Optional, Pattern, Tuple

from mcdreforged.api.command import GreedyText, Literal
from mcdreforged.api.rtext import RAction, RColor, RText, RTextList
from mcdreforged.api.types import (
    InfoFilter,
    PermissionLevel,
    PluginServerInterface,
)
from mcdreforged.api.utils import Serializable

from . import i18n


DEFAULT_PATTERN = r"standing on air - force-sending blocks below"

# 容错导入 InfoActionFlag：低版本 MCDR 上给一句可读提示，而不是裸 ImportError。
MIN_MCDR_VERSION = "2.15.0"

STATE_FILE_NAME = "state.json"      # 命中历史；与用户的 config.json 分开
CONFIG_FILE_NAME = "config.json"
CONFIG_BACKUP_SUFFIX = ".old"       # 配置写坏时的备份后缀

COMMAND = "!!logfilter"
COMMAND_ALIAS = "!!lf"
# 两种写法是**同一个 Literal 节点的两条字面量**（MCDR 的 Literal 接受一组），
# 而不是两棵复制出来的命令树——后者迟早会漂移成两种行为。
ROOT_LITERALS = (COMMAND, COMMAND_ALIAS)

# 帮助页每行 ``!!lf <子命令>`` 要排在同一列，所以左边一栏按**最长的子命令**留宽。
# 用常量算出来而不是写死数字：以后加子命令，对齐自动跟着走。
# 元数据读不到名字时的兜底（真机上不会发生）。
_PLUGIN_NAME_FALLBACK = "Server Log Filter"
_TITLE_WIDTH = 53          # MC 聊天窗口默认字体下大约一行
_TITLE_BAR_MIN = 4

# 提示里显示的可读路径；由本文件所在目录名推导，不会与插件 id 漂移。
_CONFIG_FOLDER_DISPLAY = "config/{}".format(os.path.basename(os.path.dirname(os.path.abspath(__file__))))

try:
    from mcdreforged.api.types import InfoActionFlag
except ImportError:  # pragma: no cover - 仅在 MCDR < 2.15.0 上触发
    InfoActionFlag = None  # type: ignore[assignment]


# --------------------------------------------------------------------------
#  配置与状态
# --------------------------------------------------------------------------


class Config(Serializable):
    language: str = i18n.AUTO
    """消息语言。``auto`` = 跟随 MCDR 当前 config.yml 里的 language 设置；也可写 zh_cn / en_us。"""

    patterns: List[str] = [DEFAULT_PATTERN]
    """正则规则列表。对服务端每行日志的「正文」用 re.search 匹配，命中即从控制台隐去。"""

    log_matched_lines: bool = False
    """调试用：把被隐去的行以 INFO 级别写进 MCDR 日志，便于确认规则是否生效。"""

    report_on_server_stop: bool = True
    """服务端停止时，在 MCDR 日志里汇总本次运行共隐去了多少行。"""

    warn_about_stale_rules: bool = True
    """某条规则连续多个开服周期零命中时，在下次启动完成后给出提醒。"""

    stale_rule_threshold: int = 3
    """连续多少个开服周期零命中才提醒。调大可以容忍「本来就罕见」的规则。"""

    validate_patterns: bool = True
    """载入时用短探测串检查灾难性回溯，把这类规则拦下。强烈建议保持开启。"""

    pattern_probe_timeout_ms: int = 25
    """单条规则单个探测串的耗时上限（毫秒）。正常规则约 1 µs，余量超过一万倍。"""

    announce_config_upgrade: bool = True
    """插件升级后配置被自动补齐时，在控制台说明本次新增了哪些选项。"""

    announce_broken_config: bool = True
    """配置文件解析失败、被备份并重置时，在控制台说明原因与备份路径。"""


# `announce_broken_config` 的选项名。写成常量供 _quarantine_broken_config 使用：
# 那一刻配置已经读不出来了，只能拿这个名字去文件原文里找。测试会钉住它必须
# 是 Config 里真实存在的字段，改名时不会悄悄失配。
BROKEN_CONFIG_NOTICE_OPTION = "announce_broken_config"

# 同上，语言选项也得能从「原文」里读：配置文件写坏时，唯一能得知用户想要哪种语言的
# 地方就是他自己写的那段文本（马上就要被改名备份了）。
LANGUAGE_OPTION = "language"


# --------------------------------------------------------------------------
#  配置项一览（用于升级提示）
#
#  这里只描述「每个选项是干什么的、哪个版本加的」，用于在插件更新后告诉管理员
#  配置里多了什么。配置文件本身不写任何注释——JSON 没有注释语法，而在文件里塞
#  自定义键会被 MCDR 当成「冗余键」在下一次保存时删掉（实测确认过），
#  为此专门维护一份说明字段反而把配置弄复杂了。选项的完整说明在 README 里。
#
#  说明文字本身在语言文件里（键为 ``config_doc.<选项名>``），所以这里存的是键名
#  而不是句子——这样两种语言不会各自漂移，也顺手让「每个选项都有说明」这条不变式
#  对全部语言同时成立。
# --------------------------------------------------------------------------

# 配置项 -> (加入的版本, 语言文件里的说明键)
CONFIG_DOC: Dict[str, Tuple[str, str]] = {
    "language": (
        "1.2.2",
        "config_doc.language",
    ),
    "patterns": (
        "1.0.0",
        "config_doc.patterns",
    ),
    "log_matched_lines": (
        "1.0.0",
        "config_doc.log_matched_lines",
    ),
    "report_on_server_stop": (
        "1.0.0",
        "config_doc.report_on_server_stop",
    ),
    "warn_about_stale_rules": (
        "1.1.0",
        "config_doc.warn_about_stale_rules",
    ),
    "stale_rule_threshold": (
        "1.1.0",
        "config_doc.stale_rule_threshold",
    ),
    "validate_patterns": (
        "1.1.0",
        "config_doc.validate_patterns",
    ),
    "pattern_probe_timeout_ms": (
        "1.1.0",
        "config_doc.pattern_probe_timeout_ms",
    ),
    "announce_config_upgrade": (
        "1.2.1",
        "config_doc.announce_config_upgrade",
    ),
    "announce_broken_config": (
        "1.2.1",
        "config_doc.announce_broken_config",
    ),
}


def _config_option_names() -> List[str]:
    """按声明顺序列出用户配置项。"""
    return list(Config.get_field_annotations())


class RuleState(Serializable):
    """某条规则的跨周期历史。按 pattern 字符串记录，与配置顺序无关。"""

    hits_last_session: int = 0
    zero_streak: int = 0
    total_hits: int = 0
    last_hit_session: int = 0


class State(Serializable):
    """持久化在 ``config/server_log_filter/state.json``，与用户手写的配置分开。"""

    session_index: int = 0
    rules: Dict[str, RuleState] = {}


# --------------------------------------------------------------------------
#  规则与过滤器
# --------------------------------------------------------------------------


class Rule:
    """一条已编译的规则 + 命中计数。"""

    __slots__ = ("pattern", "regex", "count")

    def __init__(self, pattern: str, regex: Optional[Pattern] = None):
        self.pattern = pattern
        # 复用调用方已编译好的对象，避免「校验时编译一次、装载时再编译一次」
        self.regex: Pattern = regex if regex is not None else re.compile(pattern)
        self.count = 0


def _carry_counts(rules, previous: Dict[str, int]) -> int:
    """把上一份计数按 ``pattern`` 搬到新规则对象上，返回接续到的总数。

    **两条重载路径都必须调用它**：插件重载（``carry_over_from``）与命令重载
    （``reload_rules``）。少一处，一次开服周期就会被切成两段，而统计按「每轮」结算——
    明明在命中的规则会被记成「本轮零命中」，连续零命中计数跟着涨，
    最终弹出「这条规则从没命中过」的假提醒。这正是本插件要消灭的那类噪音。
    """
    carried = 0
    for rule in rules:
        hits = previous.get(rule.pattern, 0)
        if hits:
            rule.count = hits
            carried += hits
    return carried


class ServerLogFilter(InfoFilter):
    """按规则把匹配的服务端输出从控制台隐去。"""

    def __init__(self, rules: List[Rule], logger, log_matched_lines: bool):
        # tuple：迭代略快，整体赋值天然原子（重载时不会看到半新半旧的规则）
        self._rules: Tuple[Rule, ...] = tuple(rules)
        self._logger = logger
        self._log_matched_lines = log_matched_lines
        # 计数在主线程写、命令线程读，需要锁；只在命中路径上取，不影响逐行开销
        self._lock = threading.Lock()
        self._total = 0
        # hidden() 是不变常量，算一次就够了，省掉命中路径上的一个函数调用
        self._hidden_flag = InfoActionFlag.hidden() if InfoActionFlag is not None else None

    @property
    def rules(self) -> Tuple[Rule, ...]:
        return self._rules

    @property
    def total(self) -> int:
        with self._lock:
            return self._total

    def carry_over_from(self, previous: Optional["ServerLogFilter"]) -> int:
        """接续上一份实例的命中数（插件重载路径），否则一次开服周期会被切成两段。"""
        if previous is None:
            return 0
        previous_counts = {rule.pattern: rule.count for rule in previous.rules if rule.count}
        with self._lock:
            self._total = _carry_counts(self._rules, previous_counts)
            return self._total

    def reload_rules(self, rules: List[Rule], log_matched_lines: bool) -> int:
        """就地替换规则，无需重新注册 InfoFilter；返回接续过来的命中数。

        **同名规则要接着数。** 管理员通常在服务端运行中改配置、敲 `!!logfilter reload`，
        从零开始数会把这一轮已经命中的次数抹掉——规则明明在工作，却被记成「零命中」。
        只有真的从配置里删掉的规则，它的计数才该消失。
        """
        with self._lock:
            previous = {rule.pattern: rule.count for rule in self._rules if rule.count}
            self._rules = tuple(rules)
            self._log_matched_lines = log_matched_lines
            self._total = _carry_counts(self._rules, previous)
            return self._total

    def reset_counters(self) -> None:
        with self._lock:
            self._total = 0
            for rule in self._rules:
                rule.count = 0

    def match(self, content: str) -> Optional[Rule]:
        """返回第一条命中的规则，没有则返回 None。

        逐行热路径用它：一行只需要知道「该不该隐去」，找到第一条就可以停。
        """
        for rule in self._rules:
            if rule.regex.search(content):
                return rule
        return None

    def matching_rules(self, content: str) -> List[Rule]:
        """**所有**命中的规则。只给 `!!logfilter test` 用。

        与 `match()` 分开是有意的：`test` 的价值在于告诉管理员「你写的规则里哪些真的生效了」，
        只说第一条会让人以为其余几条没匹配上（于是去改本来写对的规则）。
        热路径不受影响——那里仍然是遇到第一条就返回。
        """
        return [rule for rule in self._rules if rule.regex.search(content)]

    def filter_server_info(self, info) -> None:
        rules = self._rules
        if not rules:
            return

        content = info.content
        if content is None:
            content = getattr(info, "raw_content", None)
        if not content:
            return

        rule = self.match(content)
        if rule is None:
            return

        info.action_flag = self._hidden_flag  # 不能 return False：那等于 discarded()

        with self._lock:
            rule.count += 1
            self._total += 1

        if self._log_matched_lines:
            self._logger.info(_t("rule.hidden", content=content))


# --------------------------------------------------------------------------
#  规则装载：编译 + 灾难性回溯探测
# --------------------------------------------------------------------------

# 探测串：短到最坏情况只花几百毫秒，又长到能把危险模式与正常写法拉开几个数量级。
# （标定数据见 benchmarks/bench_filter.py 第 4 节。）
PROBE_SUBJECTS = (
    "a" * 22,
    "a" * 22 + "!",
    "0" * 22 + "!",
    "a " * 11 + "!",
)


def _probe_pattern(regex: Pattern, budget_ms: int) -> Optional[Tuple[int, float]]:
    """检查正则的灾难性回溯风险；正常返回 None，可疑则返回 (探测串长度, 耗时秒数)。

    只返回数字不返回句子：措辞属于语言文件的职责，由调用方拼消息。
    为什么用短探测串就能判断，见 docs/DEVELOPMENT.md「实现注记」。
    """
    budget = budget_ms / 1000.0
    for subject in PROBE_SUBJECTS:
        start = time.perf_counter()
        regex.search(subject)
        cost = time.perf_counter() - start
        if cost > budget:
            return len(subject), cost
    return None


# 用来判断「这条规则是不是匹配一切」。刻意选得**差异极大**（字母/数字/标点/空白/中文），
# 这样窄规则不可能全中；只有 `.` `.*` `^` `a?` `\w*` `.+` 这类才会全中。
# 这类规则不会卡顿，但会让整个控制台变成一片空白——管理员会以为服务端挂了。
CATCH_ALL_SUBJECTS = (
    "a",
    "0",
    "ZZZ",
    "--- !!! ???",
    "   ",
    "玩家加入了游戏",
)


def _matches_everything(regex) -> bool:
    """这条规则是不是「随便什么行都命中」。

    必须在``_probe_pattern``之后再调用：万一某个模式既宽又慢，
    回溯探测会先把它拦下，不会走到这里。
    """
    return all(regex.search(subject) for subject in CATCH_ALL_SUBJECTS)


def _build_rules(
    server: PluginServerInterface,
    patterns: List[str],
    validate: bool = True,
    probe_budget_ms: int = 25,
) -> List[Rule]:
    """编译规则；单条写错不影响其他规则，只在日志里报错或警告。"""
    global _rejected_patterns, _warned_patterns, _duplicate_patterns

    # 预算 <= 0 会把**每一条**规则都判成「太慢」→ 过滤静默失效、报错还指向「回溯风险」
    # 这个错误的方向。夹到 1 ms：真实规则在微秒级、危险模式在百毫秒级，两边都分得清。
    if validate:
        probe_budget_ms = max(1, probe_budget_ms)

    rules: List[Rule] = []
    _rejected_patterns = []
    _warned_patterns = []
    _duplicate_patterns = []
    seen = set()
    for raw in patterns:
        pattern = (raw or "").strip()
        if not pattern:
            continue
        if pattern in seen:
            # 同一条写两遍：第二条永远不会命中（match() 返回第一条），但 state.rules
            # 以 pattern 为键——两条共享一份统计，第二条的 zero_streak 会一直涨，
            # 最后弹出一条假的「从没命中过」。留一条、说出来，别让它悄悄污染统计。
            server.logger.warning(_t("rule.duplicate", pattern=pattern))
            _duplicate_patterns.append(pattern)
            continue
        seen.add(pattern)
        try:
            regex = re.compile(pattern)
        except (re.error, OverflowError, RecursionError) as error:
            # re.error 不是唯一的失败方式：`a{999999999999}` 抛 OverflowError、
            # 上千层嵌套抛 RecursionError。只接 re.error 的话，一个打错的量化符
            # 就能把整个 on_load 带下去——而这条路径的承诺是「单条写错不影响其他规则」。
            server.logger.warning(
                _t("rule.compile_failed", pattern=pattern, error=error)
            )
            _rejected_patterns.append(pattern)
            continue
        if validate:
            problem = _probe_pattern(regex, probe_budget_ms)
            if problem is not None:
                server.logger.error(
                    _t(
                        "rule.dangerous",
                        pattern=pattern,
                        detail=_t(
                            "probe.too_slow",
                            length=problem[0],
                            cost=problem[1] * 1000,
                            budget=probe_budget_ms,
                        ),
                    )
                )
                _rejected_patterns.append(pattern)
                continue
            if _matches_everything(regex):
                # 只警告、不丢弃：把管理员亲手写的规则偷偷扔掉是另一种意外，
                # 而这条警告已经把问题说清楚了（它出现在控制台，不受本插件影响）。
                server.logger.warning(_t("rule.catch_all", pattern=pattern))
                _warned_patterns.append(pattern)
        rules.append(Rule(pattern, regex))
    return rules


# --------------------------------------------------------------------------
#  模块级状态
# --------------------------------------------------------------------------

_config: Optional[Config] = None
_log_filter: Optional[ServerLogFilter] = None
_state: Optional[State] = None
# on_load 拿到的 PluginServerInterface。注意不能从命令的 source 取：那条路拿到的是
# ServerInterface，并没有 load_config_simple()（它只属于 PluginServerInterface）。
_server: Optional[PluginServerInterface] = None

# 本次加载解析出的语言，所有消息经 _t() 走它；读到配置前它是回落链的终点。
_language: str = i18n.FALLBACK_LANGUAGE

# 用户当初写的 setting（可能仍是 ``auto``）；只用来在 !!logfilter 里说明语言来源。
_language_setting: str = i18n.AUTO

# 本次载入被跳过的规则（正则写错、或触发回溯保护），供 !!logfilter 说明
# 「为什么启用的条数比配置里少」。
_rejected_patterns: List[str] = []
_warned_patterns: List[str] = []
_duplicate_patterns: List[str] = []

# 只有完成过启动的周期才计入「零命中」统计，否则连续启动失败会刷出假提醒。
_session_reached_startup = False

# 由 _config_migrator 填、_announce_new_options 报：本次缺了哪些选项（= 新增的）。
_newly_added_options: List[str] = []

# 配置是否刚因写坏被重置过；是的话 patterns 是默认值而非用户配置，不能据此清理历史。
_config_was_reset = False


def _t(key: str, **kwargs) -> str:
    """按本次解析出的语言取一条消息（见 ``_language``）。"""
    return i18n.translate(key, _language, **kwargs)


def _mcdr_language(server: PluginServerInterface) -> Optional[str]:
    """问 MCDR 当前用的是哪种语言，供 ``language: auto`` 使用。

    读不到就退回 MCDR 的配置字典，再不行返回 None。绝不抛异常。
    """
    getter = getattr(server, "get_mcdr_language", None)
    if callable(getter):
        try:
            language = getter()
        except Exception:  # noqa: BLE001 - 探测性调用，失败就走下面的退路
            pass
        else:
            if isinstance(language, str) and language.strip():
                return language

    getter = getattr(server, "get_mcdr_config", None)
    if callable(getter):
        try:
            config = getter()
        except Exception:  # noqa: BLE001
            return None
        if isinstance(config, dict):
            language = config.get("language")
            if isinstance(language, str):
                return language
    return None


def _resolve_language(server: PluginServerInterface, setting, warn: bool = True) -> str:
    """解析 ``language`` 选项，必要时就「这个值我不认识」提醒一次。

    ``warn=False`` 供 _quarantine_broken_config 用：随后 _load_config 会用同一份
    原文再解析一遍，警告在那里发就够了，不必说两遍。
    """
    choice = i18n.resolve(setting, _mcdr_language(server))
    if choice.note_key is not None and warn:
        server.logger.warning(
            i18n.translate(choice.note_key, choice.language, **choice.note_args)
        )
    problem = i18n.get_catalog(choice.language).error
    if problem is not None and warn:
        # 语言文件本身读不出来（打包出错、被人改坏）时说清楚，别让它退化成键名。
        server.logger.warning(
            i18n.translate(
                "catalog.unreadable",
                choice.language,
                path=i18n.relative_catalog_path(choice.language),
                error=problem,
            )
        )
    return choice.language


def _read_bytes(path: str) -> Optional[bytes]:
    """把文件整个读成字节；读不动（权限之类）返回 None。"""
    try:
        with open(path, "rb") as handle:
            return handle.read()
    except OSError:
        return None


def _decode_utf8(data: Optional[bytes]) -> Optional[str]:
    """按 UTF-8 解码，不是合法 UTF-8 就返回 None。

    配置里可以写中文规则，所以「解码不了」和语法错误一样属于把配置写坏了：
    调用方要照常备份 + 重置，而不是让 UnicodeDecodeError 冒到 on_load 把插件打下来
    （它继承自 ValueError，不会被 ``except OSError`` 拦住）。
    """
    if data is None:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _log_summary(server: PluginServerInterface, rules: List[Rule]) -> None:
    if rules:
        server.logger.info(_t("summary.rules_enabled", count=len(rules)))
    else:
        server.logger.warning(_t("summary.no_rules"))


def _load_state(server: PluginServerInterface) -> State:
    return server.load_config_simple(
        file_name=STATE_FILE_NAME, target_class=State, echo_in_console=False
    )


def _save_state(server: PluginServerInterface, state: State) -> None:
    server.save_config_simple(state, file_name=STATE_FILE_NAME)


def _config_migrator(read_data) -> bool:
    """``load_config_simple(data_processor=...)`` 钩子：记下缺了哪些选项（即新增的）。

    补写由 MCDR 自己完成，所以始终返回 False。
    """
    global _newly_added_options

    if not isinstance(read_data, dict):
        return False

    present = set(read_data)
    _newly_added_options = [
        name for name in _config_option_names() if name not in present
    ]
    return False


def _announce_new_options(server: PluginServerInterface) -> None:
    """插件升级后，把新增了哪些配置项告诉管理员。"""
    if not _newly_added_options:
        return
    lines = [
        "",
        "=" * 66,
        _t("upgrade.title", count=len(_newly_added_options)),
    ]
    for name in _newly_added_options:
        since, doc_key = CONFIG_DOC[name]
        lines.append(_t("upgrade.entry", name=name, since=since))
        lines.append(_t("upgrade.entry_desc", desc=_t(doc_key)))
    lines += [
        _t("upgrade.config_path", folder=_CONFIG_FOLDER_DISPLAY),
        _t("upgrade.readme_hint"),
        "=" * 66,
        "",
    ]
    server.logger.info("\n".join(lines))


def _load_config(server: PluginServerInterface) -> Config:
    """读取配置，并把语言**最早**定下来（后面的播报都要用它）。

    开关只决定说不说：``announce_config_upgrade`` 关掉也照补配置。
    """
    global _newly_added_options, _config_was_reset, _language, _language_setting

    _newly_added_options = []
    data = _read_bytes(os.path.join(server.get_data_folder(), CONFIG_FILE_NAME))
    _config_was_reset = _quarantine_broken_config(server, CONFIG_FILE_NAME, data) is not None
    config = server.load_config_simple(target_class=Config, data_processor=_config_migrator)

    setting = config.language
    if _config_was_reset:
        # 配置刚被重置成默认值（language 也是默认的 auto），但用户当初明明写了自己的
        # 选择；那份选择只剩原文里有，读它才不至于「修配置过程中语言突然变了」。
        raw = _decode_utf8(data)
        if raw is not None:
            setting = _raw_str_option(raw, LANGUAGE_OPTION) or setting
    _language_setting = setting
    _language = _resolve_language(server, setting)

    if config.announce_config_upgrade:
        _announce_new_options(server)
    return config


def _raw_bool_option(raw: str, name: str) -> Optional[bool]:
    """从可能并不合法的配置原文里尽力读出一个布尔选项。

    只在配置已写坏时用（那一刻用户真正的设置只剩这份原文）；找不到返回 None，
    调用方按默认开启处理。
    """
    match = re.search(
        r'"{}"\s*:\s*(true|false)\b'.format(re.escape(name)),
        raw,
        flags=re.IGNORECASE,
    )
    if match is None:
        return None
    return match.group(1).lower() == "true"


def _raw_str_option(raw: str, name: str) -> Optional[str]:
    """同 ``_raw_bool_option``，但读字符串选项（目前只有 ``language``）。"""
    match = re.search(
        r'"{}"\s*:\s*"([^"]*)"'.format(re.escape(name)),
        raw,
    )
    if match is None:
        return None
    value = match.group(1).strip()
    return value or None


def _quarantine_broken_config(
    server: PluginServerInterface, file_name: str, data: Optional[bytes] = None
) -> Optional[str]:
    """配置解析失败时先把它挪到 ``<name>.old``，并把解析错误说明返回给上层。

    必须在交给 MCDR 之前自己看一遍：``load_config_simple`` 默认 ``failure_policy
    ='regen'``，会直接用默认值覆盖原文件，用户写的规则就此消失。三种「写坏」都在
    这里拦下——JSON 语法错误、顶层不是对象、以及**不是合法的 UTF-8**（后者同样会让
    MCDR 解析失败，所以也得先备份）。备份永远执行，``announce_broken_config`` 只管
    说不说；开关与语言只能从原文里读（此刻没有解析好的配置）。
    """
    path = os.path.join(server.get_data_folder(), file_name)
    if not os.path.isfile(path):
        return None

    if data is None:
        data = _read_bytes(path)
        if data is None:
            return None  # 读不动（权限之类）就交给 MCDR 自己处理

    raw = _decode_utf8(data)

    # 语言得先定：下面那句「哪里出错了」也要用它。此刻只能从原文里取，取不到就问
    # MCDR（auto 的语义）。这里不发声，让紧接着的 _load_config 去发那唯一的警告。
    setting = i18n.AUTO
    if raw is not None:
        setting = _raw_str_option(raw, LANGUAGE_OPTION) or i18n.AUTO
    language = _resolve_language(server, setting, warn=False)

    def say(key: str, **kwargs) -> str:
        return i18n.translate(key, language, **kwargs)

    if raw is None:
        reason = say("broken.not_utf8")
    else:
        try:
            parsed = json.loads(raw)
        except ValueError as error:
            reason = say("broken.json_error", error=error)
        else:
            if isinstance(parsed, dict):
                return None
            reason = say("broken.not_an_object", kind=type(parsed).__name__)

    backup = path + CONFIG_BACKUP_SUFFIX
    try:
        os.replace(path, backup)
    except OSError as error:
        # 连备份都失败 = 原文件会被 MCDR 覆盖掉，属数据丢失，不受任何开关影响。
        server.logger.error(
            say("broken.backup_failed", file=file_name, error=error)
        )
        return reason

    if _raw_bool_option(raw or "", BROKEN_CONFIG_NOTICE_OPTION) is False:
        return reason  # 灯关了：备份照做，只是不再播报

    shown_path = "{}/{}".format(_CONFIG_FOLDER_DISPLAY, file_name)
    server.logger.error(
        "\n".join(
            [
                "",
                "=" * 66,
                say("broken.title"),
                say("broken.file_line", path=shown_path),
                say("broken.backup_line", path=shown_path + CONFIG_BACKUP_SUFFIX),
                say("broken.reason_line", reason=reason),
                say("broken.regenerated", file=file_name),
                say("broken.reload_hint"),
                say("broken.cause_comma"),
                say("broken.cause_trailing"),
                "=" * 66,
                "",
            ]
        )
    )
    return reason


def _orphan_patterns(state: State, patterns: List[str]) -> List[str]:
    """列出 state.json 里「配置中已经没有了」的规则。

    对照**配置原文**而非编译成功的规则：写错正则、或被回溯保护拦下的规则仍算存在，
    否则改好错别字后它的历史统计已经归零了。
    """
    configured = set()
    for raw in patterns:
        pattern = (raw or "").strip()
        if pattern:
            configured.add(pattern)
    return [pattern for pattern in state.rules if pattern not in configured]


def _forget_orphans(state: State, patterns: List[str]) -> List[str]:
    """就地删掉孤立的规则统计，返回被删掉的 pattern（不写盘）。

    配置刚被自动重置时**不清理**：那一刻 patterns 是默认值而非用户配置，
    照此清理会连用户的历史一起抹掉。
    """
    if _config_was_reset:
        return []
    removed = _orphan_patterns(state, patterns)
    for pattern in removed:
        del state.rules[pattern]
    return removed


def _prune_state(
    server: PluginServerInterface, state: State, patterns: List[str]
) -> List[str]:
    """把「配置里已删除」的规则统计从 state.json 里清掉。

    只在真清掉了东西时才写盘。必须在重载时做，不能等服务端停止：后者要一个完整
    开服周期才跑，管理员删完规则去看 state.json 会看到已经没用的旧统计。
    """
    removed = _forget_orphans(state, patterns)
    if removed:
        _save_state(server, state)
    return removed


def _announce_pruned(server: PluginServerInterface, removed: List[str]) -> None:
    """告诉管理员 state.json 里清掉了哪些统计（配置里已删除的规则）。"""
    if not removed:
        return
    lines = [_t("state.pruned_header", count=len(removed))]
    lines += [_t("state.pruned_entry", pattern=pattern) for pattern in removed]
    server.logger.info("\n".join(lines))


def _update_state_after_session(
    server: PluginServerInterface, state: State, rules: List[Rule], patterns: List[str]
) -> None:
    """一次开服周期结束时，把命中数写进历史并推进「连续零命中」计数。"""
    state.session_index += 1
    session_index = state.session_index

    for rule in rules:
        entry = state.rules.get(rule.pattern)
        if entry is None:
            entry = RuleState()
            state.rules[rule.pattern] = entry
        entry.hits_last_session = rule.count
        entry.total_hits += rule.count
        if rule.count > 0:
            entry.zero_streak = 0
            entry.last_hit_session = session_index
        else:
            entry.zero_streak += 1

    _forget_orphans(state, patterns)  # 顺手清掉已删除的规则，避免文件无限增长

    _save_state(server, state)


def _collect_stale(
    state: State, rules: List[Rule], threshold: int
) -> List[Tuple[str, RuleState]]:
    """找出连续零命中已达阈值的规则（只关心当前配置里还在的）。"""
    stale: List[Tuple[str, RuleState]] = []
    for rule in rules:
        entry = state.rules.get(rule.pattern)
        if entry is not None and entry.zero_streak >= threshold:
            stale.append((rule.pattern, entry))
    return stale


def _emit_stale_warning(
    server: PluginServerInterface,
    stale: List[Tuple[str, RuleState]],
    threshold: int,
) -> None:
    """在开服完成（Done）之后给一次醒目提醒。

    整段拼成一条消息再发，避免被几十行带时间戳的 WARNING 淹没。每条规则只列模式
    （次数已在标题里）；只有比阈值更久没动静的才在它后面补一句额外说明。
    """
    lines = [
        "",
        "=" * 66,
        _t("stale.title", count=len(stale), threshold=threshold),
    ]
    for pattern, entry in stale:
        suffix = ""
        if entry.zero_streak > threshold:
            suffix = _t("stale.entry_extra", streak=entry.zero_streak)
        lines.append("  · {}{}".format(pattern, suffix))
    lines += [
        _t("stale.cause_header"),
        _t("stale.cause_regex"),
        _t("stale.cause_gone"),
        _t("stale.advice_test"),
        _t("stale.advice_remove"),
        _t("stale.advice_threshold"),
        _t("stale.advice_reset"),
        _t("stale.note"),
        "=" * 66,
        "",
    ]
    server.logger.warning("\n".join(lines))


# --------------------------------------------------------------------------
#  生命周期
# --------------------------------------------------------------------------


def on_load(server: PluginServerInterface, prev_module) -> None:
    global _config, _log_filter, _state, _server, _session_reached_startup
    global _language, _language_setting

    _server = server

    if InfoActionFlag is None:
        # 还没有配置可读，语言只能问 MCDR 自己；然后放弃注册，避免每行都抛异常。
        _language_setting = i18n.AUTO
        _language = i18n.resolve(i18n.AUTO, _mcdr_language(server)).language
        server.logger.error(_t("plugin.requires_mcdr", version=MIN_MCDR_VERSION))
        return

    _config = _load_config(server)
    _state = _load_state(server)
    rules = _build_rules(
        server,
        _config.patterns,
        validate=_config.validate_patterns,
        probe_budget_ms=_config.pattern_probe_timeout_ms,
    )
    _log_filter = ServerLogFilter(rules, server.logger, _config.log_matched_lines)

    _announce_pruned(server, _prune_state(server, _state, _config.patterns))

    carried = _log_filter.carry_over_from(getattr(prev_module, "_log_filter", None))
    if carried:
        server.logger.info(_t("reload.carried", count=carried))

    # 真实重载会构造全新模块，_session_reached_startup 会回到 False；若服务端正在
    # 运行，这次重载就会把当前周期从统计里抹掉，所以要把它一并接续过来。
    if getattr(prev_module, "_session_reached_startup", False):
        _session_reached_startup = True

    # 必须在 on_load 注册；卸载时 MCDR 会自动移除，无需反注册。
    server.register_info_filter(_log_filter)

    # 两种拼法都登记进 MCDR 的 !!help，用户不用猜哪个是「正式」写法。
    for prefix in ROOT_LITERALS:
        server.register_help_message(prefix, _t("help.logfilter"))
    # 权限要求只挂在这里一处，覆盖下面所有分支（见 _admin_only）。
    server.register_command(
        Literal(ROOT_LITERALS)
        .requires(_admin_only, _admin_denied_message)
        .runs(_show_help)
        .then(Literal("help").runs(_show_help))
        .then(Literal("list").runs(_show_status))
        .then(Literal("reload").runs(_reload))
        .then(Literal("reset").runs(_reset_streaks))
        .then(Literal("test").then(GreedyText("text").runs(_test_line)))
    )

    _log_summary(server, rules)


def _apply_config(server: PluginServerInterface) -> Tuple[List[Rule], List[str], int]:
    """重读配置并就地替换规则（不重新注册命令与过滤器）。

    返回 ``(规则列表, 被清掉的旧规则统计, 接续过来的命中数)``。
    """
    global _config
    _config = _load_config(server)
    rules = _build_rules(
        server,
        _config.patterns,
        validate=_config.validate_patterns,
        probe_budget_ms=_config.pattern_probe_timeout_ms,
    )
    carried = _log_filter.reload_rules(rules, _config.log_matched_lines)
    pruned = [] if _state is None else _prune_state(server, _state, _config.patterns)
    return rules, pruned, carried


def on_server_start(server: PluginServerInterface) -> None:
    # 每轮服务端从零计数，这样停止时的汇总才是「本次运行」的准确数字。
    global _session_reached_startup
    _session_reached_startup = False
    if _log_filter is not None:
        _log_filter.reset_counters()


def on_server_startup(server: PluginServerInterface) -> None:
    """服务端完成启动（控制台出现 ``Done``）时触发。

    零命中提醒放这里而不是 on_server_start：开服瞬间日志量大，会被淹没。
    """
    global _session_reached_startup
    _session_reached_startup = True

    if (
        not _config.warn_about_stale_rules
        or _state is None
        or _log_filter is None
        or _config.stale_rule_threshold <= 0
    ):
        return

    stale = _collect_stale(_state, list(_log_filter.rules), _config.stale_rule_threshold)
    if stale:
        _emit_stale_warning(server, stale, _config.stale_rule_threshold)


def on_server_stop(server: PluginServerInterface, server_return_code: int) -> None:
    global _session_reached_startup

    if _log_filter is None or _config is None:
        return

    if _session_reached_startup and _state is not None:
        try:
            _update_state_after_session(
                server, _state, list(_log_filter.rules), _config.patterns
            )
        except Exception:  # noqa: BLE001 - 状态写盘失败不应影响服务端收尾
            server.logger.exception(_t("state.save_failed"))
    _session_reached_startup = False

    if not _config.report_on_server_stop:
        return
    total = _log_filter.total
    if total:
        server.logger.info(_t("session.report", total=total))


# --------------------------------------------------------------------------
#  命令实现
# --------------------------------------------------------------------------


def _admin_only(source) -> bool:
    """整个命令面都限管理员：状态页会印出你写的规则，`test` 会验证日志内容。

    要求只写在**根节点**上。MCDR 在解析路径上对每个节点都跑一遍 requirement，
    所以根上一个就门住了全部子命令；在五个子命令上各写一遍是重复的声明，
    迟早会漏掉一个（新增子命令时最容易忘）。
    """
    return source.has_permission(PermissionLevel.ADMIN)


_HELP_SUBCOMMANDS = ("help", "list", "test", "reload", "reset")
_HELP_COMMAND_WIDTH = len(COMMAND_ALIAS) + 1 + max(len(s) for s in _HELP_SUBCOMMANDS)


def _plugin_title(server) -> Tuple[str, Optional[str]]:
    """``(名称, 版本)``；取不到就退回常量 / None。

    取元数据这一步**不允许抛异常**：它只是画个标题，失败了顶多少印一个版本号，
    不该让整条命令变成报错。
    """
    getter = getattr(server, "get_self_metadata", None)
    if getter is None:
        return _PLUGIN_NAME_FALLBACK, None
    try:
        metadata = getter()
    except Exception:  # noqa: BLE001 - 标题而已，读不到就退让
        return _PLUGIN_NAME_FALLBACK, None
    name = getattr(metadata, "name", None) or _PLUGIN_NAME_FALLBACK
    version = getattr(metadata, "version", None)
    return name, (str(version) if version is not None else None)


def _title_line() -> RTextList:
    """顶栏：``========  Server Log Filter v1.4.0  ========``。

    两侧等号数按标题长度算，凑到大约一行宽；名称与版本用不同颜色区分。
    """
    name, version = _plugin_title(_server)
    core = "{} v{}".format(name, version) if version else name
    bars = max(_TITLE_BAR_MIN, (_TITLE_WIDTH - len(core) - 4) // 2)
    rule = "=" * bars
    line = RTextList(RText(rule, RColor.gold), "  ", RText(name, RColor.aqua))
    if version:
        line.append(RText(" v" + version, RColor.yellow))
    line.append("  ")
    line.append(RText(rule, RColor.gold))
    return line


def _field(label: str, value: str, value_colour=RColor.green) -> RTextList:
    """``标签: 值`` —— 标签 aqua、值默认绿（计数）。

    帮助页是「命令 aqua / 说明 white」，状态页用同一套：标签 aqua、值 white 或 green、
    备注 gray。两个界面看起来才是一家的。
    """
    return RTextList(RText(label, RColor.aqua), RText(value, value_colour))


def _help_line(description: str, command: str, action) -> RTextList:
    """一行 ``!!lf <子命令> -- 说明``，左栏按 ``_HELP_COMMAND_WIDTH`` 对齐。

    点击只挂在前面的命令上（补的对齐空格不参与点击目标）；
    命令用 aqua、说明用 white、分隔符用 gray——都比原来的 dark_gray 看得清。
    """
    padding = " " * (_HELP_COMMAND_WIDTH - len(command))
    click = command + (" " if action is RAction.suggest_command else "")
    return RTextList(
        RText(command, RColor.aqua).set_click_event(action, click),
        RText(padding + " -- ", RColor.gray),
        RText(description, RColor.white),
    )


def _admin_denied_message(source) -> str:
    """权限不足时说的话——默认那句「权限不足」会让人以为「我明明是 OP 啊」。

    MCDR 的权限与游戏内的 OP **毫无关系**：它只读 ``permission.yml``，且默认级别是 ``user``。
    这里把「你没有权限」和「怎么给自己权限」一起说清楚。
    """
    return _t("command.need_admin")



def _show_help(source) -> None:
    """没有子命令时的落地页：把可用的命令列出来。

    以前裸 `!!logfilter` 与 `list` 显示同一屏状态，于是「命令记错了」和「我就是想看状态」
    得到完全一样的回应——前者只能自己去翻 README。现在前者给帮助，`list` 才是状态。

    每行做成可点击的，点击目标就是那行命令本身（显示什么就执行什么）。
    ``test`` 需要跟一段日志文本，直接执行没有意义，所以只把命令**填进输入框**。

    这里**不印状态页那句运行提示**（服务端日志不受影响之类）：这屏是命令清单，
    运行时的注意事项属于状态页。

    每条都写成独立的 ``_t("...")`` 调用（而不是循环里的变量）：键集不变式靠 AST 从调用点
    收集字面量，键名一旦进了变量，检查就会把它当成「没人用」而报错。
    """
    run, suggest = RAction.run_command, RAction.suggest_command
    parts = RTextList(
        _title_line(),
        "\n",
        RText(_t("help.usage", command=COMMAND, alias=COMMAND_ALIAS), RColor.gray),
        "\n",
        RText(_t("help.permission"), RColor.yellow),
        "\n",
        _help_line(
            _t("help.entry_help"), COMMAND_ALIAS + " help", run
        ),
        "\n",
        _help_line(
            _t("help.entry_list"), COMMAND_ALIAS + " list", run
        ),
        "\n",
        _help_line(
            _t("help.entry_test"), COMMAND_ALIAS + " test", suggest
        ),
        "\n",
        _help_line(
            _t("help.entry_reload"), COMMAND_ALIAS + " reload", run
        ),
        "\n",
        _help_line(
            _t("help.entry_reset"), COMMAND_ALIAS + " reset", run
        ),
    )
    source.reply(parts)


def _show_status(source) -> None:
    rules = _log_filter.rules
    if i18n.normalize(_language_setting) in ("", i18n.AUTO):
        language_line = _t("status.language_auto", code=_language)
    else:
        language_line = _t("status.language_set", code=_language)
    parts = RTextList(
        _title_line(),
        "\n",
        _field(_t("status.rule_count"), str(len(rules))),
        _field(_t("status.hidden_now"), str(_log_filter.total) + _t("status.lines")),
        "\n",
        _field(_t("status.language_label"), language_line, RColor.white),
    )

    if not rules:
        parts.append("\n")
        parts.append(RText(_t("status.no_rules"), RColor.yellow))

    threshold = _config.stale_rule_threshold
    # 编号**只在真的需要时才补空格**：10 条以上补到两位，让 ``]`` 对齐；
    # 9 条以内直接印 ``[1]``，那个空格对用户只是噪音（曾被问过一次）。
    index_width = len(str(len(rules)))
    for index, rule in enumerate(rules, start=1):
        parts.append("\n")
        parts.append(
            RText(
                _t("status.rule_index", index=str(index).rjust(index_width)),
                RColor.gray,
            )
        )
        parts.append(RText(_t("status.rule_hits", count=rule.count), RColor.green))
        parts.append(RText("-- ", RColor.gray))
        parts.append(RText(rule.pattern, RColor.white))
        entry = _state.rules.get(rule.pattern)
        if entry is not None and entry.last_hit_session:
            parts.append(
                RText(
                    _t("status.last_hit", session=entry.last_hit_session),
                    RColor.gray,
                )
            )
        if entry is not None and entry.zero_streak:
            if threshold and entry.zero_streak >= threshold:
                parts.append(
                    RText(_t("status.streak_warn", streak=entry.zero_streak), RColor.red)
                )
            else:
                parts.append(
                    RText(_t("status.streak", streak=entry.zero_streak), RColor.gray)
                )

    if _warned_patterns:
        parts.append("\n")
        parts.append(
            _field(
                _t("status.warned_label"),
                _t("status.warned", count=len(_warned_patterns)),
                RColor.red,
            )
        )

    if _rejected_patterns:
        parts.append("\n")
        parts.append(
            _field(
                _t("status.rejected_label"),
                _t("status.rejected", count=len(_rejected_patterns)),
                RColor.yellow,
            )
        )

    if _duplicate_patterns:
        parts.append("\n")
        parts.append(
            _field(
                _t("status.duplicate_label"),
                _t("status.duplicate", count=len(_duplicate_patterns)),
                RColor.yellow,
            )
        )

    if _state.session_index:
        parts.append("\n")
        parts.append(
            _field(
                _t("status.sessions_label"),
                _t("status.sessions", count=_state.session_index),
                RColor.white,
            )
        )

    parts.append("\n")
    parts.append(RText(_t("status.tip_label"), RColor.aqua))
    parts.append(RText(_t("status.tip"), RColor.gray))
    source.reply(parts)


def _reload(source) -> None:
    # 用插件自己的 _server，不能用 source.get_server()：后者没有 load_config_simple()
    rules, pruned, carried = _apply_config(_server)
    _log_summary(_server, rules)
    _announce_pruned(_server, pruned)
    reply = RTextList(
        RText(_t("reload.done"), RColor.green),
        RText(_t("reload.count", count=len(rules)), RColor.gray),
    )
    if carried:
        # 让管理员看见「这一轮的计数没有丢」，否则他会以为重载把统计清了
        reply.append(RText(_t("reload.carried_session", count=carried), RColor.gray))
    if pruned:
        reply.append(RText(_t("reload.pruned", count=len(pruned)), RColor.gray))
    source.reply(reply)


def _reset_streaks(source) -> None:
    """清空「连续零命中」计数，用于「确认这条规则本来就罕见，别再来烦我」。"""
    cleared = 0
    for rule in _log_filter.rules:
        entry = _state.rules.get(rule.pattern)
        if entry is not None and entry.zero_streak:
            entry.zero_streak = 0
            cleared += 1
    try:
        _save_state(_server, _state)
    except Exception:  # noqa: BLE001
        _server.logger.exception(_t("state.reset_save_failed"))
    source.reply(
        RTextList(
            RText(_t("reset.done"), RColor.green),
            RText(_t("reset.count", count=cleared), RColor.gray),
        )
    )


# 控制台整行的前缀（``[12:00:00] [Server thread/INFO]: ``）。过滤器匹配的是 MCDR
# **剥掉前缀之后**的正文，而管理员习惯整行复制，所以 !!logfilter test 也得先剥掉，
# 否则 ``^`` 锚定的规则会被误报成「不命中」——人会去改本来写对的规则。
_LOG_LINE_PREFIX = re.compile(r"^\[[^\]]*\]\s*\[[^\]]*\]\s*:\s?")


def _test_line(source, context) -> None:
    text = context["text"]
    body = _LOG_LINE_PREFIX.sub("", text, count=1) or text
    matched = _log_filter.matching_rules(body)

    if not matched:
        reply = RTextList(
            RText(_t("test.miss"), RColor.green),
            RText(_t("test.miss_detail"), RColor.gray),
        )
    else:
        # 全部列出来：只报第一条会让人以为其余几条没匹配上
        reply = RTextList(
            RText(_t("test.hit"), RColor.yellow),
            RText(_t("test.hit_detail"), RColor.gray),
            RText(", ".join(rule.pattern for rule in matched), RColor.white),
        )
    if body != text:
        reply.append("\n")
        reply.append(RText(_t("test.stripped_note"), RColor.gray))
    source.reply(reply)
