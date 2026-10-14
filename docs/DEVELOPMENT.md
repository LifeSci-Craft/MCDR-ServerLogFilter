# 开发与打包

这一份面向**要改代码、打包、或核对兼容性**的人。
只想把插件用起来的读者请回 [README.md](../README.md)。

- [仓库结构](#仓库结构)
- [自行打包](#自行打包)
- [测试](#测试)
- [已验证内容](#已验证内容)
- [实现注记（为什么这么写）](#实现注记为什么这么写)
- [环境要求的由来](#环境要求的由来)
- [版本兼容性实测](#版本兼容性实测)
- [发布流程](#发布流程)

---

## 仓库结构

本插件是 MCDR 标准的「根元数据 + 同名代码子包」：

```
MCDR-ServerLogFilter/
├── mcdreforged.plugin.json    插件元数据
├── server_log_filter/         代码子包（与插件 id 同名）
│   ├── __init__.py            插件主体
│   ├── i18n.py                语言解析 / 目录读取 / 安全翻译
│   └── lang/                  语言文件（一语言一文件，见 lang/README.md）
│       ├── en_us.json         回落语言，必须存在
│       ├── zh_cn.json
│       └── README.md          面向译者的指引（不打包）
├── CHANGELOG.md               变更记录，只留最新一版（随发布包分发）
├── LICENSE
├── README.md / README_en.md   主文档，面向使用者（不打包）
├── docs/DEVELOPMENT.md        本文件（不打包）
├── pack.py                    打包器
├── tests/                     测试套件（含 tests/README.md 细则）
├── tools/                     变异检查、跨版本矩阵
└── benchmarks/                性能基准
```

只有 `pack.py` 的白名单里列出的文件会进发布包，其余一律不外发。
白名单的判定是**结构化**的，不是一串文件名：包内**任意层级**的 `*.py`
（保证以后加子模块不会被漏掉，有 `test_packager_ships_submodules_recursively` 钉住），
加上 `lang/` 下**正好三层**的 `*.json`（`PACKAGE_DATA_DIRS` 里声明的数据目录与扩展名）。
想加一个数据目录，是改这张表、不是改文件清单。

### 为什么把语言文件打进包，而不是运行时下载

问过、量过、**决定不下载**。记录在这里，免得以后有人再"顺手优化"一遍。

包内逐条量出来的压缩体积（`ZIP_DEFLATED`，同上表的构建方式）：

| 变体 | 体积变化 |
|---|---|
| **现状**：两份语言都打包（打包器产出 **25,646 B**） | — |
| 去掉 `zh_cn.json` | −3,260 B / −12.7% |
| 两份语言都不打包 | −6,154 B / −24.0% |

三个结论：

1. **`en_us.json` 绝不能去掉。** 它是回落语言：一旦去掉，任何一次下载失败都会让
   **每一条消息退化成裸键名**——正是上面那个「静默失败」的故障模式。所以现实中能省的
   只有 `zh_cn.json`，也就是 **13%**。
2. **11% 换不来一个网络故障点。** 本插件的卖点是「零意外」：一个只在 MCDR 侧过滤日志、
   其余完全离线的小插件，如果为了 3.2 KB 变成「启动时可能要联网」，那笔账不划算——
   断网或被墙的中文用户会直接看到英文提示，而这恰恰是最需要中文的那批人。
3. **首屏还会变复杂。** 同步下载会阻塞 `on_load`（MCDR 主线程，也就是拖慢开服）；
   改成后台线程下载则会出现「前几条消息英文、之后突然变中文」的错位。
   两种都不如「文件就在包里」干净。

于是语言文件的增删仍然走 **PR + 发版**：一位译者提交 `ja_jp.json` → 合并进 `main`
→ 下个版本随包分发。想要「合并即可用」的代价，是要接受上面那三个问题，目前判断不值得。

### 关于语言文件为什么必须走 loader

MCDR 用 `zipimport` 直接从 `.mcdr` 里 import 插件，所以打包后 `__file__` 形如
`<临时目录>/xxx.mcdr/server_log_filter/__init__.py`——**它指向压缩包内部，不是一个真实路径**。
此时 `open(os.path.join(os.path.dirname(__file__), "lang", "zh_cn.json"))` 会抛
`NotADirectoryError`，若被 `try/except` 吞掉，**整份消息会静默退化成裸键名**
（控制台出现 `summary.rules_enabled` 这种字样），开发机上完全看不出来。

因此 `i18n.py` 的主路径是 loader：

```python
pkgutil.get_data(__package__, "lang/zh_cn.json")     # 首选，zip 与目录通吃
importlib.resources.files(__package__).joinpath("lang").iterdir()   # 列目录（发现新语言）
open(os.path.join(os.path.dirname(__file__), ...))   # 仅作为解包运行的兜底
```

这条约束被两处钉死：
`tests/test_plugin.py::test_the_packed_plugin_can_still_read_its_catalogues`
在子进程里 import **真实构建出来的 `.mcdr`**，断言读到的是真句子而不是键名；
`tools/mutation_check.py` 的 `skip_the_catalogues` 变异把 `pkgutil.get_data` 换成
`data = None`，确认测试会因此变红。

## 持续集成

`.github/workflows/ci.yml` 跑三个 job，对应上面三条命令：

| job | 内容 | 为什么要分开 |
|---|---|---|
| `unit` | `MCDR_SKIP_E2E=1 pytest tests`，Python 3.10 / 3.13 | 快（约 40 s），两个 Python 版本都验一验语法与行为 |
| `mutation` | `python tools/mutation_check.py` | 约 4 分钟，且**会改写源码再还原** —— 不能和别的 job 共用工作区 |
| `e2e` | `pytest tests/test_e2e.py`（真实 MCDR） | 最慢、最依赖环境；失败时上传日志便于排查 |

⛔ **`mutation` job 不能设 `MCDR_SKIP_E2E`**：至少有一条变异（`arrives_after`）
只有端到端测试能抓住，跳过 e2e 会让它被 pytest skip → 退出码 0 → 被误报成「测试没抓住」。
`tools/mutation_check.py` 自己会拒绝在这种环境下运行，比指望 CI 配置写对可靠。

> 跨版本矩阵（`tools/mcdr_matrix.py`）**不在 CI 里**：它需要三个各自装了不同 MCDR 的
> Python 环境，在 runner 上重建不划算。发版前本地跑。

## 包体积：打包时剥掉注释与 docstring

**仓库保留注释，包不保留。** `pack.py` 在建包时把每个 `.py` 的注释与 docstring 清空
（`packaged_source()`），源码文件本身一字不动。实测这一步让产物从 **31,454 B 降到 21,605 B
（−31%）**——注释与 docstring 在 `__init__.py` 里占原始字节的 16%，但占其**压缩后**体积的一半，
因为它们是不重复的自然语言，deflate 压不动。

两个实现要点：

- 用 :mod:`tokenize` 找注释，**不用正则**：`#` 出现在字符串里（URL、颜色码、正则）时正则会把
  字符串也切掉。有测试专门放一个 `"url#fragment"` 进去钉住这一点。
- **清空而不是删行**：行号与仓库完全一致（有测试断言逐文件行数相等），
  所以从已安装插件报出的 traceback 仍指向仓库源码的同一行。
  函数体只有一句 docstring 时补 `pass`，否则不合法。
- 剥完立刻 `compile()` 一次自检：发一个「解析不了」的包只会在用户那边爆炸，而这里是最后一道能拦住的关口。

> ⛔ **不要试图换 bzip2 / lzma 压缩。** 它们纸面更小（bzip2 −9.8%、lzma −5.6%），
> 但 **CPython 的 zipimport 只认 deflate**：实测在 Python 3.13 上直接
> `zlib.error: Error -3 while decompressing data: invalid distance too far back`，
> **插件完全加载不了**。这是硬限制，不是取舍。
> ⚠️ 测这个必须在**隔离的 cwd** 里跑，否则 `import server_log_filter` 会命中工作目录里的源码，
> 得到「能导入」的假阳性（第一次就踩了这个）。

加上 CHANGELOG 精简（107 行 → 54 行）与压缩级别 6→9，**最终产物 19,817 B → 19,764 B（约 19.8 KB）**：
比本轮起点小 37%，比 1.3.0（25,739 B）还小 23%。

### 其余已穷尽、结论是不值的路（别再试）

| 手段 | 实测收益 | 结论 |
|---|---|---|
| bzip2 / lzma | −9.8% / −5.6% | ⛔ zipimport 只认 deflate，**插件加载不了** |
| 合并两个语言文件（键名只写一次） | −802 B (3.5%) | 值得换？不值得：改掉 `lang/<code>.json` 的约定、要动加载器与约 30 条语言测试，翻译者还得在一个文件里同时看到两种语言 |
| 语言 JSON 压成单行 | −106 B (0.5%) | 牺牲仓库可读性，不值 |
| 元数据 JSON 压成单行 | −35 B | 不值得一个 diff |
| 压缩级别 6→9 | −53 B | ✅ 已采用（解压速度与级别无关，零运行开销） |
| 删掉 `CONFIG_DOC` 的长说明 | 约 −1,020 B 原始 | ⛔ 那 11 条会被写进用户的 `config.json` 当选项注释，是插件唯一的自带文档 |
| 只发 `.pyc` | — | ⛔ 与 Python 版本绑定，MCDR 支持多版本 → 直接不可用 |
| 删 LANGUAGE / LICENSE | 627 B | 随包分发是 MIT 的常规做法，且用户明确要求保留 |


## 打包是可复现的

**同样的源码永远打出同样的字节。** 每个 zip 成员都通过手工构造的 `ZipInfo` 写入，
时间戳固定为 `FIXED_ZIP_TIMESTAMP`（1980-01-01，zip 能存的最早时间），
权限位固定 `0644`，宿主系统固定 unix。

为什么值得多这几行：早期版本直接 `zf.write()`，zip 条目记录的是**文件 mtime**，
于是同一天改一次文件、或者换台机器打包，就会得到不同的 sha256。
**这会让「拿源码按 tag 重建、比对发布资产的哈希」这条路彻底走不通** ——
用户只能确认下载没损坏，无法确认这个包确实来自那份源码。

⚠️ 所以：**发布时正文里写的 sha256，必须来自实际要上传的那个文件**，
而验证方式就是 `python pack.py && sha256sum`。

> 另外注意：`.py` 成员在打包时会被剥掉注释与 docstring（见上一节），
> 所以比对产物不能直接和仓库文件比字节，要和 `packaged_source()` 比。

## 自行打包

```bash
python pack.py            # 生成 ServerLogFilter-v<版本号>.mcdr
```

发布包里只有 **7 个文件**：

| 文件 | 说明 |
|---|---|
| `mcdreforged.plugin.json` | 插件元数据（必需） |
| `server_log_filter/__init__.py` | 插件代码 |
| `server_log_filter/i18n.py` | 语言支持 |
| `server_log_filter/lang/en_us.json` | 英文文案（回落语言） |
| `server_log_filter/lang/zh_cn.json` | 中文文案 |
| `CHANGELOG.md` | 变更记录，**只含最新一个版本**，随包分发 |
| `LICENSE` | MIT 许可证 |

三个 README（`README.md` / `README_en.md` / `lang/README.md`）**都不打包**：
MCDR 从不读取它们，内容与 Releases 页面重复，而去掉后产物体积几乎减半
（实测两个根 README 就占了压缩体积的 **49.6%**）。
`lang/README.md` 是写给**做翻译的人**看的，属于仓库协作材料，对运行毫无用处。

> **包体积变化**：1.2.1 是 18,302 B（4 个文件），1.2.2 是 25,646 B，1.3.0 是 **约 26.5 KB**（都是 7 个文件）。
> 增加的 3 个文件里，`i18n.py` + 两份 `lang/*.json` 是**真实新增的内容**——
> 同一份文本仍只存一遍，中文文案与 1.2.1 的内嵌字符串逐字一致，只是从 `.py` 里搬到了
> JSON 里。多出的约 7 KB 是「一份插件现在带两门语言」的实际代价，不是打包出错。
>
> ⚠️ **这个字节数只在 LF 工作区构建时成立。** 编辑工具在 Windows 上容易写出 CRLF，
> 而打包器读的是**磁盘原始字节**，产物会因此偏大（实测同一个提交：CRLF 版 25,730 B、
> LF 版 25,646 B；`git status` 两边都是干净的，因为 `core.autocrlf` 把差异掩盖了）。
> 更隐蔽的是 `git checkout` 会把工作区重写成 LF，所以「先打包、后切过分支」也会错开。
> **打包后必须逐成员与 `git show <ref>:<path>` 比对 sha256，全部一致才算产物可信。**
> 也正因如此，包内的 `CHANGELOG.md` 只写「约 25.6 KB」，精确值只放在 Release 页面。
>
> 另外，代码里的**注释也做过一轮精简**：说明性的长段落搬到本文件（见「实现注记」），
> 代码里只留「改这里会踩什么坑」的一句话。`server_log_filter/__init__.py` 从 1110 行
> 降到 990 行（注释+docstring 占比 25% → 16%），`i18n.py` 从 252 行降到 238 行，
> 两者合计让压缩后的包小了约 **4 KB**——比砍掉一整个语言文件（3.3 KB）还多。
> 这纯粹是注释的收益，一行可执行代码都没动。

> **`CHANGELOG.md` 只保留最新版本一节**，旧条目在发新版本时删掉（全文留在 Releases 页面上）。
> 它与包一起分发，任何一个历史条目都会**永久**占着用户的体积——实测 1.2.0 → 1.2.1
> 涨的 10.3% 全部来自它自己变长。`test_changelog_keeps_only_the_latest_release`
> 钉住这一点：必须恰好只有一节，且版本号等于 `mcdreforged.plugin.json` 里的当前版本。

> **为什么必须用白名单？** 早期版本的这一节用的是 `rglob("*")` 加一个很短的 `skip` 列表，
> 那是**黑名单**思路，会被仓库里任何新文件悄悄带进发布包。两个具体后果：
>
> 1. **发布包直接加载失败。** MCDR 会校验 `.mcdr` 根级条目（`PackedPlugin._check_dir_legality`），
>    根目录出现 `conftest.py`、`setup.py` 这类模块就抛
>    `IllegalPluginStructure: Packed plugin cannot contain other module`。测试用的
>    `conftest.py` 恰好就在根目录，所以黑名单方案会让插件**完全无法加载**。
> 2. **体积失控。** 按 `tests/README.md` 装了 `.testlibs/` 之后，黑名单会把整个 MCDR
>    及其依赖一起打进包里：实测 **1362 个文件、7.11 MB**。
>
> `pack.py` 本身也不进包，原因和 `conftest.py` 相同——根级模块会让 MCDR 拒绝加载。

> ⚠️ **`.mcdr` 是含时间戳的 zip，因此哈希不能跨检出目录复现。**
> `git checkout` 会重写工作区文件的 mtime，同一个目录连续构建两次哈希是稳定的，
> 但换检出目录或重新 checkout 之后构建，哈希就会变（**内容相同**）。
> 所以 **Release 正文里的 SHA256 必须用实际上传的那个文件来算**。

## 测试

```bash
python -m pip install --target .testlibs -r tests/requirements-test.txt
PYTHONPATH=.testlibs python -m pytest tests -v     # Windows: $env:PYTHONPATH=".testlibs"
```

> ### 什么时候跑什么（约定）
>
> **开发过程中只跑与本次改动相关的那几条**，确认当前版本能过就行：
>
> ```bash
> PYTHONPATH=.testlibs python -m pytest tests/test_plugin.py -k language -q
> PYTHONPATH=.testlibs python -m pytest tests/test_e2e.py -k auto_follows -q
> ```
>
> **提交 PR 前才统一跑一次全量**：完整测试套件 + `tools/mutation_check.py` + 跨版本矩阵。
> 出问题就在那一轮集中修，修完再提 PR。这样既避免反复烧时间，又保证合并前是完整的证据。
> 汇报时说清「哪些跑过、哪些没跑」，不要用「全绿」掩盖没跑的部分。

当前 **377 个用例**，分三层：

| 层 | 位置 | 说明 |
|---|---|---|
| 单元 | `tests/test_plugin.py` | 过滤逻辑、配置对象、命令面与权限、界面、打包、迁移、坏配置保全、语言、重载与计数（253 条） |
| 端到端 | `tests/test_e2e.py` | 启动**真实 MCDR**，加载 `pack.py` 产出的 `.mcdr`，用假服务端跑完整生命周期（37 条） |
| 跨版本 | `tools/mcdr_matrix.py` | 同一个包在多个 MCDR 版本上逐项核对 |

另有两个工具：

```bash
python tools/mutation_check.py    # 故意改坏实现，确认测试会变红（51 个变异，51/51 应被抓住）
python benchmarks/bench_filter.py # 性能基准，README / CHANGELOG 里引用的数字都由它产出
python benchmarks/bench_versions.py 旧.mcdr 新.mcdr   # 两个版本逐项对比，见「版本间性能对比」
```

> **变异检查的判定看 pytest 的退出码：只有 `exit == 1` 才算「被抓住」。**
> `4`（用法错误）/ `5`（没收集到用例）都说明是脚本自己写错了，不是测试变红——
> 早期把 `4` 也当成命中，得到过一个假的满分。
>
> `MUTATIONS` 的条目是 `(名字, 目标文件, 变换函数, baseline 选择器)`，因此变异可以打在
> `i18n.py` 或两份语言 JSON 上，不只是 `__init__.py`；每个文件各缓存一份 baseline。
> 语言相关的 8 条包括：忽略 `language` 配置项、忽略 MCDR 语言、`pkgutil.get_data`
> 直接返回 `None`（模拟打包后读不到文案）、不报未知语言、不报坏目录、
> 坏配置时不从原文读语言、**语言文件少一个键**、**模板少一个占位符**。
> 后两条保证「文案本身」也在被测范围内，而不只是代码路径。

`tools/mcdr_matrix.py` 用法：传入若干「装了 MCDR 的解释器」，插件包会用 `pack.py` 现场构建。

```bash
python tools/mcdr_matrix.py /path/to/mcdr-2.15.0/python /path/to/mcdr-2.15.7/python /path/to/mcdr-2.16.0/python
python tools/mcdr_matrix.py --current
```

细则（用例分组、变异清单、踩过的坑）见 [tests/README.md](../tests/README.md)。

## 已验证内容

### 单元测试（过滤逻辑、边界与安全属性）

- 目标刷屏行被隐去，且保留 `process`（关键安全属性）
- **15 类绝不能误伤的行逐一验证不受影响**：启动完成 `Done (...)!`、玩家进出、`Stopping the server` /
  `Stopping server`、`moved too quickly`、`moved wrongly`、`Rejecting UseItemOnPacket`、
  `dropping items too fast`、聊天签名问题、`lost connection`、版本启动行、`Preparing level`、
  死亡消息、`Saving and pausing game...`
- `content` 为 `""` / `None` / 纯空白时不崩溃
- 换玩家名同样命中
- 多规则各自独立计数；`reload` **接续同名规则的计数**（被删掉的规则才丢计数）
- 非法正则被跳过且产生警告，不影响其他规则
- 近乎相同但不同的行**不**命中（证明不是无脑全过滤）
- **零命中提醒**：达阈值才提醒、命中即归零、启动失败的周期不计入、热重载不误判、
  **规则从配置删除后其统计立刻清除**（重载时即清，不必等服务端停止；
  仍写在配置里但被跳过的规则不受影响；配置刚被自动重置时不动状态文件）、
  可从配置关闭、`reset` 能清空、提醒内容不重复
- **正则安全检查**：4 类灾难性回溯模式被拦下，8 种真实写法全部放行
- **升级提示与迁移**：旧版本格式的配置在真实 MCDR 上自动补齐，并在日志里报告新增了哪些选项；
  每个配置项都必须写明加入版本与说明（有测试保证，漏写就会变红）
- **坏配置保全**：解析失败时原文件被备份为 `config.json.old`，新配置以默认值重建，
  并在日志里给出原因与出错行列；空文件、顶层非对象同样处理。**不是合法 UTF-8 的文件也算写坏**
  （中文 Windows 记事本存成「ANSI」就会这样）——它此前会让插件整个加载失败，见「实现注记」
- **三条提示的开关**（`announce_config_upgrade` / `announce_broken_config` /
  `warn_about_stale_rules`，默认全开）：关掉只影响**说不说**，不影响**做不做**——
  配置照补、坏文件照备份、统计照记。写坏配置时开关只能从**原文**里读（配置已解析失败），
  因此大小写、空格、写在校验错误之前或之后都有效；「连备份都失败」属数据丢失警告，
  不受开关影响。以上每条都有单测 + 真实 MCDR 端到端各验一遍

### 单元测试（语言支持）

- **`auto` 双向跟随**：MCDR 是 `zh_cn` → 中文；MCDR 是 `en_us` → 英文
- **显式值压过 MCDR**：配置写 `zh_cn`、MCDR 是英文，仍然说中文
- **宽松识别**：`EN_us` / `zh-CN` / `  en  ` / `zh` / `zh_TW` 都能认出来
- **没出货的语言**：`auto` 时**静默**回落（用户把选择委托给了 MCDR）；
  显式写一个不存在的语言时回落 + **一次**警告，不刷屏
- **目录坏掉不影响运行**：读不出文件 → 回落 + 报告一次；**两份同时坏掉也不崩**
- **打包后仍能读到文案**：子进程 import 真实 `.mcdr`，断言输出的是真句子（非裸键名），
  且语言列表来自 zip 内部
- **`reload` 立即切换语言**，不需要重启服务端
- **MCDR 语言 API 缺席 / 抛异常**都不会把插件带下去（退到 `get_mcdr_config()["language"]`，再退到回落语言）
- **坏配置播报用原文里的语言**（那一刻没有解析好的 Config）
- **英文真的到达了每一处输出**：零命中提醒、命令与运行摘要、删规则播报与规则报错、升级提示
- **不变式**（对**所有**出货语言同时成立，新增语言自动纳入）：
  - 每个语言文件都能解析；键集与回落语言完全一致；占位符集合一致（不能只翻一半）
  - **代码里 `_t("…")` 用到的键全部存在**，且**没有键是没人用的**（双向校验）
  - `CONFIG_DOC` 的键集合 == `Config` 的字段集合（所以「每个选项都有说明」对每种语言都成立）
  - `en_us` 里不含任何 CJK 字符（防止把中文原样复制过去）

> 键集合的提取靠 **AST**：遍历源码里所有 `_t(...)` / `say(...)` / `translate(...)`
> 调用的**第一个位置参数**并取字面量。这样「新增一处输出但忘了加文案」会直接变红，
> 不需要人工维护一份键名单。

### 端到端（真实 MCDR + 假服务端，跑完整生命周期）

控制台回显实测（默认规则）：

| 日志行 | 结果 |
|---|---|
| `Player <任意玩家> standing on air - force-sending blocks below` | ✅ 隐去（出现 0 次） |
| `Steve moved too quickly!` / `moved wrongly!` | ✅ 保留 |
| `Steve joined the game` | ✅ 保留 |
| 任意普通日志行 | ✅ 保留 |
| `Stopping server` | ✅ 保留 |

插件日志：

```
插件 server_log_filter@<版本> 已加载
已启用 1 条日志过滤规则；命中后仅从 MCDR 控制台隐去，服务端日志不受影响
本次运行共从 MCDR 控制台隐去 3 行服务端日志（服务端日志文件不受影响）
```

全程**无任何报错**。

另外还有一条**带 canary 的端到端用例**，专门守「隐去但保留事件分发」这个安全属性：
它额外让过滤规则命中**服务端启动行**，然后同时断言「该行确实离开了控制台」和
「MCDR 的 `SERVER_STARTUP` 事件仍然被派发」。这样 `hidden()` 与 `discarded()`
这两种实现才会产生可观测的差异——否则默认规则只匹配刷屏行，而刷屏行不参与生命周期判定，
两种实现看起来一模一样。

测试还断言了最关键的安全属性——被隐去的行**保留 `process`、仅摘掉 `echo_to_console`**，
并且**真的没有出现在控制台上**，同时**事件仍然照常派发**。
如果未来 MCDR 改变 `hidden()` 的语义，测试会直接失败，而不是让插件在服务器上静默出问题。

端到端组共有 **12 次真实 MCDR 启动**（正常一次、零命中提醒一次、坏配置一次、
关掉写坏提示一次、删规则前后各一次、**`auto` 跟随 MCDR 两个方向各一次、显式语言压过 MCDR 一次、
打包产物带语言文件一次、配置不是 UTF-8 一次、真的敲一遍命令一次**），
整个 `tests` 目录约 40 秒，可用 `MCDR_SKIP_E2E=1` 跳过。

> **命令面在真 MCDR 上是被真的敲出来的**，不是只对着假服务器断言。探针插件在
> `on_server_startup` 里调 `server.execute_command('!!lf')` 等四条命令，再用横幅把每段输出夹起来
> （`E2E-CMD-BEGIN/END`），测试逐段读取。**这条路径的威力在于**：`execute_command` 与
> `PluginCommandSource` 都是真实实现，所以「别名到底有没有注册进 MCDR」这种问题在这里藏不住。
> 它由环境变量 `E2E_COMMAND_PROBE` 开关，**只在需要的那一个实例里打开**——其余实例的输出
> 保持逐字节不变，因为好几个用例是靠「某个字符串**不出现**」来断言的。

> **在探针里写代码要小心一件事**：它是嵌在 `test_e2e.py` 的三引号字符串里的，所以探针自己的
> 代码里**不能出现三引号 docstring**（会把外层字符串提前截断，报一个很难看懂的
> `unterminated string literal`）。探针里一律用 `#` 注释。
>
### 权限门为什么写在根节点上

全部子命令都限 ADMIN（状态页会印出用户写的规则，`test` 会验证日志内容）。
要求**只写在根 Literal 上**：MCDR 的 `_execute_command` 在解析路径上对**每个节点**都跑一遍
`__check_requirements`，所以根上一个要求就门住了所有分支。
在五个子命令上各写一遍是重复的声明，新增子命令时最容易漏掉一个。

**被拒时要给出可操作的理由。** MCDR 默认只回一句「权限不足」，而玩家会想「我明明是 OP 啊」——
   因为 MCDR 根本不读原版 OP：权限只来自 `permission.yml`（默认 `user`），
   `.testlibs` 里没有任何读取 `ops.json` 的代码。所以用
   `requires(_admin_only, failure_message_getter=_admin_denied_message)` 把「怎么办」一并说出来
   （`failure_message_getter` 自 MCDR 2.7.0 起支持，远早于我们的 2.15.0 门槛）。

验证方式值得记下来：**不要**只看 `_requirements` 就该下结论。测试用 MCDR 真实的
`node._entry_execute(source, command)`，以 USER / ADMIN 两种 source 各跑一遍，
断言 USER 抛 `RequirementNotMet`、ADMIN 能拿到执行项。
（别用 `_execute_command`：MCDR 在那里只是**收集**执行项、稍后才真正调用，
所以「没抛异常」并不代表命令会被执行——这一点最初的测试就写错了一次。）

### 两条「规则写得不对」的守卫（职责不同，别混成一个桶）

加载时两道检查，顺序不能换：

1. **灾难性回溯**（早就有）：短探测串上超时就**拒绝**该规则。它会让 MCDR 主线程卡顿。
2. **匹配一切**（1.4.1 新增）：用一组差异极大的探测串（`a` / `0` / `ZZZ` / `--- !!! ???` /
   三个空格 / `玩家加入了游戏`）判断它是否命中所有非空行，命中就**警告但保留**。
   `.`、`.*`、`^`、`a?`、`\w*`、`.+` 都会隐去每一行，控制台一片空白，管理员会以为服务端挂了。

两处刻意的取舍：

- **只警告、不丢弃**。把管理员亲手写的规则悄悄扔掉是另一种意外，而这条警告出现在控制台
  （插件的 `logger.*` 不走信息过滤器，见「MCDR 内省要点」），一定能被看见。
- **警告要落在状态页上**，不能只写日志。管理员是在「控制台看着不对」的时候才去敲
  `!!logfilter list` 的，那时日志早就滚过去了。

第三道（1.4.1 新增）不是检查而是**去重**：同一条规则写两遍时，第二条永远不可能命中
（`match()` 返回第一条），但统计文件以规则文本为键、两条共享一份记录，
于是第二条的 `zero_streak` 一直涨，又是一个假提醒。

### 一次开服周期不能在重载处被切断

「本次已隐去」是**按开服周期**结算的：服务端停止时写进 `state.json`，据此推进
「连续零命中」。所以任何在本轮中途替换规则对象的操作，都必须把**同名规则**的计数接过来，
否则这一轮会被切成两段，规则会被记成「零命中」。

两条重载路径都得做，而且必须用同一个实现（`_carry_counts`）：

| 路径 | 入口 |
|---|---|
| 插件重载 | `on_load` → `carry_over_from(prev_module._log_filter)` |
| 命令重载 | `!!logfilter reload` → `_apply_config` → `reload_rules` |

⛔ 1.4.1 之前只有前者做了。后者把计数清零，于是「改配置 → reload」这个 README 推荐的日常流程
会伪造出零命中提醒。**修的时候注意现有测试：`test_reload_replaces_rules_and_zeroes_counters`
当时用的是「规则被完全换掉」的场景，那里归零本来就无害，所以它没能暴露这个洞** ——
补的是「规则保留时应接续」这一半。

### 帮助页的排版：宽度、对齐、配色

三条硬性要求，都是用户提的，各自有测试钉住：

**帮助页与状态页共用同一套视觉语言**，不是各写一份：同一个 `_title_line()`，
标签一律 `aqua`（帮助页的命令、状态页的「规则数: / 语言: / 提示:」都用它），
内容 `white` 或绿，备注 `gray`。这条不是审美偏好——两个界面长成两家，用户会以为
它们来自不同插件。

1. **顶栏约一行宽**：MC 聊天默认字体下大约 53 列。等号数量**按标题长度算**
   （`(_TITLE_WIDTH - len(core) - 4) // 2`），所以换版本号、改插件名都不会破坏两侧对称。
2. **说明文字对齐成一列**：左栏宽度 = `len(别名) + 1 + 最长子命令`，用常量**算出来**
   而不是写死数字——以后加子命令，对齐自动跟着走。
   ⚠️ **对齐只在它真的买到对齐时才该补。** 规则编号原来写死 `{index:>2}`，
   于是 1 条规则时印成 `[ 1]`——那个空格是为 10 条以上准备的，9 条以内纯属噪音
   （用户问过一次「为什么中间有个空格」）。现在宽度按当前条数算：
   `len(str(len(rules)))`，两种规模都有测试钉住。**「为了整齐而补的空格」也要能说清它对齐了什么。**
   ⚠️ 补出来的对齐空格**不能**算进点击目标，否则点击时命令后面会多带一串空格。
   状态页的规则行同样有测试钉住「`--` 落在同一列」。
3. **不用 `dark_gray` / `black`**：管理员多半在深色终端上看，深灰字几乎看不见。
   有帮助页与状态页各一条测试扫描整棵 RText 树的 `color` 字段。

把命令与说明分色还有个副作用值得记：**i18n 里不再需要重复写一遍命令**
（原先是 `"{command} list —— 查看状态"`），命令部分由代码拼。少了一处「同一件事写两遍」。

⚠️ 顶栏的版本号来自 `server.get_self_metadata().version`（2.15.0 就有）。
这一步**包在 try 里**：它只是画个标题，读不到顶多少印一个版本号，
不该让整条命令变成报错——与「捕获的类型太窄」一节是同一条道理。

⛔ **写这类「扫颜色」的测试时必须扫原对象，不能扫 `str(reply)`。**
本轮的 `test_the_help_page_has_no_dark_text` 第一版就是先 `str()` 再找颜色，
而字符串里根本没有颜色信息 → 断言永远空过（典型的花架子测试）。
**是变异检查抓到的**：把说明改回 `dark_gray` 竟然全绿。改扫 RText 对象后立刻变红。

### 帮助行为什么是整行可点击、以及控制台的限制

帮助页每行都 `set_click_event`，点击目标就是那行命令本身（显示什么就执行什么）；
`test` 例外，用 `suggest_command` 把命令**填进输入框**——它还要跟一段日志文本，直接执行没有意义。

⛔ **MCDR 控制台不支持点击。** `misc_utils.print_text_to_console` 走
`RTextBase.to_colored_text()`，而该方法的 docstring 明确写着
「Click event and hover event will be ignored」。所以：
按钮只对**游戏内聊天**有用；控制台里那几行必须**本身就是可复制的命令文本**——
有一条测试专门钉住这一点（`test_the_help_text_stays_usable_as_plain_text`），
免得哪天改成纯按钮，控制台里的管理员就什么都看不到了。

> **e2e 里的语言是分两处控制的**，因为 MCDR 自己的界面文案会被断言扫到：
> `_build_instance(..., mcdr_language="en_us", plugin_language="zh_cn")` ——
> 实例的 MCDR 用 `en_us`（否则 `Server process stopped with code 0` 这类用于判定
> 生命周期的**英文**标志行会消失，`grep` 证实该 key 只存在于 MCDR 的 `en_us.yml`），
> 而**插件**的语言单独 pin 成 `zh_cn`。要测跟随行为时把 `plugin_language` 传 `None`，
> 生成的 `config.json` 里就不写 `language` 键 → 插件走 `auto`。
>
> **`auto` 的两个方向都要测**，因为「跟随 MCDR 当前设置」和「跟随某个内置默认值」
> 在有配置 + 无配置时表现不同，只测一个方向会把它俩混淆：
>
> | MCDR 实例的 `language` | 生成的插件配置 | 期望输出 |
> |---|---|---|
> | `en_us` | 不写 `language` 键 | 英文（顺带覆盖 1.2.2 升级提示） |
> | `zh_cn` | 不写 `language` 键 | **中文** |
> | `zh_cn` | `"language": "en_us"` | 英文（显式值压过 MCDR） |
>
> 坏配置用例里的 `BROKEN_CONFIG` 会**自带** `"language": "zh_cn"`——
> 那才贴近真实场景（用户本来就是这么写的），也顺带证明原文可读。

## 实现注记（为什么这么写）

代码里的注释只留「改这里会踩什么坑」的一句话；原因、实测数字和历史教训集中在这节。
改动相关代码前先读对应条目。

### 为什么必须是 `hidden()` 而不是 `discarded()`

MCDR 在 `mcdreforged/info_reactor/impl/server_reactor.py` 里靠**日志行**判定启动完成：

```python
if handler.test_server_startup_done(info):
    self.mcdr_server.add_flag(MCDReforgedFlag.SERVER_STARTUP)
    self.mcdr_server.plugin_manager.dispatch_event(MCDRPluginEvents.SERVER_STARTUP, ())
```

`InfoActionFlag.hidden()` = `send_to_server | process`，只摘掉 `echo_to_console`；
而 `InfoFilter` 返回 `False`（或设成 `discarded()`）是把整条丢掉。**后者会让 MCDR
的启动 / 停止 / 玩家进出检测失效**——这不是理论风险，端到端组里有一条 canary 用例
专门守它（见 [tests/README.md](../tests/README.md) 的 canary 一节）。

服务端自己的 `server/logs/latest.log` 由服务端进程用 log4j 写入，与 MCDR 无关，
**一条都不会少**。

### 热路径的预算

`filter_server_info` 在 MCDR **主线程**上逐行调用（不是任务执行器线程），所以：

- 正则在载入时预编译（`Rule.regex`），逐行只做 `regex.search()`；
- 规则存 **tuple**，命中即 `return`，不命中就整体退化为 N 次 `search`；
- `hidden()` 这个不变常量在构造函数里算一次，命中路径上直接复用；
- 计数加锁，但**只在命中那一条路径上**取锁，不影响未命中的行；
- 只有开了 `log_matched_lines`（调试用）才碰语言查找——正常路径上一次 `_t()` 都不调。

实测（`benchmarks/bench_filter.py`，可复现）：

| 规则条数 | 每行开销 |
|---|---|
| 1 | ≈ 0.27 µs |
| 5 | ≈ 0.55 µs |
| 25 | ≈ 1.80 µs |
| 50 | ≈ 3.20 µs |

对照：MCDR 自己解析同一行约 **2.1 µs**。也就是说 1 条规则时本插件约占 MCDR
每行开销的 13%；即便 50 条规则、1000 行/秒的极端突发，也只占一个核心的 **0.3%**。

**所以「规则太多导致卡服」不是这个插件的性能问题**，也没有为它引入任何优化。
曾经试过的两个方向都实测无效，留在这里免得再试一遍：

- **把 N 条规则合并成一个 `(a)|(b)|(c)` 大正则**：1 条时略快，**2 条起反而更慢且超线性**
  （2 条 0.49 µs、5 条 1.16 µs、50 条 20 µs）。原因是 `re.search` 在每个起始位置都要
  逐支尝试整个 alternation，而分开的 N 次 `search` 各自能走字面量前缀的快速路径。
  加上命名组归属在嵌套捕获组下不可靠（`lastgroup` 可能是 `None`）、
  行内 `(?i)` 这类全局标志不允许出现在中段，这条路是净亏。
- **把 `match()` 内联进 `filter_server_info`**：省下的函数调用与测量噪声同量级
  （0.03 µs/行，约 MCDR 每行开销的 1.5%），不值得牺牲可读性。

真正会让主线程冻结的是**灾难性回溯**，见下条。

### 探测是启发式，不是形式化保证（`validate_patterns` 的真实强度）

先把结论说清楚，免得有人把它当成安全边界：**载入时的回溯探测是一个启发式，它拦不住
所有病态正则。** 它只保证"绝大多数会在真实日志行上冻住主线程的写法，在加载期就被拒绝
并说清原因"。

为什么不能做成形式化保证：Python 标准库的 `re` **没有超时机制**，`re.search` 是 C 层
的一次调用，纯 Python 加不了 deadline（`regex` 这类第三方库才有）。所以任何"跑一遍看看
多慢"的方案都只是在赌那一跑足够短——包括这个探测本身。

#### 两道判据，顺序不能换

带嵌套量词的表达式（如 `(a+)+$`）匹配失败时按 2^n 回溯，**一行**就能冻住 MCDR。
现在的探测分两层，`_probe_pattern` 按这个顺序执行：

1. **增长判据（主判据）**：`GROWTH_PROBE_PAIRS` 里每个字母表一对短串（n 与 2n），
   比 `t(2n) / t(n)` 是否超过 `PROBE_GROWTH_RATIO`。它必须**最先**跑，而且长度必须
   短到对最坏的模式也能跑完——这是整层设计里最硬的一个数：φ^n 类模式每 +2 字符约
   ×2.6，实测 `(a|aa)+$` 在 12 字符上 0.04 ms、24 字符上 6 ms、28 字符上 86 ms、
   40 字符上 31 s、**200 字符上约 10^40 步**。所以 `GROWTH_PROBE_LENGTHS` 是
   `(12, 24)`：再往上，守卫自己就先被卡住了。
2. **绝对耗时兜底**：`LONG_PROBE_SUBJECTS`（接近真实日志行长的 57~91 字符文本）逐个
   试跑，超过该档预算即拒绝。真实 MC 日志行普遍 60–300 字符，只在长串上才变坏的模式
   由这一层负责。这一层只在增长判据没发现问题时才执行，所以正常写法是"免费"的。

两处容易踩的坑，都写在这里：

- **配对的探测串必须是"匹配失败"的那种**（结尾带一个不属于字符集的 `!`）。匹配成功的
  串几乎不花时间（`(a|aa)+$` 在 `"a"*12` 上 4 µs 就返回），拿它当分母，比值判据等于没测。
- **每个字母表都要一对，不能只留 `a`**。`(x+x+)+y` 这类模式在 `a` 上完全不回溯（1 µs），
  只在全是 `x` 的串上才爆炸。字母表覆盖不足，这一层就形同虚设。

#### 已知拦不住的形态（如实记账）

- **增长比低于阈值的慢指数**。实测 `a*a*a*a*a*b` 在 n 与 2n 上的比值约 4.2（低于
  `PROBE_GROWTH_RATIO = 8`），于是它通过第一层；但它在真实长度上确实致命（`"a"*200`
  实测 **123 秒**）。它靠第二层的长串兜底被拦下——本机实测探测耗时约 1.1 s。
- **只在超出探测长度后才爆发的模式**。探测最长只到 91 字符；一条 150 字符才变坏的
  模式会通过探测。这是"不能测更长的串"的直接代价（见上）。
- **最坏情况下的探测耗时**。单条危险规则的探测最坏约 1.5 s（实测 `(\w+\s?)*$`），
  因为那 1.5 s 就是一个 24 字符串单次 `re.search` 的耗时，无法中断。这是一次性加载
  开销，换来的是"不会在每一行上各卡一次"；但它确实是 `!!lf reload` 命令线程的可感知
  停顿，所以 `pattern_probe_timeout_ms` 有上下限（见下）。

#### 与 `pattern_probe_timeout_ms` 的关系

该选项的语义是**单个探测串的预算**，不是"每行可接受开销"（两者量级不同：正常规则
每行约 1 µs，这里是 25 ms 级别的加载期上限）。它只影响**载入期**，与逐行匹配成本无关。
下限夹到 1 ms（防止预算 ≤0 把所有规则判成危险），上限 2000 ms（防止写成一个巨大的值
把 `on_load` / `!!lf reload` 卡住），钳制时会 warning 一次。

标定（`benchmarks/bench_filter.py` 第 4 节 + 本次修复的实测）：

- 危险模式在增长档上就远超比值阈值或预算 → 被拦下；
- 8 种真实写法最坏仅几微秒 → 余量约 **9000 倍**，不会误伤。

### 「零命中提醒」的价值与两个假提醒防护

它**不降低运行开销**（这一点被数据否定过：删规则省下的时间远低于测量噪声），
价值在配置卫生：尽早发现写错的规则、以及已经没必要再过滤的规则。
两个必须保留的防护，否则会给出**错误的**提醒：

1. **只统计「完成过启动」的周期**（`_session_reached_startup`）。否则服务端连续
   启动失败（mod 报错之类）会把所有规则刷成零命中。
2. **热重载要接续命中计数**，**并且**接续上面那个标记。真实重载会构造一个全新的
   模块，标记会归零；只接续计数不接续标记，整轮周期仍会被丢弃。

清理（`_forget_orphans`）的两条刻意保留的边界，防止「清理」变成「误删」：

- 孤立项按**配置原文**判，不按编译成功的规则。仍写在配置里但被跳过的规则
  （正则写错 / 被回溯探测拦下）**保留历史**——否则改好错别字后历史已经归零。
- 配置因写坏**被自动重置**时（`_config_was_reset`）两个清理点**都跳过**：那一刻的
  `patterns` 是默认值而不是用户配置。守卫必须放在 `_forget_orphans` 里（两个调用点
  共享），只放在 `_prune_state` 会漏掉关服那条路。

### 为什么要在 MCDR 之前自己检查配置文件

`load_config_simple` 默认 `failure_policy='regen'`：解析失败时**直接用默认值覆盖原文件**。
最常见的触发方式就是往 `patterns` 里加规则时漏一个逗号——用户辛苦写的规则会就此消失。

所以插件先自己读一遍原文：能解析成 dict 就交给 MCDR；否则先把文件改名成
`config.json.old`，再让 MCDR 重建。**备份动作永远执行**，`announce_broken_config`
只管说不说。两个细节：

- 那一刻没有解析好的 `Config`，所以开关与语言都只能用 `_raw_bool_option` /
  `_raw_str_option` 从**原文**里做文本查找，因此写在语法错误**之后**也照样有效。
- 「连 `os.replace` 都失败」意味着原文件真的会被覆盖掉，属数据丢失，**不受任何开关
  影响**，一律报出去。

#### 三种「写坏」，第三种曾经会让插件整个加载不了

「写坏」有三种：**JSON 语法错误**、**顶层不是对象**、以及**文件不是合法 UTF-8**。
第三种是 1.2.3 才补上的，而且它原来的表现远比前两种严重。

`config.json` 里可以写中文规则，所以编码错误是真实存在的：在中文 Windows 上用记事本
存成「ANSI」就是 GBK。MCDR 自己也解析不了它，因此它理应走同一条备份 + 重建的路。
但原来的 `_read_text_file` 只 `except OSError`，而 `open(..., encoding="utf-8").read()`
抛的是 **`UnicodeDecodeError`（`ValueError` 的子类）**——没被接住，一路冒到 `_load_config`
再到 `on_load`，结果是 **MCDR 里根本没有这个插件**。一个辅助函数的签名写着
「读不动就返回 None（不抛异常）」，而它其实会抛——**docstring 承诺与实现不符，是这类
bug 最好的藏身处**。

所以现在按字节读、显式解码：`_read_bytes()` 只管 I/O，`_decode_utf8()` 把「解码不了」
变成一个可以上报的返回值而不是异常。**推论值得记住：只要某个文件是「用户手写、插件去读」的，
读取路径就必须假设它可能不是 UTF-8。**

一个副产物：这种情况下读不出用户写的 `language`（那正是读不出来的东西），
所以播报只能退回 MCDR 自己的语言。这是当时唯一还能确定的语言，端到端用例断言的就是英文。

### 两个「取值写错」的坑

它们都属于「用户把某个值写成了不合法的东西，插件却报出与真实原因无关的错误」。

**`pattern_probe_timeout_ms <= 0` 会把所有规则拒掉。** 「预算 0 毫秒」意味着任何耗时都算
超支，于是每条规则都被判成「有灾难性回溯风险」——连纯字面量也不例外。过滤静默失效，
而日志把人往「规则写错了」的方向带。现在下限夹到 **1 ms**：真实规则在微秒级、
危险模式在百毫秒级，1 ms 两边都分得清（见「短探测串为什么能判断回溯风险」的标定数字）。

**`!!logfilter test` 会把锚定规则误报成「不命中」。** 过滤器匹配的是 MCDR **剥掉
`[时间] [线程/级别]:` 前缀之后**的正文（`info.content`），而命令拿用户粘贴的原文去匹配。
管理员习惯整行复制，于是 `^Player \w+ joined` 这种写法会被告知「不命中」——
而它其实工作得好好的，人反而会去改一条本来正确的规则。现在测试前先剥掉前缀
（`_LOG_LINE_PREFIX`），并在回复里说明它这么做了。前缀正则只匹配最常见的
`[..] [../LEVEL]:` 形态，认不出就不剥，行为与从前一致，不会有回归。

#### 「捕获的类型太窄」是一个反复出现的 bug 家族

上面那条编码问题属于这一类，1.3.0 复查时又找到两处**同族**的：

| 位置 | 只接了 | 实际会抛 | 后果 |
|---|---|---|---|
| `_read_text_file` | `OSError` | `UnicodeDecodeError`（`ValueError`） | 插件整个加载不了 |
| `re.compile(pattern)` | `re.error` | `OverflowError`（`a{999999999999}`）、`RecursionError`（上千层嵌套） | 插件整个加载不了 |
| 模板 `format(**kwargs)` | `KeyError`/`IndexError`/`ValueError` | `AttributeError`（译文写成 `{a.b}`） | 消息路径抛异常 |

共同点：**代码在同一条路径上明确承诺了「绝不抛异常 / 单条写错不影响其他」，而 except 只覆盖了
最常见的失败类型。** 写这类防御性代码时，问题不是「该接哪种异常」，而是
「这一步在什么条件下**不能**抛异常」——那就该按后者来写列表。三处现在分别是
`(re.error, OverflowError, RecursionError)`、`(KeyError, IndexError, ValueError,
AttributeError, TypeError)`、以及「按字节读 + 显式解码」。三条都有对应的变异锚点：
把 except 改窄，测试必须变红。

（顺带一个反例：`re.search` 在**匹配**阶段不会再抛这类异常——实测 400 层嵌套编译通过后
`search` 正常。所以只堵 `compile` 这一处就够了，不必给热路径加 try。）

### 语言必须在配置加载路径上最早定下来

坏配置的播报、缺选项的播报都要用它。配置写坏时读到的是默认值，所以那时改从原文里
取用户写的语言。`_quarantine_broken_config` 内部用 `warn=False` 解析一次，随后的
`_load_config` 用同一份原文再解析一次——**警告只在后者发一次**，不说两遍。

### 开关只控制「说不说」

`announce_config_upgrade` / `announce_broken_config` / `warn_about_stale_rules`
都**不控制「做不做」**：配置照补、坏文件照备份、统计照记。改动这类开关必须写测试
钉住这一条，否则实现很容易退化成「关掉提示 = 连安全动作一起跳过」。

### 语言：为什么不用 MCDR 自带的插件翻译机制

**MCDR 确实自带一套插件翻译**（`PackedPlugin(MultiFilePlugin)._on_ready` 会调
`__register_default_translation()`，自动把 `<插件>/lang/*.json|yml` 注册为插件翻译；
`server.tr(key, *args, _mcdr_tr_language=..., **kwargs)` 负责查表）。我们的目录名
（`plugin_constant.PLUGIN_TRANSLATION_FILES_PATH = 'lang'`）和扁平 JSON 格式**本来就和它一致**，
所以这不是巧合，而是可以随时切换的。**调研过、量过、决定不切**——记录在这里，
免得以后有人再从头查一遍。

先说**不会损失**的（逐条已核对 MCDR 源码）：回落链完全一致
（`LanguageFallbackHandler.auto()` 就是 `en_us` + `{'zh_tw': ['zh_cn'], 'zh_cn': ['zh_tw']}`，
我们的 `FALLBACKS` 本来就是照抄它）；键缺失同样返回键名本身；`lang/` 的读取用
`open_file()`，**在打包的 zip 里也读得到**（我们手写的 pkgutil / importlib 那套它已具备）；
显式语言能通过 `_mcdr_tr_language` 生效，且不传 `_mcdr_tr_fallback_language` 时默认仍是 `auto()`；
**键名不会撞车**（MCDR 自带翻译全部挂在唯一顶层键 `mcdreforged.*` 下，
我们的 `summary` / `status` / `broken` … 与它不相交）；时机也没问题（语言注册在
`plugin.ready()`，**早于** `on_load` 事件派发）；`tr()` / `rtr()` / `lang/` 自动注册都远早于
我们 2.15.0 的底线。

再说**会损失**的：

| 损失 | 说明 |
|---|---|
| **坏占位符会让插件崩** | MCDR 的 `TranslationManager.translate` 在 `formatter.format()` 失败时 `raise ValueError('Failed to apply args ...')`；本插件刻意是「返回原文、不抛」。同一个错误发生在 `on_load` 里就是**插件加载失败** |
| 键名缺失每次打一条 ERROR | MCDR 先 `logger.error('Error translate text ...')` 再返回键名；本插件是静默返回 |
| 宽松写入要自己保留 | MCDR 按**精确字符串**查表 + 固定回落表：不认 `zh-CN` / `EN_us`，也没有同基匹配（`zh` → `zh_cn`） |
| 「语言不支持」的警告要自己保留 | MCDR 没有「插件自己的 `language` 选项非法」这个概念 |
| 读不出的语言文件换了归属 | 变成 MCDR 自己打的异常堆栈（信息更多，但不受我们的开关控制） |
| `_t()` 不再是纯函数 | 需从 `_server` 取，测试里「替换 catalogue 读取函数」的手法失效 → 约 30 条语言测试 + 8 条变异要改写 |
| **省下的比想象中少** | 目录发现、`resolve`、宽松归一化、以及上面两条补偿都得留着：实际只能删约 **100 行**（`i18n.py` 238 → 约 140），压缩后包体只小 **约 1.1–1.3 KB** |

结论：**保持自研**。所有代价都落在「我们自己刻意加的那几条健壮性」上，尤其第一条——
官方机制外面永远得套一层 `try/except`，套了之后既不是「纯官方」也不是「纯自研」，
反而多一层壳。收益只有约 1.2 KB + 100 行，还要重写 30 条测试，不划算。

> 真要切换时的关键差异只有三处：给 `_t()` 换成 `server.tr(key, _mcdr_tr_language=_language)`
> 并包一层兜底；保留 `normalize()` 与同基匹配；保留「取值不支持」的警告。
> 目录名和文件格式都不用动。

### 一个测试必须覆盖「接线」而不只是函数

第 9 组（灾难性回溯防护）踩过：只在 `_build_rules` 层面测「危险模式被拦下」，
然后把 `on_load` 里的 `validate=_config.validate_patterns` 改成 `validate=False`，
测试**依然全绿**。函数正确 ≠ 它真的被挂上了。同类接线用例还有
`test_on_load_wires_the_migrator_into_config_loading`、`test_reload_command_also_migrates`。

### 版本间性能对比

`benchmarks/bench_versions.py` 回答「这次改动有没有变慢」。它把两个 `.mcdr`（或两个源码目录）
各自解到临时目录，**分别在独立子进程里 import 并测量**——两个版本永远不会共用同一个模块对象。

```bash
python benchmarks/bench_versions.py ServerLogFilter-v1.2.2.mcdr ServerLogFilter-v1.3.0.mcdr
```

**报告的是「多次分批里的最快一批」，不是平均值。** 这一点是实测调出来的：外部干扰
（别的进程、GC、调频）只会让某一批变慢、不会变快，所以最快的那批最接近代码的真实成本。
最初用平均值时，单条规则那一档在同一个版本上重复三次就能抖动 **约 20%**，足以把噪声当成回归；
改成取最小批次后，5 / 25 / 50 条规则三档的复跑差异都收敛到 **±1.5%** 以内。

1.2.2 → 1.3.0 的实测（同一台机器，三次复跑）：

| 规则数 | v1.2.2 | v1.3.0 | 差异 |
|---|---|---|---|
| 1 | 0.170 µs | 0.172 µs | +1.3% |
| 5 | 0.423 µs | 0.428 µs | +1.3% |
| 25 | 1.635 µs | 1.633 µs | −0.1% |
| 50 | 3.310 µs | 3.287 µs | −0.7% |

载入开销（编译 + 回溯探测，25 条规则）约 0.022 ms，两版相同；
模块冷导入约 0.54 s（其中绝大部分是 import `mcdreforged` 本身），两版相同。

**结论：1.3.0 在过滤热路径上没有可测量的变化。** 差异全部落在复跑噪声内，
而「最慢的一批」在绝对值上也只有微秒量级——对照 MCDR 自己解析一行要 ~2.1 µs，
这个插件在 1 条规则时约占其 8%。1.3.0 修的四处都是**错误路径**（写坏的配置、写错的取值），
那些路径本来就不在逐行循环里。

产物体积从 25,739 B 增到 27,526 B（+1.8 KB），来自新增的三条测试所需的代码、
新增的 6 条语言消息，以及 `!!logfilter` 多输出的三行。**这是功能与健壮性的代价，不是性能开销。**

## 环境要求的由来

- **MCDReforged >= 2.15.0**

  > 为什么不是 2.13？本插件的核心设计依赖 `InfoActionFlag.hidden()`（只摘控制台回显、保留事件分发），
  > 而这个类是 **MCDR 2.15.0** 才引入的。2.14.x 及更早的 `InfoFilter` 只有「返回 `False` 即丢弃整条」
  > 的语义，无法等价实现本插件的行为，所以不支持。
  > 装到旧版本上不会静默出错——MCDR 会直接提示 `依赖项 mcdreforged@x.y.z 不满足版本约束 >=2.15.0`。

- Python >= 3.9（随 MCDR 自带）

**门槛只由 `InfoActionFlag` 决定。** 后续版本新增功能用到的 API
（`save_config_simple`、带 `file_name` 的 `load_config_simple`、嵌套 `Serializable`、
`on_server_startup`、`data_processor`）经逐版本检查，**自 MCDR 2.13.0 起就全部存在**，
因此最低版本要求一直没有变化。

## 版本兼容性实测

| MCDR 版本 | 结果 |
|---|---|
| 2.13.0 / 2.14.1 | 被 MCDR 的依赖检查拦下，提示 `依赖项 mcdreforged@x.y.z 不满足版本约束 >=2.15.0` |
| **2.15.0**（最低支持） | 加载、过滤、零命中提醒、状态文件、命令（含 `!!logfilter reset`）、语言跟随全部实测通过 |
| 2.15.7 | 同上，全部通过 |
| 2.16.0 | 同上，全部通过 |

全部功能在这三个版本上行为**完全一致**（实测：提醒出现时机、连续零命中计数推进、
状态文件写入、命令树注册、**`auto` 跟随 MCDR 语言**均相同，零异常）。
可用 `tools/mcdr_matrix.py` 自行复跑。

矩阵里与语言有关的判定是 **`spoke_chinese`**：生成的 `config.json` 首位写 `"language": "auto"`，
而矩阵实例的 MCDR 固定为 `language: zh_cn`，于是「输出里没有出现英文句子」同时证明了两件事——
插件确实读到了 MCDR 的语言，且 `auto` 是**默认行为**（不写这个键也一样）。

Minecraft 侧：过滤发生在 MCDR 侧（对服务端 stdout 逐行匹配），
因此**与 MC 版本无耦合**。1.16.5 / 1.19.4 / 1.20.1 / 1.20.6 / 1.21.8 / 1.21.11 /
26.1 / 26.2 / 26.3 均已用真实服务端实测通过。

唯一与 MC 版本相关的是**默认规则的目标日志**：`standing on air - force-sending blocks below`
仅由 **MC 26.3** 产生；装到更早版本上插件照常工作，只是默认规则不会命中任何内容。

## 发布流程

0. **提 PR 前统一跑一次完整清单**（开发过程中只跑相关子集）：
   ```bash
   PYTHONPATH=.testlibs python -m pytest tests -q      # 全量 288
   python tools/mutation_check.py                      # 50/50
   python tools/mcdr_matrix.py <mcdr2150> <mcdr215> <mcdr216>   # 跨版本矩阵
   ```
   出现问题就在这一轮集中修，修完再提 PR。**矩阵不通过就不发版。**
1. 改动合入 `main`（对外动作前先征得维护者同意）
2. 更新 `CHANGELOG.md` 与 `mcdreforged.plugin.json` 里的版本号；
   同时**把 `CHANGELOG.md` 裁成只剩本次版本一节**（旧条目删除，Releases 里已有全文）
3. 判定是否属于**实质更新**：只有用户拿到的东西变了（插件行为 / 配置项 / 命令 / 兼容性）
   才发新版本；纯测试、纯文档、纯打包脚本改动不发版本
4. `python pack.py` 构建产物，并记下**该文件**的 SHA256
5. 创建 Release 并上传资产；正文里写明改动与 SHA256

> 打 tag 可以交给 Release API 一并完成：创建 Release 时同时给 `tag_name` 与
> `target_commitish=<sha>` 即可，不必单独 push tag。
