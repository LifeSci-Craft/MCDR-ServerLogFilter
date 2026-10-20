#!/usr/bin/env python3
"""Mutation check: deliberately break the plugin and confirm the tests notice.

A passing suite proves nothing on its own. This script introduces a handful of
targeted defects, one at a time, and requires the relevant tests to fail. If any
mutation survives, the test that is supposed to guard that behaviour is
decorative and needs strengthening.

Run from the repository root::

    python tools/mutation_check.py

Needs the ``.testlibs`` setup (see tests/README.md).
"""

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SRC = "server_log_filter/__init__.py"
I18N = "server_log_filter/i18n.py"
LANG_ZH = "server_log_filter/lang/zh_cn.json"
PACK = "pack.py"

WARN_BLOCK = """    if (
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
"""

ON_START_TAIL = """    if _log_filter is not None:
        _log_filter.reset_counters()
"""

def disable_warning(src):
    return src.replace(WARN_BLOCK, "    return  # mutation\n")

def threshold_off_by_one(src):
    return src.replace(
        "if entry is not None and entry.zero_streak >= threshold and count == 0:",
        "if entry is not None and entry.zero_streak > threshold and count == 0:  # mutation",
    )

def drop_startup_guard(src):
    return src.replace(
        "    if _session_reached_startup:\n",
        "    if True:  # mutation: startup guard dropped\n",
    )

def break_carry_over(src):
    return src.replace(
        "        if previous is None:\n            return 0\n",
        "        if previous is None:\n            return 0\n        return 0  # mutation\n",
    )

def drop_session_flag_carry_over(src):
    return src.replace(
        '    if getattr(prev_module, "_session_reached_startup", False):\n'
        "        _session_reached_startup = True\n",
        "    # mutation: running-session flag not carried over\n",
    )

def disable_probe(src):
    return src.replace(
        "        validate=_config.validate_patterns,",
        "        validate=False,  # mutation",
    )

def warn_before_startup(src):
    if WARN_BLOCK not in src or ON_START_TAIL not in src:
        return src
    src = src.replace(WARN_BLOCK, "    pass  # mutation\n")
    early = ON_START_TAIL + """    # mutation: warn before the server finished starting
    if (
        _config is not None
        and _state is not None
        and _log_filter is not None
        and _config.warn_about_stale_rules
        and _config.stale_rule_threshold > 0
    ):
        _m_rules = list(_log_filter.rules)
        _m = _collect_stale(
            _state, _m_rules, _log_filter.counted_hits(_m_rules),
            _config.stale_rule_threshold,
        )
        if _m:
            _emit_stale_warning(server, _m, _config.stale_rule_threshold)
"""
    return src.replace(ON_START_TAIL, early, 1)
def inject_a_key_into_the_config(src):
    """往配置里塞一个非选项键（模拟「自己加注释」的诱惑）。

    配置里除了真正的选项不该有任何键，否则文件会变复杂——这正是注释字段被移除的原因。
    注入后它会被当成一个真字段，于是生成的 config.json 里会多出一项。

    锚点原来挂在 `_config_option_names()` 上；那个函数随 1.4.3 移除升级播报一起删掉了，
    于是变异施加不上（工具会正确地把它算成「脚本自己过期」，而不是「测试没抓住」）。
    """
    return src.replace(
        'BROKEN_CONFIG_NOTICE_OPTION = "announce_broken_config"\n',
        'BROKEN_CONFIG_NOTICE_OPTION = "announce_broken_config"\n'
        "\n"
        "# mutation: the shape an earlier revision shipped to imitate JSON comments\n"
        'Config.__annotations__["#手写的注释"] = str\n'
        'setattr(Config, "#手写的注释", "x")\n',
        1,
    )


QUARANTINE_CALL = (
    "    _config_was_reset = _quarantine_broken_config(server, CONFIG_FILE_NAME, data) is not None\n"
)

DOC_SUFFIX_GUARD = "        if entry.zero_streak > threshold:\n"

def drop_quarantine_call(src):
    """不再检查配置文件 —— 坏文件会被 MCDR 直接覆盖，用户的规则就此消失。"""
    return src.replace(
        QUARANTINE_CALL, "    _config_was_reset = False  # mutation: no quarantine\n"
    )

def drop_backup_move(src):
    """检测到了问题，但不真的备份。"""
    return src.replace(
        "        os.replace(path, backup)\n",
        "        pass  # mutation: no backup\n",
    )

def repeat_streak_for_every_rule(src):
    """恢复成「每条规则都重复一遍零命中次数」的旧格式。"""
    return src.replace(
        DOC_SUFFIX_GUARD,
        "        if True:  # mutation: always annotate\n",
    )

def ignore_the_broken_config_switch(src):
    """无视 announce_broken_config —— 关了也照样播报。"""
    return src.replace(
        '    if _raw_bool_option(raw or "", BROKEN_CONFIG_NOTICE_OPTION) is False:\n',
        "    if False:  # mutation: broken-config switch ignored\n",
        1,
    )

def silence_reset_announcement(src):
    """不再提示配置已被重置（坏文件被悄悄换掉了）。"""
    anchor = '    server.logger.error(\n        "\\n".join('
    if anchor not in src:
        return src
    return src.replace(anchor, "    return reason  # mutation: reset not announced\n" + anchor, 1)


PRUNE_ON_LOAD = (
    "    _announce_pruned(server, _prune_state(server, _state, _config.patterns))\n"
)
PRUNE_RELOAD_STEP = "        pruned = _prune_state(server, _state, configured)\n"
PRUNE_CALL = "    removed = _forget_orphans(state, patterns)\n"
RESET_GUARD = "    if _config_was_reset:\n"

def drop_prune_on_load(src):
    """重载插件时不再清理已删除规则的统计（回到「要等一次完整开服周期」）。"""
    return src.replace(PRUNE_ON_LOAD, "    pass  # mutation: no prune on load\n")

def drop_prune_on_reload_command(src):
    """!!logfilter reload 不再清理已删除规则的统计。

    锚点跟着 SLF-A-08 的重构走：清理那一步现在住在 ``try`` 里（失败只降级为说明），
    所以直接替换那一条调用。
    """
    return src.replace(PRUNE_RELOAD_STEP, "            pruned = []  # mutation: no prune on reload\n")

def prune_against_compiled_rules(src):
    """按「编译成功的规则」而不是「配置原文」判断孤立项 —— 会误删被安全探测拦下的规则。"""
    return src.replace(
        PRUNE_CALL,
        "    removed = _forget_orphans(state, [r.pattern for r in _log_filter.rules])  # mutation\n",
    )

def ignore_the_reset_guard(src):
    """配置刚被自动重置（写坏）时也照常清理 —— 会顺手抹掉用户还在的历史。"""
    return src.replace(RESET_GUARD, "    if False:  # mutation: reset guard ignored\n")


# --- language ---------------------------------------------------------------
#
# These exist because this feature has a nasty failure mode: getting it wrong does not
# crash anything, it just quietly stops talking to the user. Two of them mutate a
# language file rather than Python, which is why every entry below names its target.

LANGUAGE_ASSIGN = "    _language = _resolve_language(server, setting)\n"
# Anchor on the *callee* rather than the whole call: `_resolve_language` now passes an
# extra `notes=` bucket, so a full-statement anchor drifts on every signature change.
MCDR_LOOKUP = "            _mcdr_language(server),\n"
NOTE_GUARD = "    if choice.note_key is not None and warn:\n"
CATALOG_GUARD = "    if problem is not None and warn:\n"
QUARANTINE_LANGUAGE = (
    "        setting = _raw_str_option(raw, LANGUAGE_OPTION) or i18n.AUTO\n"
)
LOADER_READ = (
    "    try:\n"
    "        data = pkgutil.get_data(__package__, relative)\n"
    "    except (OSError, ImportError, ValueError):\n"
    "        data = None\n"
)


def ignore_the_language_option(src):
    """无视配置里的 language —— 一律用回落语言说话。"""
    return src.replace(
        LANGUAGE_ASSIGN, "    _language = i18n.FALLBACK_LANGUAGE  # mutation\n"
    )


def ignore_the_mcdr_language(src):
    """auto 不再跟随 MCDR，直接落回默认语言。"""
    return src.replace(
        MCDR_LOOKUP, "            None,  # mutation: do not ask MCDR\n"
    )


def skip_the_catalogues(src):
    """不再经过加载器读语言文件 —— 打包成 .mcdr 后整包就只剩裸键名。"""
    return src.replace(LOADER_READ, "    data = None  # mutation: bypass the loader\n")


def silence_the_unknown_language_warning(src):
    """认不出的 language 取值被静默吞掉（用户会以为设置生效了）。"""
    return src.replace(NOTE_GUARD, "    if False:  # mutation\n", 1)


def silence_the_unreadable_catalogue_warning(src):
    """语言文件坏掉也不吭声。"""
    return src.replace(CATALOG_GUARD, "    if False:  # mutation\n", 1)


def silence_the_language_probe_failures(src):
    """探测性失败（语言探测 / 语言目录列举）不再留痕 —— 中文管理员会突然看到英文。

    锚点跟着 SLF-A-12 的修复走：``_note`` 现在返回「是否真的写入了」（``i18n`` 的兜底
    分支靠它判断），所以这里插一句 ``return False``，等价于「记不下任何诊断」。
    """
    return src.replace(
        "def _note(key: str, key_name: str, **kwargs) -> bool:\n",
        "def _note(key: str, key_name: str, **kwargs) -> bool:  # mutation: swallow\n"
        "    return False\n",
        1,
    )


def ignore_the_language_in_a_broken_config(src):
    """写坏的配置里写的语言不再被读到 —— 播报会莫名其妙换一种语言。"""
    return src.replace(
        QUARANTINE_LANGUAGE, "        setting = i18n.AUTO  # mutation: ignore the file\n"
    )


def drop_a_translation(src):
    """某个语言少了一条消息（用户会看到裸键名）。适用于语言文件而非 Python。"""
    data = json.loads(src)
    data.pop("status.tip", None)
    return json.dumps(data, ensure_ascii=False, indent=2) + "\n"


def drop_a_placeholder(src):
    """译文里漏掉 {count} —— 句子不再只是难看，而是说谎。"""
    data = json.loads(src)
    data["summary.rules_enabled"] = data["summary.rules_enabled"].replace("{count}", "")
    return json.dumps(data, ensure_ascii=False, indent=2) + "\n"


def raise_on_an_undecodable_config(src):
    """不再把「不是 UTF-8」当成配置写坏 —— 解码错误会一路冒到 on_load，插件加载不了。"""
    return src.replace(
        '    try:\n'
        '        return data.decode("utf-8")\n'
        '    except UnicodeDecodeError:\n'
        '        return None\n',
        '    return data.decode("utf-8")  # mutation: decode errors escape\n',
    )


def drop_the_probe_floor(src):
    """去掉探测预算的下限 —— 预算写成 0 时每一条规则都会被判成「太慢」而拒掉。

    上限保留（否则 0 会被上限"顺手救回来"，变成一次等价变异，测试永远抓不到）。
    """
    return src.replace(
        "        clamped = min(MAX_PROBE_BUDGET_MS, max(MIN_PROBE_BUDGET_MS, probe_budget_ms))\n",
        "        clamped = min(MAX_PROBE_BUDGET_MS, probe_budget_ms)  # mutation: no floor\n",
    )


def drop_the_test_prefix_strip(src):
    """!!logfilter test 不再剥掉控制台前缀 —— `^` 锚定的规则会被误报成不命中。

    锚点跟着 SLF-A-02 的修复走：剥前缀那几行从 ``_test_line`` 搬进了
    ``_matching_body``（过滤与 ``test`` 共用的一处），但语义没变。
    """
    return src.replace(
        "    if isinstance(source, str):\n"
        "        if not _LOG_LINE_PREFIX.match(source):\n"
        "            return source\n",
        "    if isinstance(source, str):\n"
        "        if True:  # mutation: no prefix strip\n"
        "            return source\n",
    )


def narrow_the_compile_catch(src):
    """回到只接 re.error —— 一个打错的量化符就能把整个 on_load 带下去。"""
    return src.replace(
        "        except (re.error, OverflowError, RecursionError) as error:\n",
        "        except re.error as error:  # mutation: too narrow\n",
    )


def narrow_the_format_catch(src):
    """格式化失败不再兜住 AttributeError —— 一个 `{a.b}` 的笔误就能让消息路径抛异常。"""
    return src.replace(
        "    except (KeyError, IndexError, ValueError, AttributeError, TypeError):\n",
        "    except (KeyError, IndexError, ValueError):  # mutation: too narrow\n",
    )


def bare_command_shows_status_again(src):
    """裸命令又回到状态页 —— 「命令打错了」和「我要看状态」再次得到同一个回应。"""
    return src.replace(
        "        .runs(_show_help)\n",
        "        .runs(_show_status)  # mutation\n",
    )


def drop_the_command_alias(src):
    """!!lf 别名被去掉（只剩一种拼法）。"""
    return src.replace(
        "ROOT_LITERALS = (COMMAND, COMMAND_ALIAS)\n",
        "ROOT_LITERALS = (COMMAND,)  # mutation\n",
    )


def open_everything_up(src):
    """权限门形同虚设：任何玩家都能看到规则与日志内容。"""
    return src.replace(
        "    return source.has_permission(PermissionLevel.ADMIN)\n",
        "    return True  # mutation\n",
    )


def drop_the_click_events(src):
    """帮助行不再可点（游戏内点了没反应）。"""
    return src.replace(
        "        RText(command, RColor.aqua).set_click_event(action, click),\n",
        "        RText(command, RColor.aqua),  # mutation\n",
    )


def drop_the_permission_denial_explanation(src):
    """权限被拒时又只剩默认的「权限不足」——玩家会以为「我明明是 OP 啊」。"""
    return src.replace(
        "        .requires(_admin_only, _admin_denied_message)\n",
        "        .requires(_admin_only)  # mutation\n",
    )


def drop_the_version_from_the_title(src):
    """顶栏不再显示版本号。"""
    return src.replace(
        "    if version:\n"
        "        line.append(RText(\" v\" + version, RColor.yellow))\n",
        "    if False:  # mutation\n"
        "        line.append(RText(\" v\" + version, RColor.yellow))\n",
    )


def break_the_help_alignment(src):
    """帮助行不再补空格 —— 说明文字会跟着子命令长短左右乱跳。"""
    return src.replace(
        '    padding = " " * (_HELP_COMMAND_WIDTH - len(command))\n',
        '    padding = ""  # mutation\n',
    )


def bring_back_dark_text(src):
    """重新用上 dark_gray —— 深色终端上几乎看不见。"""
    return src.replace(
        '        RText(description, RColor.white),\n',
        '        RText(description, RColor.dark_gray),  # mutation\n',
    )


def drop_the_status_row_separator(src):
    """状态页的规则行不再有 ``--`` —— 与帮助页的形状就不一致了。"""
    return src.replace(
        '        parts.append(RText("-- ", RColor.gray))\n',
        '        parts.append(RText("", RColor.gray))  # mutation\n',
    )


def status_labels_lose_their_colour(src):
    """状态页的标签退回 gray —— 又和帮助页的 aqua 分成两套。"""
    return src.replace(
        '    return RTextList(RText(label, RColor.aqua), RText(value, value_colour))\n',
        '    return RTextList(RText(label, RColor.gray), RText(value, value_colour))  # mutation\n',
    )


def always_pad_the_rule_index(src):
    """编号永远补到两位 —— 9 条以内会出现 ``[ 1]`` 这种多余空格。"""
    return src.replace(
        "    index_width = len(str(len(rules)))\n",
        "    index_width = 2  # mutation\n",
    )


def ship_the_code_with_its_comments(src):
    """打包时不再剥注释 —— 包又变回「仓库什么样就发什么样」。"""
    return src.replace(
        "                data = packaged_source(path)\n",
        "                data = path.read_bytes()  # mutation\n",
    )


def strip_without_keeping_line_numbers(src):
    """剥注释时整行删掉而不是留空 —— 包内行号与仓库对不上，堆栈会指错行。"""
    return src.replace(
        "            lines[row - 1] = line[:col].rstrip() + newline\n",
        '            lines[row - 1] = ""  # mutation: shift line numbers\n',
    )


def zero_the_counts_on_a_command_reload(src):
    """命令重载又回到「计数清零」—— 本轮已命中的次数被抹掉。

    ``reload_rules`` 里 ``_carry_counts(...)`` 既把旧计数搬到新规则对象上、又返回
    「接续到的命中数之和」，所以把这一行换成 ``return 0`` 就等于整个接续逻辑消失。
    """
    return src.replace(
        "            return _carry_counts(self._rules, previous)\n",
        "            return 0  # mutation\n",
    )


def count_only_the_first_matching_rule(src):
    """SLF-02 的回归锚点：命中路径又回到「只给第一条计数」。

    这一条是本次修复的核心承诺，必须有变异守着——否则以后有人为了省这点开销把它
    改回去，测试不会变红。
    """
    return src.replace(
        "        hit: List[Rule] = []\n"
        "        for rule in self._rules:\n"
        "            if rule.regex.search(content):\n"
        "                hit.append(rule)\n",
        "        hit: List[Rule] = []\n"
        "        for rule in self._rules:\n"
        "            if rule.regex.search(content):\n"
        "                hit.append(rule)\n"
        "                break  # mutation: only the first rule is counted\n",
    )


def stale_criterion_drops_the_session_count(src):
    """SLF-02/05 的判据回归：``_collect_stale`` 不再看「本轮是否真的零命中」。"""
    return src.replace(
        "if entry is not None and entry.zero_streak >= threshold and count == 0:",
        "if entry is not None and entry.zero_streak >= threshold:  # mutation\n",
    )


def drop_the_type_level_quarantine(src):
    """SLF-04：类型写坏的配置不再被检疫（会被 MCDR 的 REGEN 静默覆盖）。"""
    return src.replace(
        "                reason = _type_error_reason(parsed, say)\n"
        "                if reason is None:\n"
        "                    return None          # 四种「写坏」都不成立，交给 MCDR\n",
        "                return None  # mutation: no type-level quarantine\n",
    )


def drop_the_state_quarantine(src):
    """SLF-08：state.json 损坏时不再备份，直接走 MCDR 的静默重置。"""
    return src.replace(
        "        _quarantine_broken_state(server, STATE_FILE_NAME)\n",
        "        pass  # mutation: state.json not quarantined\n",
    )


def drop_the_state_lock(src):
    """SLF-08：``_state`` 的读改写不再互斥。"""
    return src.replace(
        "    with _state_mutex():\n        state.session_index += 1\n",
        "    if True:  # mutation: no state lock\n        state.session_index += 1\n",
    )


def drop_the_reload_try(src):
    """SLF-15：``!!lf reload`` 又没有异常兜底。

    去掉 ``except`` 子句，让异常直接穿出命令处理器——这正是修复前的状态。
    保留 ``try:`` 是为了不动缩进（重排整段的变异很容易写成不合法代码）。
    """
    return src.replace(
        "    except Exception as error:  # noqa: BLE001 - 命令面必须给出可读回复\n",
        "    except KeyboardInterrupt as error:  # mutation: no guard for real failures\n",
        1,
    )


def report_only_the_first_matching_rule(src):
    """`test` 又只报第一条命中规则。"""
    return src.replace(
        "        return [rule for rule in self._rules if rule.regex.search(content)]\n",
        "        first = self.match(content)\n"
        "        return [first] if first else []  # mutation\n",
    )


def drop_the_catch_all_guard(src):
    """去掉「匹配一切」守卫 —— 一个字符的打错又能静音整个控制台且毫无提示。"""
    return src.replace(
        "            if _matches_everything(regex) or _hides_every_blank_line(regex):\n",
        "            if False:  # mutation\n",
    )


def stop_deduplicating_rules(src):
    """不再去重 —— 同一条规则写两次又会互相污染统计。"""
    return src.replace(
        "            _duplicate_patterns.append(pattern)\n"
        "            continue\n",
        "            pass  # mutation\n",
    )


def let_the_timestamp_follow_the_file_mtime(src):
    """时间戳回到文件 mtime —— 包不再可复现。

    别写成 ``ZipInfo(rel)``：它的**默认** date_time 恰好就是 1980-01-01，
    与固定常量相同，于是这个变异不改变任何行为、测试永远抓不到（犯过一次）。
    """
    return src.replace(
        "            info = zipfile.ZipInfo(rel, date_time=FIXED_ZIP_TIMESTAMP)\n",
        "            info = zipfile.ZipInfo(\n"
        "                rel, date_time=zipfile.ZipInfo.from_file(path).date_time\n"
        "            )  # mutation: follow the file mtime\n",
    )


def overwrite_the_action_flag_again(src):
    """SLF-A-03 的回归锚点：命中又**整体赋值**，把别的插件设的位抹掉。

    MCDR 的 ``put_info`` 按注册顺序跑遍所有 filter，返回 False 的那个已经把
    ``action_flag`` 置成 ``discarded()``，别的插件也可能 ``cancel_send_to_server()``；
    整体赋值会把它们抹回来——被丢弃的行又开始派发事件、被拦下的命令照发。
    """
    return src.replace(
        "        info.action_flag &= self._hide_mask\n",
        "        info.action_flag = self._hide_mask  # mutation: other filters' bits overwritten\n",
    )


def count_hits_instead_of_lines(src):
    """SLF-A-04 的回归锚点：``total`` 又变回「各规则命中数之和」。

    一行被两条重叠规则命中时它会加 2，而状态页与关服汇报把它当「已隐去的行数」印出来。
    """
    return src.replace(
        "                if count_line:\n                    self._total += 1\n",
        "                if count_line:\n                    self._total += len(hit)  # mutation\n",
    )


def count_console_input_too(src):
    """SLF-A-04 的另一半：控制台输入又混进「已隐去多少行服务端日志」。

    ``echo_to_console`` 只对服务端输出生效（``info_reactor_manager.put_info``），
    管理员敲的那一行并没有被隐去。
    """
    return src.replace(
        "        if not self.count_matches(content, count_line=not info.is_from_console):\n",
        "        if not self.count_matches(content):  # mutation: console input counted\n",
    )


def let_a_blank_body_return_early(src):
    """SLF-A-07 的回归锚点：空正文又提前 return。

    只匹配空串的规则（``^$``）于是在真实过滤里一次也不命中，而插件一边为它发
    catch-all 告警、``!!lf test`` 一边回答「会被隐去」。
    """
    return src.replace(
        "        if content is None:\n            return\n",
        "        if not content:\n            return  # mutation: blank body skipped\n",
    )


def drop_the_player_prefix_strip(src):
    """SLF-A-02 的回归锚点：``!!lf test`` 不再剥玩家发言的 ``<玩家名> `` 前缀。

    真实过滤拿到的是 MCDR 剥好的正文（``hello there``），而命令侧留着 ``<Steve> `` 去匹配，
    于是 ``^hello`` 这类**正确**的规则被报成「不会被隐去」。
    """
    return src.replace(
        "        player = _PLAYER_CHAT_PREFIX.match(body)\n"
        "        if player is not None and _PLAYER_NAME.fullmatch(player.group(\"name\")):\n"
        "            body = body[player.end():]\n",
        "        # mutation: the player-name prefix is left in the body\n",
    )


# (名称, 被改的文件, 改法, pytest 选择器)
MUTATIONS = [
    ("idle-rule warning disabled", SRC, disable_warning,
     ["tests/test_plugin.py", "-k", "idle or stale or streak"]),
    ("threshold changed from >= to >", SRC, threshold_off_by_one,
     ["tests/test_plugin.py", "-k", "threshold or idle_rule"]),
    ("'session finished starting' guard removed", SRC, drop_startup_guard,
     ["tests/test_plugin.py", "-k", "never_reached_startup"]),
    ("reload counter carry-over broken", SRC, break_carry_over,
     ["tests/test_plugin.py", "-k", "reload_does_not_fake"]),
    ("running-session flag lost on reload", SRC, drop_session_flag_carry_over,
     ["tests/test_plugin.py", "-k", "reload_carries_the_running_session"]),
    ("catastrophic-backtracking probe disabled", SRC, disable_probe,
     ["tests/test_plugin.py", "-k", "catastrophic or applies_the_probe or validate_patterns"]),
    ("warning moved before the Done line", SRC, warn_before_startup,
     ["tests/test_e2e.py", "-k", "arrives_after"]),
    ("a non-option key injected into the config", SRC, inject_a_key_into_the_config,
     ["tests/test_plugin.py", "-k", "no_comment_fields or generated_config_contains_only"]),
    ("the broken-config switch ignored", SRC, ignore_the_broken_config_switch,
     ["tests/test_plugin.py", "-k", "switch or leniently"]),
    ("broken config no longer quarantined", SRC, drop_quarantine_call,
     ["tests/test_plugin.py", "-k", "broken_config or quarantine or on_load_checks"]),
    ("problem detected but file not backed up", SRC, drop_backup_move,
     ["tests/test_plugin.py", "-k", "backed_up_before"]),
    ("streak repeated for every rule again", SRC, repeat_streak_for_every_rule,
     ["tests/test_plugin.py", "-k", "no_annotation or does_not_repeat"]),
    ("config reset not announced", SRC, silence_reset_announcement,
     ["tests/test_plugin.py", "-k", "announcement_says_what_where_and_why"]),
    ("no pruning of deleted rules on plugin load", SRC, drop_prune_on_load,
     ["tests/test_plugin.py", "-k", "prunes_its_state_immediately or announces_what_it_removed"]),
    ("no pruning of deleted rules on !!logfilter reload", SRC, drop_prune_on_reload_command,
     ["tests/test_plugin.py", "-k", "prunes_its_state_on_the_reload_command"]),
    ("orphans judged against the compiled rules", SRC, prune_against_compiled_rules,
     ["tests/test_plugin.py", "-k", "rejected_by_the_safety_probe"]),
    ("history wiped when the config was auto-reset", SRC, ignore_the_reset_guard,
     ["tests/test_plugin.py", "-k", "reset_config_does_not_wipe"]),
    ("the language option ignored", SRC, ignore_the_language_option,
     ["tests/test_plugin.py", "-k",
      "explicit_language_overrides or leniently or switches_on_reload"]),
    ("auto no longer follows MCDR", SRC, ignore_the_mcdr_language,
     ["tests/test_plugin.py", "-k", "auto_follows_mcdr or idle or threshold"]),
    ("catalogues read from the filesystem, not the loader", I18N, skip_the_catalogues,
     ["tests/test_plugin.py", "-k", "packed_plugin_can_still_read"]),
    ("an unrecognised language value not reported", SRC, silence_the_unknown_language_warning,
     ["tests/test_plugin.py", "-k", "unknown_language"]),
    ("an unreadable catalogue not reported", SRC, silence_the_unreadable_catalogue_warning,
     ["tests/test_plugin.py", "-k", "broken_catalogue_falls_back"]),
    ("a broken config's language not read from its own text", SRC,
     ignore_the_language_in_a_broken_config,
     ["tests/test_plugin.py", "-k", "broken_config_notice_uses_the_language"]),
    ("a message missing from one translation", LANG_ZH, drop_a_translation,
     ["tests/test_plugin.py", "-k", "same_keys"]),
    ("a placeholder dropped from a translation", LANG_ZH, drop_a_placeholder,
     ["tests/test_plugin.py", "-k", "keeps_the_placeholders"]),
    ("an undecodable config treated as fatal", SRC, raise_on_an_undecodable_config,
     ["tests/test_plugin.py", "-k", "not_utf8"]),
    ("the probe budget floor removed", SRC, drop_the_probe_floor,
     ["tests/test_plugin.py", "-k", "nonsense_probe_budget"]),
    ("the console prefix no longer stripped", SRC, drop_the_test_prefix_strip,
     ["tests/test_plugin.py", "-k", "strips_the_console_prefix"]),
    ("the compile catch narrowed back to re.error", SRC, narrow_the_compile_catch,
     ["tests/test_plugin.py", "-k", "overflows_is_skipped or deeply_nested_is_skipped"]),
    ("the format catch narrowed again", I18N, narrow_the_format_catch,
     ["tests/test_plugin.py", "-k", "bad_attribute_is_returned"]),
    ("the bare command wired back to the status screen", SRC, bare_command_shows_status_again,
     ["tests/test_plugin.py", "-k", "bare_command_is_wired_to_the_help_screen"]),
    ("the !!lf alias dropped", SRC, drop_the_command_alias,
     ["tests/test_plugin.py", "-k", "both_spellings"]),
    ("the admin gate opened up", SRC, open_everything_up,
     ["tests/test_plugin.py", "-k", "open_to_players"]),
    ("the help lines made unclickable", SRC, drop_the_click_events,
     ["tests/test_plugin.py", "-k", "clickable"]),
    ("the permission denial explanation dropped", SRC,
     drop_the_permission_denial_explanation,
     ["tests/test_plugin.py", "-k", "not_vanilla_op"]),
    ("the version dropped from the title bar", SRC, drop_the_version_from_the_title,
     ["tests/test_plugin.py", "-k", "names_the_plugin_and_the_version or different_colour"]),
    ("the help rows no longer aligned", SRC, break_the_help_alignment,
     ["tests/test_plugin.py", "-k", "aligned_in_one_column"]),
    ("dark text brought back", SRC, bring_back_dark_text,
     ["tests/test_plugin.py", "-k", "no_dark_text"]),
    ("the status rows lost their separator", SRC, drop_the_status_row_separator,
     ["tests/test_plugin.py", "-k", "status_rule_rows_have_the_same_separator"]),
    ("the status labels lost their colour", SRC, status_labels_lose_their_colour,
     ["tests/test_plugin.py", "-k", "same_palette_as_help"]),
    ("the rule index always padded", SRC, always_pad_the_rule_index,
     ["tests/test_plugin.py", "-k", "single_digit_rule_index"]),
    ("comments shipped in the package again", PACK, ship_the_code_with_its_comments,
     ["tests/test_plugin.py", "-k", "carries_no_comments"]),
    ("stripping that shifts line numbers", PACK, strip_without_keeping_line_numbers,
     ["tests/test_plugin.py", "-k", "keeps_line_numbers"]),
    ("counts zeroed on a command reload", SRC, zero_the_counts_on_a_command_reload,
     ["tests/test_plugin.py", "-k", "does_not_fake_an_idle_session or counts_of_rules_that_survive"]),
    ("only the first matching rule is counted", SRC, count_only_the_first_matching_rule,
     ["tests/test_plugin.py", "-k",
      "shadowed or every_matching_rule_is_counted or really_does_not_match or no_longer_short_circuits"]),
    ("the stale criterion ignores this session's count", SRC,
     stale_criterion_drops_the_session_count,
     ["tests/test_plugin.py", "-k",
      "matched_this_session or skipped_session or shadowed or single_hit_resets"]),
    ("a type-broken config is not quarantined", SRC, drop_the_type_level_quarantine,
     ["tests/test_plugin.py", "-k",
      "type_error or type_mismatch or sound_config_is_not_touched"]),
    ("a broken state.json is not quarantined", SRC, drop_the_state_quarantine,
     ["tests/test_plugin.py", "-k", "broken_state_file or type_broken_state or loading_the_state"]),
    ("state updates take no lock", SRC, drop_the_state_lock,
     ["tests/test_plugin.py", "-k",
      "state_updates_take_the_module_lock or concurrent_state_writers"]),
    ("the reload command has no exception guard", SRC, drop_the_reload_try,
     ["tests/test_plugin.py", "-k", "reload_command_replies_instead"]),
    ("language probe failures swallowed silently", SRC, silence_the_language_probe_failures,
     ["tests/test_plugin.py", "-k",
      "failing_language_probe or failing_mcdr_language or listing_failure or unfilled_placeholders"]),
    ("only the first matching rule reported", SRC, report_only_the_first_matching_rule,
     ["tests/test_plugin.py", "-k", "lists_every_matching_rule"]),
    ("the catch-all guard dropped", SRC, drop_the_catch_all_guard,
     ["tests/test_plugin.py", "-k", "hides_everything or catch_all_rule_is_kept or says_which_rule"]),
    ("rule de-duplication removed", SRC, stop_deduplicating_rules,
     ["tests/test_plugin.py", "-k", "written_twice or poison_the_statistics or order_of_first_appearance"]),
    ("the zip timestamp follows the file mtime", PACK,
     let_the_timestamp_follow_the_file_mtime,
     ["tests/test_plugin.py", "-k", "identical_bytes or pinned_timestamp"]),
    # 第三轮审查（SLF-A-02 / A-03 / A-04 / A-07）的四个回归锚点：都是本轮改掉的语义，
    # 没有守卫的话以后有人「顺手改回去」测试也不会红。
    ("the action flag overwritten again", SRC, overwrite_the_action_flag_again,
     ["tests/test_plugin.py", "-k", "only_clears_the_echo_bit"]),
    ("hits counted instead of hidden lines", SRC, count_hits_instead_of_lines,
     ["tests/test_plugin.py", "-k",
      "every_matching_rule_is_counted or console_input_is_not_counted"]),
    ("console input counted as a hidden server line", SRC, count_console_input_too,
     ["tests/test_plugin.py", "-k", "console_input_is_not_counted"]),
    ("a blank body returns early again", SRC, let_a_blank_body_return_early,
     ["tests/test_plugin.py", "-k", "blank_body_line_is_really_matched"]),
    ("the player-name prefix left in the body", SRC, drop_the_player_prefix_strip,
     ["tests/test_plugin.py", "-k", "agrees_with_the_real_filter"]),
]

#: pytest 摘要里两个必须分开看的数字。**这是本工具最重要的判据**：
#: ``failed`` 是"测试真的没通过"（变异被抓到），``error`` 是 fixture / 收集期出错
#: （环境坏了、锚点写歪了）。两者的退出码都是 1，只看退出码会把它们混成一个数，
#: 于是"测试有牙齿"和"环境坏掉"在这张表上不可区分（这就是 SLF-07 的一半）。
SUMMARY_RE = re.compile(
    r"(?:(?P<failed>\d+)\s+failed)?"
    r"(?:,?\s*(?P<error>\d+)\s+error)?"
    r"(?:,?\s*(?P<passed>\d+)\s+passed)?"
)


def _parse_summary(text: str) -> dict:
    """从 pytest 的 ``-q`` 输出里取出 ``failed`` / ``error`` / ``passed`` 计数。

    取不到的计数按 **0** 处理（"1 passed, 338 deselected" 里没有 failed/error，
    那就是 0 个），并从最后一行往前找第一条带计数的行。
    """
    counts = {"failed": 0, "error": 0, "passed": 0, "no_tests": False, "parsed": False}
    for line in reversed(text.splitlines()):
        stripped = line.strip()
        if not any(
            word in stripped
            for word in ("failed", "error", "passed", "deselected", "no tests ran")
        ):
            continue
        if "no tests ran" in stripped:
            counts["no_tests"] = True
            return counts
        matched = False
        for match in SUMMARY_RE.finditer(stripped):
            for key in ("failed", "error", "passed"):
                value = match.group(key)
                if value is not None:
                    counts[key] = int(value)
                    matched = True
        if matched:
            counts["parsed"] = True
            return counts
    return counts


def _purge_bytecode(root: Path) -> int:
    """删掉 ``root`` 下所有 ``__pycache__``，返回删掉的目录数。

    必须在**每次 pytest 运行前后**都做：变异改写的是源码，而 ``.pyc`` 是按
    (mtime, size) 失效的——同一秒内写完、大小又恰好相同的两次内容（变异与还原就是
    这样）会让 CPython 继续用旧字节码，于是"变异了但测试跑的是原版"，表现为
    不可复现的假存活。这也是 SLF-07 里那条"全量跑存活、单条复跑抓到"的直因。
    """
    removed = 0
    for cache in list(root.rglob("__pycache__")):
        if cache.is_dir():
            shutil.rmtree(cache, ignore_errors=True)
            removed += 1
    return removed


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    digest.update(path.read_bytes())
    return digest.hexdigest()


def pytest_ok(workdir, selector):
    """跑一次 pytest，返回 ``(退出码, 摘要行, 计数)``。

    隔离掉字节码缓存（见 ``_purge_bytecode``），并显式关掉字节码写入 —— 这样"变异
    到底有没有被跑起来"就只取决于磁盘上的源码。
    """
    _purge_bytecode(workdir)
    env = dict(os.environ)
    env["PYTHONPATH"] = ".testlibs"
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    proc = subprocess.run(
        [sys.executable, "-m", "pytest"] + selector + ["-q", "--no-header", "-p", "no:cacheprovider"],
        cwd=str(workdir), env=env, capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=600,
    )
    _purge_bytecode(workdir)
    text = proc.stdout + proc.stderr
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    summary = next((line for line in reversed(lines)
                    if "passed" in line or "failed" in line or "error" in line
                    or "no tests ran" in line), "(no summary)")
    return proc.returncode, summary, _parse_summary(text)


def verdict(returncode, counts, summary):
    """把一次 pytest 运行判成 ``caught`` / ``SURVIVED`` / ``HARNESS`` / ``ERRORS-ONLY``。

    **顺序要紧**：先看收集（退出码 4/5 / "no tests ran"），再看 ``failed``，
    最后才看退出码。反过来写会把"有 failed"也当成 HARNESS —— 那个 bug 让整表 37 条
    被误报成"锚点失效"，而它们其实条条都抓住了。

    * ``caught``      —— 有 ``failed``：测试真的没通过，变异被抓住。
    * ``SURVIVED``    —— 退出码 0、测试全绿：变异活下来了。
    * ``ERRORS-ONLY`` —— 退出码非 0 但一个 ``failed`` 都没有：fixture / 收集期出错。
      **不算抓住**；这正是把环境问题误判成"测试有牙齿"的那一类。
    * ``HARNESS``     —— 退出码 4/5 或没有测试被收集：选择器过期了。
    """
    if counts.get("no_tests") or returncode in (4, 5):
        return "HARNESS"
    if counts.get("failed"):
        return "caught"
    if returncode != 0:
        return "ERRORS-ONLY"
    return "SURVIVED"


def main():
    # 至少有一条变异只有端到端测试能抓住（``arrives_after``）。跳过 e2e 的话，
    # 那些用例会被 pytest skip → 退出码 0 → 变异被误判成 SURVIVED，
    # 于是这个工具开始报假警。宁可直接拒绝运行。
    if os.environ.get("MCDR_SKIP_E2E"):
        print("MCDR_SKIP_E2E is set: the end-to-end suite would be skipped, and at least one")
        print("mutation is only caught there. Unset it and run again.")
        return 2
    if not (REPO / ".testlibs").is_dir():
        print("run the tests/README.md setup first (.testlibs is missing)")
        return 1

    workdir = Path(tempfile.mkdtemp(prefix="mutation_check_"))
    print("workdir: {}".format(workdir))
    shutil.copytree(REPO, workdir / "repo",
                    ignore=shutil.ignore_patterns(".git", ".pytest_cache", "__pycache__"))
    repo = workdir / "repo"
    print("purged {} __pycache__ dir(s) from the copy".format(_purge_bytecode(repo)))
    # One pristine copy per file a mutation touches, so a mutation can be undone
    # without rebuilding the tree.
    baselines = {
        relative: (repo / relative).read_text(encoding="utf-8")
        for _, relative, _, _ in MUTATIONS
    }

    code, summary, counts = pytest_ok(repo, ["tests/test_plugin.py", "-k",
                                             "stale or idle or threshold or reload_does_not_fake or "
                                             "never_reached_startup or catastrophic or "
                                             "no_comment_fields or "
                                             "generated_config_contains_only or prun or "
                                             "rejected_by_the_safety_probe or switch or "
                                             "leniently or raw_bool or language or english or "
                                             "auto or catalogue or translation or packed_plugin"])
    print("baseline (unmutated): exit={} {}".format(code, summary))
    if code != 0 or counts.get("failed") or counts.get("error"):
        print("baseline is not green — fix the tests first")
        return 1
    print()

    killed = 0
    broken = []           # 锚点失效 / 选择器过期：是脚本自己过期了，不是测试没抓住
    noisy = []            # 只 error 不 failed：环境坏了，不能算抓住
    survived = []         # 测试全绿：变异活下来了
    for name, relative, mutate, selector in MUTATIONS:
        baseline = baselines[relative]
        mutated = mutate(baseline)
        if mutated == baseline:
            broken.append(name)
            print("  !! {}: mutation did not apply (anchor moved?)".format(name))
            continue
        if relative.endswith(".py"):
            try:
                compile(mutated, relative, "exec")
            except SyntaxError as error:
                broken.append(name)
                print("  !! {:<52} mutated code does not compile: line {}".format(
                    name, error.lineno))
                continue
        target = repo / relative
        target.write_text(mutated, encoding="utf-8")
        # 把"这次变异到底往盘上写了什么"钉成可复核的指纹：报告里出现假存活时，
        # 第一件事就是拿它和手工注入的结果比对。
        digest = _sha256(target)
        try:
            code, summary, counts = pytest_ok(repo, selector)
        finally:
            target.write_text(baseline, encoding="utf-8")

        outcome = verdict(code, counts, summary)
        detail = "exit={} {}".format(code, summary)
        if outcome == "caught":
            print("  caught      {:<52} sha256={} {}".format(name, digest[:12], detail))
            killed += 1
        elif outcome == "HARNESS":
            broken.append(name)
            print("  HARNESS     {:<52} {} — selector matched nothing".format(name, detail))
        elif outcome == "ERRORS-ONLY":
            noisy.append(name)
            print("  ERRORS-ONLY {:<52} {} — errors, NOT failures".format(name, detail))
        else:
            survived.append(name)
            print("  SURVIVED    {:<52} sha256={} {}".format(name, digest[:12], detail))

    print()
    print("caught {} / {}".format(killed, len(MUTATIONS)))
    print("  survived (tests are decorative):      {}".format(len(survived)))
    print("  errors only (environment, not tests): {}".format(len(noisy)))
    print("  harness (stale anchor or selector):   {}".format(len(broken)))
    if survived:
        print("survived mutations — the corresponding tests are decorative:")
        for name in survived:
            print("   - {}".format(name))
    if noisy:
        print("mutations whose run only produced errors (NOT counted as caught):")
        for name in noisy:
            print("   - {}".format(name))
    if broken:
        print("mutations that could not be applied / selected:".format())
        for name in broken:
            print("   - {}".format(name))
    if survived or noisy or broken:
        print()
        print("the temporary tree is kept for inspection: {}".format(repo))
        return 1
    shutil.rmtree(workdir, ignore_errors=True)
    print("every mutation was caught — the tests have teeth")
    return 0

if __name__ == "__main__":
    sys.exit(main())
