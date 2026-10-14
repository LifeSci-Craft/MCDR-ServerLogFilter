# MCDR-ServerLogFilter

**Language / 语言:** **English** | [简体中文](README.md)

> An MCDReforged plugin that hides noisy server console lines from the MCDR console, while leaving the server's own log file completely untouched.

[![MCDR](https://img.shields.io/badge/MCDReforged-%3E%3D2.15-blue)](https://mcdreforged.com/)
[![License](https://img.shields.io/badge/license-MIT-green)](LICENSE)
[![Python](https://img.shields.io/badge/python-%3E%3D3.9-blue)](https://www.python.org/)

| | |
|---|---|
| Plugin ID | `server_log_filter` |
| Commands | `!!logfilter` / `!!lf` |
| Requires | MCDR **2.15.0+** (tested on 2.15.0 / 2.15.7 / 2.16.0; MC-version independent) |
| License | MIT |

> Building or modifying the plugin, or looking for the packaging and test docs? See [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md).

## Contents

- [Features](#features)
- [Installation](#installation)
- [Commands](#commands)
- [Configuration](#configuration)
- [Notes](#notes)
- [License](#license)

## Features

- Filters server output with regex rules; a matching line is hidden from the **MCDR console only**, and the server's own `server/logs/latest.log` keeps every line
- Hiding the console echo is not discarding the line: the event is still dispatched, so other plugins and MCDR's own start / stop / join-leave detection are unaffected
- The default rule targets the Minecraft 26.3 "standing on air" spam (an upstream false positive; Mojira [MC-311474](https://bugs.mojang.com/browse/MC-311474) / [MC-311727](https://bugs.mojang.com/browse/MC-311727))
- Rules can be added or changed at any time; `!!logfilter test <text>` verifies one before you rely on it
- A rule that matches nothing for several sessions is reported once; rules with catastrophic backtracking are refused at load (skipped with a warning, the others keep working)
- A broken config file is backed up as `config.json.old` and rebuilt; new options are filled in after plugin upgrades, never overwriting your values
- English and Simplified Chinese; every command needs administrator permission

## Installation

1. Download `ServerLogFilter-vX.Y.Z.mcdr` from [Releases](../../releases), or build it yourself (see [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md));
2. Drop it into the MCDR `plugins/` folder;
3. If MCDR is running, use `!!MCDR reload plugin server_log_filter`; if not, just start it;
4. The first load generates `config/server_log_filter/config.json`; the defaults work out of the box.

Running several backends? Put one copy in each; configurations are independent.

## Commands

`!!logfilter` and `!!lf` are completely interchangeable. **Every command needs MCDR administrator permission** (this is not the same as being op in the game: MCDR only reads its own `permission.yml`, and unlisted players default to `user`). Grant it in the MCDR console with `!!MCDR permission set <name> admin`.

| Command | Effect |
|---|---|
| `!!logfilter` (= `!!logfilter help`) | Help page (each line is clickable in in-game chat) |
| `!!logfilter list` | Status: active language and where it comes from, per-rule hits and idle streaks, number of skipped rules |
| `!!logfilter test <text>` | Whether a line would be hidden, and which rule matches; pasting the whole console line works too (the `[time] [thread/level]:` prefix is stripped first) |
| `!!logfilter reload` | Re-read the config and apply it immediately, no restart needed; statistics for rules removed from the config are cleared as well |
| `!!logfilter reset` | Clear the idle-session counters and start observing again |

## Configuration

`config/server_log_filter/config.json` is generated on first load:

```json
{
    "language": "auto",
    "patterns": [
        "standing on air - force-sending blocks below"
    ],
    "log_matched_lines": false,
    "report_on_server_stop": true,
    "warn_about_stale_rules": true,
    "stale_rule_threshold": 3,
    "validate_patterns": true,
    "pattern_probe_timeout_ms": 25,
    "announce_config_upgrade": true,
    "announce_broken_config": true
}
```

<details><summary>Show options</summary>

| Key | Type | Default | Description |
|---|---|---|---|
| `language` | `string` | `"auto"` | Message language. `auto` follows MCDR; `zh_cn` / `en_us` pin it (case and `-` / `_` are free; `zh_tw` resolves to `zh_cn`, anything missing falls back to `en_us`) |
| `patterns` | `string[]` | see above | Regex rules, matched against the **body** of each line (MCDR has already stripped the `[time] [thread/level]:` prefix). A match hides the line |
| `log_matched_lines` | `bool` | `false` | Debug aid: write every hidden line to the MCDR log at INFO level |
| `report_on_server_stop` | `bool` | `true` | Log a summary of how many lines were hidden this run, on server stop |
| `warn_about_stale_rules` | `bool` | `true` | Warn once when a rule has matched nothing for several sessions (only sessions that finished starting count) |
| `stale_rule_threshold` | `int` | `3` | How many consecutive idle sessions before warning; `0` disables this reminder |
| `validate_patterns` | `bool` | `true` | At load, probe each rule and refuse ones with catastrophic backtracking (skipped with a warning) |
| `pattern_probe_timeout_ms` | `int` | `25` | Time budget for **one probe subject** in the check above, in milliseconds. It only affects how long loading the plugin / `!!logfilter reload` takes, never the per-line cost of filtering (a normal rule is about 1 µs per line). Values below 1 are treated as 1 and above 2000 as 2000, with a warning when clamped. The check is a **heuristic**: it does not catch every pathological regex (growth ratios below the threshold, or patterns that only blow up on very long input). See [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md) |
| `announce_config_upgrade` | `bool` | `true` | Say which options were filled in after an upgrade |
| `announce_broken_config` | `bool` | `true` | Say why a broken config was backed up and reset, and where the backup went |

</details>

**Matching uses `re.search` (substring match), not full match.** Writing `standing on air` matches the whole line, no `.*` needed. The flip side: `.` matches any character, so use `\.` for a literal dot.

**Adding rules:** append the log body to `patterns` (leave the other keys as they are), verify with `!!logfilter test <text>`, then run `!!logfilter reload`:

```json
{
    "patterns": [
        "standing on air - force-sending blocks below",
        "Ignoring chat session from .* due to missing Services public key"
    ]
}
```

A few notes:

- A rule that fails to compile is skipped with a warning in the log; the others keep working. To switch filtering off for a while, empty `patterns` and run `!!logfilter reload`.
- Idle statistics live in `state.json`, separate from your `config.json`; the plugin never edits your config file. Remove a rule from the config and its statistics are cleared on `reload`.
- A broken config is renamed to `config.json.old` before the defaults are rebuilt (one backup slot only). A file that is not UTF-8 (for example saved as ANSI) counts as broken too.
- The three notice switches (`warn_about_stale_rules`, `announce_config_upgrade`, `announce_broken_config`) only control the message: backing up, filling in and counting always keep running.
- Adding a language: copy `en_us.json` to `<code>.json` and translate the values; see `server_log_filter/lang/README.md`. No code changes needed.

## Notes

- Filtering happens entirely on the MCDR side, so it is independent of the MC version. The default rule only means something on MC 26.3; on other versions the plugin works as usual and the default rule simply never matches.
- Avoid overly broad rules (`.`, `.*`, `^`): the console can start looking like the server is not running. A warning is printed at load, and `!!logfilter list` flags them.
- For the complete raw log, always read `server/logs/latest.log`.

## License

[MIT](LICENSE)
