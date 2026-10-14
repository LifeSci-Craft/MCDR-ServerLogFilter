"""Message lookup for Server Log Filter.

Every human-facing string lives in ``server_log_filter/lang/<code>.json`` — one flat
``key -> template`` object per language, so adding a language is adding a file and never
touching code (see ``lang/README.md``).

Two rules this module exists to guarantee:

* a template whose placeholders do not match the call is returned **as is**, never raised —
  a bad translation must not be able to take the server down;
* a missing key falls back to ``en_us`` and finally to the key name, so the worst case is
  a readable-but-untranslated message.

Catalogues are read through the import system, not ``open()``. Why that is load-bearing:
docs/DEVELOPMENT.md「实现注记」.

MCDR's own plugin-translation API (``server.tr`` plus the ``lang/`` auto-registration)
was considered and deliberately **not** used — the directory name and JSON format already
match it, so switching stays possible. Side-by-side and reasons: docs/DEVELOPMENT.md.
"""

import importlib.resources
import json
import os
import pkgutil
import threading
from typing import Any, Dict, List, NamedTuple, Optional, Sequence


#: Last resort for every fallback chain: the one language the docs assume is present.
FALLBACK_LANGUAGE = "en_us"

#: Value of the ``language`` option meaning "do what MCDR does".
AUTO = "auto"

LANG_DIR_NAME = "lang"

#: Catalogue key of the warning raised when the configured language is not one we ship.
#: Named here rather than buried in a return value, so the catalogue-coverage test can
#: see it — it never appears as a literal at a ``translate()`` call site.
UNSUPPORTED_KEY = "language.unsupported"


#: MCDR ships ``en_us`` / ``zh_cn`` / ``zh_tw``. We ship the first two, so a ``zh_tw``
#: admin is pointed at the sibling script they can still read — the same preference order
#: MCDR's own ``LanguageFallbackHandler.auto()`` uses.
FALLBACKS: Dict[str, Sequence[str]] = {
    "zh_tw": ("zh_cn",),
    "zh_cn": ("zh_tw",),
}


class Catalog(NamedTuple):
    """One language file, plus why it is empty if it could not be read."""

    messages: Dict[str, str]
    error: Optional[str]


class Choice(NamedTuple):
    """Outcome of resolving the ``language`` option.

    ``note_key`` / ``note_args`` describe a warning to print (an unrecognised value);
    they are ``None`` / empty when the request was understood.
    """

    language: str
    note_key: Optional[str]
    note_args: Dict[str, Any]


def _filesystem_dir() -> str:
    """Where this file lives on disk — meaningless inside a packed plugin, see below."""
    return os.path.dirname(os.path.abspath(__file__))


def _resource_bytes(relative: str) -> Optional[bytes]:
    """Read a file that ships inside the plugin package, or return None.

    ``pkgutil.get_data`` asks the loader instead of the filesystem, which is the only
    thing that works for a zipped ``.mcdr``. The plain read below is a fallback for an
    unusual loader, never the only path.
    """
    try:
        data = pkgutil.get_data(__package__, relative)
    except (OSError, ImportError, ValueError):
        data = None
    if data is not None:
        return data

    try:
        with open(os.path.join(_filesystem_dir(), *relative.split("/")), "rb") as handle:
            return handle.read()
    except OSError:
        return None


def _list_lang_files(notes: Optional[Dict[str, str]] = None) -> List[str]:
    """File names in the package's ``lang`` folder, empty if it cannot be listed.

    ``notes`` is an optional bucket the caller passes in so a failure of the *loader*
    path is not swallowed without a trace: it used to be a bare ``except Exception:
    pass``, which meant a broken loader silently degraded the plugin to raw message
    keys with nothing in the log. The exception is still swallowed (this is a probe --
    it must never take the plugin down) but it is now observable.

    The bucket is filled through the plugin's ``_note`` helper (imported lazily to keep
    this module free of an import cycle), so entries arrive already translated and
    formatted and the caller only has to join them.
    """

    def record(key: str, error: BaseException) -> None:
        if notes is None:
            return
        detail = "{}: {}".format(type(error).__name__, error)
        # Prefer the plugin's ``_note`` so the entry gets translated right away (see the
        # docstring). It is only reachable while the plugin has set up its contextvar;
        # a standalone caller of this module gets the untranslated form, not silence.
        try:
            from . import _note

            _note("language.list_failed", key, api=key, error=detail)
        except TypeError:  # 没有 _note（单独 import 本模块）或没有活动上下文
            notes[key] = "{} failed (api={}, error={})".format(key, key, detail)

    try:
        folder = importlib.resources.files(__package__).joinpath(LANG_DIR_NAME)
        return sorted(entry.name for entry in folder.iterdir())
    except Exception as error:  # noqa: BLE001 - loader quirks fall back to the plain path
        record("importlib.resources", error)
    try:
        return sorted(os.listdir(os.path.join(_filesystem_dir(), LANG_DIR_NAME)))
    except OSError as error:
        record("os.listdir", error)
        return []


def available_languages(notes: Optional[Dict[str, str]] = None) -> List[str]:
    """Every shipped language code, sorted.

    Read from the package rather than hardcoded: dropping in a ``<code>.json`` is enough
    to make it selectable.
    """
    codes = sorted(
        os.path.splitext(name)[0]
        for name in _list_lang_files(notes)
        if name.endswith(".json")
    )
    return codes or [FALLBACK_LANGUAGE]


def normalize(code: Any) -> str:
    """``zh-CN`` / ``ZH_cn`` / ``zh_cn`` are the same request."""
    if not isinstance(code, str):
        return ""
    return code.strip().lower().replace("-", "_")


# Catalogue cache. Keyed by language; also holds failures, so a file that fails to parse
# is reported once rather than on every message.
#
# Guarded by ``_CATALOGS_LOCK``: the main thread reads catalogues while filtering lines
# (``log_matched_lines``) and command threads read/write them at the same time, and a
# plain dict read racing with an insert is not something to leave to CPython's GIL.
_CATALOGS: Dict[str, Catalog] = {}
_CATALOGS_LOCK = threading.Lock()


def relative_catalog_path(language: str) -> str:
    """The catalogue's path as it appears in the repository (for messages).

    Not the on-disk path: inside a packed plugin that would be a path containing
    ``.mcdr``, which tells a user nothing useful.
    """
    return "{}/{}/{}.json".format(__package__, LANG_DIR_NAME, language)


def get_catalog(language: str) -> Catalog:
    """Load ``<language>.json`` (cached). Never raises: failure gives an empty catalog."""
    cached = _CATALOGS.get(language)
    if cached is not None:
        return cached

    data = _resource_bytes("{}/{}.json".format(LANG_DIR_NAME, language))
    if data is None:
        catalog = Catalog({}, "the file is missing from the plugin package")
    else:
        try:
            parsed = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as error:
            catalog = Catalog({}, "{}: {}".format(type(error).__name__, error))
        else:
            if isinstance(parsed, dict):
                # Non-string values would break formatting later; drop them here so the
                # problem surfaces as a readable key instead of a TypeError mid-message.
                catalog = Catalog(
                    {k: v for k, v in parsed.items() if isinstance(v, str)}, None
                )
            else:
                catalog = Catalog({}, "the catalogue must be a JSON object")
    with _CATALOGS_LOCK:
        # ``setdefault`` rather than a plain store: two threads may have raced on the
        # same language, and both results are equally valid -- the first one wins so the
        # cache stays a pure function of the file.
        return _CATALOGS.setdefault(language, catalog)


def clear_cache() -> None:
    """Forget every loaded catalogue (used by the tests)."""
    with _CATALOGS_LOCK:
        _CATALOGS.clear()


def translate(key: str, language: str = FALLBACK_LANGUAGE, **kwargs: Any) -> str:
    """``key`` in ``language`` → the default language → the key itself."""
    template = _find_template(key, language)
    if template is None:
        return key
    if not kwargs:
        return template
    try:
        return template.format(**kwargs)
    except (KeyError, IndexError, ValueError, AttributeError, TypeError):
        # A translation that lost or misspelled a placeholder is a bug in that file, not
        # a reason to fail the caller: show it as written so the mistake is visible.
        # AttributeError matters too: ``{a.b}`` in a translation is a plain typo, and it
        # would otherwise escape this function and contradict the promise above.
        return template


def _find_template(key: str, language: str) -> Optional[str]:
    """The template for ``key`` in ``language``, or the fallback catalogue's, or None."""
    template = get_catalog(language).messages.get(key)
    if template is None and language != FALLBACK_LANGUAGE:
        template = get_catalog(FALLBACK_LANGUAGE).messages.get(key)
    return template


def translate_values(key: str, language: str, values: Dict[str, Any]) -> str:
    """Like ``translate`` but takes the substitutions as a mapping.

    Needed whenever one of the substitutions is **also a parameter name of
    ``translate`` itself** — ``language`` in ``language.detect_failed_summary``. Passing
    it as a keyword to ``translate()`` collides and raises ``TypeError``, while dropping
    it leaves the placeholder unformatted; the mapping form is the way through.
    """
    template = _find_template(key, language)
    if template is None:
        return key
    try:
        return template.format(**values)
    except (KeyError, IndexError, ValueError, AttributeError, TypeError):
        return template


def _match(code: str, available: Sequence[str]) -> Optional[str]:
    """Return the shipped language best representing ``code``, or None."""
    if not code:
        return None
    if code in available:
        return code
    for fallback in FALLBACKS.get(code, ()):
        if fallback in available:
            return fallback
    base = code.split("_")[0]
    for candidate in available:
        if candidate.split("_")[0] == base:
            return candidate
    return None


def _last_resort(available: Sequence[str]) -> str:
    if FALLBACK_LANGUAGE in available:
        return FALLBACK_LANGUAGE
    return available[0] if available else FALLBACK_LANGUAGE


def resolve(
    setting: Any,
    mcdr_language: Any = None,
    available: Optional[Sequence[str]] = None,
    notes: Optional[Dict[str, str]] = None,
) -> Choice:
    """Work out which language to speak (matching rules: docs/DEVELOPMENT.md).

    :param setting: the plugin's ``language`` option — ``auto`` (or blank) follows MCDR.
    :param mcdr_language: MCDR's own language, used when ``setting`` is ``auto``.
    :param available: override the shipped language list (only the tests do this).
    :param notes: optional bucket that collects probe failures (see ``_list_lang_files``).
    """
    languages = (
        list(available) if available is not None else available_languages(notes)
    )
    wanted = normalize(setting)

    if wanted in ("", AUTO):
        # ``auto`` is a deliberate request to defer, so an unsupported MCDR language is
        # resolved silently: the admin never asked for anything specific.
        return Choice(_match(normalize(mcdr_language), languages) or _last_resort(languages), None, {})

    matched = _match(wanted, languages)
    if matched is not None:
        return Choice(matched, None, {})

    fallback = _last_resort(languages)
    return Choice(
        fallback,
        UNSUPPORTED_KEY,
        {"value": str(setting), "fallback": fallback, "options": ", ".join([AUTO] + languages)},
    )
