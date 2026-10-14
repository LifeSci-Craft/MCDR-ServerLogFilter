#!/usr/bin/env python3
"""Verify the packed ``.mcdr``: reproducible, minimal, stripped, and actually loadable.

Five separate things are checked, because each has a different way of going wrong quietly:

* **Reproducibility.** The packer pins zip timestamps and permission bits so that packing the
  same source twice yields byte-identical output, and normalises every shipped text file to LF
  so that the *same tag* packs identically on Windows and on Linux. Both are what lets anyone
  verify a release by rebuilding it from the tag and comparing a hash — a claim worth testing
  rather than believing, since a single stray ``mtime`` or a CRLF silently destroys it. Until
  this tool existed, SLF's own docs asked the packer's owner to do that member-by-member by
  hand, and the packer did not normalise line endings at all.
* **Contents.** Only the plugin payload, the metadata, the licence and the changelog ship. MCDR
  refuses to load an archive containing a root-level module such as ``pack.py`` or
  ``conftest.py``, so a denylist-style packer breaks the release the moment anyone adds a file
  to the repository root — hence allowlist plus this assertion.
* **The changelog.** Exactly one release section, and it names the version being shipped.
* **Stripping.** Comments and docstrings belong to the repository, not to the artifact. The
  stripper is in ``pack.py``, so this asserts it ran rather than trusting it: a packer whose
  stripper quietly stopped working would otherwise only show up as a slightly larger file.
* **Loadability.** The metadata parses, declares the expected id/version/dependency, and every
  ``.py`` inside still compiles after the stripper has been over it.

Usage::

    python tools/check_artifact.py            # build twice into a temp dir and check
    python tools/check_artifact.py path.mcdr  # check an existing artifact (no rebuild)
"""

import argparse
import ast
import hashlib
import io
import json
import re
import sys
import tempfile
import tokenize
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import pack  # noqa: E402

#: Everything that may appear in the archive. Anything else is a bug in the packer.
EXPECTED_ROOT_FILES = {"mcdreforged.plugin.json", "LICENSE", "CHANGELOG.md"}

#: Root-level entries that must never ship. MCDR rejects a packed plugin containing a
#: root-level module, so these are not merely untidy — they break loading.
FORBIDDEN_ROOTS = (
    "README.md",
    "README_en.md",
    "pack.py",
    "conftest.py",
    "tests/",
    "tools/",
    "benchmarks/",
    "docs/",
    ".github/",
    "__pycache__/",
)

#: ``## [1.4.1] - 2026-10-07`` — the repository's changelog spelling.
_VERSION_HEADING = re.compile(r"^##\s+\[?v?([0-9][0-9A-Za-z.\-]*)\]?")


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def check_reproducible(workdir: Path) -> Path:
    """Pack twice and require identical bytes. Returns the first artifact."""
    first = workdir / "first.mcdr"
    second = workdir / "second.mcdr"
    pack.build(first)
    pack.build(second)

    digest_first = sha256(first)
    digest_second = sha256(second)
    assert digest_first == digest_second, (
        "packing the same source twice produced different bytes:\n"
        "  {}\n  {}\n"
        "Something in the zip is not pinned — a timestamp, a permission bit, or the order "
        "of the entries.".format(digest_first, digest_second)
    )
    print("reproducible    : {}".format(digest_first))
    return first


def check_line_endings_are_normalised(workdir: Path) -> None:
    """Packing CRLF sources must produce the same bytes as packing LF ones.

    The working tree's line endings depend on the operating system and on whether some tool
    rewrote a file outside git's ``.gitattributes`` filter. If the artifact inherited them, the
    same tag would hash differently on Windows and on Linux, and "rebuild it and compare the
    sha256" would stop being a verification — which is exactly what SLF's packer used to do
    (a CRLF tree hashed to ``14de8732…`` where the LF tree hashed to ``08231bdf…``, and the
    compensating procedure was a paragraph in ``docs/DEVELOPMENT.md`` asking a human to compare
    members by hand). So the shipped text files are rewritten to CRLF here, packed again, and
    the two artifacts are required to be identical — then put back.
    """
    baseline = sha256(pack.build(workdir / "lf.mcdr"))

    targets = [path for path in pack.collect() if path.suffix in pack.TEXT_SUFFIXES]
    assert targets, "no text files are shipped, so this check would pass vacuously"
    saved = {path: path.read_bytes() for path in targets}
    try:
        for path in targets:
            ending = saved[path].replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")
            assert b"\r\n" in ending, str(path)
            path.write_bytes(ending)
        crlf_digest = sha256(pack.build(workdir / "crlf.mcdr"))
    finally:
        for path, data in saved.items():
            path.write_bytes(data)

    assert baseline == crlf_digest, (
        "the artifact depends on the source's line endings:\n"
        "  LF   {}\n  CRLF {}\n"
        "packaged_bytes() is supposed to normalise them away.".format(baseline, crlf_digest)
    )
    print("line endings    : CRLF sources pack identically")


def check_contents(archive_path: Path) -> None:
    """Only the payload ships, and only from inside the plugin package."""
    with zipfile.ZipFile(archive_path) as archive:
        names = sorted(archive.namelist())
        assert names, "the archive is empty"

        found = set(names)
        assert "mcdreforged.plugin.json" in found, "mcdreforged.plugin.json is missing"

        for prefix in FORBIDDEN_ROOTS:
            offenders = [name for name in names if name.startswith(prefix)]
            assert not offenders, "{} should not be shipped: {}".format(prefix, offenders)

        roots = {name for name in names if "/" not in name}
        unexpected = roots - EXPECTED_ROOT_FILES
        assert not unexpected, "unexpected root entries: {}".format(sorted(unexpected))

        stray = [
            name
            for name in names
            if name.endswith(".py") and not name.startswith("server_log_filter/")
        ]
        assert not stray, "python files outside the plugin package: {}".format(stray)

        # The catalogues have to travel with the code, or every message degrades to its key.
        catalogues = [name for name in names if name.startswith("server_log_filter/lang/")]
        assert catalogues, "no language catalogues in the archive"

        # And no shipped text may carry a CRLF, for the reason argued in
        # check_line_endings_are_normalised().
        for name in names:
            if not name.endswith((".json", ".md", ".py")):
                continue
            body = archive.read(name)
            assert b"\r\n" not in body, "{} ships CRLF line endings".format(name)

        print("files           : {}".format(len(names)))
        for name in names:
            print("  {:52} {:>7} B".format(name, archive.getinfo(name).file_size))


def check_changelog(archive_path: Path) -> None:
    """The shipped changelog carries exactly one release, and it is the one being shipped."""
    with zipfile.ZipFile(archive_path) as archive:
        if "CHANGELOG.md" not in archive.namelist():
            raise AssertionError("CHANGELOG.md is not shipped")
        text = archive.read("CHANGELOG.md").decode("utf-8")
        headings = [line for line in text.splitlines() if _VERSION_HEADING.match(line)]
        assert len(headings) == 1, (
            "the shipped changelog carries {} release sections, not one: {}".format(
                len(headings), headings[:4]
            )
        )
        metadata = json.loads(archive.read("mcdreforged.plugin.json").decode("utf-8"))
        version = metadata.get("version", "")
        in_changelog = _VERSION_HEADING.match(headings[0]).group(1)
        assert in_changelog == version, (
            "the shipped changelog is {} but the plugin version is {}".format(
                in_changelog, version
            )
        )
        print("changelog       : {} only ({} B)".format(
            headings[0].strip(), len(text.encode("utf-8"))))


def check_stripped(archive_path: Path) -> None:
    """No comment and no docstring survives into the artifact."""
    with zipfile.ZipFile(archive_path) as archive:
        for name in archive.namelist():
            if not name.endswith(".py"):
                continue
            source = archive.read(name).decode("utf-8")

            for token in tokenize.generate_tokens(io.StringIO(source).readline):
                assert token.type != tokenize.COMMENT, "{} ships a comment on line {}: {}".format(
                    name, token.start[0], token.string
                )

            for node in ast.walk(ast.parse(source)):
                if not isinstance(
                    node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
                ) or not node.body:
                    continue
                first = node.body[0]
                if (
                    isinstance(first, ast.Expr)
                    and isinstance(first.value, ast.Constant)
                    and isinstance(first.value.value, str)
                ):
                    raise AssertionError(
                        "{} ships a docstring on line {}".format(name, first.lineno)
                    )
        print("stripped        : no comments, no docstrings")


def check_loadable(archive_path: Path) -> None:
    """Metadata parses and every shipped module still compiles after stripping."""
    with zipfile.ZipFile(archive_path) as archive:
        metadata = json.loads(archive.read("mcdreforged.plugin.json").decode("utf-8"))
        assert metadata.get("id"), metadata
        assert metadata.get("version"), metadata
        assert metadata.get("dependencies", {}).get("mcdreforged"), (
            "no mcdreforged dependency declared: {}".format(metadata)
        )
        print("plugin id       : {} v{}".format(metadata["id"], metadata["version"]))
        print("mcdreforged dep : {}".format(metadata["dependencies"]["mcdreforged"]))

        for name in archive.namelist():
            if not name.endswith(".py"):
                continue
            source = archive.read(name).decode("utf-8")
            try:
                compile(source, name, "exec")
            except SyntaxError as error:
                raise AssertionError(
                    "{} does not compile inside the archive: {}".format(name, error)
                ) from error
        print("modules compile : all .py entries")


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("artifact", nargs="?", help="an existing .mcdr; built here if omitted")
    args = parser.parse_args()

    if args.artifact:
        path = Path(args.artifact).resolve()
        if not path.is_file():
            raise SystemExit("no such file: {}".format(path))
        print("checking        : {}".format(path))
        check_contents(path)
        check_stripped(path)
        check_loadable(path)
        check_changelog(path)
    else:
        with tempfile.TemporaryDirectory(prefix="slf_artifact_") as folder:
            workdir = Path(folder)
            path = check_reproducible(workdir)
            check_line_endings_are_normalised(workdir)
            check_contents(path)
            check_stripped(path)
            check_loadable(path)
            check_changelog(path)

    print()
    print("all artifact checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
