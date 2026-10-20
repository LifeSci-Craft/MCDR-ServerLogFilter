"""Server Log Filter —— 在 MCDR 侧隐去服务端的刷屏日志。

按用户给的正则规则把匹配行从 MCDR 控制台隐去；服务端自己的
``server/logs/latest.log`` 完全不受影响。为什么值得这么做、以及全部实测数字，
见 README.md 与 benchmarks/bench_filter.py。

四条不能改的底线（原因与教训见 docs/DEVELOPMENT.md「实现注记」）：
1. 命中时只能**按位清掉** ``echo_to_console``（``action_flag &= self._hide_mask``）。
   既不能 ``return False`` —— 那等于 ``discarded()``，会让 MCDR 的启动 / 停止 /
   玩家进出检测失效；也不能整体赋值成 ``hidden()`` —— MCDR 会按注册顺序跑遍所有
   filter，整体赋值会把别的插件刚设的 ``discarded()`` / ``cancel_send_to_server()``
   抹回来（SLF-A-03）。
2. ``filter_server_info`` 跑在 MCDR 主线程，只做「按顺序短路匹配」，别加额外开销。
3. 任何一条消息都不允许因为配置或语言文件写坏而抛异常。
4. 文案一律走 ``_t()``（见 lang/<语言代码>.json），代码里不写死面向用户的句子。
5. 界面里**不用 dark_gray / black**：管理员多半在深色终端上看，深色字看不清。

需要 MCDR >= 2.15.0：``InfoActionFlag`` 自该版本引入；更低版本打一条错误并停用。
"""

import contextvars
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
    """正则规则列表。对服务端每行日志的「正文」用 re.search 匹配，命中即从控制台隐去。

    这里的可变默认值是**刻意依赖 MCDR 的 Serializable 语义**：反序列化时
    （``Config.deserialize``）每个实例都会被重新赋值，类属性不会在实例之间共享；
    ``Config()`` 直接构造出来的实例拿到的也只是同一份列表。因此**不要**在代码里就地
    修改 ``self.patterns``（追加 / 删除 / 排序）——那才会真的污染类属性与后续实例。
    需要变更时先 ``list(...)`` 拷贝。同样的约定适用于 ``State.rules``。
    """

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
    """单条规则、单个探测串的耗时上限（毫秒）。

    只影响**载入期**的灾难性回溯探测：真正逐行匹配时的每行开销是由规则本身决定的，
    与这个选项无关（详见 docs/DEVELOPMENT.md「探测是启发式，不是形式化保证」）。
    小于 1 按 1 处理；大于 ``MAX_PROBE_BUDGET_MS`` 按上限处理，且会 warning 一次。
    """

    announce_broken_config: bool = True
    """配置文件解析失败、被备份并重置时，在控制台说明原因与备份路径。"""


# `announce_broken_config` 的选项名。写成常量供 _quarantine_broken_config 使用：
# 那一刻配置已经读不出来了，只能拿这个名字去文件原文里找。测试会钉住它必须
# 是 Config 里真实存在的字段，改名时不会悄悄失配。
BROKEN_CONFIG_NOTICE_OPTION = "announce_broken_config"

# 同上，语言选项也得能从「原文」里读：配置文件写坏时，唯一能得知用户想要哪种语言的
# 地方就是他自己写的那段文本（马上就要被改名备份了）。
LANGUAGE_OPTION = "language"


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


# 控制台整行的前缀（``[12:00:00] [Server thread/INFO]: ``）。``!!lf test`` 收到的是
# 管理员整行复制的原文，而过滤器匹配的是**剥离之后的正文**，所以两条路径都要经
# ``_matching_body``（SLF-A-02）。
_LOG_LINE_PREFIX = re.compile(r"^\[[^\]]*\]\s*\[[^\]]*\]\s*:\s?")

# 玩家发言的玩家名前缀。MCDR 的 vanilla handler 剥掉控制台前缀之后**还会**把
# ``<Steve> `` 这一截从正文里剥掉（``get_player_message_parsing_formatter``），而且只在
# 名字通过 ``[a-zA-Z0-9_]{3,16}`` 校验时才当它是玩家发言。这里照抄这两条判据。
#
# 为什么是照抄而不是调用 MCDR 自己的解析器：MCDR 没有公开的「拿当前 server handler」
# 接口（``ServerInterface`` 里那个是私有属性），从插件里够到它只能走
# ``server._mcdr_server`` 这类内部结构——那会让一次 MCDR 小版本升级把 ``!!lf test``
# 打坏。所以这里只复刻这一条最小规则，并且**只在这一处**复刻。
_PLAYER_CHAT_PREFIX = re.compile(r"^(?:\[Not Secure\] )?<(?P<name>[^>]+)> ")
_PLAYER_NAME = re.compile(r"[a-zA-Z0-9_]{3,16}")


def _matching_body(source) -> Optional[str]:
    """取出**真正参与匹配的正文**——这是唯一的取值路径（SLF-A-02）。

    ``source`` 有两种形态，两条路径都必须经这里：

    * MCDR 交给过滤器的 ``Info``：正文就是 ``info.content``，读不到时退回 ``raw_content``；
    * 管理员在 ``!!lf test`` 里粘贴的控制台整行（``str``）：整行**不是**正文，要先按
      MCDR 自己的两步剥——``[时间] [线程/级别]:`` 与玩家发言的 ``<玩家名> ``。

    少了第二步，同一条规则会在两条路径上给出相反结论：``^hello`` 这类**正确**的规则
    被报成「不会被隐去」（假阴性，管理员于是去改一条本来就对的规则），而含玩家名前缀的
    规则被报成「会被隐去」却在真实过滤上永不命中（假阳性）。

    没有控制台前缀的 ``str`` 原样返回：MCDR 自己也只有认得出这个前缀时才做玩家发言
    解析（认不出就退回 basic handler，正文就是整行），这里必须与它一致。
    """
    if isinstance(source, str):
        if not _LOG_LINE_PREFIX.match(source):
            return source
        body = _LOG_LINE_PREFIX.sub("", source, count=1)
        player = _PLAYER_CHAT_PREFIX.match(body)
        if player is not None and _PLAYER_NAME.fullmatch(player.group("name")):
            body = body[player.end():]
        return body

    content = source.content
    if content is None:
        content = getattr(source, "raw_content", None)
    return content


class ServerLogFilter(InfoFilter):
    """按规则把匹配的服务端输出从控制台隐去。

    计数口径（SLF-02，**这是本类的核心约定**）：**一行命中的每一条规则都计数**，
    而不是只给第一条。原因是一次真实的错误诊断：命中路径原来是"遇到第一条匹配就
    return 并只给它计数"，于是一行被多条重叠规则命中时，只有第一条的统计在动；
    被它遮蔽的那条 ``count`` 永远为 0、``zero_streak`` 一路涨到阈值，插件就会建议
    管理员**删掉一条正在正常工作、删掉后对应日志立刻重新刷屏的规则**。

    （顺带说明为什么不能只改判据：被遮蔽的规则 ``count`` 确实是 0，
    "本轮 count == 0" 区分不出"被遮蔽"与"真的没命中"。必须在计数这一层修。）

    代价与补偿：最坏情况下每行要跑完全部规则（原来是找到第一条就停）。规则本身是
    预编译的 ``re.search``，实测仍在微秒级——数字见 docs/DEVELOPMENT.md
    「热路径的预算」，那里也记着"合并成一个正则反而更慢"的实测结论。

    另一个计数口径（SLF-A-04）：``total``（状态页的「本次已隐去」、关服汇报的行数）
    是**隐去的行数**，一行命中几条规则都只加一；各规则的 ``count`` 才是「各命中几次」。
    两者一般不相等，说明文字在 ``status.counting_note`` 里；控制台输入不计入行数
    （MCDR 的 ``echo_to_console`` 只对服务端输出生效）。
    """

    def __init__(self, rules: List[Rule], logger, log_matched_lines: bool):
        # tuple：迭代略快，整体赋值天然原子（重载时不会看到半新半旧的规则）
        self._rules: Tuple[Rule, ...] = tuple(rules)
        self._logger = logger
        self._log_matched_lines = log_matched_lines
        # 计数在主线程写、命令线程读，需要锁；只在命中路径上取，不影响逐行开销
        self._lock = threading.Lock()
        self._total = 0
        # 命中时只把 echo_to_console 清掉的掩码，算一次就够了，省掉命中路径上的一个
        # 函数调用。写成 ``default() ^ echo_to_console``（= 全部位去掉 echo 这一位）
        # 而不是 ``~InfoActionFlag.echo_to_console``：``enum.Flag.__invert__`` 在旧解释器
        # 上不保证存在，而本插件的底线是 Python 3.9。
        self._hide_mask = InfoActionFlag.default() ^ InfoActionFlag.echo_to_console

    @property
    def rules(self) -> Tuple[Rule, ...]:
        return self._rules

    @property
    def total(self) -> int:
        with self._lock:
            return self._total

    def counted_hits(self, rules: Optional[List[Rule]] = None) -> List[int]:
        """持锁读出一份命中数快照，与 ``rules`` 一一对应。

        状态页**不**裸读 ``rule.count``（SLF-10）：同一个类里「有的统计加锁、有的不加」
        会让后来者无法判断哪些是刻意不加锁的。状态页不是热路径，取锁的代价可以忽略，
        于是统一成「所有计数都经锁读」这一种口径。快照还顺手解决了「遍历期间规则被
        ``!!lf reload`` 换掉」的显示错位（SLF-12）。
        """
        with self._lock:
            target = self._rules if rules is None else tuple(rules)
            return [rule.count for rule in target]

    def carry_over_from(self, previous: Optional["ServerLogFilter"]) -> int:
        """接续上一份实例的计数（插件重载路径），否则一次开服周期会被切成两段。

        逐条规则的 ``count`` 按 pattern 搬过来，``total``（**已隐去的行数**）原样接续
        ——重载不该让「本次已隐去」归零（SLF-A-04）。返回值是接续到的**规则命中数
        之和**，只用于日志里那句「接续 N 条命中计数」。
        """
        if previous is None:
            return 0
        previous_total = previous.total
        previous_counts = {rule.pattern: rule.count for rule in previous.rules if rule.count}
        with self._lock:
            self._total = previous_total
            return _carry_counts(self._rules, previous_counts)

    def reload_rules(self, rules: List[Rule], log_matched_lines: bool) -> int:
        """就地替换规则，无需重新注册 InfoFilter；返回接续过来的**规则命中数之和**。

        **同名规则要接着数。** 管理员通常在服务端运行中改配置、敲 `!!logfilter reload`，
        从零开始数会把这一轮已经命中的次数抹掉——规则明明在工作，却被记成「零命中」。
        只有真的从配置里删掉的规则，它的计数才该消失。

        ``_total`` 是行数而不是各规则计数之和（SLF-A-04），所以它**不按被删掉的规则
        重算**：那些行确实已经被隐去了，删规则不该改写「本次已隐去多少行」。
        """
        with self._lock:
            previous = {rule.pattern: rule.count for rule in self._rules if rule.count}
            self._rules = tuple(rules)
            self._log_matched_lines = log_matched_lines
            return _carry_counts(self._rules, previous)

    def reset_counters(self) -> None:
        with self._lock:
            self._total = 0
            for rule in self._rules:
                rule.count = 0

    def count_matches(self, content: str, count_line: bool = True) -> List[Rule]:
        """给这一行命中的**每一条**规则计数，并按 ``self._rules`` 顺序返回它们。

        逐行热路径用它。为什么不再"遇到第一条就 return"：那样只有第一条规则的统计
        在动，被它遮蔽的规则 ``count`` 永远是 0、``zero_streak`` 一路涨到阈值，插件
        就会建议管理员删掉一条**正在正常工作**的规则（SLF-02）。一行命中几条就记几条，
        而"隐去"仍然只需要发生一次。

        ``count_line`` 为真时同时把 ``total``（**已隐去的行数**）加一——一行命中几条
        规则都只加一（SLF-A-04）。控制台输入要传假：MCDR 的 ``echo_to_console`` 只对
        服务端输出生效（``info_reactor_manager.put_info``），把它算进「已隐去多少行
        服务端日志」会让状态页与关服汇报多报。
        """
        hit: List[Rule] = []
        for rule in self._rules:
            if rule.regex.search(content):
                hit.append(rule)
        if hit:
            with self._lock:
                for rule in hit:
                    rule.count += 1
                if count_line:
                    self._total += 1
        return hit

    def match(self, content: str) -> Optional[Rule]:
        """返回第一条命中的规则，没有则返回 None。

        只回答"哪一条最先命中"（约等于"是哪条规则把它隐去的"）；需要计数请用
        ``count_matches()``。
        """
        for rule in self._rules:
            if rule.regex.search(content):
                return rule
        return None

    def matching_rules(self, content: str) -> List[Rule]:
        """**所有**命中的规则，且**不改动任何统计**。只给 `!!logfilter test` 用。"""
        return [rule for rule in self._rules if rule.regex.search(content)]

    def filter_server_info(self, info) -> None:
        rules = self._rules
        if not rules:
            return

        content = _matching_body(info)
        # 空串也是合法正文（``[12:00:00] [Server thread/INFO]: `` 这种行 MCDR 给出的
        # content 就是空串）：旧写法 ``if not content`` 会把 ``^$`` 这类规则永久挡在
        # 门外，而插件一边为它发 catch-all 告警、``!!lf test`` 一边回答「会被隐去」
        # ——三处说法互相矛盾（SLF-A-07）。只有真的拿不到正文才早退。
        if content is None:
            return

        # 控制台输入不算「已隐去的服务端日志行」（SLF-A-04）：echo_to_console 只对
        # 服务端输出生效。
        if not self.count_matches(content, count_line=not info.is_from_console):
            return

        # **只清 echo_to_console 这一位**，不整体赋值（SLF-A-03）：MCDR 会按注册顺序
        # 跑遍所有 filter，别的插件可能刚设过 discarded() 或 cancel_send_to_server()，
        # 整体赋值会把它们抹回来（被丢弃的行又开始派发事件、被拦下的命令照发）。
        info.action_flag &= self._hide_mask

        if self._log_matched_lines:
            self._logger.info(_t("rule.hidden", content=content))


# --------------------------------------------------------------------------
#  规则装载：编译 + 灾难性回溯探测
# --------------------------------------------------------------------------

# 探测串分三部分，执行顺序不能换。详见 docs/DEVELOPMENT.md「探测是启发式，不是形式化保证」。
#
# **一、增长对（多个字母表各一对，n 与 2n）。** 判据是 ``t(2n) / t(n)`` 超过
# ``PROBE_GROWTH_RATIO`` 即指数级。它是**主**判据，而且必须在**任何**长探测串之前跑：
#
# * 长度必须短到对最坏的模式也能跑完。这是整层设计里最硬的一个数：φ^n 类模式每加
#   2 个字符约 ×2.6，实测 `(a|aa)+$` 在 12 字符上 0.04 ms、20 字符上 1.8 ms、
#   24 字符上 6 ms、28 字符上 86 ms、40 字符上 31 s、200 字符上 10^40 步。
#   **12 与 24 是仍能给出可靠信号（比值远大于阈值）、又能把最坏耗时留在百毫秒级
#   的翻倍档**；再往上（16/32 起）单次 `re.search` 就会跑到秒级甚至更久，而 C 层
#   无法超时——那等于守卫自己先把 MCDR 卡住。
# * 配对的两个串必须都是「匹配失败」的（结尾带一个不属于字符集的 `!`）：匹配成功的
#   探测串几乎不花时间（`(a|aa)+$` 在 `"a"*12` 上 4 µs 就返回），拿它当分母等于没测。
# * 为什么每个字母表都要一对，而不是只留 `a`：`(x+x+)+y` 这类模式在 `a` 上完全
#   不回溯（1 µs），只在全是 `x` 的串上才爆炸。字母表覆盖不足，这一层就形同虚设。
GROWTH_PROBE_LENGTHS = (12, 24)
GROWTH_PROBE_ALPHABETS = ("a", "x", "0", "a ")
GROWTH_PROBE_PAIRS = tuple(
    tuple(fill * length + "!" for length in GROWTH_PROBE_LENGTHS)
    for fill in GROWTH_PROBE_ALPHABETS
)

# **二、接近真实日志行长的探测串（57~91 字符）。** 真实 MC 日志行普遍 60–300 字符，
# 只在长串上才变坏的模式在这里被绝对耗时判据拦下（长档用更小的独立预算）。这些串刻意
# 写成**多样化的真实文本**，不是又一串 `a`：全 `a` 的长串会被 φ^n 类模式直接引爆
# （200 个 `a` 对 `(a|aa)+$` 约 10^40 步，本身就是一次挂死），而多样文本对这类模式
# 只是一次普通的不匹配，反而更接近它将来要跑的真实输入。
LONG_PROBE_SUBJECTS = (
    "Player Steve standing on air - force-sending blocks below",
    "Player 0123456789abcdef standing on air - force-sending blocks below!",
    "Steve lost connection: Disconnected (0.0.0.0:25565) " + "x" * 60,
    "a " * 45 + "!",
    "0" * 90 + "!",
)

# 三、合并视图，供需要"所有探测长度"的地方（文档、键集检查）使用。
PROBE_SUBJECTS = tuple(
    subject for pair in GROWTH_PROBE_PAIRS for subject in pair
) + LONG_PROBE_SUBJECTS

# 第一对增长档（n 与 2n）的长度，作为「短档 / 长档预算」的分界。
SHORT_PROBE_LENGTH = GROWTH_PROBE_LENGTHS[0]

# 长探测串的**独立**预算：长档本身更贵，用同一个 25 ms 预算会把一条只是走得慢的
# 正常规则也判成危险，所以长档只拿短档预算的一小部分。
LONG_PROBE_FRACTION = 0.5
MIN_LONG_PROBE_BUDGET_MS = 1.0

# 增长判据：对 n 与 2n 计时，比值超过它即视为指数级回溯。
# 实测 `(a|aa)+$` 在 22+`!` → 44+`!` 上约 ×19（4.5 ms → 87 ms）；正常规则两组都是
# 微秒级、低于 ``PROBE_GROWTH_NOISE_MS`` 而直接跳过判断。比值对机器快慢不敏感，
# 比绝对耗时更稳，是这一层的**主**判据；预算判据是兜底。
PROBE_GROWTH_RATIO = 8.0
# 比值要成立，**两次**测量都得离开计时器噪声：
#
# * 大的一次要肉眼可见（``PROBE_GROWTH_FLOOR_MS``）；
# * 小的一次只需要高于 ``PROBE_GROWTH_NOISE_MS``（约 2 万次 ``perf_counter`` 滴答，
#   抖动远小于 8×），而不是原来那个 0.5 ms。
#
# 第二档从 0.5 ms 降到 20 µs 是一个真实缺陷的修法：`(a|aa)+$` 在 13 字符上只要
# 39 µs（低于 0.5 ms），于是**比值判据被整条跳过**，只剩绝对预算兜底；而那条模式在
# 25 字符上约 11 ms，在 25 ms 预算下照样通过。结果就是「P1 要拦的那条模式」在快机器
# 上被放行、在慢机器（或 CI）上被拦下——同一份代码给出两种结论。分母只要离开噪声，
# 288× 的比值本身就是可靠证据。
PROBE_GROWTH_FLOOR_MS = 0.5
PROBE_GROWTH_NOISE_MS = 0.02

# ``pattern_probe_timeout_ms`` 的下限与上限。下限 1 ms 防止「预算 ≤0 把所有规则
# 全判危险」；上限 2000 ms 防止把它写成一个巨大的值之后，``on_load``（以及
# ``!!lf reload`` 的命令线程）在探测上同步卡住（SLF-09）。
#
# 上限与探测改造是一致关系：探测总时长上界约为「各探测串的该档预算之和」，而增长
# 判据会在危险模式上提前收工，所以把上限限在 2000 ms 时单条规则的最坏探测时长仍有界
# （见 docs/DEVELOPMENT.md）。
MIN_PROBE_BUDGET_MS = 1
MAX_PROBE_BUDGET_MS = 2000

# ``_probe_pattern`` 只回报「哪一类问题」，措辞交给调用方按语言拼。命名成常量，让
# 「这些键真的有人用」对键集不变式检查（tests/test_plugin.py）可见——它们不会以字面量
# 出现在任何 _t() 调用点上。
PROBE_DETAIL_KEYS = ("probe.too_slow", "probe.growth")

# 语言探测失败时经 ``_note()`` 记下的键。同样不会以字面量出现在任何 _t() 调用点上，
# 列成常量是为了让键集不变式检查看得见（tests/test_plugin.py）。
NOTE_KEYS = (
    "language.detect_failed",
    "language.detect_failed_summary",
    "language.list_failed",
)


def _subject_budget_ms(subject: str, budget_ms: float) -> float:
    """按探测串长度给出这一档的预算（比增长档更长的串用更小的独立预算）。"""
    if len(subject) > SHORT_PROBE_LENGTH:
        return max(MIN_LONG_PROBE_BUDGET_MS, budget_ms * LONG_PROBE_FRACTION)
    return budget_ms


def _probe_pattern(regex: Pattern, budget_ms: int) -> Optional[Tuple[str, float, int]]:
    """检查正则的灾难性回溯风险；正常返回 None，可疑则返回 (原因键, 耗时秒数, 预算毫秒)。

    三个判据，**顺序不能换**：

    1. **增长率**（每个字母表的 n 与 2n）：``t(2n) / t(n)`` 超过 ``PROBE_GROWTH_RATIO``
       即指数级。主判据，最先执行。必须先算比值、再判预算，而且探测长度必须短：
       指数级模式在更长的串上会让**单次** ``re.search`` 跑几年，C 层无法超时，
       只有不去测量那个长度才是安全的。
    2. **短档绝对耗时**：任一增长档探测串超过该档预算。
    3. **长档绝对耗时**：接近真实日志行长的探测串超过长档预算（预算更小）。
       这是给"只在长串上才变坏"的模式准备的兜底，只有前面没发现问题时才执行。

    只返回数字与原因键，不返回句子：措辞属于语言文件的职责，由调用方拼消息。
    **这是启发式，不是形式化保证**：它拦不住所有病态正则，见 docs/DEVELOPMENT.md。
    """
    floor = PROBE_GROWTH_FLOOR_MS / 1000.0
    noise = PROBE_GROWTH_NOISE_MS / 1000.0
    for pair in GROWTH_PROBE_PAIRS:
        costs: List[float] = []
        for subject in pair:
            budget = _subject_budget_ms(subject, budget_ms) / 1000.0
            start = time.perf_counter()
            regex.search(subject)
            costs.append(time.perf_counter() - start)
        # 比值先行：它是**机器无关**的证据，而且能在一个字母表上就把 n 与 2n 的
        # 差距看出来。两次测量都要离开噪声才比较——分母的门槛是 **20 µs** 而不是
        # 0.5 ms，否则「小规模还在微秒级、大规模已经是毫秒级」这类模式会被整条跳过
        # （见 ``PROBE_GROWTH_NOISE_MS``）。剩下两档都只是微秒级的普通规则仍会跳过。
        if costs[-1] > floor and costs[0] > noise:
            if costs[-1] / costs[0] > PROBE_GROWTH_RATIO:
                return (PROBE_DETAIL_KEYS[1], costs[-1], int(budget_ms))
        for subject, cost in zip(pair, costs):
            budget = _subject_budget_ms(subject, budget_ms) / 1000.0
            if cost > budget:
                return (PROBE_DETAIL_KEYS[0], cost, int(budget * 1000))

    for subject in LONG_PROBE_SUBJECTS:
        budget = _subject_budget_ms(subject, budget_ms) / 1000.0
        start = time.perf_counter()
        regex.search(subject)
        cost = time.perf_counter() - start
        if cost > budget:
            return (PROBE_DETAIL_KEYS[0], cost, int(budget * 1000))
    return None


# 用来判断「这条规则是不是匹配一切」。刻意选得**差异极大**（字母/数字/标点/空白/中文），
# 这样窄规则不可能全中；只有 `.` `.*` `^` `a?` `\w*` `.+` 这类才会全中。
#
# 这里**刻意不放空串**：`.` / `.+` / `\S` 这类真正的 catch-all 本来就不匹配空串
# （Python 的 `.` 不匹配换行也不匹配空串），把 `""` 加进来会让 `all(...)` 把它们
# 全部漏报。而只想命中空行的规则（`^$`）由 ``_hides_every_blank_line`` 单独判——见下。
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


def _hides_every_blank_line(regex) -> bool:
    """这条规则是不是「命中空行」（`^$` 这类）。

    与 ``_matches_everything`` 并列的第二个「会让控制台变空」的形态：它只吞掉空行，
    影响面比 `.*` 小，但同样是管理员会漏掉的一类写法错误。

    判据是**补集**而不是往探测串里加空串：``_matches_everything`` 用的是 `all()`，
    往里加一个空串只会把 `.` / `.+` 这些真正的 catch-all 漏掉（它们不匹配空串），
    却依然认不出 `^$`（它只匹配空串、不匹配任何非空样本）。所以这里单独判：
    匹配空串、且一个非空样本都不匹配 —— 两个条件都满足才动手，纯字面量规则不会被误伤。
    """
    if regex.search("") is None:
        return False
    return not any(regex.search(subject) for subject in CATCH_ALL_SUBJECTS)



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
    # 上限同理：探测在 on_load / !!lf reload 的调用线程上同步跑，写成一个巨大的值
    # 会把这次加载卡住（SLF-09）。两个方向都只在真的钳制过时 warning 一次。
    if validate:
        clamped = min(MAX_PROBE_BUDGET_MS, max(MIN_PROBE_BUDGET_MS, probe_budget_ms))
        if clamped != probe_budget_ms:
            server.logger.warning(
                _t(
                    "probe.budget_clamped",
                    given=probe_budget_ms,
                    used=clamped,
                    minimum=MIN_PROBE_BUDGET_MS,
                    maximum=MAX_PROBE_BUDGET_MS,
                )
            )
        probe_budget_ms = clamped

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
                detail_key, detail_cost, detail_budget = problem
                server.logger.error(
                    _t(
                        "rule.dangerous",
                        pattern=pattern,
                        detail=_t(
                            detail_key,
                            cost=detail_cost * 1000,
                            budget=detail_budget,
                        ),
                    )
                )
                _rejected_patterns.append(pattern)
                continue
            if _matches_everything(regex) or _hides_every_blank_line(regex):
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

# 这三个列表由命令线程在 _build_rules 里**整体重新赋值**（CPython 下整体赋值是原子的，
# 不会出现「字典在迭代中被改大小」这类崩溃）。状态页读取处必须先取局部快照
# （``list(...)``），否则一次 !!lf reload 与 !!lf list 交错时可能显示上一版计数（SLF-12）。

# 诊断信息在**本次调用的上下文**里收集，而不是挂在模块全局上：
# 命令线程与主线程可能同时走 _resolve_language / _load_config，共享一个 dict 会互相污染，
# 也会把上一个线程的旧诊断混进这一次的警告里（SLF-06）。值是**已经翻好**的句子，
# 键是去重用的标识；收集时就翻译，是为了让汇总消息只做拼装、不再套一层模板。
_notes: contextvars.ContextVar[Optional[Dict[str, str]]] = contextvars.ContextVar(
    "server_log_filter_notes", default=None
)


def _note(key: str, key_name: str, **kwargs) -> bool:
    """在当前上下文的诊断表里记一条，返回**是否真的记下了**。

    没有活动上下文就丢弃，绝不抛异常——调用方靠返回值判断，而不是靠捕获异常
    （``i18n._list_lang_files`` 就是这样做的，见 SLF-A-12：那里原来的兜底分支
    因为这里从不抛而永远不可达）。
    """
    bucket = _notes.get()
    if bucket is None:
        return False
    bucket[key_name] = _t(key, **kwargs)
    return True


# ``_state`` 的读改写锁（SLF-08）。
#
# 改 ``_state`` 的路径分别跑在 MCDR 主线程（on_server_stop → _update_state_after_session）
# 与命令线程（!!lf reload → _prune_state、!!lf reset → _reset_streaks），原来没有任何互斥，
# 存在丢更新。这里用一把模块级锁把「读—改—写盘」整段串起来。
#
# 两个刻意的边界，写在这里免得以后被误当成疏漏：
# - 延迟创建（double-checked）：插件真实重载会新建模块，模块级互斥量在重载期间可能被
#   旧模块的线程持有着；延迟创建让新模块至少拿到一把属于自己的锁。
# - 这把锁**管不住 MCDR 自己**的原子写：``save_config_simple`` 用固定临时名
#   ``<name>.tmp``。本插件内部的写盘都已在这把锁里，所以不会再并发写同一个文件；
#   但同一目录上若有别的代码在写，仍可能互踩。详见 docs/DEVELOPMENT.md
#   「state.json 的并发窗口」。
# - **读侧同样经它取快照**（SLF-A-10）：状态页（``_show_status``）不是热路径，
#   取锁的代价可以忽略，而免锁读会印出半新半旧的组合（streak 来自本轮、开服周期号
#   来自上一轮）。于是「``_state`` 的每一次访问都在锁里」成为一条能一眼判断的规则。
_state_lock_guard = threading.Lock()
_state_lock: Optional[threading.Lock] = None


def _state_mutex() -> threading.Lock:
    """取 ``_state`` 的读改写锁（延迟创建，见 ``_state_lock`` 的说明）。"""
    global _state_lock
    lock = _state_lock
    if lock is None:
        with _state_lock_guard:
            lock = _state_lock
            if lock is None:
                lock = _state_lock = threading.Lock()
    return lock

# 只有完成过启动的周期才计入「零命中」统计，否则连续启动失败会刷出假提醒。
_session_reached_startup = False

# 配置是否刚因写坏被重置过；是的话 patterns 是默认值而非用户配置，不能据此清理历史。
_config_was_reset = False


def _t(key: str, **kwargs) -> str:
    """按本次解析出的语言取一条消息（见 ``_language``）。"""
    return i18n.translate(key, _language, **kwargs)


def _mcdr_language(server: PluginServerInterface) -> Optional[str]:
    """问 MCDR 当前用的是哪种语言，供 ``language: auto`` 使用。

    读不到就退回 MCDR 的配置字典，再不行返回 None。绝不抛异常——但**失败要留下痕迹**：
    以前这里两处 ``except Exception: pass`` 把探测性失败完全吞掉，MCDR 的 API 一变，
    中文管理员就会突然看到英文消息而日志里一个字都没有（SLF-06）。
    现在把失败记进 ``_note()``，由 ``_resolve_language`` 汇总成一条 warning。
    """
    getter = getattr(server, "get_mcdr_language", None)
    if callable(getter):
        try:
            language = getter()
        except Exception as error:  # noqa: BLE001 - 探测性调用，失败就走下面的退路
            _note(
                "language.detect_failed",
                "get_mcdr_language",
                api="get_mcdr_language",
                error="{}: {}".format(type(error).__name__, error),
            )
        else:
            if isinstance(language, str) and language.strip():
                return language

    getter = getattr(server, "get_mcdr_config", None)
    if callable(getter):
        try:
            config = getter()
        except Exception as error:  # noqa: BLE001
            _note(
                "language.detect_failed",
                "get_mcdr_config",
                api="get_mcdr_config",
                error="{}: {}".format(type(error).__name__, error),
            )
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
    notes: Dict[str, str] = {}
    token = _notes.set(notes)
    try:
        choice = i18n.resolve(
            setting,
            _mcdr_language(server),
            notes=notes,
        )
    finally:
        _notes.reset(token)

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
    if notes and warn:
        # 去重后合成一条：探测失败本身不致命（已经走退路），但必须可观。
        # 诊断在**收集时**就按当时的语言翻好，这里只拼装，避免把未格式化的模板
        # 当成参数塞进另一条模板（那样对外只会看到一串 ``{count}``）。
        server.logger.warning(
            i18n.translate_values(
                "language.detect_failed_summary",
                choice.language,
                {
                    "count": len(notes),
                    "language": choice.language,
                    "details": "; ".join(sorted(set(notes.values()))),
                },
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
    """读 state.json；损坏时先备份成 ``state.json.old`` 再交给 MCDR 重建（SLF-08）。"""
    with _state_mutex():
        _quarantine_broken_state(server, STATE_FILE_NAME)
        return server.load_config_simple(
            file_name=STATE_FILE_NAME, target_class=State, echo_in_console=False
        )


def _save_state(server: PluginServerInterface, state: State) -> None:
    """写 state.json。调用方必须已经持有 ``_state_mutex()``（见 ``_state_lock``）。"""
    server.save_config_simple(state, file_name=STATE_FILE_NAME)


def _load_config(server: PluginServerInterface) -> Config:
    """读取配置，并把语言**最早**定下来（后面的播报都要用它）。"""
    global _config_was_reset, _language, _language_setting

    data = _read_bytes(os.path.join(server.get_data_folder(), CONFIG_FILE_NAME))
    _config_was_reset = _quarantine_broken_config(server, CONFIG_FILE_NAME, data) is not None
    config = server.load_config_simple(target_class=Config)

    setting = config.language
    if _config_was_reset:
        # 配置刚被重置成默认值（language 也是默认的 auto），但用户当初明明写了自己的
        # 选择；那份选择只剩原文里有，读它才不至于「修配置过程中语言突然变了」。
        raw = _decode_utf8(data)
        if raw is not None:
            setting = _raw_str_option(raw, LANGUAGE_OPTION) or setting
    _language_setting = setting
    _language = _resolve_language(server, setting)
    return config


def _type_error_reason(parsed, say) -> Optional[str]:
    """原文能解析成对象，但 ``Config.deserialize`` 吃不下它 —— 返回原因文案或 None。

    **类型写坏**（合法 JSON、字段类型不符，例如 ``"stale_rule_threshold": "three"``）
    只有到 ``Config.deserialize`` 才会暴露，而 MCDR 默认 ``failure_policy='regen'``
    会直接用默认值覆盖原文件：用户写的 patterns 静默消失、不留 ``.old``、也没有那条
    解释性提示（SLF-04）。这里先自己无副作用地试一次反序列化
    （MCDR 的 Serializable 从不变更传入的 dict）。

    与另外三种「写坏」共用 ``_quarantine_broken_config`` 的**同一次**备份与播报，
    所以本函数只算原因，不碰文件。
    """
    try:
        Config.deserialize(parsed)
    except Exception as error:  # noqa: BLE001 - 类型/取值任何不符都走同一条路
        return say("broken.bad_types", error="{}: {}".format(type(error).__name__, error))
    return None


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
    """同 ``_raw_bool_option``，但读字符串选项（目前只有 ``language``）。

    同时接受单引号：严格 JSON 里字符串只能是双引号，但这段原文**本来就是写坏的文件**，
    手写/工具生成时混进单引号很常见；只认双引号会让抄回来的 language 悄悄失效（SLF-14）。
    """
    match = re.search(
        r"""['"]{}['"]\s*:\s*['"]([^'"]*)['"]""".format(re.escape(name)),
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
    ='regen'``，会直接用默认值覆盖原文件，用户写的规则就此消失。**四种「写坏」**都在
    这里拦下——JSON 语法错误、顶层不是对象、**字段类型不符**（合法 JSON 但
    ``Config.deserialize`` 吃不下，SLF-04）、以及**不是合法的 UTF-8**。备份永远执行，
    ``announce_broken_config`` 只管说不说；开关与语言只能从原文里读（此刻没有解析好的
    配置）。
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
            if not isinstance(parsed, dict):
                reason = say("broken.not_an_object", kind=type(parsed).__name__)
            else:
                reason = _type_error_reason(parsed, say)
                if reason is None:
                    return None          # 四种「写坏」都不成立，交给 MCDR

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

    _announce_broken_config(server, say, file_name, reason)
    return reason


def _announce_broken_config(server, say, file_name: str, reason: str) -> None:
    """配置「写坏 / 读不动」被备份后的那条解释性提示（两种检疫共用）。"""
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


def _quarantine_broken_state(
    server: PluginServerInterface, state_file: str
) -> Optional[str]:
    """``state.json`` 的检疫：写了但读不出来（语法 / 类型 / 编码坏）时备份 + 播报。

    以前只有 ``config.json`` 有这层保护，state.json 一旦损坏就走 MCDR 的 REGEN 静默重置，
    命中历史与连续零命中计数无声消失、不留 ``.old``（SLF-08）。这里把它补上，并复用与
    配置完全相同的一套 `.old` 约定；文案用同一个 ``broken.*`` 段落，因为管理员要的信息
    是一样的（哪个文件、备份在哪、为什么）。返回原因文案，没问题则返回 None。
    """
    path = os.path.join(server.get_data_folder(), state_file)
    if not os.path.isfile(path):
        return None
    data = _read_bytes(path)
    if data is None:
        return None

    raw = _decode_utf8(data)
    if raw is None:
        reason = _t("broken.not_utf8")
    else:
        try:
            parsed = json.loads(raw)
        except ValueError as error:
            reason = _t("broken.json_error", error=error)
        else:
            if not isinstance(parsed, dict):
                reason = _t("broken.not_an_object", kind=type(parsed).__name__)
            else:
                try:
                    State.deserialize(parsed)
                except Exception as error:  # noqa: BLE001 - 同 _quarantine_unloadable_config
                    reason = _t(
                        "broken.bad_types",
                        error="{}: {}".format(type(error).__name__, error),
                    )
                else:
                    return None

    backup = path + CONFIG_BACKUP_SUFFIX
    try:
        os.replace(path, backup)
    except OSError as error:
        server.logger.error(
            _t("broken.backup_failed", file=state_file, error=error)
        )
        return reason

    # state.json 里没有开关（它是插件自己写的文件），所以只有一条 warning。
    server.logger.warning(
        _t("state.broken_backup", file=state_file, backup=state_file + CONFIG_BACKUP_SUFFIX)
        + "\n"
        + _t("broken.reason_line", reason=reason)
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
    与 ``_update_state_after_session`` 共用同一把 ``_state_mutex()``（SLF-08）。
    """
    with _state_mutex():
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
    """一次开服周期结束时，把命中数写进历史并推进「连续零命中」计数。

    整个「读—改—写盘」在 ``_state_mutex()`` 里：本函数跑在 MCDR 主线程，而
    ``!!lf reload``（``_prune_state``）与 ``!!lf reset``（``_reset_streaks``）跑在
    命令线程，没有互斥就会丢更新（SLF-08）。
    """
    with _state_mutex():
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
    state: State, rules: List[Rule], counts: List[int], threshold: int
) -> List[Tuple[str, RuleState]]:
    """找回本轮**一次都没命中**、且零命中年数已达阈值的规则。

    ``counts`` 必须是与 ``rules`` 一一对应的**持锁快照**（``counted_hits()``），
    本函数不裸读 ``rule.count``（SLF-A-01）：计数由 MCDR 主线程在
    ``filter_server_info`` 里写，而本路径跑在 task executor 线程，裸读可能刚好读到
    写入前的那一刻——一条正在命中的规则被读成 0，于是刚命中过的规则被弹
    「连续 N 轮零命中，建议删除」，与本插件要消灭的那类噪音同类。

    「本轮 count == 0」这一条判据也不能少（SLF-02 / SLF-05）：一行命中多条重叠规则时
    每一条都会被计数，但仍可能有规则是真的没命中。它同时挡住了 SLF-05 那个假提醒：
    管理员把写错的规则改对之后，若中间有一个周期没走到 startup（崩溃 / 手动 kill），
    残留的 zero_streak 会停在阈值上；下一轮它已经命中过（count > 0），于是不再重复告警。
    """
    stale: List[Tuple[str, RuleState]] = []
    for rule, count in zip(rules, counts):
        entry = state.rules.get(rule.pattern)
        if entry is not None and entry.zero_streak >= threshold and count == 0:
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
        #
        # 必须**显式**把三个状态变量置回 None：真实重载会构造新模块（本来就是 None），
        # 但模块级全局在进程内是复用的，跨版本降级 / 二次 on_load 时它们可能还指着旧的
        # 对象。若不清，on_server_startup 一进来就会 None.warn_about_stale_rules 或
        # 拿旧过滤器算统计（SLF-03）。
        _config = None
        _log_filter = None
        _state = None
        _session_reached_startup = False
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


def _apply_config(
    server: PluginServerInterface,
) -> Tuple[List[Rule], List[str], int, Optional[str]]:
    """重读配置并就地替换规则（不重新注册命令与过滤器）。

    返回 ``(规则列表, 被清掉的旧规则统计, 接续过来的命中数, 统计清理失败的原因)``。

    最后一项是 SLF-A-08 的修法：``reload_rules`` **之后**才失败的步骤（写 state.json）
    不能把整条命令说成「没生效」——规则那时已经在过滤线上跑了，管理员据此会去重复
    reload / 重启、排查一个并不存在的问题。所以这里自己兜住清理的异常，把原因原样
    交给调用方去回复，而不是让它穿出去变成「重载失败，规则保持原样」。
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
    # 拿一份 patterns 的局部副本再交给清理：`_config` 是模块级对象，命令线程下一次
    # 重载会整体替换它（SLF-12）。
    configured = list(_config.patterns)
    pruned: List[str] = []
    prune_error: Optional[str] = None
    try:
        pruned = _prune_state(server, _state, configured)
    except Exception as error:  # noqa: BLE001 - 规则已生效，清理失败只降级为说明
        prune_error = "{}: {}".format(type(error).__name__, error)
        server.logger.exception(_t("reload.prune_failed", error=prune_error))
    return rules, pruned, carried, prune_error


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

    # 判空必须与 on_server_stop 形状一致，``_config`` 必须在最前面（SLF-03）：
    # 低版本 MCDR 上 on_load 打完「请升级」就 return，_config 仍是 None，而 MCDR
    # 照常派发生命周期事件——原来这里第一句就是 None.warn_about_stale_rules，
    # 每次开服完成都抛一条 AttributeError。
    if (
        _config is None
        or not _config.warn_about_stale_rules
        or _config.stale_rule_threshold <= 0
    ):
        return

    # 规则元组与计数快照都取好再进 ``_state`` 的锁（SLF-A-01）：_collect_stale 的判据
    # 里有「本轮命中数」，而计数由 MCDR 主线程在 filter_server_info 里写，本函数却跑在
    # task executor 线程（plugin_manager.dispatch_event 的默认策略 always_new_task）——
    # 裸读 rule.count 可能刚好读到写入前的那一刻。counted_hits() 走过滤器自己的锁，
    # 与状态页是同一条口径。
    rules = list(_log_filter.rules)
    counts = _log_filter.counted_hits(rules)
    with _state_mutex():
        stale = _collect_stale(_state, rules, counts, _config.stale_rule_threshold)
    if stale:
        _emit_stale_warning(server, stale, _config.stale_rule_threshold)


def on_server_stop(server: PluginServerInterface, server_return_code: int) -> None:
    global _session_reached_startup

    if _config is None:
        return

    if _session_reached_startup:
        try:
            # 规则与计数在各线程里写，所以先取一份快照再交给状态更新（SLF-08/SLF-10）。
            rules = list(_log_filter.rules)
            _update_state_after_session(server, _state, rules, _config.patterns)
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
    if _log_filter is None:
        source.reply(RText(_t("cmd.not_initialised"), RColor.red))
        return

    # 一切都在**局部快照**上操作：规则元组与三个模块级列表都会被命令线程的
    # `!!lf reload` 整体替换，直接边遍历边读会让一次 list/reload 交错显示上一版计数
    # （SLF-12）。命中数则统一经 `counted_hits()` 持锁读（SLF-10）。
    rules = tuple(_log_filter.rules)
    counts = _log_filter.counted_hits(list(rules))
    warned = list(_warned_patterns)
    rejected = list(_rejected_patterns)
    duplicate = list(_duplicate_patterns)
    hidden_lines = _log_filter.total
    # ``_state`` 的**读侧也走 ``_state_mutex()``**（SLF-A-10）：写侧
    # （``_update_state_after_session`` / ``_prune_state`` / ``_reset_streaks``）都在
    # 这把锁里，免锁读会印出半新半旧的组合（某条规则的 streak 来自本轮、开服周期号
    # 来自上一轮），也让「哪些读加锁」在同文件里出现第三种口径（SLF-10 要消除的正是它）。
    with _state_mutex():
        streaks = {
            pattern: (entry.last_hit_session, entry.zero_streak)
            for pattern, entry in _state.rules.items()
        }
        session_index = _state.session_index

    if i18n.normalize(_language_setting) in ("", i18n.AUTO):
        language_line = _t("status.language_auto", code=_language)
    else:
        language_line = _t("status.language_set", code=_language)
    parts = RTextList(
        _title_line(),
        "\n",
        _field(_t("status.rule_count"), str(len(rules))),
        _field(_t("status.hidden_now"), str(hidden_lines) + _t("status.lines")),
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
        parts.append(RText(_t("status.rule_hits", count=counts[index - 1]), RColor.green))
        parts.append(RText("-- ", RColor.gray))
        parts.append(RText(rule.pattern, RColor.white))
        last_hit_session, zero_streak = streaks.get(rule.pattern, (0, 0))
        if last_hit_session:
            parts.append(
                RText(
                    _t("status.last_hit", session=last_hit_session),
                    RColor.gray,
                )
            )
        if zero_streak:
            if threshold and zero_streak >= threshold:
                parts.append(
                    RText(_t("status.streak_warn", streak=zero_streak), RColor.red)
                )
            else:
                parts.append(
                    RText(_t("status.streak", streak=zero_streak), RColor.gray)
                )

    if warned:
        parts.append("\n")
        parts.append(
            _field(
                _t("status.warned_label"),
                _t("status.warned", count=len(warned)),
                RColor.red,
            )
        )

    if rejected:
        parts.append("\n")
        parts.append(
            _field(
                _t("status.rejected_label"),
                _t("status.rejected", count=len(rejected)),
                RColor.yellow,
            )
        )

    if duplicate:
        parts.append("\n")
        parts.append(
            _field(
                _t("status.duplicate_label"),
                _t("status.duplicate", count=len(duplicate)),
                RColor.yellow,
            )
        )

    if session_index:
        parts.append("\n")
        parts.append(
            _field(
                _t("status.sessions_label"),
                _t("status.sessions", count=session_index),
                RColor.white,
            )
        )

    parts.append("\n")
    # 「各规则命中数之和」不一定等于「本次已隐去」：一行可能被多条重叠规则命中，
    # 而只需要隐去一次。不说明的话管理员会以为统计坏了（SLF-13）。
    parts.append(RText(_t("status.counting_note"), RColor.gray))
    parts.append("\n")
    parts.append(RText(_t("status.tip_label"), RColor.aqua))
    parts.append(RText(_t("status.tip"), RColor.gray))
    source.reply(parts)


def _reload(source) -> None:
    # 用插件自己的 _server，不能用 source.get_server()：后者没有 load_config_simple()
    if _server is None:
        source.reply(RText(_t("cmd.not_initialised_reload"), RColor.red))
        return
    # 重载路径会读配置、编译正则、写 state，任一步都可能抛（配置文件权限、正则爆炸、
    # 磁盘满）。`!!lf reset` 早就兜了异常，命令面两种待遇说不过去：MCDR 只会记一条
    # traceback，管理员得不到任何回复（SLF-15）。
    try:
        rules, pruned, carried, prune_error = _apply_config(_server)
        _log_summary(_server, rules)
        _announce_pruned(_server, pruned)
    except Exception as error:  # noqa: BLE001 - 命令面必须给出可读回复
        # 详情拼进消息本身，而不是只靠 logger.exception 的 traceback：管理员在控制台
        # 也常常只看到命令回复那一行，异常类型与原因必须跟着它一起出现。
        detail = "{}: {}".format(type(error).__name__, error)
        _server.logger.exception(_t("reload.failed", error=detail))
        source.reply(RText(_t("reload.failed_reply", error=detail), RColor.red))
        return
    reply = RTextList(
        RText(_t("reload.done"), RColor.green),
        RText(_t("reload.count", count=len(rules)), RColor.gray),
    )
    if carried:
        # 让管理员看见「这一轮的计数没有丢」，否则他会以为重载把统计清了
        reply.append(RText(_t("reload.carried_session", count=carried), RColor.gray))
    if pruned:
        reply.append(RText(_t("reload.pruned", count=len(pruned)), RColor.gray))
    if prune_error:
        # 规则**已经生效**，只有统计清理失败——回复必须说清这个区别（SLF-A-08），
        # 否则管理员会以为自己的修改没进去，然后去重复 reload / 重启。
        reply.append(
            RText(
                _t("reload.prune_failed_reply", count=len(rules), error=prune_error),
                RColor.red,
            )
        )
    source.reply(reply)


def _reset_streaks(source) -> None:
    """清空「连续零命中」计数，用于「确认这条规则本来就罕见，别再来烦我」。"""
    if _server is None or _state is None or _log_filter is None:
        source.reply(RText(_t("cmd.not_initialised_reset"), RColor.red))
        return
    cleared = 0
    try:
        # 与 on_server_stop / _prune_state 共用同一把锁，别让命令线程的读改写
        # 和主线程的结算互相覆盖（SLF-08）。
        with _state_mutex():
            for rule in _log_filter.rules:
                entry = _state.rules.get(rule.pattern)
                if entry is not None and entry.zero_streak:
                    entry.zero_streak = 0
                    cleared += 1
            _save_state(_server, _state)
    except Exception as error:  # noqa: BLE001
        _server.logger.exception(_t("state.reset_save_failed"))
        # 不许谎报成功（SLF-A-05）：内存里的 zero_streak 确实清零了（本轮不再告警），
        # 但没有落盘——重启或重载之后旧计数会原样回来，而管理员刚刚看到的是绿字
        # 「已重置」，于是判断「reset 不起作用」。这里明确说清哪一半成功、哪一半失败，
        # 并把异常原因一起给出来（控制台常常只看得到回复这一行）。
        detail = "{}: {}".format(type(error).__name__, error)
        source.reply(RText(_t("state.reset_save_failed_reply", error=detail), RColor.red))
        return
    source.reply(
        RTextList(
            RText(_t("reset.done"), RColor.green),
            RText(_t("reset.count", count=cleared), RColor.gray),
        )
    )


def _test_line(source, context) -> None:
    if _log_filter is None:
        source.reply(RText(_t("cmd.not_initialised"), RColor.red))
        return

    text = context["text"]
    # 正文与真实过滤**共用同一个取值函数**（SLF-A-02）：MCDR 剥两截（控制台前缀 +
    # 玩家发言的 ``<玩家名> ``），这里也必须剥两截，否则 ``^hello`` 这类**正确**的
    # 规则会被报成「不会被隐去」，管理员于是去改一条本来就对的规则。
    # 只粘贴了前缀本身（``[12:00:00] [Server thread/INFO]: ``）时剥完是**空串**——
    # 空串是合法正文（SLF-A-07），必须按空串测（SLF-15）。
    body = _matching_body(text)
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
