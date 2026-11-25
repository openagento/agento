"""Panel message catalogs: the language registry, the layered messages, the language choice.

The contract for module authors is docs/development/i18n.md. This package reads JSON only and
imports no module code.
"""
from __future__ import annotations

import enum
import hashlib
import json
import logging
import os
import re
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import NamedTuple, cast

logger = logging.getLogger(__name__)

SOURCE_LANGUAGE = "en"
FILE_NAME = "i18n.json"
VERSION = 1
BUILTIN_DIR = Path(__file__).parent
UI_NAMESPACE, PANEL_NAMESPACE = "ui", "panel"
BUILTIN_NAMESPACES = (UI_NAMESPACE, PANEL_NAMESPACE)
LANGUAGE_PATH = "admin/locale/language"
_LANGUAGE_OWNER, _LANGUAGE_FIELD = LANGUAGE_PATH.split("/", 1)

LOGIN_KEYS = (
    "login.title",
    "login.username",
    "login.password",
    "login.submit",
    "login.document_title",
    "login.error.AUTH_INVALID_CREDENTIALS",
    "login.error.AUTH_THROTTLED",
    "login.error.RATE_LIMITED",
    "login.error.generic",
)

TAG_RE = re.compile(r"^[a-z]{2,3}(-[A-Z][a-z]{3})?(-([A-Z]{2}|[0-9]{3}))?$")
PLURAL_CATEGORIES = frozenset({"zero", "one", "two", "few", "many", "other"})
SOURCE_LAYER, PACK_LAYER, OVERRIDE_LAYER = "catalog", "packs", "overrides"
_SHARED_LAYERS = (PACK_LAYER, OVERRIDE_LAYER)
_LAYERS = (SOURCE_LAYER, *_SHARED_LAYERS)
_KEYS = {"version", "languages", SOURCE_LAYER, PACK_LAYER, OVERRIDE_LAYER}
_HOLE = re.compile(r"\{([A-Za-z0-9_]+)\}")
_IDENTIFIER = re.compile(r"[A-Za-z0-9_.:/-]{1,120}")
NOT_AN_IDENTIFIER = "<not an identifier>"

Message = str | dict[str, str]
Namespaces = dict[str, dict[str, Message]]


@dataclass(frozen=True)
class Source:
    name: str
    directory: Path
    manifest_dir: Path


class Login(NamedTuple):
    version: str
    messages: dict[str, Message]


@dataclass(frozen=True)
class Catalogs:
    languages: dict[str, str]
    messages: dict[str, Namespaces]
    fallback: dict[str, Namespaces]
    versions: dict[str, str]
    login: dict[str, Login]
    default_language: str | None
    namespaces: dict[str, str]


def sources_for(module_dirs_in_order: Sequence[tuple[str, Path]]) -> list[Source]:
    """The two built-in sources, then each module in the given order. A module that takes a
    built-in namespace is left out: ``module:validate`` reports it."""
    from ..access import acl

    builtin = [
        Source(UI_NAMESPACE, BUILTIN_DIR / UI_NAMESPACE, BUILTIN_DIR / UI_NAMESPACE),
        Source(PANEL_NAMESPACE, BUILTIN_DIR / PANEL_NAMESPACE, acl.CORE_FILE.parent),
    ]
    modules = [Source(name, Path(d), Path(d)) for name, d in module_dirs_in_order
               if name not in BUILTIN_NAMESPACES]
    return [*builtin, *modules]


def ordered_sources(manifests: Sequence) -> list[Source]:
    """The sources of the given module manifests in ``resolve_order``: the one order that
    decides which module wins inside a layer, whatever order the manifests arrive in."""
    from ..dependency_resolver import resolve_order

    return sources_for([(module_name(m.path), m.path) for m in resolve_order(list(manifests))])


def enabled_manifests(module_dirs: Sequence[tuple[str, Path]], status: Mapping[str, bool]) -> list:
    """The manifests of the modules that give texts: the one enablement rule of ``web`` and of a
    process with no snapshot. ``modules.json`` names a module by its directory (the routes and
    the ACL of ``web``) or by its manifest name (``bootstrap``); a module that it disables by
    either name gives no catalog."""
    from ..module_loader import parse_module_dir
    from ..module_status import is_enabled

    return [manifest for name, module_dir in module_dirs
            if is_enabled(name, status) and (manifest := parse_module_dir(module_dir)) is not None
            and is_enabled(manifest.name, status)]


def enabled_sources() -> list[Source]:
    from .. import module_discovery
    from ..bootstrap import CORE_MODULES_DIR, USER_MODULES_DIR
    from ..module_status import read_module_status

    module_dirs = module_discovery.module_dirs_by_name(CORE_MODULES_DIR, USER_MODULES_DIR)
    return ordered_sources(enabled_manifests(module_dirs, read_module_status()))


def _resolved(root: Path, relative: str) -> Path | None:
    """The path ``relative`` of a module directory with every link followed, or None when it is
    outside that directory (SEC-5). The directory itself may be a link: it is resolved first,
    so the module directory is the target of that link."""
    try:
        base = Path(root).resolve()
        target = (base / relative).resolve()
    except (OSError, ValueError, RuntimeError):
        return None
    return target if target.is_relative_to(base) else None


class _No(enum.Enum):
    ABSENT = "absent"
    UNUSABLE = "unusable"


ABSENT, UNUSABLE = _No.ABSENT, _No.UNUSABLE


def _located(root: Path, relative: str) -> Path | _No:
    """One file of a module directory, in three states: the regular file inside the directory
    (SEC-5); ``ABSENT`` when the directory has no entry of that name; ``UNUSABLE`` for an entry
    that is anything else (a directory, a pipe, a device, a link with no target, a link that
    leaves the directory). Nothing is opened: the read of a pipe can wait with no end."""
    try:
        if not os.path.lexists(Path(root) / relative):
            return ABSENT
        path = _resolved(root, relative)
        return path if path is not None and path.is_file() else UNUSABLE
    except (OSError, ValueError):
        return UNUSABLE


def _json(root: Path, relative: str) -> object:
    """The JSON content of one file of a module directory, ``ABSENT``, or ``UNUSABLE`` (also
    for content that is not JSON): the one reader of every file that the loader and the
    validator read."""
    path = _located(root, relative)
    if isinstance(path, _No):
        return path
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError):
        return UNUSABLE


def module_json(module_dir: Path, relative: str) -> dict:
    """The JSON object of one file of a module directory, or an empty one when the file is
    absent, outside the directory, not JSON or not an object."""
    content = _json(Path(module_dir), relative)
    return content if isinstance(content, dict) else {}


def module_name(module_dir: Path) -> str:
    """The name of a module, which is its namespace: ``name`` of its ``module.json``, else the
    directory name. The validator, the loader and the derived keys all use this one."""
    name = module_json(module_dir, "module.json").get("name")
    return name if isinstance(name, str) and name else Path(module_dir).name


def _named(value: object) -> str:
    """The value as a diagnostic may show it: an identifier, a tag or a relative path. Any other
    content of a manifest or a catalog stays out of a log line and a validation message (SEC-6)."""
    return value if isinstance(value, str) and _IDENTIFIER.fullmatch(value) else NOT_AN_IDENTIFIER


def _encodable(text: str) -> bool:
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def catalog_file(module_dir: Path, relative: object) -> Path | None:
    """The file a declaration names, or None when the path leaves the module directory, is
    absolute, or does not end in ``.json`` (SEC-5)."""
    if not isinstance(relative, str) or not relative.endswith(".json") or Path(relative).is_absolute():
        return None
    return _resolved(module_dir, relative)


def holes(message: Message) -> frozenset[str]:
    texts = [message] if isinstance(message, str) else message.values()
    return frozenset(name for text in texts for name in _HOLE.findall(text))


def message_error(message: object, source_message: Message | None = None) -> str | None:
    """Why this catalog entry is refused, or None. ``source_message`` is the English source
    of the same key, when one exists: a translation may not name a param the source lacks."""
    if isinstance(message, dict):
        if "other" not in message:
            return "a plural message needs 'other'"
        if not set(message) <= PLURAL_CATEGORIES or not all(isinstance(v, str) for v in message.values()):
            return f"a plural message maps {sorted(PLURAL_CATEGORIES)} to text"
    elif not isinstance(message, str):
        return "a message is text or a plural object"
    if not all(_encodable(text) for text in ([message] if isinstance(message, str) else message.values())):
        return "a message is text that UTF-8 can encode"
    if source_message is not None and (unknown := holes(message) - holes(source_message)):
        return f"unknown param {_named(sorted(unknown)[0])!r}"
    return None


def entry_error(key: str, message: object, source_message: Message | None = None) -> str | None:
    """Why this ``key: message`` pair is refused, or None: the one check of every entry that
    ``load`` serves and ``module:validate`` accepts."""
    if not _encodable(key):
        return "a key is text that UTF-8 can encode"
    return message_error(message, source_message)


def translation_error(key: str, message: object, english: Mapping[str, Message]) -> str | None:
    """Why this translation is refused, or None. A key with no English source in JSON (a panel
    descriptor text, a limit label) gets no params where it is shown, so its translation names none."""
    return entry_error(key, message, english.get(key, ""))


def _is_language_entry(entry: object) -> bool:
    return (isinstance(entry, dict) and set(entry) == {"tag", "name"} and isinstance(entry["tag"], str)
            and isinstance(entry["name"], str) and bool(entry["name"].strip()) and _encodable(entry["name"]))


def _declaration_problems(data: object) -> list[tuple[str, str]]:
    """``(structural reason, validation message)`` of each problem. The reason names a position
    and never a value, so ``load`` may log it."""
    if not isinstance(data, dict):
        return [("not an object", "i18n.json must be an object")]
    out = [("unknown key", f"i18n.json: unknown key {_named(k)!r}") for k in sorted(set(data) - _KEYS)]
    if type(data.get("version")) is not int or data["version"] != VERSION:
        out.append(("version", f"i18n.json: 'version' must be {VERSION}"))
    languages = data.get("languages", [])
    if not isinstance(languages, list):
        out.append(("languages is not a list", "i18n.json: 'languages' must be a list"))
        languages = []
    for index, entry in enumerate(languages):
        if not _is_language_entry(entry):
            out.append((f"languages[{index}] is not {{tag, name}}",
                        f"i18n.json: languages[{index}] must be {{tag, name}} with a non-empty name"))
        elif not TAG_RE.fullmatch(entry["tag"]):
            out.append((f"languages[{index}] tag",
                        f"i18n.json: language tag {_named(entry['tag'])!r} must match {TAG_RE.pattern}"))
    for layer in _LAYERS:
        files = data.get(layer, {})
        if not isinstance(files, dict):
            out.append((f"{layer} is not an object", f"i18n.json: {layer!r} must be an object of language tag to file"))
            continue
        out += [(f"{layer} language tag",
                 f"i18n.json: {layer} language tag {_named(tag)!r} must match {TAG_RE.pattern}")
                for tag in files if not TAG_RE.fullmatch(tag)]
    return out


def declaration_errors(data: object) -> list[str]:
    """Why this ``i18n.json`` content is refused. One function for ``module:validate`` and
    for ``load``, so the two cannot disagree."""
    return [message for _reason, message in _declaration_problems(data)]


def _declaration(source: Source) -> dict:
    data = _json(source.directory, FILE_NAME)
    if data is ABSENT:
        return {}
    if data is UNUSABLE:
        _log_unusable(source, FILE_NAME)
        return {}
    if problems := _declaration_problems(data):
        logger.warning("i18n skip: module=%s file=%s reason=%s", _named(source.name), FILE_NAME, problems[0][0])
        return {}
    assert isinstance(data, dict)
    return data


def _log_unusable(source: Source, name: str) -> None:
    logger.warning("i18n skip: module=%s file=%s reason=unusable file", _named(source.name), name)


def _file_content(source: Source, layer: str, language: str, relative: object) -> dict:
    declared = catalog_file(source.directory, relative) is not None
    data = _json(source.directory, cast(str, relative)) if declared else None
    if not isinstance(data, dict):
        logger.warning("i18n skip: module=%s layer=%s language=%s reason=unreadable file",
                       _named(source.name), layer, language)
        return {}
    return data


def _layer_entries(source: Source, declaration: dict, layer: str,
                   namespaces: frozenset[str]) -> Iterator[tuple[str, str, str, object]]:
    """``(language, namespace, key, message)`` of one layer of one source. A source-layer file
    is flat and belongs to the source's own namespace; a pack or override file is nested, and
    only a namespace of ``namespaces`` (the sources of this load) is read from it."""
    for language, relative in declaration.get(layer, {}).items():
        content = _file_content(source, layer, language, relative)
        nested = {source.name: content} if layer == SOURCE_LAYER else content
        for namespace, entries in nested.items():
            reason = ("not an enabled namespace" if namespace not in namespaces
                      else None if isinstance(entries, dict) else "not an object")
            if reason:
                logger.warning("i18n skip: module=%s layer=%s language=%s namespace=%s reason=%s",
                               _named(source.name), layer, language, _named(namespace), reason)
                continue
            for key, message in entries.items():
                yield language, namespace, key, message


def _config_messages(system: object) -> dict[str, str]:
    out: dict[str, str] = {}
    for path, field in (system.items() if isinstance(system, dict) else ()):
        if not isinstance(field, dict):
            continue
        for attribute in ("label", "description"):
            if isinstance(text := field.get(attribute), str) and text:
                out[f"config.{path}.{attribute}"] = text
        options = field.get("options")
        for option in options if isinstance(options, list) else ():
            if isinstance(option, dict) and isinstance(option.get("label"), str) and "value" in option:
                out[f"config.{path}.option.{option['value']}"] = option["label"]
    return out


def _titled(entries: object) -> Iterator[tuple[str, str]]:
    for entry in entries if isinstance(entries, list) else ():
        if isinstance(entry, dict) and isinstance(entry.get("id"), str) and isinstance(entry.get("title"), str):
            yield entry["id"], entry["title"]


def _acl_messages(data: object) -> dict[str, str]:
    if not isinstance(data, dict):
        return {}
    out: dict[str, str] = {}
    screens = data.get("screens")
    for screen in screens if isinstance(screens, list) else ():
        leaves = list(_titled(screen.get("leaves"))) if isinstance(screen, dict) else []
        if leaves and isinstance(screen.get("title"), str):
            out[f"acl.screen.{leaves[0][0]}"] = screen["title"]
        out.update((f"acl.{leaf}", title) for leaf, title in leaves)
    out.update((f"acl.{operation}", title) for operation, title in _titled(data.get("operations")))
    return out


MANIFEST_FILES = ("module.json", "system.json", "acl.json")
_REGULAR = "must be a regular file inside the module directory"


def module_title(name: str) -> str:
    return " ".join(word.capitalize() for word in name.split("_"))


def manifest_messages(module_dir: Path) -> dict[str, str]:
    """The English source messages a directory's manifests give: ``system.json`` labels,
    descriptions and static option labels, the config module title, ``acl.json`` titles.
    The manifest text is the source, so no ``en`` catalog repeats it."""
    module_dir = Path(module_dir)
    out: dict[str, str] = {}
    if isinstance(_located(module_dir, "system.json"), Path):
        out["module.title"] = module_title(module_name(module_dir))
        out.update(_config_messages(_json(module_dir, "system.json")))
    out.update(_acl_messages(_json(module_dir, "acl.json")))
    return out


def english_source(source: Source) -> dict[str, Message]:
    """The valid English source messages of one source: its manifest texts, then its ``en`` catalog."""
    out: dict[str, Message] = dict(manifest_messages(source.manifest_dir))
    declaration = _json(source.directory, FILE_NAME)
    files = declaration.get(SOURCE_LAYER) if isinstance(declaration, dict) else None
    relative = files.get(SOURCE_LANGUAGE) if isinstance(files, dict) else None
    declared = catalog_file(source.directory, relative) is not None
    content = _json(source.directory, cast(str, relative)) if declared else None
    for key, message in (content.items() if isinstance(content, dict) else ()):
        if entry_error(key, message) is None:
            out[key] = cast(Message, message)
    return out


def _file_errors(module_dir: Path, layer: str, language: str, relative: object,
                 own_namespace: str, english_of) -> list[str]:
    where = f"i18n.json: {layer}.{_named(language)}"
    path = catalog_file(module_dir, relative)
    if path is None:
        return [f"{where}: path {_named(relative)!r} must be relative, stay inside the module directory "
                "and end in .json"]
    state = _located(module_dir, cast(str, relative))
    if state is ABSENT:
        return [f"{where}: file {_named(relative)!r} not found"]
    if state is UNUSABLE:
        return [f"{where}: file {_named(relative)!r} {_REGULAR}"]
    content = _json(module_dir, cast(str, relative))
    if not isinstance(content, dict):
        return [f"{where}: {_named(relative)!r} must be a JSON object"]
    nested = {own_namespace: content} if layer == SOURCE_LAYER else content
    out = []
    for namespace, entries in nested.items():
        english = english_of(namespace)
        if english is None:
            out.append(f"{where}: namespace {_named(namespace)!r} is not a known module, 'ui' or 'panel'")
        elif not isinstance(entries, dict):
            out.append(f"{where}: namespace {_named(namespace)!r} must be an object of key to message")
        else:
            is_source_text = layer == SOURCE_LAYER and language == SOURCE_LANGUAGE
            out += [f"{where}: {_named(namespace)}:{_named(key)}: {problem}" for key, message in entries.items()
                    if (problem := entry_error(key, message) if is_source_text
                        else translation_error(key, message, english))]
    return out


def module_errors(module_dir: Path, module_dirs: Sequence[Path]) -> list[str]:
    """Why ``module:validate`` refuses this module's ``i18n.json`` and the files it names.
    ``module_dirs`` is every known module: a pack or an override may target one of them, or a
    built-in namespace. A partial catalog is valid."""
    module_dir = Path(module_dir)
    state = _located(module_dir, FILE_NAME)
    if state is ABSENT:
        return []
    name = module_name(module_dir)
    if name in BUILTIN_NAMESPACES:
        return [f"i18n.json: the namespace {name!r} belongs to the framework; a module with this name ships no i18n.json"]
    if state is UNUSABLE:
        return [f"i18n.json: the file {_REGULAR}"]
    declaration = _json(module_dir, FILE_NAME)
    if isinstance(declaration, _No):
        return ["i18n.json: invalid JSON"]
    if problems := declaration_errors(declaration):
        return problems
    assert isinstance(declaration, dict)
    others = [Path(d) for d in module_dirs if Path(d) != module_dir]
    sources = {s.name: s for s in sources_for([(module_name(d), d) for d in others])}
    sources[name] = Source(name, module_dir, module_dir)
    english: dict[str, dict[str, Message]] = {}

    def english_of(namespace: str) -> dict[str, Message] | None:
        if namespace not in sources:
            return None
        if namespace not in english:
            english[namespace] = english_source(sources[namespace])
        return english[namespace]

    return [problem for layer in _LAYERS
            for language, relative in declaration.get(layer, {}).items()
            for problem in _file_errors(module_dir, layer, language, relative, name, english_of)]


def _registry(declared: Sequence[tuple[Source, dict]]) -> dict[str, str]:
    return {entry["tag"]: entry["name"] for _source, declaration in declared
            for entry in declaration.get("languages", [])}


def _skip(source: Source, layer: str, language: str, namespace: str, key: str, reason: str) -> None:
    logger.warning("i18n skip: module=%s layer=%s language=%s namespace=%s key=%s reason=%s",
                   _named(source.name), layer, language, _named(namespace), _named(key), reason)


def _version(messages: Namespaces, fallback: Namespaces) -> str:
    canonical = json.dumps({"messages": messages, "fallback": fallback}, sort_keys=True,
                           separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def _login(messages: Namespaces, fallback: Namespaces) -> Login:
    """Exactly ``LOGIN_KEYS`` of one language, each with its English text when the language lacks it."""
    layers = (fallback.get(PANEL_NAMESPACE, {}), messages.get(PANEL_NAMESPACE, {}))
    texts = {key: layer[key] for key in LOGIN_KEYS for layer in layers if key in layer}
    return Login(_version({PANEL_NAMESPACE: texts}, {}), texts)


def _missing_from(messages: Namespaces, english: Namespaces) -> Namespaces:
    missing = {namespace: {key: message for key, message in entries.items()
                           if key not in messages.get(namespace, {})}
               for namespace, entries in english.items()}
    return {namespace: entries for namespace, entries in missing.items() if entries}


def _read_source(source: Source, namespaces: frozenset[str]) -> tuple[dict, dict[str, str], dict[str, list]]:
    """``(declaration, manifest messages, entries per layer)`` of one source. Whatever its files
    make a reader raise ends here: the source gives nothing, and the other sources load."""
    try:
        declaration = _declaration(source)
        for file in MANIFEST_FILES:
            if _json(source.manifest_dir, file) is UNUSABLE:
                _log_unusable(source, file)
        return (declaration, manifest_messages(source.manifest_dir),
                {layer: list(_layer_entries(source, declaration, layer, namespaces)) for layer in _LAYERS})
    except Exception as exc:
        logger.warning("i18n skip: module=%s reason=%s", _named(source.name), type(exc).__name__)
        return {}, {}, {layer: [] for layer in _LAYERS}


def _named_sources(sources: Sequence[Source]) -> list[Source]:
    named = [source for source in sources if _IDENTIFIER.fullmatch(source.name)]
    if len(named) != len(sources):
        logger.warning("i18n skip: modules=%d reason=module name is not an identifier", len(sources) - len(named))
    return named


def _default_in(module_dir: Path | None, registry: Mapping[str, object]) -> str:
    """The ``config.json`` default of the instance language when it is a tag of ``registry``,
    else the source language. A default that is dropped is logged, and its value is not."""
    defaults = _json(module_dir, "config.json") if module_dir is not None else ABSENT
    if defaults is UNUSABLE:
        logger.warning("i18n skip: module=%s file=config.json reason=unusable file", _LANGUAGE_OWNER)
    default = defaults.get(_LANGUAGE_FIELD) if isinstance(defaults, dict) else None
    if isinstance(default, str) and default in registry:
        return default
    if default is not None:
        logger.warning("i18n: the config.json default of %s is not a registered language; %s is used",
                       LANGUAGE_PATH, SOURCE_LANGUAGE)
    return SOURCE_LANGUAGE


def _default_language(sources: Sequence[Source], registry: Mapping[str, object]) -> str | None:
    """The ``config.json`` default of the instance language, or None when no source is its owner module."""
    owner = next((source for source in sources if source.name == _LANGUAGE_OWNER), None)
    if owner is None:
        return None
    try:
        return _default_in(owner.directory, registry)
    except Exception as exc:
        logger.warning("i18n: the default instance language cannot be read (%s)", type(exc).__name__)
        return SOURCE_LANGUAGE


def load(sources: Sequence[Source]) -> Catalogs:
    """Compose the catalogs of the given sources, in their order. Reads JSON only. An entry or
    a file that is not valid is skipped with one log line and the lower layer stays, so an
    extension file never stops the caller. A pack or an override reaches only the namespaces
    of the given sources."""
    sources = _named_sources(sources)
    namespaces = frozenset(source.name for source in sources)
    read = [(source, *_read_source(source, namespaces)) for source in sources]
    english: Namespaces = {source.name: {} for source in sources}
    translated: dict[str, list] = {layer: [] for layer in _LAYERS}
    for source, _declared, manifest, layers in read:
        source_entries = [(SOURCE_LANGUAGE, source.name, key, text) for key, text in manifest.items()]
        for language, namespace, key, message in (*source_entries, *layers[SOURCE_LAYER]):
            if language != SOURCE_LANGUAGE:
                translated[SOURCE_LAYER].append((source, language, namespace, key, message))
            elif problem := entry_error(key, message):
                _skip(source, SOURCE_LAYER, language, namespace, key, problem)
            else:
                english[namespace][key] = cast(Message, message)
        for layer in _SHARED_LAYERS:
            translated[layer] += [(source, *entry) for entry in layers[layer]]

    composed: dict[str, Namespaces] = {
        SOURCE_LANGUAGE: {namespace: dict(entries) for namespace, entries in english.items()}}
    for layer in _LAYERS:
        writers: dict[tuple[str, str, str], str] = {}
        for source, language, namespace, key, message in translated[layer]:
            if problem := translation_error(key, message, english[namespace]):
                _skip(source, layer, language, namespace, key, problem)
                continue
            written = (language, namespace, key)
            if written in writers:
                logger.warning(
                    "i18n conflict: layer=%s language=%s namespace=%s key=%s modules=%s,%s winner=%s",
                    layer, language, _named(namespace), _named(key), writers[written], source.name, source.name)
            writers[written] = source.name
            composed.setdefault(language, {}).setdefault(namespace, {})[key] = cast(Message, message)

    languages = _registry([(source, declaration) for source, declaration, _manifest, _layers in read])
    source_text = {namespace: entries for namespace, entries in composed[SOURCE_LANGUAGE].items() if entries}
    messages = {tag: {namespace: entries for namespace, entries in composed.get(tag, {}).items() if entries}
                for tag in languages}
    fallback = {tag: {} if tag == SOURCE_LANGUAGE else _missing_from(messages[tag], source_text) for tag in languages}
    return Catalogs(
        languages=languages, messages=messages, fallback=fallback,
        versions={tag: _version(messages[tag], fallback[tag]) for tag in languages},
        login={tag: _login(messages[tag], fallback[tag]) for tag in languages},
        default_language=_default_language(sources, languages),
        namespaces={source.directory.name: source.name for source in sources})


_CATALOGS: Catalogs | None = None


def install(catalogs: Catalogs | None) -> None:
    """Set the process snapshot: ``web`` does it once at start. None clears it."""
    global _CATALOGS
    _CATALOGS = catalogs


def current() -> Catalogs:
    """The process snapshot; on first use in a process that installed none, the enabled modules' files."""
    if _CATALOGS is None:
        install(load(enabled_sources()))
    assert _CATALOGS is not None
    return _CATALOGS


def languages() -> dict[str, str]:
    """``{tag: own-language name}``. From the snapshot when one is installed; else read off
    disk on each call, so a CLI process sees a pack that was enabled a moment ago."""
    if _CATALOGS is not None:
        return _CATALOGS.languages
    return _registry([(source, _declaration(source)) for source in enabled_sources()])


def normalize(tag: object, registry: Mapping[str, str]) -> str:
    return tag if isinstance(tag, str) and tag in registry else SOURCE_LANGUAGE


def effective(stored: str | None, instance: str, registry: Mapping[str, str]) -> str:
    return stored if stored is not None and stored in registry else normalize(instance, registry)


def _stored_instance_language(conn, *, strict: bool) -> str | None:
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT value FROM core_config_data WHERE scope = 'default' AND scope_id = 0"
                " AND path = %s AND encrypted = 0", (LANGUAGE_PATH,))
            row = cur.fetchone()
    except Exception as exc:
        if strict:
            raise
        logger.warning("i18n: the instance language cannot be read (%s)", type(exc).__name__)
        return None
    return row["value"] if row else None


def _owner_default() -> str | None:
    """The ``config.json`` default of the instance language, or None when its owner module is
    disabled. ``web`` answers from its snapshot; a process with no snapshot reads the files."""
    if _CATALOGS is not None:
        return _CATALOGS.default_language
    return _default_language(enabled_sources(), languages())


def instance_language(conn, *, strict: bool = False) -> str:
    """The deployment's panel language: ENV, then the default-scope DB row, then ``config.json``.
    The owner module disabled, or a tag the registry lacks, gives the source language.
    ``strict`` raises when the DB read fails, so a caller that stores the answer never stores a guess."""
    from ..config_resolver import path_to_env_key

    default = _owner_default()
    if default is None:
        return SOURCE_LANGUAGE
    configured = os.environ.get(path_to_env_key(LANGUAGE_PATH))
    if configured is None:
        configured = _stored_instance_language(conn, strict=strict)
    return normalize(default if configured is None else configured, languages())


def account_language(conn) -> str:
    """The tag a new or migrated account gets: one decision for ``create_user`` and the upgrade backfill."""
    return effective(None, instance_language(conn, strict=True), languages())
