#!/usr/bin/env python3
"""Build the distributable ``.mcdr`` package for Server Log Filter.

Usage::

    python pack.py [output.mcdr]

The packer is deliberately **allowlist-based**. MCDR validates the root entries of a
packed plugin (``PackedPlugin._check_dir_legality``) and refuses to load an archive that
contains a root-level module such as ``conftest.py`` or ``setup.py``. A denylist-style
``rglob("*")`` packer therefore breaks the release as soon as anyone adds a file to the
repository root, and it also sweeps in ``.testlibs/`` (the whole MCDR distribution) once
the test dependencies are installed.

Only these are shipped:

* ``mcdreforged.plugin.json`` — package metadata (required)
* ``server_log_filter/**.py`` — the plugin code, recursively (submodules included)
* ``server_log_filter/lang/*.json`` — the message catalogues (one file per language)
* ``LICENSE``, ``CHANGELOG.md`` — licence text and the shipped changelog

The ``.py`` files are shipped with their comments and docstrings blanked out
(``packaged_source()``): the repository keeps them, the artifact does not need them, and
they were about an eighth of the artifact. Line numbers are preserved, so a traceback
from the installed plugin still points at the right line of the repository file.

``README.md`` / ``README_en.md`` are deliberately **excluded**: they are long, they
duplicate what the release page already says, and MCDR never reads them. Keeping
them out cuts roughly half off the artifact. ``server_log_filter/lang/README.md``
— the contributor guide for translators — is excluded for the same reason.

``pack.py`` itself is intentionally **not** included, for the same root-module reason.
"""

import ast
import io
import json
import re
import sys
import tokenize
import zipfile
from pathlib import Path

SRC = Path(__file__).resolve().parent

# Zip entries carry a timestamp. Taking it from the file's mtime makes the artifact
# unreproducible: packing the same source twice gives two different hashes, so nobody
# can verify a release by rebuilding it from the tag and comparing sha256.
# 1980-01-01 00:00 is the earliest date a zip can store, and the usual choice for
# "this timestamp carries no information".
FIXED_ZIP_TIMESTAMP = (1980, 1, 1, 0, 0, 0)

#: Trimmed to its latest section on the way into the artifact (see ``packaged_changelog``).
CHANGELOG_NAME = "CHANGELOG.md"

# Root-level files that ship with the plugin.
#
# README.md / README_en.md are intentionally absent: MCDR never reads them, they
# duplicate the release page, and together they were over half the artifact size.
ROOT_FILES = {
    "mcdreforged.plugin.json",
    "LICENSE",
    CHANGELOG_NAME,
}

# Code package: same name as the plugin id.
PACKAGE_NAME = "server_log_filter"

# Data inside the package that ships alongside the code. The message catalogues must
# travel with the plugin — without them every message degrades to its raw key — and
# keeping them as separate .json files is what lets a translator add a language
# without touching Python.
PACKAGE_DATA_DIRS = {
    "lang": (".json",),
}

#: Entries packed as text, and therefore normalised to LF.
#:
#: ``.py`` is not listed because ``packaged_source`` already rebuilds it line by line. The rest
#: are copied as bytes, and their line endings in the working tree depend on the operating
#: system and on whether anything rewrote them outside git's ``.gitattributes`` filter — which
#: is how a Windows pack and a Linux pack of the same tag end up with different hashes, and how
#: "rebuild it from the tag and compare" quietly stops working. Normalising here means the
#: artifact depends on the source, not on the machine that packed it.
TEXT_SUFFIXES = (".json", ".md")


def _is_package_payload(rel: Path) -> bool:
    """True for files inside the plugin package that belong in the artifact."""
    if rel.parts[0] != PACKAGE_NAME:
        return False
    if rel.suffix == ".py":
        # Every module, at any depth: a silently dropped submodule would produce an
        # artifact that imports fine here and explodes on the user's machine.
        return True
    # A data directory such as ``lang/``: only the extensions we asked for, and only
    # directly inside it. ``lang/README.md`` (the translator guide) stays out.
    if len(rel.parts) == 3 and rel.parts[1] in PACKAGE_DATA_DIRS:
        return rel.suffix in PACKAGE_DATA_DIRS[rel.parts[1]]
    return False


def plugin_version() -> str:
    with open(SRC / "mcdreforged.plugin.json", encoding="utf-8") as fh:
        return json.load(fh)["version"]


def collect() -> list:
    """Return the sorted list of files to ship."""
    files = []
    for path in SRC.rglob("*"):
        if not path.is_file():
            continue
        if "__pycache__" in path.parts or ".git" in path.parts:
            continue
        if path.suffix == ".pyc":
            continue
        rel = path.relative_to(SRC)
        if rel.parent == Path(".") and rel.name in ROOT_FILES:
            files.append(path)
        elif rel.parts and _is_package_payload(rel):
            # Recursive by design: checking ``rel.parts[0]`` (not ``rel.parent``) is what
            # lets ``server_log_filter/sub/helper.py`` and ``server_log_filter/lang/x.json``
            # ship too. Using ``rel.parent`` would silently drop them and produce a
            # broken release.
            files.append(path)
    return sorted(files)


def _blank_comments(source: str) -> str:
    """Replace every ``#`` comment with nothing, **keeping the line itself**.

    Uses :mod:`tokenize` rather than a regex, so a ``#`` inside a string literal (a URL,
    a regex, a colour code) is left alone. Blanking instead of deleting keeps line
    numbers identical to the repository file.
    """
    lines = source.splitlines(keepends=True)
    for token in tokenize.generate_tokens(io.StringIO(source).readline):
        if token.type == tokenize.COMMENT:
            row, col = token.start
            line = lines[row - 1]
            newline = "\n" if line.endswith("\n") else ""
            lines[row - 1] = line[:col].rstrip() + newline
    return "".join(lines)


def _blank_docstrings(source: str) -> str:
    """Same treatment for docstrings.

    A body whose only statement was the docstring gets ``pass`` instead, otherwise the
    result would not compile.
    """
    tree = ast.parse(source)
    lines = source.splitlines(keepends=True)
    for node in ast.walk(tree):
        if not isinstance(
            node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
        ):
            continue
        body = node.body
        if not body:
            continue
        first = body[0]
        if not (
            isinstance(first, ast.Expr)
            and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)
        ):
            continue
        # ``def f(): "doc"`` puts the docstring on the header line; blanking that line
        # would delete the definition. Leave such (rare) forms untouched.
        header_line = getattr(node, "lineno", None)
        if header_line is not None and first.lineno == header_line:
            continue
        lines[first.lineno - 1] = (
            " " * first.col_offset + "pass\n" if len(body) == 1 else "\n"
        )
        for index in range(first.lineno, first.end_lineno):
            lines[index] = "\n"
    return "".join(lines)


def packaged_source(path: Path) -> bytes:
    """The bytes to ship for one ``.py`` file: comments and docstrings blanked out.

    Line numbers are preserved on purpose — an exception from the installed plugin then
    reports the same line as the repository file it came from.

    The result is compiled once here: shipping a file that does not parse would only be
    discovered by a user, and this is the last place that can catch it.
    """
    source = path.read_text(encoding="utf-8")
    stripped = _blank_comments(_blank_docstrings(source))
    try:
        compile(stripped, str(path), "exec")
    except SyntaxError as error:  # pragma: no cover - a bug in the stripper
        raise SystemExit(
            "stripping {} produced invalid code: {}".format(path, error)
        ) from error
    return stripped.encode("utf-8")


def packaged_bytes(path: Path) -> bytes:
    """The bytes to ship for one non-Python file, with line endings normalised.

    See :data:`TEXT_SUFFIXES` for why the normalisation has to happen here rather than being
    left to the working tree.
    """
    data = path.read_bytes()
    if path.suffix in TEXT_SUFFIXES:
        return data.replace(b"\r\n", b"\n")
    return data


#: ``## [1.4.1] - 2026-10-07`` or ``## v1.6.0`` — both spellings name the release.
_CHANGELOG_HEADING = re.compile(r"^##\s+\[?v?([0-9][0-9A-Za-z.\-]*)\]?")


def packaged_changelog(path: Path) -> bytes:
    """The changelog as it ships: **only the latest release section**.

    The artifact is supposed to carry one release's notes (older ones live on the release
    page), and the repository file is allowed to keep history as soon as that promise is
    enforced here rather than by hand. The guard makes an empty or unparsable changelog fail
    the pack instead of shipping silently.
    """
    text = packaged_bytes(path).decode("utf-8")
    lines = text.splitlines(keepends=True)
    headings = [index for index, line in enumerate(lines) if _CHANGELOG_HEADING.match(line)]
    if not headings:
        raise SystemExit("{} carries no '## <version>' section".format(path.name))
    if len(headings) > 1:
        text = "".join(lines[: headings[1]])
    if sum(1 for line in text.splitlines() if _CHANGELOG_HEADING.match(line)) != 1:
        raise SystemExit("{} did not trim to a single release".format(path.name))
    return text.encode("utf-8")


def build(out_path: Path) -> Path:
    files = collect()
    if not files:
        raise SystemExit("nothing to pack; is mcdreforged.plugin.json present?")
    mandatory = SRC / "mcdreforged.plugin.json"
    if mandatory not in files:
        raise SystemExit("mcdreforged.plugin.json must be in the package")
    if not any(p.parent == SRC / PACKAGE_NAME for p in files):
        raise SystemExit(f"{PACKAGE_NAME}/ contains no .py files")
    # Without a catalogue every message degrades to its raw key, and the artifact still
    # loads — so the failure would only be noticed by a user.
    if not any(
        p.suffix == ".json" and p.parent.name in PACKAGE_DATA_DIRS for p in files
    ):
        raise SystemExit("no language catalogues found; every message would be a raw key")

    # 压缩级别 9：只影响打包耗时。deflate 的**解压**速度与压缩级别无关
    # （格式没变，只是编码端多花点力气），所以对用户是零开销。
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for path in files:
            rel = path.relative_to(SRC).as_posix()
            # Comments stay in the repository, where they are useful to read; the artifact
            # only needs the code. Everything else is normalised to LF so the bytes depend
            # on the source rather than on the machine that packed them, and the changelog
            # is trimmed to the release being shipped.
            if path.suffix == ".py":
                data = packaged_source(path)
            elif path.name == CHANGELOG_NAME:
                data = packaged_changelog(path)
            else:
                data = packaged_bytes(path)
            # Hand-built ZipInfo rather than zf.write(): mtime, permission bits and host
            # system all have to be pinned, or the same source packs into different bytes
            # on a different day (or a different OS).
            info = zipfile.ZipInfo(rel, date_time=FIXED_ZIP_TIMESTAMP)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 3              # unix
            info.external_attr = 0o644 << 16    # regular file, rw-r--r--
            zf.writestr(info, data)
    return out_path


def main() -> int:
    if len(sys.argv) > 1:
        out_path = Path(sys.argv[1]).resolve()
    else:
        out_path = SRC / f"ServerLogFilter-v{plugin_version()}.mcdr"

    build(out_path)
    size_kib = out_path.stat().st_size / 1024
    print(f"packed {len(collect())} files -> {out_path} ({size_kib:.1f} KiB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
