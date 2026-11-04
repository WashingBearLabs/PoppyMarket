"""Structural validator for the theme registry.

Standalone and stdlib-only: it runs as a plain script in the registry
repository and is imported as a module by the test suite.

Finding messages are fixed strings. They never echo input values, because an
entry may carry credentials in a URL.
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import socket
import stat
import subprocess
import sys
import time as time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from email.message import Message
from pathlib import Path, PurePosixPath
from typing import Protocol, cast
from urllib.parse import urlparse

VALIDATOR_NAME = "poppymarket-validate-registry"
VALIDATOR_VERSION = "1.0.0"
THEME_REPO_OWNER = "WashingBearLabs"

MAX_REGISTRY_DOCUMENT_BYTES = 1024 * 1024
MAX_REGISTRY_ENTRIES_PER_SOURCE = 500
# App / theme identifier format (a DNS label). Matched with ``fullmatch``.
APP_NAME_SRC = r"[a-z0-9][a-z0-9-]{0,62}"
APP_NAME_PATTERN = re.compile(APP_NAME_SRC)

# Mirrors of Poppy's RegistryEntry rules. The literals are copied, never
# imported (this file is stdlib-only), and a Poppy-side test pins each one.
# poppy/core/apps/asset_types.py COMMIT_SHA_PATTERN; used with ``fullmatch``.
COMMIT_SHA_PATTERN = re.compile(r"[0-9a-f]{40}")
# poppy/core/apps/registry.py _THEME_TEXT_CAPS (``len()`` in code points).
THEME_TEXT_CAPS = {
    "display_name": 64,
    "author": 64,
    "description": 500,
    "long_description": 4000,
    "version": 64,
    "category": 40,
}
MAX_THEME_TAGS = 20
MAX_THEME_TAG_LENGTH = 32
# registry.py _ASSET_PATH_FORBIDDEN; used with ``search``.
ASSET_PATH_FORBIDDEN = re.compile(r"[\\:?#%\s\x00-\x1f\x7f]")
MAX_ASSET_PATH_LENGTH = 255
# theme_repo_hosts.py _THEME_REPO_PATH; used with ``fullmatch``.
THEME_REPO_PATH = re.compile(r"/[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}")
THEME_PREVIEW_EXTENSIONS = frozenset({".png", ".jpg", ".jpeg", ".webp"})
ALLOWED_ASSET_EXTENSIONS = frozenset({".png", ".svg", ".jpg", ".jpeg"})
# theme_repo_hosts.py DEFAULT_THEME_REPO_HOSTS.
MAX_REGISTRY_ASSET_BYTES = 2 * 1024 * 1024
MAX_REGISTRY_ASSET_BYTES_PER_FETCH = 64 * 1024 * 1024
# kind -> (asset fields, allowed extensions); mirrors Poppy's _ASSET_FIELDS_BY_KIND.
ASSET_FIELDS_BY_KIND: dict[str, tuple[tuple[str, ...], frozenset[str]]] = {
    "theme": (("preview_image",), THEME_PREVIEW_EXTENSIONS),
    "app": (("icon", "hero_image"), ALLOWED_ASSET_EXTENSIONS),
}

_THEME_REPO_HOSTS = frozenset({"github.com"})

# PoppyMarket-stricter mirrors (copied, never imported; US-010 pins each).
# poppy/core/themes/manifest.py THEME_SEMVER_PATTERN, used with ``fullmatch``;
# ``re.ASCII`` keeps ``\d`` to [0-9] as the JSON Schema does.
SEMVER_PATTERN = re.compile(
    r"^\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$", re.ASCII
)
# manifest.py RESERVED_THEME_IDS + marketplace.py legacy aliases + the
# built-in theme directories (``reserved_theme_ids(BUILTIN_THEMES_DIR)``).
RESERVED_THEME_IDS = frozenset(
    {
        "active",
        "default",
        "marketplace",
        "installed",
        "files",
        "sdk",
        "dark",
        "paper",
        "poppy-glass",
    }
)
# manifest.py RESERVED_THEME_ID_PREFIXES.
RESERVED_THEME_ID_PREFIXES: tuple[str, ...] = ("wb-",)
# Mirror of poppy/core/workbench/release_text.py PUBLIC_TEXT_REFUSED (WP-C19).
# Read with ``ast`` by Poppy's parity test, so it stays one top-level annotated
# assignment of a set display of hex integer literals. All of Cc (U+0000-001F,
# U+007F-009F), the bidi embeddings/overrides/isolates (U+202A-202E,
# U+2066-2069), U+200B, U+FEFF, U+2028 and U+2029.
REFUSED_CODEPOINTS: frozenset[int] = frozenset(
    {
        0x0000, 0x0001, 0x0002, 0x0003, 0x0004, 0x0005, 0x0006, 0x0007,
        0x0008, 0x0009, 0x000A, 0x000B, 0x000C, 0x000D, 0x000E, 0x000F,
        0x0010, 0x0011, 0x0012, 0x0013, 0x0014, 0x0015, 0x0016, 0x0017,
        0x0018, 0x0019, 0x001A, 0x001B, 0x001C, 0x001D, 0x001E, 0x001F,
        0x007F, 0x0080, 0x0081, 0x0082, 0x0083, 0x0084, 0x0085, 0x0086,
        0x0087, 0x0088, 0x0089, 0x008A, 0x008B, 0x008C, 0x008D, 0x008E,
        0x008F, 0x0090, 0x0091, 0x0092, 0x0093, 0x0094, 0x0095, 0x0096,
        0x0097, 0x0098, 0x0099, 0x009A, 0x009B, 0x009C, 0x009D, 0x009E,
        0x009F,
        0x200B, 0xFEFF, 0x2028, 0x2029,
        0x202A, 0x202B, 0x202C, 0x202D, 0x202E,
        0x2066, 0x2067, 0x2068, 0x2069,
    }
)  # fmt: skip

_THEME_ALLOWED_KEYS = frozenset(
    {
        "name",
        "kind",
        "display_name",
        "description",
        "long_description",
        "version",
        "author",
        "repo_url",
        "ref",
        "preview_image",
        "tags",
        "category",
    }
)
# Poppy fields a theme must not carry, named in the finding; any other extra
# key is reported under UNKNOWN_FIELD so its name is never echoed.
_THEME_FORBIDDEN_NAMED = (
    "image",
    "port",
    "icon",
    "hero_image",
    "manifest_url",
    "capabilities",
    "use_cases",
)
_DISPLAY_TEXT_FIELDS = (
    "display_name",
    "description",
    "long_description",
    "author",
    "category",
)

# Document codes.
DOC_UNREADABLE = "DOC_UNREADABLE"
DOC_TOO_LARGE = "DOC_TOO_LARGE"
DOC_NOT_UTF8 = "DOC_NOT_UTF8"
DOC_INVALID_JSON = "DOC_INVALID_JSON"
DOC_DUPLICATE_KEY = "DOC_DUPLICATE_KEY"
DOC_SHAPE = "DOC_SHAPE"
DOC_TOO_MANY_ENTRIES = "DOC_TOO_MANY_ENTRIES"
DOC_NOT_CANONICAL = "DOC_NOT_CANONICAL"

# Entry codes.
ENTRY_NOT_OBJECT = "ENTRY_NOT_OBJECT"
ENTRY_DUPLICATE = "ENTRY_DUPLICATE"
ENTRY_NAME_CONFLICT = "ENTRY_NAME_CONFLICT"
ENTRY_FIELD_MISSING = "ENTRY_FIELD_MISSING"
ENTRY_FIELD_TYPE = "ENTRY_FIELD_TYPE"
ENTRY_FIELD_INVALID = "ENTRY_FIELD_INVALID"
ENTRY_FIELD_FORBIDDEN = "ENTRY_FIELD_FORBIDDEN"
ENTRY_NAME_RESERVED = "ENTRY_NAME_RESERVED"

FILE_MISSING = "FILE_MISSING"
FILE_NOT_REGULAR = "FILE_NOT_REGULAR"
FILE_TOO_LARGE = "FILE_TOO_LARGE"
FILE_CONTENT_MISMATCH = "FILE_CONTENT_MISMATCH"
DOC_ASSETS_TOO_LARGE = "DOC_ASSETS_TOO_LARGE"

UNKNOWN_FIELD = "<unknown>"
_TOP_LEVEL_KEYS = ("version", "updated_at", "apps")
_REQUIRED_TEXT_FIELDS = ("name", "description", "version", "author")
_KINDS = ("app", "theme")
_OPTIONAL_STRING_FIELDS = (
    "display_name",
    "long_description",
    "category",
    "repo_url",
    "ref",
    "image",
    "icon",
    "hero_image",
    "preview_image",
    "manifest_url",
)
_APP_SOURCES = ("image", "repo_url")
_LIST_FIELDS = ("tags", "capabilities", "use_cases")
_DATE_PATTERN = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")


@dataclass(frozen=True)
class Finding:
    """One validation problem. ``message`` never contains input values."""

    code: str
    index: int | None
    entry: str | None
    field: str | None
    message: str


@dataclass(frozen=True)
class Report:
    """Result of validating one registry document."""

    findings: list[Finding]
    entries_checked: int = 0

    @property
    def ok(self) -> bool:
        return not self.findings


class _DuplicateKeyError(Exception):
    """Raised by the JSON object hook when an object repeats a key."""


def _object_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateKeyError
        result[key] = value
    return result


def _reject_constant(name: str) -> object:
    raise ValueError("non-finite number" if name else "")


def _doc_finding(code: str, field: str | None, message: str) -> Finding:
    return Finding(code, None, None, field, message)


def _entry_kind(entry: dict[str, object]) -> str | None:
    """The effective kind, or None when ``kind`` is present but invalid."""
    if "kind" not in entry:
        return "app"
    kind = entry["kind"]
    if isinstance(kind, str) and kind in _KINDS:
        return kind
    return None


def validate_registry_asset_path(path: str, extensions: frozenset[str]) -> str:
    """Return ``path`` or raise ``ValueError`` (mirrors Poppy's asset-path rule).

    The error text is fixed and never echoes the path.
    """
    if not path:
        raise ValueError("path must not be empty")
    if len(path) > MAX_ASSET_PATH_LENGTH:
        raise ValueError("path is too long")
    if path.startswith("/"):
        raise ValueError("path must be relative")
    if ASSET_PATH_FORBIDDEN.search(path):
        raise ValueError("path contains a forbidden character")
    for segment in path.split("/"):
        if not segment:
            raise ValueError("path must not contain empty segments")
        if segment.startswith("."):
            raise ValueError("path segments must not start with '.'")
    if PurePosixPath(path).suffix.lower() not in extensions:
        raise ValueError("path has a disallowed extension")
    return path


def _theme_repo_url_problem(url: str) -> str | None:
    """Mirror ``validate_theme_repo_url``; return a fixed message or None."""
    if "?" in url or "#" in url:
        return "Repository URL must not carry a query or fragment."
    try:
        parsed = urlparse(url)
        port = parsed.port
    except ValueError:
        return "Repository URL could not be parsed."
    if parsed.scheme != "https":
        return "Repository URL must use https."
    host = parsed.hostname
    if (
        not host
        or host not in _THEME_REPO_HOSTS
        or parsed.netloc != parsed.netloc.lower()
    ):
        return "Repository host is not allowed."
    if (
        parsed.username is not None
        or parsed.password is not None
        or "@" in parsed.netloc
    ):
        return "Repository URL must not contain userinfo."
    if port is not None or parsed.netloc.endswith(":"):
        return "Repository URL must not specify a port."
    path = parsed.path
    if not THEME_REPO_PATH.fullmatch(path) or any(
        seg in (".", "..") for seg in path.split("/")
    ):
        return "Repository path must be exactly /<owner>/<repo>."
    return None


def validate_entry(entry: object, *, org: str = THEME_REPO_OWNER) -> list[Finding]:
    """Check one registry entry. Pure: no file or network access."""
    if not isinstance(entry, dict):
        return [
            Finding(ENTRY_NOT_OBJECT, None, None, None, "Entry must be a JSON object.")
        ]
    item: dict[str, object] = entry  # pyright: ignore[reportUnknownVariableType]
    findings: list[Finding] = []

    kind = _entry_kind(item)
    name = item.get("name")
    label: str | None = None
    if kind is not None and isinstance(name, str) and APP_NAME_PATTERN.fullmatch(name):
        label = f"{kind}:{name}"

    def add(code: str, field: str, message: str) -> None:
        findings.append(Finding(code, None, label, field, message))

    if "kind" in item:
        raw_kind = item["kind"]
        if not isinstance(raw_kind, str):
            add(ENTRY_FIELD_TYPE, "kind", "Field must be a string.")
        elif raw_kind not in _KINDS:
            add(ENTRY_FIELD_INVALID, "kind", "Field must be 'app' or 'theme'.")

    for field in _REQUIRED_TEXT_FIELDS:
        if field not in item:
            add(ENTRY_FIELD_MISSING, field, "Required field is missing.")
            continue
        value = item[field]
        if not isinstance(value, str):
            add(ENTRY_FIELD_TYPE, field, "Field must be a string.")
        elif value == "":
            add(ENTRY_FIELD_INVALID, field, "Field must not be empty.")
        elif field == "name" and not APP_NAME_PATTERN.fullmatch(value):
            add(
                ENTRY_FIELD_INVALID,
                field,
                "Name must be 1-63 lowercase letters, digits or hyphens.",
            )

    _check_shared_entry_rules(item, kind, add)
    _check_policy_rules(item, kind, org, add)
    return findings


def _check_shared_entry_rules(
    item: dict[str, object],
    kind: str | None,
    add: Callable[[str, str, str], None],
) -> None:
    """The remaining ``RegistryEntry`` rules (types first, then semantics)."""
    typed_ok: set[str] = set()
    for field in _OPTIONAL_STRING_FIELDS:
        if field not in item:
            continue
        if item[field] is None or isinstance(item[field], str):
            typed_ok.add(field)
        else:
            add(ENTRY_FIELD_TYPE, field, "Field must be a string or null.")
    lists: dict[str, list[str]] = {}
    for field in _LIST_FIELDS:
        if field not in item:
            lists[field] = []
            continue
        raw = item[field]
        items: list[object] = raw if isinstance(raw, list) else []  # pyright: ignore[reportUnknownVariableType]
        strings = [x for x in items if isinstance(x, str)]
        if isinstance(raw, list) and len(strings) == len(items):
            lists[field] = strings
        else:
            add(ENTRY_FIELD_TYPE, field, "Field must be an array of strings.")
    if "port" in item:
        port = item["port"]
        if not isinstance(port, int) or isinstance(port, bool):
            add(ENTRY_FIELD_TYPE, "port", "Field must be an integer.")

    def text(field: str) -> str | None:
        value = item.get(field)
        return value if field in typed_ok and isinstance(value, str) else None

    ref = text("ref")
    if ref is not None and not COMMIT_SHA_PATTERN.fullmatch(ref):
        add(ENTRY_FIELD_INVALID, "ref", "Ref must be 40 lowercase hex characters.")

    if kind == "app":
        sources_typed = all(f in typed_ok or f not in item for f in _APP_SOURCES)
        if sources_typed and not text("image") and not text("repo_url"):
            add(ENTRY_FIELD_MISSING, "image", "An app needs an image or a repo_url.")
        return
    if kind != "theme":
        return

    repo_url = text("repo_url")
    if "repo_url" in typed_ok:
        if not repo_url:
            add(ENTRY_FIELD_MISSING, "repo_url", "A theme requires a repo_url.")
        else:
            problem = _theme_repo_url_problem(repo_url)
            if problem is not None:
                add(ENTRY_FIELD_INVALID, "repo_url", problem)
    elif "repo_url" not in item:
        add(ENTRY_FIELD_MISSING, "repo_url", "A theme requires a repo_url.")
    if item.get("ref") is None:
        add(ENTRY_FIELD_MISSING, "ref", "A theme requires a ref.")
    preview = text("preview_image")
    if preview is not None:
        try:
            validate_registry_asset_path(preview, THEME_PREVIEW_EXTENSIONS)
        except ValueError:
            add(ENTRY_FIELD_INVALID, "preview_image", "Preview path is not valid.")
    for field, cap in THEME_TEXT_CAPS.items():
        value = item.get(field)
        if isinstance(value, str) and len(value) > cap:
            add(ENTRY_FIELD_INVALID, field, "Field is longer than its limit.")
    tags = lists.get("tags")
    if tags is not None and (
        len(tags) > MAX_THEME_TAGS or any(len(t) > MAX_THEME_TAG_LENGTH for t in tags)
    ):
        add(ENTRY_FIELD_INVALID, "tags", "Too many tags or a tag is too long.")


def _has_refused(value: str) -> bool:
    return any(ord(ch) in REFUSED_CODEPOINTS for ch in value)


def _tag_version_problem(version: str) -> bool:
    """True when ``v<version>`` would fail Poppy's ref-format rules."""
    return ".." in version or version.endswith((".", ".lock"))


def _check_policy_rules(
    item: dict[str, object],
    kind: str | None,
    org: str,
    add: Callable[[str, str, str], None],
) -> None:
    """PoppyMarket rules stricter than ``RegistryEntry``."""
    for key in ("source", "_source"):
        if key in item:
            add(ENTRY_FIELD_FORBIDDEN, key, "Entries must not carry this field.")
    if kind not in _KINDS:
        return

    def string(field: str) -> str | None:
        value = item.get(field)
        return value if isinstance(value, str) else None

    for field in _DISPLAY_TEXT_FIELDS:
        value = string(field)
        if value is not None and _has_refused(value):
            add(ENTRY_FIELD_INVALID, field, "Field contains a refused character.")
    tags = item.get("tags")
    if isinstance(tags, list) and any(
        isinstance(t, str) and _has_refused(t)
        for t in tags  # pyright: ignore[reportUnknownVariableType]
    ):
        add(ENTRY_FIELD_INVALID, "tags", "A tag contains a refused character.")

    if kind == "app":
        _check_app_policy(item, add)
        return

    name = string("name")
    valid_name = name if name and APP_NAME_PATTERN.fullmatch(name) else None
    if valid_name is not None and (
        valid_name in RESERVED_THEME_IDS
        or valid_name.startswith(RESERVED_THEME_ID_PREFIXES)
    ):
        add(ENTRY_NAME_RESERVED, "name", "Theme name is reserved.")

    unknown = False
    for key in item:
        if key in _THEME_ALLOWED_KEYS or key in ("source", "_source"):
            continue
        if key in _THEME_FORBIDDEN_NAMED:
            add(ENTRY_FIELD_FORBIDDEN, key, "A theme must not set this field.")
        else:
            unknown = True
    if unknown:
        add(
            ENTRY_FIELD_FORBIDDEN,
            UNKNOWN_FIELD,
            "Entry has a field a theme may not set.",
        )

    repo_url = string("repo_url")
    if (
        repo_url
        and valid_name is not None
        and _theme_repo_url_problem(repo_url) is None
    ):
        expected = f"https://github.com/{org}/poppy-theme-{valid_name}"
        if repo_url not in (expected, expected + ".git"):
            add(
                ENTRY_FIELD_INVALID,
                "repo_url",
                "Repository must be the owner's poppy-theme-<name> repository.",
            )

    if item.get("preview_image") is None:
        add(ENTRY_FIELD_MISSING, "preview_image", "A theme requires a preview_image.")
    else:
        preview = string("preview_image")
        if preview is not None and valid_name is not None:
            allowed = {
                f"themes/{valid_name}/preview{ext}" for ext in THEME_PREVIEW_EXTENSIONS
            }
            if preview not in allowed:
                # The generic path rule already reported malformed paths.
                try:
                    validate_registry_asset_path(preview, THEME_PREVIEW_EXTENSIONS)
                except ValueError:
                    pass
                else:
                    add(
                        ENTRY_FIELD_INVALID,
                        "preview_image",
                        "Preview must be themes/<name>/preview.<ext>.",
                    )

    version = string("version")
    if version and (
        not SEMVER_PATTERN.fullmatch(version) or _tag_version_problem(version)
    ):
        add(ENTRY_FIELD_INVALID, "version", "Version is not a valid release.")


def _check_app_policy(
    item: dict[str, object], add: Callable[[str, str, str], None]
) -> None:
    port = item.get("port")
    if isinstance(port, int) and not isinstance(port, bool) and not 1 <= port <= 65535:
        add(ENTRY_FIELD_INVALID, "port", "Port must be between 1 and 65535.")
    repo_url = item.get("repo_url")
    if isinstance(repo_url, str) and repo_url and not repo_url.startswith("https://"):
        add(ENTRY_FIELD_INVALID, "repo_url", "Repository URL must use https.")
    for field in ("icon", "hero_image"):
        value = item.get(field)
        if not isinstance(value, str) or value.startswith("https://"):
            continue
        try:
            validate_registry_asset_path(value, ALLOWED_ASSET_EXTENSIONS)
        except ValueError:
            add(ENTRY_FIELD_INVALID, field, "Value must be https:// or an asset path.")


def _check_shape(doc: dict[str, object]) -> list[Finding]:
    findings: list[Finding] = []
    for key in _TOP_LEVEL_KEYS:
        if key not in doc:
            findings.append(
                _doc_finding(DOC_SHAPE, key, "Required top-level key is missing.")
            )
    if any(key not in _TOP_LEVEL_KEYS for key in doc):
        findings.append(
            _doc_finding(DOC_SHAPE, UNKNOWN_FIELD, "Unexpected top-level key.")
        )
    version = doc.get("version")
    if "version" in doc and (not isinstance(version, str) or version == ""):
        findings.append(
            _doc_finding(DOC_SHAPE, "version", "Field must be a non-empty string.")
        )
    updated_at = doc.get("updated_at")
    if "updated_at" in doc:
        valid = False
        if isinstance(updated_at, str) and _DATE_PATTERN.fullmatch(updated_at):
            try:
                datetime.date.fromisoformat(updated_at)
                valid = True
            except ValueError:
                valid = False
        if not valid:
            findings.append(
                _doc_finding(
                    DOC_SHAPE, "updated_at", "Field must be a YYYY-MM-DD date."
                )
            )
    if "apps" in doc and not isinstance(doc["apps"], list):
        findings.append(_doc_finding(DOC_SHAPE, "apps", "Field must be an array."))
    return findings


def _check_entries(apps: list[object]) -> list[Finding]:
    findings: list[Finding] = []
    seen: set[tuple[str, str]] = set()
    app_names: set[str] = set()
    for item in apps:
        if isinstance(item, dict):
            entry: dict[str, object] = item  # pyright: ignore[reportUnknownVariableType]
            if _entry_kind(entry) == "app" and isinstance(entry.get("name"), str):
                app_names.add(str(entry["name"]))
    for index, item in enumerate(apps):
        findings.extend(replace(f, index=index) for f in validate_entry(item))
        if not isinstance(item, dict):
            continue
        entry = item  # pyright: ignore[reportUnknownVariableType]
        kind = _entry_kind(entry)
        name = entry.get("name")
        if kind is None or not isinstance(name, str):
            continue
        label = f"{kind}:{name}" if APP_NAME_PATTERN.fullmatch(name) else None
        if (kind, name) in seen:
            findings.append(
                Finding(
                    ENTRY_DUPLICATE,
                    index,
                    label,
                    "name",
                    "Another entry has the same kind and name.",
                )
            )
        seen.add((kind, name))
        if kind == "theme" and name in app_names:
            findings.append(
                Finding(
                    ENTRY_NAME_CONFLICT,
                    index,
                    label,
                    "name",
                    "A theme must not share a name with an app.",
                )
            )
    return findings


def _asset_paths(entry: dict[str, object]) -> list[tuple[str, str]]:
    """(field, path) pairs whose own value passes the shared path rule."""
    kind = _entry_kind(entry)
    if kind is None:
        return []
    fields, extensions = ASSET_FIELDS_BY_KIND[kind]
    result: list[tuple[str, str]] = []
    for field in fields:
        value = entry.get(field)
        if not isinstance(value, str):
            continue
        if kind == "app" and value.startswith("https://"):
            continue
        try:
            validate_registry_asset_path(value, extensions)
        except ValueError:
            continue
        result.append((field, value))
    return result


def _magic_matches(extension: str, head: bytes) -> bool:
    if extension == ".png":
        return head.startswith(b"\x89PNG\r\n\x1a\n")
    if extension in (".jpg", ".jpeg"):
        return head.startswith(b"\xff\xd8\xff")
    if extension == ".webp":
        return head[:4] == b"RIFF" and head[8:12] == b"WEBP"
    return False


def _inspect_file(
    root: Path, rel: str, *, check_magic: bool
) -> tuple[str | None, str, int | None]:
    """Return (code, fixed message, size) for one registry-relative file."""
    current = root
    mode = 0
    size = 0
    for segment in rel.split("/"):
        current = current / segment
        try:
            info = os.lstat(current)
        except (FileNotFoundError, NotADirectoryError):
            return FILE_MISSING, "Referenced file does not exist.", None
        except OSError:
            return FILE_NOT_REGULAR, "Referenced file cannot be inspected.", None
        mode, size = info.st_mode, info.st_size
        if stat.S_ISLNK(mode):
            return FILE_NOT_REGULAR, "Referenced path must not use a symlink.", None
    if not stat.S_ISREG(mode):
        return FILE_NOT_REGULAR, "Referenced path is not a regular file.", None
    try:
        inside = current.resolve().is_relative_to(root.resolve())
    except (OSError, RuntimeError):
        inside = False
    if not inside:
        return FILE_NOT_REGULAR, "Referenced path escapes the checkout.", None
    if size > MAX_REGISTRY_ASSET_BYTES:
        return FILE_TOO_LARGE, "Referenced file exceeds the asset size limit.", size
    if check_magic:
        try:
            with open(current, "rb") as handle:
                head = handle.read(12)
        except OSError:
            return FILE_NOT_REGULAR, "Referenced file cannot be read.", size
        if not _magic_matches(PurePosixPath(rel).suffix.lower(), head):
            return (
                FILE_CONTENT_MISMATCH,
                "File content does not match its extension.",
                size,
            )
    return None, "", size


def _entry_file_results(
    entry: dict[str, object], index: int | None, root: Path
) -> tuple[list[Finding], dict[str, int]]:
    kind = _entry_kind(entry)
    name = entry.get("name")
    label = (
        f"{kind}:{name}"
        if kind is not None
        and isinstance(name, str)
        and APP_NAME_PATTERN.fullmatch(name)
        else None
    )
    findings: list[Finding] = []
    sizes: dict[str, int] = {}
    for field, rel in _asset_paths(entry):
        code, message, size = _inspect_file(root, rel, check_magic=kind == "theme")
        if size is not None:
            sizes[rel] = size
        if code is not None:
            findings.append(Finding(code, index, label, field, message))
    return findings, sizes


def check_entry_files(
    entry: dict[str, object], index: int | None, *, root: Path
) -> list[Finding]:
    """File findings for the registry-relative files one entry names."""
    return _entry_file_results(entry, index, root)[0]


def _check_files(apps: list[object], root: Path) -> list[Finding]:
    findings: list[Finding] = []
    sizes: dict[str, int] = {}
    for index, item in enumerate(apps):
        if not isinstance(item, dict):
            continue
        entry: dict[str, object] = item  # pyright: ignore[reportUnknownVariableType]
        found, found_sizes = _entry_file_results(entry, index, root)
        findings.extend(found)
        sizes.update(found_sizes)
    if sum(sizes.values()) > MAX_REGISTRY_ASSET_BYTES_PER_FETCH:
        findings.append(
            _doc_finding(
                DOC_ASSETS_TOO_LARGE,
                None,
                "Referenced files exceed the per-fetch asset budget.",
            )
        )
    return findings


def validate_document(
    data: bytes,
    *,
    org: str = THEME_REPO_OWNER,
    root: Path | None = None,
) -> Report:
    """Validate registry document bytes; ``root`` enables the file checks."""
    del org
    if len(data) > MAX_REGISTRY_DOCUMENT_BYTES:
        return Report(
            [
                _doc_finding(
                    DOC_TOO_LARGE, None, "Registry document exceeds the size limit."
                )
            ]
        )
    if data.startswith(b"\xef\xbb\xbf"):
        return Report(
            [
                _doc_finding(
                    DOC_NOT_UTF8, None, "Registry document must not start with a BOM."
                )
            ]
        )
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return Report(
            [_doc_finding(DOC_NOT_UTF8, None, "Registry document is not valid UTF-8.")]
        )
    try:
        parsed: object = json.loads(
            text, object_pairs_hook=_object_pairs, parse_constant=_reject_constant
        )
    except _DuplicateKeyError:
        return Report(
            [_doc_finding(DOC_DUPLICATE_KEY, None, "An object repeats a key.")]
        )
    except (ValueError, RecursionError):
        return Report(
            [
                _doc_finding(
                    DOC_INVALID_JSON, None, "Registry document is not valid JSON."
                )
            ]
        )

    if not isinstance(parsed, dict):
        return Report([_doc_finding(DOC_SHAPE, None, "Top level must be an object.")])
    doc: dict[str, object] = parsed  # pyright: ignore[reportUnknownVariableType]

    findings = _check_shape(doc)
    apps = doc.get("apps")
    checked = 0
    if isinstance(apps, list):
        items: list[object] = apps  # pyright: ignore[reportUnknownVariableType]
        checked = len(items)
        if checked > MAX_REGISTRY_ENTRIES_PER_SOURCE:
            findings.append(
                _doc_finding(
                    DOC_TOO_MANY_ENTRIES, "apps", "Too many entries in the registry."
                )
            )
        else:
            findings.extend(_check_entries(items))
            if root is not None:
                findings.extend(_check_files(items, root))

    try:
        canonical = json.dumps(doc, indent=2, ensure_ascii=False) + "\n"
    except (ValueError, RecursionError):
        canonical = None
    if canonical != text:
        findings.append(
            _doc_finding(
                DOC_NOT_CANONICAL,
                None,
                "Document is not in canonical form (2-space indent, trailing newline).",
            )
        )
    return Report(findings, checked)


class ManifestScalarError(ValueError):
    """The theme.yaml text is not in a form the reader can vouch for."""


# Characters PyYAML's reader refuses outright; refusing them here keeps the reader
# from returning values for text PyYAML cannot load at all.
_YAML_NON_PRINTABLE = re.compile(
    "[^\x09\x0a\x0d\x20-\x7e\x85\xa0-퟿-�\U00010000-\U0010ffff]"
)
_REFUSED_TEXT_CHARS = ("﻿", "\x85", chr(0x2028), chr(0x2029))
_REFUSED_KEY_STARTS = frozenset("\"'?:[{&*!|>%@`\t")
# Node properties, aliases and reserved indicators: never written by dump_manifest.
_REFUSED_VALUE_STARTS = frozenset("&!*%@`")
# Indicators only when followed by whitespace or the end of the line.
_SPACED_INDICATORS = frozenset("?:-")
_YAML_SPACE = " \t"
_IDENTITY_KEY_LINE = re.compile(r"(id|version)\s*:")
_IDENTITY_LINE = re.compile(r"(id|version): (.*)")
_PLAIN_VALUE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]*")
_SINGLE_QUOTED_VALUE = re.compile(r"'(?:[^']|'')*'")
_DOUBLE_QUOTED_VALUE = re.compile(r'"[^"\\]*"')
_DOCUMENT_MARKER = re.compile(r"(---|\.\.\.)( |$)")
# PyYAML 6.0.3 implicit resolvers, copied verbatim from yaml/resolver.py as
# (tag, pattern, verbose). A plain scalar matching any of them is not a str.
# Compiled with re.X when verbose, as PyYAML does; tested with .match().
YAML_IMPLICIT_RESOLVERS: tuple[tuple[str, str, bool], ...] = (
    (
        "bool",
        (
            "^(?:yes|Yes|YES|no|No|NO\n           "
            "         |true|True|TRUE|false|False"
            "|FALSE\n                    |on|On|ON"
            "|off|Off|OFF)$"
        ),
        True,
    ),
    (
        "float",
        (
            "^(?:[-+]?(?:[0-9][0-9_]*)\\.[0-9_]*(?"
            ":[eE][-+][0-9]+)?\n                  "
            "  |\\.[0-9][0-9_]*(?:[eE][-+][0-9]+)?"
            "\n                    |[-+]?[0-9][0-9"
            "_]*(?::[0-5]?[0-9])+\\.[0-9_]*\n      "
            "              |[-+]?\\.(?:inf|Inf|INF"
            ")\n                    |\\.(?:nan|NaN|"
            "NAN))$"
        ),
        True,
    ),
    (
        "int",
        (
            "^(?:[-+]?0b[0-1_]+\n                 "
            "   |[-+]?0[0-7_]+\n                  "
            "  |[-+]?(?:0|[1-9][0-9_]*)\n         "
            "           |[-+]?0x[0-9a-fA-F_]+\n   "
            "                 |[-+]?[1-9][0-9_]*("
            "?::[0-5]?[0-9])+)$"
        ),
        True,
    ),
    (
        "merge",
        ("^(?:<<)$"),
        False,
    ),
    (
        "null",
        ("^(?: ~\n                    |null|Null|NULL\n                    | )$"),
        True,
    ),
    (
        "timestamp",
        (
            "^(?:[0-9][0-9][0-9][0-9]-[0-9][0-9]-"
            "[0-9][0-9]\n                    |[0-9"
            "][0-9][0-9][0-9] -[0-9][0-9]? -[0-9]"
            "[0-9]?\n                     (?:[Tt]|"
            "[ \\t]+)[0-9][0-9]?\n                 "
            "    :[0-9][0-9] :[0-9][0-9] (?:\\.[0-"
            "9]*)?\n                     (?:[ \\t]*"
            "(?:Z|[-+][0-9][0-9]?(?::[0-9][0-9])?"
            "))?)$"
        ),
        True,
    ),
    (
        "value",
        ("^(?:=)$"),
        False,
    ),
    (
        "yaml",
        ("^(?:!|&|\\*)$"),
        False,
    ),
)
_YAML_IMPLICIT_RESOLVER_REGEXES = tuple(
    re.compile(pattern, re.X if verbose else 0)
    for _tag, pattern, verbose in YAML_IMPLICIT_RESOLVERS
)


def _split_manifest_lines(data: bytes) -> list[str]:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ManifestScalarError("theme.yaml is not valid UTF-8") from exc
    if any(ch in text for ch in _REFUSED_TEXT_CHARS):
        raise ManifestScalarError("theme.yaml has a BOM or an extra line break")
    if _YAML_NON_PRINTABLE.search(text):
        raise ManifestScalarError("theme.yaml has a non-printable character")
    lines = text.split("\n")
    for index, line in enumerate(lines):
        if line.endswith("\r"):
            line = lines[index] = line[:-1]
        if "\r" in line:
            raise ManifestScalarError("theme.yaml has a bare carriage return")
    return lines


def _check_document_markers(lines: list[str]) -> None:
    if lines and lines[0].startswith("%"):
        raise ManifestScalarError("theme.yaml has a directive")
    for index, line in enumerate(lines):
        if _DOCUMENT_MARKER.match(line) and not (index == 0 and line == "---"):
            raise ManifestScalarError("theme.yaml is not a single plain document")


def _skip_space(line: str, i: int) -> int:
    while i < len(line) and line[i] in _YAML_SPACE:
        i += 1
    return i


def _is_spaced_indicator(line: str, i: int) -> bool:
    return line[i] in _SPACED_INDICATORS and (
        i + 1 == len(line) or line[i + 1] in _YAML_SPACE
    )


def _quote_end(line: str, start: int, quote: str) -> int | None:
    """Index just past the closing quote searched from ``start``; None if open."""
    i = start
    while i < len(line):
        ch = line[i]
        if quote == '"' and ch == "\\":
            i += 2
            continue
        if ch == quote:
            if quote == "'" and line[i + 1 : i + 2] == "'":
                i += 2
                continue
            return i + 1
        i += 1
    return None


def _flow_end(line: str, start: int) -> int:
    depth = 0
    i = start
    while i < len(line):
        ch = line[i]
        if ch in "'\"":
            end = _quote_end(line, i + 1, ch)
            if end is None:
                break
            i = end
            continue
        if ch in "[{":
            depth += 1
        elif ch in "]}":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    raise ManifestScalarError("theme.yaml has a flow collection that spans lines")


def _after_closed_value(line: str, pos: int) -> int | None:
    """Where scanning resumes after a closed quoted or flow node.

    Returns the position just past a key colon, or None when the line ends there
    (end of line or a comment). Anything else is a form the reader refuses.
    """
    i = _skip_space(line, pos)
    if i == len(line):
        return None
    if line[i] == "#" and i > pos:
        return None
    if line[i] == ":" and (i + 1 == len(line) or line[i + 1] in _YAML_SPACE):
        return i + 1
    raise ManifestScalarError("theme.yaml has text after a quoted or flow value")


def _scan_plain(line: str, start: int) -> int | None:
    """Scan a plain scalar; return the position just past a key colon, if any."""
    for j in range(start, len(line)):
        ch = line[j]
        if ch == ":" and (j + 1 == len(line) or line[j + 1] in _YAML_SPACE):
            return j + 1
        if ch == "#" and j > start and line[j - 1] in _YAML_SPACE:
            return None
    return None


@dataclass
class _ScanState:
    quote: str | None = None
    block_indent: int | None = None  # key column of an open block scalar


def _scan_line(line: str, state: _ScanState) -> bool:
    """Advance the open-construct state over one line that starts outside a scalar.

    Returns whether the line's first node is a mapping key (a colon follows it).
    Node properties (``&anchor``, ``!tag``), aliases, explicit ``?``/``:``
    indicators and a second key on one line are refused, because each of them
    can hide a quoted scalar or a key from a line-based reader, and
    ``dump_manifest`` writes none of them.
    """
    n = len(line)
    i = 0
    while i < n and line[i] == " ":
        i += 1
    if i < n and line[i] == "\t":
        raise ManifestScalarError("theme.yaml indents with a tab")
    while i < n and line[i] == "-" and _is_spaced_indicator(line, i):
        i = _skip_space(line, i + 1)
    key_column = i
    after_key = False
    while i < n:
        ch = line[i]
        if ch == "#":
            return after_key
        if ch in _REFUSED_VALUE_STARTS or _is_spaced_indicator(line, i):
            raise ManifestScalarError("theme.yaml has a node property or indicator")
        if ch in "'\"":
            end = _quote_end(line, i + 1, ch)
            if end is None:
                state.quote = ch
                return after_key
            nxt = _after_closed_value(line, end)
        elif ch in "[{":
            nxt = _after_closed_value(line, _flow_end(line, i))
        elif ch in "|>":
            state.block_indent = key_column
            return after_key
        else:
            nxt = _scan_plain(line, i)
        if nxt is None:
            return after_key
        if after_key:
            raise ManifestScalarError("theme.yaml has two keys on one line")
        after_key = True
        i = _skip_space(line, nxt)
    return after_key


def _top_level_lines(lines: list[str]) -> list[tuple[int, str]]:
    """Column-0 key lines (with indexes) outside every open scalar or flow."""
    state = _ScanState()
    found: list[tuple[int, str]] = []
    seen_content = False
    for index, line in enumerate(lines):
        if state.quote is not None:
            if line and line[0] != " ":
                raise ManifestScalarError("theme.yaml has a quoted scalar at column 0")
            end = _quote_end(line, 0, state.quote)
            if end is None:
                continue
            state.quote = None
            if _after_closed_value(line, end) is not None:
                raise ManifestScalarError("theme.yaml has a multi-line quoted key")
            continue
        if state.block_indent is not None:
            indent = len(line) - len(line.lstrip(" "))
            if not line.strip(" ") or indent > state.block_indent:
                continue
            state.block_indent = None
        content = line.strip(" ")
        if not content or content.startswith("#"):
            continue
        if index == 0 and line == "---":
            continue
        if not seen_content:
            seen_content = True
            if line[0] == " " or line.startswith("-"):
                raise ManifestScalarError("theme.yaml is not a top-level mapping")
        is_key = _scan_line(line, state)
        if line[0] == " " or line == "-" or line.startswith("- "):
            continue
        if line[0] in _REFUSED_KEY_STARTS:
            raise ManifestScalarError("theme.yaml has a complex top-level key")
        if not is_key:
            raise ManifestScalarError("theme.yaml has a top-level line with no key")
        found.append((index, line))
    if state.quote is not None:
        raise ManifestScalarError("theme.yaml ends inside a quoted scalar")
    return found


def _read_scalar_value(raw: str) -> str:
    if _PLAIN_VALUE.fullmatch(raw):
        if any(rx.match(raw) for rx in _YAML_IMPLICIT_RESOLVER_REGEXES):
            raise ManifestScalarError(
                "theme.yaml has a plain id or version that YAML reads as a non-string"
            )
        return raw
    if _SINGLE_QUOTED_VALUE.fullmatch(raw):
        return raw[1:-1].replace("''", "'")
    if _DOUBLE_QUOTED_VALUE.fullmatch(raw):
        return raw[1:-1]
    raise ManifestScalarError("theme.yaml has an id or version in an unreadable form")


def _check_value_ends(lines: list[str], index: int) -> None:
    """Refuse an indented line after an id or version line.

    PyYAML continues a plain scalar over indented lines, even across blank and
    comment lines (``version: 1.0.0`` then ``  x`` loads ``"1.0.0 x"``); after a
    quoted value an indented line is an error. Either way the line alone is not
    the value.
    """
    for line in lines[index + 1 :]:
        if not line.strip(" \t") or line.startswith("#"):
            continue
        if line[0] in _YAML_SPACE:
            raise ManifestScalarError("theme.yaml continues an id or version value")
        return


def read_manifest_scalars(data: bytes) -> dict[str, str]:
    """Read ``id`` and ``version`` from ``theme.yaml`` bytes, refusing anything unsure.

    Whenever this returns, ``yaml.safe_load`` of the same bytes is a mapping whose
    ``id`` and ``version`` are the same strings. Plain values that YAML types as
    non-strings are refused.
    Raises ``ManifestScalarError`` otherwise.
    """
    lines = _split_manifest_lines(data)
    _check_document_markers(lines)
    values: dict[str, str] = {}
    for index, line in _top_level_lines(lines):
        if not _IDENTITY_KEY_LINE.match(line):
            continue
        match = _IDENTITY_LINE.fullmatch(line)
        if match is None:
            raise ManifestScalarError(
                "theme.yaml has an id or version line in an odd form"
            )
        key = match.group(1)
        if key in values:
            raise ManifestScalarError(f"theme.yaml repeats {key}")
        values[key] = _read_scalar_value(match.group(2))
        _check_value_ends(lines, index)
    if "id" not in values or "version" not in values:
        raise ManifestScalarError("theme.yaml is missing id or version")
    return values


# --- Online transports (US-009) -------------------------------------------

HTTP_CHUNK_BYTES = 64 * 1024
LS_REMOTE_MAX_STDOUT_BYTES = 1024 * 1024
LS_REMOTE_GIT_CONFIG: tuple[str, ...] = (
    "protocol.allow=never",
    "protocol.https.allow=always",
    "http.followRedirects=false",
    "core.hooksPath=/dev/null",
)
_LS_REMOTE_URL = re.compile(
    r"https://github\.com/[A-Za-z0-9-]+/poppy-theme-[a-z0-9-]+\.git"
)
_API_HOST = "api.github.com"
_RAW_HOST = "raw.githubusercontent.com"


@dataclass(frozen=True)
class HttpResponse:
    status: int
    body: bytes
    over_cap: bool
    rate_limited: bool


class OnlineUnavailableError(Exception):
    """An online check could not complete; ``reason`` is one fixed string."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class _Readable(Protocol):
    def read(self, amt: int, /) -> bytes: ...


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        return None


def _is_rate_limited(status: int, headers: Message[str, str] | None) -> bool:
    if status == 429:
        return True
    if status == 403 and headers is not None:
        return str(headers.get("X-RateLimit-Remaining", "")).strip() == "0"
    return False


def _check_http_url(url: str, headers: Mapping[str, str]) -> None:
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname not in (_API_HOST, _RAW_HOST):
        raise ValueError("unsupported URL")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("unsupported URL")
    if parsed.hostname != _API_HOST and any(
        name.lower() == "authorization" for name in headers
    ):
        raise ValueError("Authorization is only allowed for api.github.com")


def default_http_get(
    url: str, *, max_bytes: int, headers: Mapping[str, str], timeout: float
) -> HttpResponse:
    """GET ``url`` without following redirects; never raises on HTTP statuses."""
    _check_http_url(url, headers)
    opener = urllib.request.build_opener(_NoRedirect)
    request = urllib.request.Request(url, headers=dict(headers), method="GET")
    start = time.monotonic()
    try:
        try:
            with opener.open(request, timeout=timeout) as response:
                body = _read_capped(response, max_bytes, start, timeout)
                status = int(response.status)
                resp_headers = response.headers
        except urllib.error.HTTPError as exc:
            body = b""
            try:
                if getattr(exc, "fp", None) is not None:
                    body = _read_capped(exc, max_bytes, start, timeout)
            finally:
                exc.close()
            return HttpResponse(
                status=exc.code,
                body=body[:max_bytes],
                over_cap=len(body) > max_bytes,
                rate_limited=_is_rate_limited(exc.code, exc.headers),
            )
    except OnlineUnavailableError:
        raise
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, (TimeoutError, socket.timeout)):
            raise OnlineUnavailableError("timeout") from None
        raise OnlineUnavailableError("network") from None
    except TimeoutError:
        raise OnlineUnavailableError("timeout") from None
    except OSError:
        raise OnlineUnavailableError("network") from None
    return HttpResponse(
        status=status,
        body=body[:max_bytes],
        over_cap=len(body) > max_bytes,
        rate_limited=_is_rate_limited(status, resp_headers),
    )


def _read_capped(
    response: _Readable, max_bytes: int, start: float, timeout: float
) -> bytes:
    """Read up to ``max_bytes + 1`` bytes under a total deadline."""
    remaining = max_bytes + 1
    chunks: list[bytes] = []
    while remaining > 0:
        chunk = response.read(min(HTTP_CHUNK_BYTES, remaining))
        if time.monotonic() - start > timeout:
            raise OnlineUnavailableError("timeout")
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def default_ls_remote(url: str, *, timeout: float) -> str:
    """Run a hardened ``git ls-remote --tags`` and return its stdout."""
    if _LS_REMOTE_URL.fullmatch(url) is None:
        raise ValueError("unsupported repository URL")
    argv = ["git"]
    for value in LS_REMOTE_GIT_CONFIG:
        argv += ["-c", value]
    argv += ["ls-remote", "--tags", url]
    env = {
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_ALLOW_PROTOCOL": "https",
    }
    for name in ("PATH", "HOME"):
        if name in os.environ:
            env[name] = os.environ[name]
    try:
        result = subprocess.run(
            argv, env=env, capture_output=True, timeout=timeout, check=False
        )
    except subprocess.TimeoutExpired:
        raise OnlineUnavailableError("timeout") from None
    except (FileNotFoundError, OSError):
        raise OnlineUnavailableError("git_failed") from None
    if result.returncode != 0:
        raise OnlineUnavailableError("git_failed")
    if len(result.stdout) > LS_REMOTE_MAX_STDOUT_BYTES:
        raise OnlineUnavailableError("too_large")
    return result.stdout.decode("utf-8", errors="replace")


# --- Online checks (US-012) -----------------------------------------------

ONLINE_BUDGET_SECONDS = 180
HTTP_TIMEOUT_SECONDS = 15
LS_REMOTE_TIMEOUT_SECONDS = 30
RETRY_SLEEPS: tuple[int, ...] = (5, 15)
MAX_MANIFEST_BYTES = 64 * 1024
MAX_REPO_RESPONSE_BYTES = 1024 * 1024
TAG_LINE_PATTERN = re.compile(r"[0-9a-f]{40}\trefs/tags/[^\n]+")

REPO_NOT_FOUND = "REPO_NOT_FOUND"
REPO_MOVED = "REPO_MOVED"
REPO_NOT_PUBLIC = "REPO_NOT_PUBLIC"
REPO_ARCHIVED = "REPO_ARCHIVED"
REPO_OWNER_MISMATCH = "REPO_OWNER_MISMATCH"
REPO_NAME_MISMATCH = "REPO_NAME_MISMATCH"
REF_NOT_TAGGED = "REF_NOT_TAGGED"
TAG_MISMATCH = "TAG_MISMATCH"
MANIFEST_MISSING = "MANIFEST_MISSING"
MANIFEST_TOO_LARGE = "MANIFEST_TOO_LARGE"
MANIFEST_UNPARSEABLE = "MANIFEST_UNPARSEABLE"
MANIFEST_ID_MISMATCH = "MANIFEST_ID_MISMATCH"
MANIFEST_VERSION_MISMATCH = "MANIFEST_VERSION_MISMATCH"

_GITHUB_API_HEADERS = {
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
    "User-Agent": "poppymarket-validate-registry",
}
_REPO_REDIRECT_STATUSES = frozenset({301, 302, 307, 308})

HttpGet = Callable[..., HttpResponse]
LsRemote = Callable[..., str]


@dataclass(frozen=True)
class Unavailable:
    """An online check that could not complete (never a finding)."""

    entry: str
    check: str
    reason: str


def _status_reason(response: HttpResponse) -> str | None:
    """Map a transient/limit status to an unavailable reason, else ``None``."""
    if response.rate_limited:
        return "rate_limited"
    if response.status == 403:
        return "forbidden"
    if 500 <= response.status <= 599:
        return "server_error"
    return None


class _OnlineRun:
    """Shared clock, sleep and deadline for one ``run_online_checks`` call."""

    def __init__(
        self,
        *,
        http_get: HttpGet,
        ls_remote: LsRemote,
        sleep: Callable[[float], object],
        clock: Callable[[], float],
        budget_seconds: float,
    ) -> None:
        self.http_get = http_get
        self.ls_remote = ls_remote
        self.sleep = sleep
        self.clock = clock
        self.deadline = clock() + budget_seconds

    def remaining(self) -> float:
        return self.deadline - self.clock()

    def get_with_retry(
        self, url: str, *, max_bytes: int, headers: Mapping[str, str]
    ) -> HttpResponse | str:
        """GET with the 404 retry policy; return the response or a reason."""
        attempt = 0
        while True:
            if self.remaining() < HTTP_TIMEOUT_SECONDS:
                return "budget"
            try:
                response = self.http_get(
                    url,
                    max_bytes=max_bytes,
                    headers=headers,
                    timeout=HTTP_TIMEOUT_SECONDS,
                )
            except OnlineUnavailableError as exc:
                return exc.reason
            if response.status != 404 or attempt >= len(RETRY_SLEEPS):
                return response
            pause = RETRY_SLEEPS[attempt]
            if self.remaining() < pause + HTTP_TIMEOUT_SECONDS:
                return "budget"
            self.sleep(pause)
            attempt += 1


def _online_finding(
    index: int, name: str, code: str, field: str, message: str
) -> Finding:
    return Finding(code, index, name, field, message)


def _check_repo(
    run: _OnlineRun, index: int, name: str, org: str, token: str | None
) -> Finding | Unavailable | None:
    headers = dict(_GITHUB_API_HEADERS)
    if token:
        headers["Authorization"] = f"Bearer {token}"
    url = f"https://{_API_HOST}/repos/{org}/poppy-theme-{name}"
    result = run.get_with_retry(url, max_bytes=MAX_REPO_RESPONSE_BYTES, headers=headers)
    if isinstance(result, str):
        return Unavailable(name, "repo", result)
    status = result.status
    reason = _status_reason(result)
    if reason is not None:
        return Unavailable(name, "repo", reason)
    if status == 404:
        return _online_finding(
            index, name, REPO_NOT_FOUND, "name", "repository not found or not public"
        )
    if status in _REPO_REDIRECT_STATUSES:
        return _online_finding(
            index, name, REPO_MOVED, "name", "repository has moved or was renamed"
        )
    if status != 200:
        return Unavailable(name, "repo", "unexpected_status")
    return _map_repo_body(result, index, name, org)


def _map_repo_body(
    result: HttpResponse, index: int, name: str, org: str
) -> Finding | Unavailable | None:
    malformed = Unavailable(name, "repo", "malformed")
    if result.over_cap:
        return malformed
    try:
        loaded: object = json.loads(result.body)
    except (ValueError, RecursionError):
        return malformed
    if not isinstance(loaded, dict):
        return malformed
    payload = cast("dict[str, object]", loaded)
    owner = payload.get("owner")
    login = (
        cast("dict[str, object]", owner).get("login")
        if isinstance(owner, dict)
        else None
    )
    repo_name = payload.get("name")
    private = payload.get("private")
    archived = payload.get("archived")
    if (
        not isinstance(private, bool)
        or not isinstance(archived, bool)
        or not isinstance(login, str)
        or not isinstance(repo_name, str)
    ):
        return malformed
    if private:
        return _online_finding(
            index, name, REPO_NOT_PUBLIC, "name", "repository is not public"
        )
    if archived:
        return _online_finding(
            index, name, REPO_ARCHIVED, "name", "repository is archived"
        )
    if login != org:
        return _online_finding(
            index, name, REPO_OWNER_MISMATCH, "name", "repository owner is not the org"
        )
    if repo_name != f"poppy-theme-{name}":
        return _online_finding(
            index, name, REPO_NAME_MISMATCH, "name", "repository name does not match"
        )
    return None


def _parse_tags(output: str) -> dict[str, str] | None:
    direct: dict[str, str] = {}
    peeled: dict[str, str] = {}
    for line in output.splitlines():
        if not TAG_LINE_PATTERN.fullmatch(line):
            return None
        sha, _, refname = line.partition("\t")
        tag = refname[len("refs/tags/") :]
        if tag.endswith("^{}"):
            peeled[tag[:-3]] = sha
        else:
            direct[tag] = sha
    return {
        tag: peeled.get(tag) or direct[tag] for tag in direct.keys() | peeled.keys()
    }


def _check_tags(
    run: _OnlineRun, index: int, entry: Mapping[str, object], name: str, org: str
) -> Finding | Unavailable | None:
    if run.remaining() < LS_REMOTE_TIMEOUT_SECONDS:
        return Unavailable(name, "tags", "budget")
    try:
        output = run.ls_remote(
            f"https://github.com/{org}/poppy-theme-{name}.git",
            timeout=LS_REMOTE_TIMEOUT_SECONDS,
        )
    except OnlineUnavailableError as exc:
        return Unavailable(name, "tags", exc.reason)
    tags = _parse_tags(output)
    if tags is None:
        return Unavailable(name, "tags", "malformed")
    ref = str(entry.get("ref"))
    wanted = f"v{entry.get('version')}"
    if wanted in tags:
        if tags[wanted] == ref:
            return None
        return _online_finding(
            index, name, TAG_MISMATCH, "ref", "tag v<version> does not point at ref"
        )
    if ref in tags.values():
        return _online_finding(
            index,
            name,
            TAG_MISMATCH,
            "ref",
            "tag v<version> is missing; ref has another tag",
        )
    return _online_finding(index, name, REF_NOT_TAGGED, "ref", "ref is not tagged")


def _check_manifest(
    run: _OnlineRun, index: int, entry: Mapping[str, object], name: str, org: str
) -> Finding | Unavailable | None:
    url = f"https://{_RAW_HOST}/{org}/poppy-theme-{name}/{entry.get('ref')}/theme.yaml"
    result = run.get_with_retry(
        url,
        max_bytes=MAX_MANIFEST_BYTES,
        headers={"User-Agent": "poppymarket-validate-registry"},
    )
    if isinstance(result, str):
        return Unavailable(name, "manifest", result)
    status = result.status
    reason = _status_reason(result)
    if reason is not None:
        return Unavailable(name, "manifest", reason)
    if 300 <= status <= 399:
        return Unavailable(name, "manifest", "redirect")
    if status == 200:
        return _map_manifest_body(result, index, entry, name)
    if 400 <= status <= 499:
        return _online_finding(
            index,
            name,
            MANIFEST_MISSING,
            "ref",
            "theme.yaml not found at ref; re-run the job if the tag was pushed "
            "in the last few minutes",
        )
    return Unavailable(name, "manifest", "unexpected_status")


def _map_manifest_body(
    result: HttpResponse, index: int, entry: Mapping[str, object], name: str
) -> Finding | None:
    if result.over_cap:
        return _online_finding(
            index, name, MANIFEST_TOO_LARGE, "ref", "theme.yaml is too large"
        )
    try:
        values = read_manifest_scalars(result.body)
    except ManifestScalarError:
        return _online_finding(
            index, name, MANIFEST_UNPARSEABLE, "ref", "theme.yaml could not be read"
        )
    if values["id"] != name:
        return _online_finding(
            index,
            name,
            MANIFEST_ID_MISMATCH,
            "ref",
            "theme.yaml id does not match name",
        )
    if values["version"] != entry.get("version"):
        return _online_finding(
            index,
            name,
            MANIFEST_VERSION_MISMATCH,
            "version",
            "theme.yaml version does not match version",
        )
    return None


def run_online_checks(
    entries: Sequence[tuple[int, Mapping[str, object]]],
    *,
    org: str = THEME_REPO_OWNER,
    http_get: HttpGet = default_http_get,
    ls_remote: LsRemote = default_ls_remote,
    sleep: Callable[[float], object] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    api_token: str | None = None,
    budget_seconds: float = ONLINE_BUDGET_SECONDS,
) -> tuple[list[Finding], list[Unavailable]]:
    """Check each selected theme entry against GitHub within a time budget."""
    run = _OnlineRun(
        http_get=http_get,
        ls_remote=ls_remote,
        sleep=sleep,
        clock=clock,
        budget_seconds=budget_seconds,
    )
    findings: list[Finding] = []
    unavailable: list[Unavailable] = []
    for index, entry in entries:
        name = str(entry.get("name"))
        outcomes = (
            _check_repo(run, index, name, org, api_token),
            _check_tags(run, index, entry, name, org),
            _check_manifest(run, index, entry, name, org),
        )
        for outcome in outcomes:
            if isinstance(outcome, Finding):
                findings.append(outcome)
            elif outcome is not None:
                unavailable.append(outcome)
    return findings, unavailable


def _format_finding(finding: Finding) -> str:
    if finding.entry is not None:
        where = finding.entry
    elif finding.index is not None:
        where = f"#{finding.index}"
    else:
        where = "-"
    return f"{finding.code} {where} {finding.field or '-'}: {finding.message}"


_SOURCE_FIELDS = ("image", "repo_url", "ref", "manifest_url", "port")
_KNOWN_FIELDS = frozenset(
    {*_THEME_ALLOWED_KEYS, *_THEME_FORBIDDEN_NAMED, *_OPTIONAL_STRING_FIELDS}
    | {"name", "description", "version", "author", "tags", "kind", "port"}
)
TOKEN_ENV_VAR = "POPPYMARKET_GITHUB_TOKEN"
_BASE_UNAVAILABLE = "base unavailable"


@dataclass(frozen=True)
class Change:
    """One added, removed or changed entry (never echoes entry text)."""

    entry: str
    change: str
    fields: list[str]
    install_source_changed: bool


def _parse_apps(data: bytes) -> list[object] | None:
    """The ``apps`` list of a registry document, or ``None`` when unusable."""
    try:
        parsed: object = json.loads(
            data.decode("utf-8"),
            object_pairs_hook=_object_pairs,
            parse_constant=_reject_constant,
        )
    except (ValueError, RecursionError, _DuplicateKeyError):
        return None
    if not isinstance(parsed, dict):
        return None
    apps = cast("dict[str, object]", parsed).get("apps")
    if not isinstance(apps, list):
        return None
    return list(cast("list[object]", apps))


def _key_of(item: object) -> tuple[str, str] | None:
    if not isinstance(item, dict):
        return None
    entry = cast("dict[str, object]", item)
    kind = _entry_kind(entry)
    name = entry.get("name")
    if kind is None or not isinstance(name, str):
        return None
    return kind, name


def _label(item: object, index: int) -> str:
    key = _key_of(item)
    if key is not None and APP_NAME_PATTERN.fullmatch(key[1]):
        return f"{key[0]}:{key[1]}"
    return f"#{index}"


def _stable(item: object) -> str:
    try:
        return json.dumps(item, sort_keys=True)
    except (TypeError, ValueError, RecursionError):
        return repr(item)


def _read_base(path: str) -> dict[tuple[str, str], dict[str, object]] | None:
    try:
        with open(path, "rb") as handle:
            data = handle.read(MAX_REGISTRY_DOCUMENT_BYTES + 1)
    except OSError:
        return None
    if len(data) > MAX_REGISTRY_DOCUMENT_BYTES:
        return None
    apps = _parse_apps(data)
    if apps is None:
        return None
    base: dict[tuple[str, str], dict[str, object]] = {}
    for item in apps:
        key = _key_of(item)
        if key is not None and key not in base:
            base[key] = cast("dict[str, object]", item)
    return base


def _field_names(keys: set[str]) -> list[str]:
    names = sorted(k for k in keys if k in _KNOWN_FIELDS)
    if any(k not in _KNOWN_FIELDS for k in keys):
        names.append(UNKNOWN_FIELD)
    return names


def _diff_entry(
    label: str, change: str, head: dict[str, object], base: dict[str, object]
) -> Change:
    if change == "added":
        keys = set(head)
    elif change == "removed":
        keys = set(base)
    else:
        keys = {k for k in set(head) | set(base) if k not in head or k not in base} | {
            k for k in head if k in base and _stable(head[k]) != _stable(base[k])
        }
    source = change == "changed" and any(k in keys for k in _SOURCE_FIELDS)
    return Change(label, change, _field_names(keys), source)


def _compute_changes(
    head: list[object],
    base: dict[tuple[str, str], dict[str, object]] | None,
) -> list[Change]:
    changes: list[Change] = []
    head_keys: set[tuple[str, str]] = set()
    for index, item in enumerate(head):
        key = _key_of(item)
        entry: dict[str, object] = (
            cast("dict[str, object]", item) if isinstance(item, dict) else {}
        )
        label = _label(cast("object", item), index)
        if key is not None:
            if key in head_keys:
                continue
            head_keys.add(key)
        if base is None or key is None or key not in base:
            changes.append(_diff_entry(label, "added", entry, {}))
        elif _stable(entry) != _stable(base[key]):
            changes.append(_diff_entry(label, "changed", entry, base[key]))
    if base is not None:
        for key, item in base.items():
            if key not in head_keys:
                label = (
                    f"{key[0]}:{key[1]}" if APP_NAME_PATTERN.fullmatch(key[1]) else "#-"
                )
                changes.append(_diff_entry(label, "removed", {}, item))
    return changes


def _format_change(change: Change) -> str:
    line = f"CHANGED {change.entry} {change.change}: {', '.join(change.fields)}"
    return line + (" [install source]" if change.install_source_changed else "")


def _select_online(
    head: list[object],
    report: Report,
    base: dict[tuple[str, str], dict[str, object]] | None,
    use_base: bool,
) -> list[tuple[int, Mapping[str, object]]]:
    blocked = {f.index for f in report.findings if f.index is not None}
    duplicated: set[tuple[str, str]] = set()
    for f in report.findings:
        if f.code == ENTRY_DUPLICATE and f.index is not None:
            key = _key_of(head[f.index]) if f.index < len(head) else None
            if key is not None:
                duplicated.add(key)
    selected: list[tuple[int, Mapping[str, object]]] = []
    for index, item in enumerate(head):
        key = _key_of(item)
        if key is None or key[0] != "theme" or key in duplicated or index in blocked:
            continue
        entry = cast("dict[str, object]", item)
        if (
            use_base
            and base is not None
            and key in base
            and _stable(entry) == _stable(base[key])
        ):
            continue
        selected.append((index, entry))
    return selected


def _format_finding_unavailable(item: Unavailable) -> str:
    return (
        f"UNVERIFIED {item.entry} {item.check}: could not verify "
        f"({item.reason}); re-run the job"
    )


def _json_report(
    report: Report,
    *,
    online: bool = False,
    online_checked: list[str] | None = None,
    unavailable: list[Unavailable] | None = None,
    changes: list[Change] | None = None,
    findings: list[Finding] | None = None,
) -> str:
    all_findings = report.findings if findings is None else findings
    payload = {
        "validator": VALIDATOR_NAME,
        "validator_version": VALIDATOR_VERSION,
        "ok": not all_findings,
        "mode": "online" if online else "offline",
        "entries_checked": report.entries_checked,
        "online_checked": online_checked or [],
        "online_unavailable": [
            {"entry": u.entry, "check": u.check, "reason": u.reason}
            for u in unavailable or []
        ],
        "changes": [
            {
                "entry": c.entry,
                "change": c.change,
                "fields": c.fields,
                "install_source_changed": c.install_source_changed,
            }
            for c in changes or []
        ],
        "findings": [
            {
                "code": f.code,
                "index": f.index,
                "entry": f.entry,
                "field": f.field,
                "message": f.message,
            }
            for f in all_findings
        ],
    }
    return json.dumps(payload, indent=2)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="validate_registry.py", description="Validate a registry document."
    )
    parser.add_argument("registry", metavar="REGISTRY")
    parser.add_argument("--root", metavar="DIR", default=None)
    parser.add_argument("--json", action="store_true", dest="as_json")
    parser.add_argument("--online", action="store_true")
    parser.add_argument("--changed-against", metavar="FILE", default=None)
    return parser


def _run(argv: list[str] | None) -> int:
    try:
        args = _build_parser().parse_args(argv)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 2
    try:
        with open(args.registry, "rb") as handle:
            data = handle.read(MAX_REGISTRY_DOCUMENT_BYTES + 1)
    except OSError:
        print(f"{DOC_UNREADABLE}: cannot read the registry file", file=sys.stderr)
        return 2
    root = Path(args.root) if args.root is not None else Path(args.registry).parent
    report = validate_document(data, root=root)
    head = _parse_apps(data) if len(data) <= MAX_REGISTRY_DOCUMENT_BYTES else None
    head_apps = head or []
    use_base = args.changed_against is not None
    base = _read_base(args.changed_against) if use_base else None

    changes: list[Change] = []
    if use_base:
        changes = _compute_changes(head_apps, base)

    findings = list(report.findings)
    checked: list[str] = []
    unavailable: list[Unavailable] = []
    if args.online:
        selected = _select_online(head_apps, report, base, use_base)
        checked = [_label(entry, index) for index, entry in selected]
        if selected:
            token = os.environ.get(TOKEN_ENV_VAR) or None
            online_findings, unavailable = run_online_checks(selected, api_token=token)
            findings.extend(online_findings)

    if args.as_json:
        print(
            _json_report(
                report,
                online=args.online,
                online_checked=checked,
                unavailable=unavailable,
                changes=changes,
                findings=findings,
            )
        )
    else:
        for finding in findings:
            print(_format_finding(finding))
        if use_base and base is None:
            print(f"CHANGED {_BASE_UNAVAILABLE}: every entry is treated as added")
        for change in changes:
            print(_format_change(change))
        for item in unavailable:
            print(_format_finding_unavailable(item))
    if findings:
        return 1
    return 3 if unavailable else 0


def main(argv: list[str] | None = None) -> int:
    """CLI entry point. Exit codes: 0 clean, 1 findings, 2 usage/I-O/internal."""
    try:
        return _run(argv)
    except Exception as exc:
        print(
            f"INTERNAL_ERROR: validator crashed ({type(exc).__name__})", file=sys.stderr
        )
        return 2


if __name__ == "__main__":
    sys.exit(main())
