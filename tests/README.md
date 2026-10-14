# 测试 / Tests

本目录是 Server Log Filter 的测试套件。**377 个用例**，覆盖过滤行为、配置、
命令面、发布打包、**真实 MCDR 端到端**、**多语言**、**并发**，以及本插件最核心的
安全属性（被隐去的行仍保留 `process`，事件照常分发）。

> 打包、跨 MCDR 版本矩阵、变异检查等更广的开发话题见
> [../docs/DEVELOPMENT.md](../docs/DEVELOPMENT.md)。

MCDR 是**硬依赖**：插件配置类继承自 `mcdreforged.api.utils.Serializable`，
没有 MCDR 连 `import server_log_filter` 都会失败。所以下面的步骤是必需的。

## 安装测试依赖

不需要建虚拟环境（`python -m venv` 在某些受限环境里会因为 `ensurepip` 失败）。
直接装到一个本地目录：

```bash
python -m pip install --target .testlibs -r tests/requirements-test.txt
```

`.testlibs/` 已在 `.gitignore` 里，不会被提交。

## 运行

```bash
# Linux / macOS
PYTHONPATH=.testlibs python -m pytest tests -v

# Windows PowerShell
$env:PYTHONPATH=".testlibs"; python -m pytest tests -v
```

CI（`.github/workflows/ci.yml`）跑的就是同一批命令：`unit`（跳过端到端，Python 3.10 / 3.13）、
`mutation`、`e2e` 三个 job。跨版本矩阵不在 CI 里（需要三个各装了不同 MCDR 的环境），发版前本地跑。

⛔ **`python tools/mutation_check.py` 不接受 `MCDR_SKIP_E2E`**：有一条变异只有端到端测试能抓住，
跳过 e2e 会让它被 pytest skip → 退出码 0 → 被误报成「测试没抓住」。脚本会直接拒绝运行并说明原因。

预期输出结尾：

```
376 passed
```

> **开发中不用每次都跑全量。** 改哪一块就只跑那一块，确认当前版本能过即可，例如
> `pytest tests/test_plugin.py -k language -q` 或 `pytest tests/test_e2e.py -k auto -q`。
> **全量套件 + 变异检查 + 跨版本矩阵留到提交 PR 前统一跑一次**，有问题集中修。
> 报告中要如实区分「跑过」与「没跑」。

## 覆盖内容

**分组之间会重叠**（一条用例可能同时落在多组），所以各组数字**不要相加**；总数以 `pytest` 的实际输出为准（当前见下）。

| 分组 | 约 | 说明 |
|---|---|---|
| 过滤行为与安全属性 | 30 | 目标刷屏行命中（3）；15 类关键行逐一验证**不**被误伤；`content` 为 `""`/`None`/纯空白不崩溃（5）；`hidden()` 保留 `process` 且绝不等于 `discarded()` |
| 规则编译与容错 | 4 | 非法正则被跳过并告警，其余规则照常工作；空/空白规则静默丢弃 |
| 计数、重载与重置 | 14 | 多规则独立计数；**`reload` 接续同名规则的计数**（旧版会归零，那是 1.4.1 修掉的 bug）；被删掉的规则才丢计数；**一行命中的每条规则都计数**（1.4.2 改掉的旧语义：以前只计第一条，见下） |
| 配置对象 | 3 | 默认值与 README 文档一致；JSON 反序列化；往返稳定 |
| MCDR 契约 | 2 | `InfoActionFlag.hidden()` 的常量构成；`InfoFilter` 允许改写 `action_flag` |
| 命令面与元数据 | 8 | `on_load` 注册项；状态/测试/重载命令输出；`reload` 的 ADMIN 权限门禁；插件元数据与 `MIN_MCDR_VERSION` 同步 |
| 发布打包 | 7 | 见下 |
| **零命中提醒** | **22** | 阈值语义、命中归零、启动失败不计入、热重载不误判、可关闭、`reset`、历史持久化、**以及「不重复输出」**，见下；另含**删除规则后统计立刻清除**（重载 / 命令两条路径、不写多余文件、不误删被拦下的规则、配置被重置时不动） |
| **灾难性回溯防护** | **37** | 8 种真实写法全部放行；多种危险模式被拦下（**其中 `(a\|aa)+$` 在长探测串上会让单次 `re.search` 跑几十小时，所以这类用例跑在带超时的子进程里**）；增长判据与长度上限、预算上下限、开关与超时可配置且**确实接在 `on_load` 上**；**预算写成 0/负数时不会把规则全部拒掉** |
| **轻量化不变量** | **6** | 热路径的结构性断言（缓存 `hidden()`、无规则短路、不再短路但保留"第一条命中"语义），外加两条宽松的耗时护栏 |
| **类型写坏的配置** | **11** | 合法 JSON 但字段类型不符时走与语法错**同一条**备份 + 播报路径（旧版会被 MCDR 的 REGEN 静默覆盖）；三种既有检疫行为不受影响；未来版本多出来的键不算类型错 |
| **升级提示与迁移** | **15** | 见下；含**升级提示开关**本身的语义（关了不播、但配置照补） |
| **坏配置的保全与重建** | **13** | 见下 |
| **写坏提示的开关** | **11** | 开关从**写坏的原文**里读取（大小写 / 空格 / 位置随意、同名后缀不算、找不到按默认开启）；被静默的只有消息，备份照做；「连备份都失败」不受开关影响 |
| **语言（i18n）** | **35** | 见下；`auto` 双向跟随、显式值压过 MCDR、宽松识别、未知语言回落、坏目录不崩、**探测失败会留一条可读 warning（旧版是静默 `except: pass`）**、**打包后仍读得到文案**、英文真的到达每一处输出，外加一整套目录结构不变式 |
| **命令面、权限、界面与打包** | **49** | `!!lf` 别名（一个节点两条字面量）、帮助页、`help`/`list` 的接线，以及 4 条在**真 MCDR** 上真的敲一遍命令的端到端 |
| **1.4.1 的修复与守卫** | **25** | 命令重载接续计数（3）、`test` 列出全部命中（1）、匹配一切的规则被警告但保留（11，含 7 条参数化）、重复规则去重（3）、以及对应的接线 |
| **1.3.0 的修复** | **9** | 非 UTF-8 配置被备份而非致命（2）、`test` 剥掉控制台前缀（2）、状态里显示语言来源与被跳过的规则（2）、量化符溢出与深层嵌套不再致命（2）、坏模板的 `AttributeError`（1） |
| **端到端（真实 MCDR）** | **37** | 见下。这一项是**独立的一整份文件**（`tests/test_e2e.py`），不与上面各组的用例重叠 |
| **生命周期与并发** | **14** | `on_load` 提前 return（低版本 MCDR）之后走完整生命周期不抛异常、模块级全局被显式重置；`state.json` 损坏时备份 + 告警；`_state` 的读改写共用一把锁；多线程真实入口小规模压测 |
| **合计** | **376** | |

> 计数含 `@pytest.mark.parametrize` 展开后的用例数，与 `pytest --collect-only` 一致。

## 端到端测试（`tests/test_e2e.py`）

这一层做单测原理上做不到的事：**启动一个真的 MCDR**，加载 `pack.py` 产出的 `.mcdr`
（不是源码目录），用一个假服务端跑完整生命周期，然后检查 MCDR **真正的控制台输出**。

流程：`Done (...)!` 启动 → 玩家进出 → 两条目标刷屏行 → 若干无害行 → `Stopping server` 退出。
假服务端以退出码 0 结束，因此 MCDR 能走完正常的停止流程并触发插件的 `on_server_stop`。

| 用例 | 断言 |
|---|---|
| `test_target_lines_are_hidden_from_the_console` | 目标行回显次数为 **0** |
| `test_innocent_lines_still_reach_the_console` | 版本行 / `Preparing level` / 玩家进出 / `moved too quickly` / `lost connection` / `Stopping server` / `Saving players` 全部保留 |
| `test_hidden_line_keeps_process_flag_end_to_end` | **安全属性的真正守门测试**，见下 |
| `test_mcdr_completes_the_lifecycle` | `Server stopped` / `Server process stopped with code 0` 正常出现（仅确认整轮运行健康，**不**证明安全属性） |
| `test_plugin_reports_what_it_hid` | 停止时汇总为「隐去 **3** 行」（2 条目标行 + 1 条 canary） |
| `test_packaged_plugin_is_what_was_loaded` | 加载的确实是 `.mcdr`，插件目录里没有源码副本 |
| `test_plugin_generated_its_default_config` | 首次运行生成的字段集合与文档完全一致 |
| `test_plugin_keeps_its_state_file_out_of_the_user_config` | 统计写入独立的 `state.json`，用户手写的 `config.json` 不被插件改动 |
| `test_healthy_run_reports_no_stale_rule` | 首次运行没有历史，且两条规则都命中，因此**不该**有任何提醒 |
| `test_stale_rule_warning_arrives_after_the_server_finished_starting` | 提醒**出现在服务端 `Done` 之后**（同一次运行里比对行序），且 `SERVER_STARTUP` 确实派发过 |
| `test_stale_rule_warning_is_specific_and_actionable` | 提醒里含阈值、真实连续次数、`!!logfilter test` 与 `reset` 的处置提示 |
| `test_only_the_idle_rule_is_flagged` | 本轮命中过的规则不会被牵连进提醒 |
| `test_state_file_advances_by_one_session` | 关服后 `session_index` 与各规则的连续零命中次数被正确推进 |
| `test_plugin_generated_its_default_config` | 首次运行生成 `config/server_log_filter/config.json`，字段与默认值正确 |
| `test_a_deleted_rule_is_announced_as_cleaned_up` | **同一个实例目录跑两次 MCDR**，中间删掉一条规则；第二次启动时控制台出现「已清除」提示（这句只在载入阶段打印，因此能证明清理发生在重载那一刻） |
| `test_a_deleted_rule_leaves_the_state_file` | 第二次运行后 `state.json` 里已无该规则，存活规则历史完好，`session_index == 2` |
| `test_auto_follows_mcdr_language_end_to_end` | 实例**不写** `language` 键、MCDR 设为 `en_us` → 插件输出全英文（同时顺带走一遍 1.2.2 的升级提示，因为它也在英文里） |
| `test_auto_follows_mcdr_into_chinese_too` | 实例**不写** `language` 键、MCDR 设为 `zh_cn` → 插件输出**中文**。这条专治一个易混点：`auto` 读的是 MCDR **当前**的 `language`，不是某个内置默认值（两个方向都测才能区分这两种实现） |
| `test_an_explicit_language_beats_the_mcdr_setting_end_to_end` | MCDR 设为 `zh_cn`、插件写 `en_us` → 插件那几行仍是英文（MCDR 自己的界面文案按它自己的设置走，所以断言只盯插件输出的那几条） |
| `test_the_packaged_plugin_carries_its_language_files` | 解开发布包，`lang/*.json` 在里面，且键集与源码目录一致 |

**这不是空断言**：把插件从 `plugins/` 拿掉后重跑，目标行回显 2 次，第 1 条用例会失败。

### 为什么必须有 canary：一段值得记下的教训

最初的端到端组里有一条
`test_mcdr_startup_and_stop_detection_still_work`，断言 `Done (...)` / `Stopping server` /
`Server stopped` 会出现，docstring 声称「若插件退化成 `discarded()` 就会失败」。

**这个声明是错的。** 实测：把插件里的 `InfoActionFlag.hidden()` 换成
`InfoActionFlag.discarded()`（即破坏本插件最核心的安全属性），端到端 6 条用例**全部通过**，
只有 2 条单元用例抓到。

原因有两层：

1. **默认规则只匹配刷屏行**，而刷屏行本来就不参与 MCDR 的生命周期判定 —— 所以
   `hidden()` 和 `discarded()` 对这些行的可见性**毫无区别**。
2. **停止检测根本不是行驱动的**。`test_server_stopping` 只用来断开 RCON
   （`server_reactor.py`），停止事件由**服务端进程退出**触发。因此任何「停止仍被检测到」
   的断言都**永远**无法区分这两种实现。

修法（已实装）——让 canary 规则去命中一条 MCDR **真正依赖**的行。经查，启动检测确实是行驱动的：

```python
# mcdreforged/info_reactor/impl/server_reactor.py
if handler.test_server_startup_done(info):
    self.mcdr_server.add_flag(MCDReforgedFlag.SERVER_STARTUP)
    self.mcdr_server.plugin_manager.dispatch_event(MCDRPluginEvents.SERVER_STARTUP, ())
```

所以端到端运行会额外注入一条 canary 规则（`For help, type`，命中启动行），并挂一个探针插件
记录 `on_server_startup` 是否被调用，然后**两头都断言**：

| 实现 | canary 行离开控制台 | `SERVER_STARTUP` 仍派发 | 判定 |
|---|---|---|---|
| `hidden()`（正确） | ✅ | ✅ | 通过 |
| `discarded()`（变异） | ✅ | ❌ | **失败** |

这样「什么都没过滤」和「把整条丢掉了」两种退化都逃不掉。

> ⚠️ **canary 模式必须正则安全。** 第一版直接用完整启动行
> `Done (0.648s)! For help, type "help"` 当模式，其中的 `(` `)` 被正则当成分组，
> 导致**永远匹配不到**、canary 静默失效、测试又退化成空断言。用 `For help, type`
> 这种无元字符的子串才稳。这同时也印证了 README 里「匹配用 `re.search`，别自己加 `.*`」那条提醒。

两个让测试跑得快且稳定的细节：

- `handler_detection: false` —— MCDR 的处理器自动探测会先采样**一整分钟**
  （`HANDLER_DETECTION_MINIMUM_SAMPLING_TIME = 60`）才启动服务端，必须关掉并显式指定 handler。
- `start_command` 里的解释器路径**要加引号**：路径含空格（如 `Paul LAM`）时，不加引号会被
  `cmd.exe` 截断成 `'C:\Users\Paul' is not recognized...`。

端到端组共用一个 MCDR 实例（约 5～6 秒，仅启动一次 MCDR）；但**与语言有关的四条
必须单独起实例**（要换 MCDR 的 `language` 或插件的 `language`，配置不同），
所以整组一共 11 次真实启动、约 33 秒。`MCDR_SKIP_E2E=1` 可跳过，缺少 `.testlibs` 时自动 skip。

> **新增的 GBK 用例断言的是英文**，这不是笔误：配置读不出来时，插件也就读不出你写的
> `language`，只能退回 MCDR 自己的语言——这一点本身也在被测之列。
>
> **e2e 里插件的语言和 MCDR 的语言是分开设定的。** 实例的 MCDR 默认用 `en_us`，
> 插件则单独 pin 成 `zh_cn`：如果让 MCDR 也说中文，它自己的界面文案会跟着变，
> 而断言要扫的控制台里那些**英文标志行**（如 `Server process stopped with code 0`）就消失了
> —— `grep` 过 MCDR 的语言文件，这个 key 只在 `en_us.yml` 里存在。
> 要测「跟随」时把插件的 `plugin_language` 传 `None`，生成的配置里就不写 `language` 键。

## 发布打包的回归防护

`pack.py` 是仓库唯一的打包器，它的产物由这里 7 个用例把关：

- `test_packaged_artifact_is_loadable` —— 跑一遍 `pack.py`，把产物交给 **MCDR 自己的**
  `PackedPlugin._check_dir_legality` 校验
- `test_packager_excludes_repo_infrastructure` —— `conftest.py` / `pack.py` / `tests/` /
  `.testlibs/` / `__pycache__` 都不在包里
- `test_packager_ships_exactly_the_allowlist` —— **把产物内容钉死为 7 个文件**
  （`mcdreforged.plugin.json` / `server_log_filter/__init__.py` / `i18n.py` /
  `lang/en_us.json` / `lang/zh_cn.json` / `CHANGELOG.md` / `LICENSE`），
  并断言三个 README（`README.md`、`README_en.md`、`lang/README.md`）**都不在**包里。
  这样任何对白名单的改动都必须显式改测试，
  避免再出现「体积悄悄变了但没人知道」
- `test_packager_keeps_artifact_small` —— 文件数 < 20 且 < 200 KiB
- `test_packager_ships_submodules_recursively` —— 在临时副本里植入
  `server_log_filter/sub/helper.py`，断言它**确实被打了进去**
- `test_packager_root_entries_would_be_illegal_if_denylisted` —— 用真实的黑名单产物断言
  MCDR **确实会拒绝**它，防止有人把 `pack.py` "简化"回黑名单写法
- `test_changelog_keeps_only_the_latest_release` —— `CHANGELOG.md` **恰好只有一节**，
  且版本号等于 `mcdreforged.plugin.json` 里的当前版本（不含旧版本的链接定义）。
  它随包分发，历史条目会永久占着用户的体积，所以旧条目在发新版时必须删掉

这一节存在的直接原因：本测试套件自己的 `conftest.py` 位于仓库根目录，而 MCDR 禁止 `.mcdr`
包含根级模块。如果打包器用 `rglob("*")` 加简短的排除列表（README 早期写法），产物会以
`IllegalPluginStructure` 加载失败；装了 `.testlibs/` 后还会膨胀到 **1362 个文件 / 7.11 MB**。
这类问题不会在单元测试里暴露，只有真的打包并交给 MCDR 校验才会发现。

> 另一条同样隐蔽的坑：白名单如果写成 `rel.parent == Path(PACKAGE_NAME)`，就只收**直接子级**，
> `server_log_filter/sub/helper.py` 这类嵌套子模块会被**静默丢弃**——打出一个缺文件的包，
> 而且不报任何错。现在的实现用 `rel.parts[0]` + 「任意层级的 `.py`」+「`lang/` 下正好三层的
> `.json`」，并有上面的用例守着。

> 还有一条只有打包后才看得见的坑：**MCDR 是用 `zipimport` 从 `.mcdr` 里 import 插件的**，
> 因此包内 `__file__` 指向压缩包内部，用 `open()` 读自带数据文件会失败。
> 本插件的消息全部存在 `lang/*.json` 里，所以这个失败会让**所有输出退化成裸键名**
> （控制台出现 `summary.rules_enabled` 这种字样），而开发机上完全正常。
> `test_the_packed_plugin_can_still_read_its_catalogues` 在子进程里 import
> **真实构建出来的 `.mcdr`** 并断言读到的是真句子，专门守这条；
> `tools/mutation_check.py` 里也有对应变异（把 `pkgutil.get_data` 换成 `None`）。

## 零命中提醒（第 8 组）

统计只在**开服 / 关服两个时点**更新，逐行过滤路径上没有任何额外开销
（有专门用例从结构上证明这一点：过滤 50 行期间不会触发任何状态写盘）。

两条最容易被忽略、也最要紧的保护：

1. **只统计「完成过启动」的周期。** 否则服务端连续启动失败（mod 报错之类）会把所有规则
   刷成零命中，然后给出完全错误的提醒。用例：`test_a_session_that_never_reached_startup_is_not_counted`。
2. **插件热重载要接续计数。** MCDR 的 `!!MCDR reload plugin` 会重新执行 `on_load`，
   新过滤器从 0 开始计数；若不管，一次正常命中的开服周期会被切成两段、看起来像零命中。
   用例：`test_reload_does_not_fake_an_idle_session`。

## 灾难性回溯防护（第 9 组）

带嵌套量词的正则匹配失败时指数级回溯，实测**单行**就要数百毫秒到数秒：

| 模式 | 输入长度 | 单行耗时 |
|---|---|---|
| `(a+)+$` | 23 | 182 ms |
| `(a+)+$` | 27 | 3.0 s |
| `^(a|a)*$` | 25 | 753 ms |

对照正常过滤：**0.2 µs/行**，不到 MCDR 自身解析同一行开销的十分之一。
也就是说这类规则比正常情况慢 **数百万倍**，足以冻结 MCDR 主线程——
这才是本插件唯一可能真正影响性能的情形。

防护做法是在载入时用 `a*22` / `a*22+"!"` 等短探测串试跑每条规则，超预算即跳过。
参数是这样标定的（`benchmarks/bench_filter.py` 可复现）：

- 4 类危险模式在探测串上耗时 190~380 ms，全部超过 25 ms 预算 → 被拦下
- 8 种真实写法在同一批探测串上最坏仅几微秒 → 余量约 **9000 倍**，不会误伤

（具体数值随机器而变，这里看的是数量级差距。）

注意最后三条用例守的是**接线**而不是函数本身：变异测试发现，只在 `_build_rules` 层面测
「危险模式被拦下」，把 `on_load` 里的 `validate=_config.validate_patterns` 改成
`validate=False` 时测试**依然全绿**。补上 `test_on_load_actually_applies_the_probe` /
`test_on_load_respects_validate_patterns_false` / `test_on_load_honours_the_timeout_setting`
之后才守得住。

## 升级提示与迁移（第 11 组）

两条不变量是这一组的核心：

1. **每个配置项都必须写明加入版本。**
   `test_every_config_option_has_a_description` 断言 `CONFIG_DOC` 的键集合与 `Config` 的真实字段
   完全一致，且每条的版本号形如 `X.Y.Z`。于是「新增了配置项却忘了写说明」会直接让测试失败
   ——那会让升级提示里出现一条没有说明的条目。
2. **配置里不得出现任何非选项键。**
   `test_the_generated_config_has_no_comment_fields` 断言生成的配置**恰好**等于选项集合。

> 第 2 条来自一次反复：早期版本为了让配置「自带注释」，往文件里注入过一批 `#<选项名>` 字段。
> 功能上可行，但让配置文件看起来复杂，最终被移除。
> 这条用例就是为了防止它（或任何类似的键）再溜回来。

升级提示本身由 `test_upgrade_announcement_lists_options_with_versions` /
`test_upgrade_announcement_explains_each_new_option` 等用例守住——
**只报选项名不够，得说清它是干什么的**。

迁移路径本身由**端到端组**覆盖：`_build_instance` 种下的配置只有 1.0.x 的三个选项，
所以每次跑 e2e 都在真实 MCDR 上走一遍「旧配置 → 自动补齐 → 日志提示」的完整流程。

> 与第 9 组同一个教训：这里也有**接线**用例（`test_on_load_wires_the_migrator_into_config_loading`、
> `test_reload_command_also_migrates`）。函数本身正确，不等于它真的被挂到了加载路径上。

> 配对实验：`tools/mutation_check.py` 里有一条变异往配置里塞一个非选项键，
> 用来证明第 2 条不变量真的在起作用。



## 坏配置的保全与重建（第 12 组）

JSON 很严格，手工加规则时漏一个逗号就会解析失败，而 MCDR 的 `failure_policy='regen'`
会**直接用默认值覆盖原文件**。所以插件在把文件交给 MCDR 之前先自己检查一遍。

这一组的重点是「保全」而不是「报错」：
`test_broken_config_is_backed_up_before_being_regenerated` 断言备份文件
**逐字节等于**用户原来写的内容——报错信息再好，文件没了也是白搭。

被当作「损坏」的几种情况都各有用例：语法错误、空文件、顶层不是 JSON 对象
（`["a","b"]` / `"str"` / `42` 三种参数化）。另有一条守卫
`test_quarantine_survives_an_unwritable_location`：连备份都失败时插件必须照常加载，
而不是在启动阶段抛异常把自己也搭进去。

端到端侧用一条**真实 MCDR** 用例收尾：写入语法错误的 `config.json`，
断言备份存在且逐字节一致、新配置可正常解析、控制台给出原因与行列号、
并且**插件仍然正常工作**（回退到默认规则并继续过滤）。

## 语言（第 13 组）

所有面向用户的文案都住在 `server_log_filter/lang/<语言码>.json` 里，**一个语言一个文件**，
扁平 `键 → 模板`。`language` 配置项的默认值是 `auto`，即跟随 MCDR 自己的语言设置；
也可以写死成 `zh_cn` / `en_us`。

解析链是「归一化 → 精确命中 → 兄弟语言 → 同基语言 → `en_us`」：

| 情形 | 结果 |
|---|---|
| `auto` + MCDR 是 `zh_cn` | 中文（**不产生任何提示**——用户是把选择委托给了 MCDR） |
| `auto` + MCDR 语言没出货（如 `fr_fr`） | 回落，**静默** |
| 显式写 `zh_tw`（未出货） | 回落到 `zh_cn`，并**说一次** |
| 显式写 `klingon` | 回落到 `en_us`，并**说一次** |
| `EN_us` / `zh-CN` / `  en  ` / `zh` | 都能认出来 |

几个设计上的取舍值得记下来：

- **`auto` 静默、显式值才警告。** 前者是「我用 MCDR 的设置」，没出货不是用户的错；
  后者是明确的错误输入，静默会让人以为设置生效了。
- **翻译失败绝不抛异常。** 模板 `format()` 出问题（比如译文漏了占位符）时返回**原文**，
  缺 key 时逐级回落，最后退化成键名——**日志插件的消息面不能成为新的故障点**。
- **配置说明也走查表。** `CONFIG_DOC` 存的是**语言键**而不是中文句子，
  于是「每个选项都有说明」这条不变式对**每一种语言**同时成立，说明文字也不会随语言漂移。
- **坏配置那条路只能从文件原文读语言**（`_raw_str_option`），因为在那一刻配置尚未解析成功；
  且内部用 `warn=False` 解析一次，避免同一条警告说两遍。

目录结构的不变式一并守着，所以「加一门语言」不需要动代码、也不会漏翻：

| 不变式 | 用例 |
|---|---|
| 每个出货的语言文件都能解析 | `test_every_shipped_language_file_loads` |
| 键集与回落语言**完全一致** | `test_every_language_file_has_exactly_the_same_keys` |
| 模板里的占位符集合一致 | `test_every_translation_keeps_the_placeholders` |
| 代码里用到的键**全部存在** | `test_every_key_the_code_asks_for_exists_in_every_catalogue` |
| **没有键是没人用的**（反向） | `test_no_catalogue_key_is_left_unused` |
| `CONFIG_DOC` 的键 == `Config` 的字段 | `test_the_config_descriptions_are_exactly_the_options` |
| `en_us` 里不含 CJK 字符 | `test_the_english_catalogue_is_actually_english` |

> 前两条不变式靠 **AST** 实现：遍历源码里所有 `_t(...)` / `say(...)` / `translate(...)`
> 调用的**第一个位置参数**、取字面量，得到「代码实际用到的键集合」，
> 再与每份语言文件的键集双向比对。于是「新加了一处输出却忘了加文案」会直接变红，
> 而不需要人工维护一份键名单——那种名单迟早会和代码脱节。

> 「翻译有没有真的到达用户面前」另有一组用例逐处验证：零命中提醒、命令与运行摘要、
> 删规则播报与规则报错、升级提示，都在英文下检查过。
> 结构对了不等于接上了，这一课在第 9 组（灾难性回溯防护）已经吃过一次。

判断一组测试有没有效，唯一可靠的办法是**故意把实现改坏，看测试会不会变红**。
本套件的关键断言都经过这一步验证，可以直接复跑：

```bash
python tools/mutation_check.py
```

脚本会依次注入 51 个缺陷，要求相关用例变红；全绿即视为测试失效。

> **判定看 pytest 的退出码：只有 `exit == 1` 才算「被抓住」。** `4`（命令行用法错误）
> 和 `5`（没收集到用例）都说明**脚本自己写错了**，不是测试变红——早期把 `4` 也当命中，
> 因此拿到过一个假的满分。变异的目标不限于 `__init__.py`：条目形如
> `(名字, 目标文件, 变换函数, baseline 选择器)`，可以打在 `i18n.py` 或语言 JSON 上。

已确认能被抓住的变异：

| 变异 | 抓住它的用例 |
|---|---|
| 关闭零命中提醒 | 第 8 组多例 |
| 阈值判断 `>=` 改成 `>` | `test_idle_rule_is_reported_once_the_streak_reaches_the_threshold` |
| 去掉「必须完成启动」的保护 | `test_a_session_that_never_reached_startup_is_not_counted` |
| 热重载计数接续失效 | `test_reload_does_not_fake_an_idle_session` |
| 热重载丢失「本周期已完成启动」标记 | `test_reload_carries_the_running_session_over_to_the_new_module` |
| 配置里被塞进一个非选项键 | `test_the_generated_config_has_no_comment_fields` 等 |
| 静默升级提示 | `test_upgrade_announcement_lists_options_with_versions` 等 |
| 无视 `announce_config_upgrade`（关了照播） | `test_upgrade_announcement_can_be_switched_off`、`test_the_upgrade_switch_survives_a_reload` |
| 无视 `announce_broken_config`（关了照播） | `test_broken_config_notice_can_be_switched_off`、`test_the_switch_is_read_leniently_from_the_raw_file` |
| 某个配置项丢掉说明 | `test_every_config_option_has_a_description` |
| 不再检查配置文件（坏文件被直接覆盖） | `test_on_load_checks_the_config_file` 等 |
| 检测到问题但不备份 | `test_broken_config_is_backed_up_before_being_regenerated` |
| 恢复成「每条规则都重复零命中次数」 | `test_a_rule_at_exactly_the_threshold_gets_no_annotation` 等 |
| 不再提示配置已被重置 | `test_backup_announcement_says_what_where_and_why` |
| 关闭灾难性回溯探测 | `test_on_load_actually_applies_the_probe` 等 |
| 提醒时机提前到 `Done` 之前 | `test_stale_rule_warning_arrives_after_the_server_finished_starting` |
| 载入时不再清理已删除规则的统计 | `test_deleting_a_rule_prunes_its_state_immediately_on_plugin_reload` 等 |
| `!!logfilter reload` 不再清理已删除规则的统计 | `test_deleting_a_rule_prunes_its_state_on_the_reload_command` |
| 孤立项按「编译成功的规则」而非配置原文判断 | `test_a_rule_rejected_by_the_safety_probe_keeps_its_history` |
| 忽略「配置刚被自动重置」的保护 | `test_a_reset_config_does_not_wipe_the_rule_history` |
| **无视 `language` 配置项** | `test_an_explicit_language_overrides_mcdr`、`test_the_language_switches_on_reload_without_a_restart` |
| **无视 MCDR 的语言设置**（`auto` 失效） | `test_auto_follows_mcdr`、`test_auto_follows_mcdr_in_the_other_direction` |
| **读不到语言文件**（`pkgutil.get_data` 恒返回 `None`） | `test_the_packed_plugin_can_still_read_its_catalogues` 等 |
| 静默「语言不存在」的警告 | `test_an_unknown_language_falls_back_and_says_so` |
| 静默「语言文件读不出来」的警告 | `test_a_broken_catalogue_falls_back_and_is_reported` |
| 坏配置时不从**原文**读语言 | `test_the_broken_config_notice_uses_the_language_from_the_raw_file` |
| **中文语言文件少一个键** | `test_every_language_file_has_exactly_the_same_keys` 等 |
| **某个模板少一个占位符** | `test_every_translation_keeps_the_placeholders` |
| **非 UTF-8 配置按致命错误处理** | `test_a_config_that_is_not_utf8_is_quarantined`、`test_a_non_utf8_config_does_not_stop_the_plugin` |
| **去掉探测预算的下限** | `test_a_nonsense_probe_budget_does_not_reject_every_rule` |
| **`test` 不再剥控制台前缀** | `test_the_test_command_strips_the_console_prefix` |
| **裸命令又接回状态页** | `test_the_bare_command_is_wired_to_the_help_screen` |
| **`!!lf` 别名被去掉** | `test_the_root_literal_answers_to_both_spellings`、`test_both_spellings_reach_mcdrs_help_list` |
| **权限门敞开**（任何玩家都能看到规则与日志内容） | `test_no_part_of_the_command_surface_is_open_to_players`（7 条参数化） |
| **帮助行不再可点** | `test_the_help_lines_are_clickable` |
| **权限拒绝的理由被去掉**（玩家又会以为是「我明明是 OP」） | `test_the_denial_explains_that_mcdr_permission_is_not_vanilla_op` |
| **顶栏不显示版本号** | `test_the_title_bar_names_the_plugin_and_the_version`、`test_the_title_uses_a_different_colour_for_the_version` |
| **帮助行不再对齐** | `test_help_rows_are_aligned_in_one_column` |
| **重新用上深灰字** | `test_the_help_page_has_no_dark_text`、`test_the_status_screen_has_no_dark_text_either` |
| **状态页规则行丢了分隔符** | `test_status_rule_rows_have_the_same_separator_as_help_rows` |
| **状态页标签颜色与帮助页不一致** | `test_the_status_screen_uses_the_same_palette_as_help` |
| **规则编号永远补到两位**（9 条以内出现 `[ 1]`） | `test_a_single_digit_rule_index_has_no_padding` |
| **打包时又把注释发出去** | `test_the_packaged_code_carries_no_comments_or_docstrings` |
| **剥注释时删行导致行号错位** | `test_stripping_keeps_line_numbers_so_tracebacks_still_match` |
| **命令重载把计数清零** | `test_the_reload_command_does_not_fake_an_idle_session`、`test_a_reload_keeps_the_counts_of_rules_that_survive` |
| **`test` 又只报第一条命中规则** | `test_the_test_command_lists_every_matching_rule` |
| **去掉「匹配一切」守卫** | `test_a_pattern_that_hides_everything_is_flagged`（7 条参数化）、`test_a_catch_all_rule_is_kept_but_warned` |
| **去掉重复规则的去重** | `test_the_same_rule_written_twice_becomes_one`、`test_a_duplicate_is_reported_and_does_not_poison_the_statistics` |
| **zip 时间戳跟着文件 mtime**（包不再可复现） | `test_packing_the_same_source_twice_gives_identical_bytes`、`test_every_member_carries_the_pinned_timestamp` |
| **编译异常只接 `re.error`**（量化符溢出/深层嵌套又变致命） | `test_a_pattern_that_overflows_is_skipped_not_fatal`、`test_a_deeply_nested_pattern_is_skipped_not_fatal` |
| **格式化异常只接三种**（`{a.b}` 又会让消息路径抛异常） | `test_a_template_with_a_bad_attribute_is_returned_raw` |

> 教训留在这里：本套件的端到端组**曾经**声称能挡住 `hidden()` → `discarded()` 的回归，
> 实测 6 条用例全部漏过，只有 2 条单元用例抓到。现在那条属性由
> `test_hidden_line_keeps_process_flag_end_to_end` 用 canary + 探针插件真正守住。



## 与 README「已验证内容」的关系

README 的「已验证内容」一节描述了这些行为，但此前仓库中并没有对应代码。
本套件把那些描述变成了**可重复执行**的断言，其中最关键的一条是：

```python
info.action_flag = InfoActionFlag.hidden()   # 只摘控制台回显
assert InfoActionFlag.process in info.action_flag        # 事件分发保留
assert InfoActionFlag.echo_to_console not in info.action_flag
```

这条断言在真实 MCDR 2.16.0 上运行，因此如果未来 MCDR 改变了 `hidden()` 的语义，
测试会立刻失败，而不是让插件在用户的服务器上静默出问题。

**注意分工**：上面这条是**单元**层的断言（直接检查 action_flag 的位），
`test_hidden_line_keeps_process_flag_end_to_end` 则是它的**端到端**对应物
（在真机上观察事件是否仍被派发）。两层都要有——单元层精确指出哪一位丢了，
端到端层证明确实穿透了 MCDR 的完整链路。最早只有单元层，端到端层是补上来的。

> 判断测试有没有效的方法：**跑变异测试**——故意把被测代码改坏，看测试会不会变红。
> 光看「全绿」没有说服力，本套件最初就是 64 项全绿却漏掉了最关键的回归。

## MCDR 版本覆盖

`pytest` 跑在钉住的 MCDR 2.16.0 上（`tests/requirements-test.txt`）。除单元 / 端到端两层外，
新增功能还在**最低支持版本**上单独验证过，否则「最低 2.15.0」只是未经验证的声明。
这个矩阵可以直接复跑：

```bash
# 传入若干「装了 MCDR 的 python 解释器」，插件包会用 pack.py 现场构建
python tools/mcdr_matrix.py /path/to/mcdr-2.15.0/python /path/to/mcdr-2.15.7/python /path/to/mcdr-2.16.0/python

# 或者只测当前解释器
python tools/mcdr_matrix.py --current
```

实测结果（2026-10-04）：

| 检查 | 2.15.0 | 2.15.7 | 2.16.0 |
|---|---|---|---|
| 插件加载 + 过滤 | ✅ | ✅ | ✅ |
| 零命中提醒出现，且在 `Done` 之后 | ✅ | ✅ | ✅ |
| 提醒内容（规则名 / 连续次数 / 从未命中过） | ✅ | ✅ | ✅ |
| `state.json` 写入、`session_index` 与 streak 正确推进 | ✅ | ✅ | ✅ |
| 无关日志行未被误伤 | ✅ | ✅ | ✅ |
| **`auto` 跟随 MCDR 语言（控制台全中文）** | ✅ | ✅ | ✅ |
| 运行时异常 | 0 | 0 | 0 |

**14 项检查在三个版本上逐项一致。** 2.13.0 / 2.14.1 仍按设计被 MCDR 依赖检查拦下
（提示「不满足版本约束 >=2.15.0」）。

> 语言那一行的判定叫 `spoke_chinese`：矩阵生成的配置首位写 `"language": "auto"`，
> 而矩阵实例的 MCDR 固定为 `language: zh_cn`，于是「输出里没有出现英文句子」一次
> 同时证明了两件事——插件确实读到了 MCDR 的语言设置，且 `auto` 就是**默认行为**。

另经逐版本 API 检查确认：新增功能用到的 `save_config_simple`、带 `file_name` 的
`load_config_simple`、嵌套 `Serializable`、`on_server_startup` 自 **2.13.0** 起均可用，
因此**最低版本要求没有变化**。

命令树的注册也做过跨版本核对（2.15.0 / 2.15.7 / 2.16.0 均能构造出
`!!logfilter` 及 `list` / `reload` / `reset` / `test` 四个子命令，零报错）。
