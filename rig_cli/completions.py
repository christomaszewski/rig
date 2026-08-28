"""Shell completion — the Python engine behind the hidden ``rig _complete`` verb.

ALL completion logic lives here; the shell wrappers (``rig completion bash|zsh``) only forward
the command line and print candidates. One source of truth: the argparse tree (``build_parser``)
merged with the noun-group translation (``_GROUP_VERBS``) that lives outside argparse — grouped
spellings (``rig image build``) and flat ones complete equally.

Menu policy (completion plan, OQ-A): TAB suggests the CANONICAL grammar only. Legacy flat
spellings (``new-run``, ``image-audit``, …) and permanent aliases (``pkg install``, ``pkg rm``)
keep parsing forever but are never suggested — completion is a teaching surface, and the docs
teach the grouped forms. The suggested set is derived, not hand-listed: everything in
``_GROUP_VERBS``'s flat targets is hidden except the verbs the top-of-cli taxonomy keeps
top-level (pull/certify/config), so a new group entry hides its flat alias automatically.

Fail-soft ABSOLUTELY: any stderr or traceback mid-TAB garbles the user's command line, so
``main`` prints nothing on any internal failure and always exits 0. The engine never writes,
never touches network or git, and reads deployment/user state raw (never through ``_load`` —
identity gates and config rendering have no place in a TAB).
"""
from __future__ import annotations

import argparse
import functools
import sys
from pathlib import Path

# Flat targets of group verbs that are ALSO canonical top-level (cli.py taxonomy: the
# deployment is the implicit noun) — everything else in _GROUP_VERBS' values is legacy-flat.
_CANONICAL_FLAT = {"pull", "certify", "config"}


def main(argv: list[str]) -> int:
    """``rig _complete [--describe] <cword> -- <word> …`` → candidates on stdout, one per line
    (``--describe``: ``candidate<TAB>help`` where a description exists — the zsh menu).
    ``rig _complete --line`` reads COMP_LINE/COMP_POINT/COMP_WORDBREAKS from the env instead —
    the bash wrapper's whole protocol, so word splitting AND readline's wordbreak trimming
    (the colon-heavy grammar's one fiddly bit) live here, tested, not in shell."""
    try:
        if argv and argv[0] == "--line":
            pairs = _line_mode()
        else:
            describe = bool(argv) and argv[0] == "--describe"
            if describe:
                argv = argv[1:]
            cword = int(argv[0])
            if argv[1] != "--":
                return 0
            pairs = [(n, h if describe else None) for n, h in described(argv[2:], cword)]
        lines = [f"{name}\t{help_}" if help_ else name for name, help_ in pairs]
        if lines:
            sys.stdout.write("\n".join(lines) + "\n")
    except Exception:
        pass
    return 0


def _line_mode() -> list[tuple[str, str | None]]:
    import os
    import re
    import shlex

    line = os.environ.get("COMP_LINE") or ""
    try:
        point = int(os.environ.get("COMP_POINT") or len(line))
    except ValueError:
        point = len(line)
    text = line[:point]
    words = None
    for tail in ("", "'", '"'):  # tolerate an unterminated quote at the cursor
        try:
            words = shlex.split(text + tail)
            break
        except ValueError:
            continue
    if not words:
        return []
    if text[-1:].isspace():
        words.append("")  # the cursor starts a fresh word
    words = words[1:] or [""]  # drop the program token (rig / ./rig)
    pairs = [(n, None) for n, _ in described(words, len(words) - 1)]
    # readline replaces only the region after the last COMP_WORDBREAKS char of the typed word
    # (bash splits `cam-service:ind` at the colon) — emit candidates trimmed to that region.
    raw = re.split(r"\s", text)[-1]
    breaks = os.environ.get("COMP_WORDBREAKS") or " \t\n\"'><=;|&(:"
    cut = max(raw.rfind(ch) for ch in breaks) + 1 if raw else 0
    if cut > 0:
        pairs = [(name[cut:], h) for name, h in pairs if len(name) > cut]
    return pairs


def candidates(words: list[str], cword: int) -> list[str]:
    return [name for name, _ in described(words, cword)]


def described(words: list[str], cword: int) -> list[tuple[str, str | None]]:
    """(candidate, help) pairs for the token at ``words[cword]`` (may be one past the end =
    an empty current word), already prefix-filtered, sorted, deduped."""
    cword = max(0, min(cword, len(words)))
    cur = words[cword] if cword < len(words) else ""
    out = _walk(list(words), cword, cur)
    seen, result = set(), []
    for name, help_ in sorted(out, key=lambda p: p[0]):
        if name.startswith(cur) and name not in seen:
            seen.add(name)
            result.append((name, help_))
    return result


# --- the grammar walk -------------------------------------------------------

def _walk(words: list[str], cword: int, cur: str) -> list[tuple[str, str | None]]:
    from .cli import _GROUP_VERBS

    parser = _parser()
    # Skip global flags to find the command token — mirrors translate_argv's scan.
    i, root_arg = 0, None
    while i < cword:
        tok = words[i]
        if tok == "--root":
            root_arg = words[i + 1] if i + 1 < len(words) else None
            i += 2
        elif tok.startswith("--root="):
            root_arg = tok[len("--root="):]
            i += 1
        elif tok.startswith("-"):
            i += 1
        else:
            break
    if i >= cword:  # completing the command token itself (or a global flag / its value)
        if cword > 0 and words[cword - 1] == "--root":
            return []  # directory — the shell's file fallback owns it
        if cur.startswith("-"):
            return [("--root", None), ("--version", None), ("--help", None)]
        return _command_menu(parser)

    command = words[i]
    group = _GROUP_VERBS.get(command)
    if group is not None and command != "config":
        if cword == i + 1 and not cur.startswith("-"):
            return _group_menu(parser, group)
        flat = group.get(words[i + 1]) if i + 1 < len(words) else None
        if flat is None:
            return []
        words = words[:i] + [flat] + words[i + 2:]  # the translate_argv rewrite
        cword -= 1
        command = flat
    elif command == "config" and cword == i + 1 and not cur.startswith("-"):
        # Bare `config` is both a group (show/render/diff) and the legacy positional-names
        # form — offer the verbs plus whatever the names position offers.
        return _group_menu(parser, group) + _positional_candidates(("config",), "names", root_arg)
    elif group is not None and command == "config" and cword > i + 1:
        flat = group.get(words[i + 1])
        if flat is not None:
            words = words[:i] + [flat] + words[i + 2:]
            cword -= 1
            command = flat
        # else: legacy `config <sensor…>` — fall through to the flat `config` parser

    action = _subparsers(parser)
    sub = action.choices.get(command) if action else None
    if sub is None:
        return []
    ctx: tuple[str, ...] = (command,)
    start = i + 1

    nested = _subparsers(sub)
    if nested is not None:
        if cword == start and not cur.startswith("-"):
            return _nested_menu(nested)
        if cword <= start:
            return []
        sub = nested.choices.get(words[start])
        if sub is None:
            return []
        ctx = (command, words[start])
        start += 1

    return _within(sub, ctx, words, start, cword, cur, root_arg)


def _within(p: argparse.ArgumentParser, ctx: tuple[str, ...], words: list[str],
            start: int, cword: int, cur: str, root_arg: str | None) -> list[tuple[str, str | None]]:
    """Complete inside one (sub)parser: flags, flag values, then positionals."""
    opts = {s: a for a in p._actions for s in a.option_strings}

    if cur.startswith("--") and "=" in cur:  # --kind=pro<TAB>
        opt, _, _ = cur.partition("=")
        act = opts.get(opt)
        vals = _option_candidates(ctx, act, root_arg) if act is not None and _takes_value(act) else []
        return [(f"{opt}={v}", h) for v, h in vals]
    if cur.startswith("-"):
        used = set(words[start:cword])
        pairs = []
        for act in p._actions:
            if not act.option_strings or act.help is argparse.SUPPRESS:
                continue
            if not isinstance(act, argparse._AppendAction) and used & set(act.option_strings):
                continue  # already given, not repeatable
            long = [s for s in act.option_strings if s.startswith("--")]
            pairs.append(((long or act.option_strings)[0], act.help))
        return pairs

    prev = words[cword - 1] if cword > start else None
    if prev in opts and _takes_value(opts[prev]):
        return _option_candidates(ctx, opts[prev], root_arg, words)

    typed: list[str] = []  # positional tokens before the cursor (flag values skipped)
    k = start
    while k < cword:
        tok = words[k]
        if tok.startswith("-") and tok != "-":
            act = opts.get(tok.partition("=")[0])
            if act is not None and _takes_value(act) and "=" not in tok:
                k += 1
        else:
            typed.append(tok)
        k += 1
    target = _positional_at(p, len(typed))
    if target is None:
        return []
    if target.choices:
        return [(str(c), None) for c in target.choices]
    return _positional_candidates(ctx, target.dest, root_arg, typed, words)


def _takes_value(act: argparse.Action) -> bool:
    return act.nargs != 0


def _positional_at(p: argparse.ArgumentParser, ntyped: int) -> argparse.Action | None:
    for act in (a for a in p._actions if not a.option_strings):
        if act.nargs in (None, "?"):
            if ntyped == 0:
                return act
            ntyped -= 1
        elif isinstance(act.nargs, int):
            if ntyped < act.nargs:
                return act
            ntyped -= act.nargs
        else:  # * / + / … absorb every later position
            return act
    return None


# --- menus ------------------------------------------------------------------

def _parser() -> argparse.ArgumentParser:
    from .cli import build_parser
    return build_parser()


def _subparsers(p: argparse.ArgumentParser):
    for act in p._actions:
        if isinstance(act, argparse._SubParsersAction):
            return act
    return None


def _helps(action) -> dict[str, str | None]:
    return {a.dest: a.help for a in getattr(action, "_choices_actions", [])}


def _command_menu(parser) -> list[tuple[str, str | None]]:
    from .cli import _GROUP_VERBS

    action = _subparsers(parser)
    hidden = ({flat for verbs in _GROUP_VERBS.values() for flat in verbs.values()}
              - _CANONICAL_FLAT)
    helps = _helps(action)
    seen_parsers: set[int] = set()
    pairs: list[tuple[str, str | None]] = []
    for name, sub in action.choices.items():
        if id(sub) in seen_parsers:
            continue  # permanent aliases (pkg install/rm live nested; top level: future-proof)
        seen_parsers.add(id(sub))
        if name in hidden:
            continue
        pairs.append((name, helps.get(name)))
    named = {n for n, _ in pairs}
    pairs += [(g, f"{'|'.join(sorted(v))}") for g, v in _GROUP_VERBS.items() if g not in named]
    return pairs


def _group_menu(parser, group: dict[str, str]) -> list[tuple[str, str | None]]:
    action = _subparsers(parser)
    helps = _helps(action)
    return [(verb, helps.get(flat)) for verb, flat in group.items()]


def _nested_menu(action) -> list[tuple[str, str | None]]:
    helps = _helps(action)
    seen_parsers: set[int] = set()
    pairs = []
    for name, sub in action.choices.items():
        if id(sub) in seen_parsers:
            continue  # `pkg install`/`pkg rm` parse forever but the menu teaches add/remove
        seen_parsers.add(id(sub))
        pairs.append((name, helps.get(name)))
    return pairs


# --- dynamic value sources (F2) ---------------------------------------------
# Raw reads only — vehicle.yaml/services.yaml/fleet.yaml/registries.yaml/index.json/rig.lock —
# never through _load() (identity gates and rendering have no place in a TAB). Keyed by the
# FLAT command context (group spellings are rewritten before lookup); a (ctx, dest) absent
# from the tables completes to nothing and the shell's file fallback takes over. Every source
# is individually fail-soft: broken YAML, no deployment, unsynced caches → [].

def _positional_candidates(ctx: tuple[str, ...], dest: str, root_arg: str | None,
                           positionals: list[str] | None = None,
                           words: list[str] | None = None) -> list[tuple[str, str | None]]:
    fn = _POSITIONAL_SOURCES.get((ctx, dest))
    return fn(root_arg, positionals or [], words or []) if fn else []


def _option_candidates(ctx: tuple[str, ...], act: argparse.Action, root_arg: str | None,
                       words: list[str] | None = None) -> list[tuple[str, str | None]]:
    if act.choices:
        return [(str(c), None) for c in act.choices]
    opt = ([s for s in act.option_strings if s.startswith("--")] or act.option_strings)[0]
    fn = _OPTION_SOURCES.get((ctx, opt))
    return fn(root_arg, [], words or []) if fn else []


def _soft(fn):
    @functools.wraps(fn)  # sources compose via .__wrapped__ (raw strings, no pair envelope)
    def wrapper(root_arg, positionals, words):
        try:
            return [(str(c), None) for c in fn(root_arg, positionals, words)]
        except Exception:
            return []
    return wrapper


def _deployment(root_arg: str | None) -> Path | None:
    from .cli import find_root
    root = (Path(root_arg).expanduser() if root_arg else find_root()).resolve()
    return root if (root / "vehicle.yaml").is_file() else None


def _read_yaml(path: Path):
    import yaml
    return yaml.safe_load(path.read_text()) or {}


def _rows(root: Path):
    data = _read_yaml(root / "vehicle.yaml")
    for tier in ("infra", "sensors", "autonomy"):
        for row in data.get(tier) or []:
            if isinstance(row, dict):
                yield row


@_soft
def _instances(root_arg, positionals, words):
    root = _deployment(root_arg)
    return [str(r["name"]) for r in _rows(root) if r.get("name")] if root else []


@_soft
def _bound_overlays(root_arg, positionals, words):
    """The instance's OWN bound refs (row `overlays:` list) — remove/reorder complete what is
    actually bound, not the whole catalog."""
    root = _deployment(root_arg)
    if not root or not positionals:
        return []
    row = next((r for r in _rows(root) if str(r.get("name")) == positionals[0]), None)
    return [str(o) for o in (row or {}).get("overlays") or []]


@_soft
def _services(root_arg, positionals, words):
    root = _deployment(root_arg)
    if not root:
        return []
    return list((_read_yaml(root / "services.yaml").get("services") or {}).keys())


def _registry_entries() -> list[tuple[str, Path]]:
    """(name, registry root) per configured registry — local-dir in place, git in the cache."""
    from .registries import cache_dir, registries_file
    if not registries_file().is_file():  # no ~/.rig yet — local sources must still complete
        return []
    out = []
    for raw in _read_yaml(registries_file()).get("registries") or []:
        raw = raw or {}
        name = str(raw.get("name") or "")
        if not name:
            continue
        root = (Path(str(raw.get("path"))).expanduser() if raw.get("path")
                else cache_dir(name))
        out.append((name, root))
    return out


def _indexes():
    import json
    for name, root in _registry_entries():
        try:
            yield name, json.loads((root / "index.json").read_text())
        except Exception:
            continue  # unsynced/broken cache — skip, never fail the sweep


def _refs(kind: str | None = None) -> list[str]:
    """Qualified `registry/name` refs plus bare names (both resolve; the bare form matches
    what the user usually types first), optionally one kind only."""
    out: list[str] = []
    for reg, index in _indexes():
        for name, meta in (index.get("packages") or {}).items():
            if kind and (meta or {}).get("kind") != kind:
                continue
            out += [f"{reg}/{name}", name]
    return out


def _sensor_ids() -> list[str]:
    return [f"sensor:{key}" for _, index in _indexes() for key in index.get("sensors") or {}]


@_soft
def _pkg_refs(root_arg, positionals, words):
    return _refs()


@_soft
def _overlay_refs(root_arg, positionals, words):
    return _refs(kind="overlay")


@_soft
def _profile_names(root_arg, positionals, words):
    return _refs(kind="profile")


@_soft
def _add_specs(root_arg, positionals, words):
    # The add grammar: registry ref | sensor:<id> | workspace/service name (paths ride the
    # shell's file fallback). Completion suggests, the route_add router decides. Broken
    # GLOBAL state (~/.rig) must not silence the local half.
    out = list(_services.__wrapped__(root_arg, positionals, words))
    try:
        out += _refs() + _sensor_ids()
    except Exception:
        pass
    return out


@_soft
def _remove_specs(root_arg, positionals, words):
    root = _deployment(root_arg)
    names = _instances.__wrapped__(root_arg, positionals, words)
    if root:  # a package name works for instance-less dependencies — from rig.lock refs
        packages = (_read_yaml(root / "rig.lock").get("packages") or {})
        names += [ref.rsplit("@", 1)[0].split("/", 1)[-1] for ref in packages]
    return names


@_soft
def _save_specs(root_arg, positionals, words):
    return (_instances.__wrapped__(root_arg, positionals, words)
            + _services.__wrapped__(root_arg, positionals, words))


@_soft
def _registry_names(root_arg, positionals, words):
    return [name for name, _ in _registry_entries()]


@_soft
def _artifacts(root_arg, positionals, words):
    root = _deployment(root_arg)
    if not root:
        return []
    out = []
    for path in sorted((root / "var" / "artifacts").glob("*.tar.gz")):
        try:
            out.append(str(path.relative_to(Path.cwd())))
        except ValueError:
            out.append(str(path))
    return out


@_soft
def _run_ids(root_arg, positionals, words):
    """Run-registry ids (newest first — the run you want is almost always recent). Raw-read
    doctrine: data_dir comes from vehicle.local.yaml/vehicle.yaml verbatim; an unresolvable
    {{var}} value bails to the file fallback rather than guessing."""
    root = _deployment(root_arg)
    if not root:
        return []
    data_dir = None
    for path in (Path("/etc/rig/vehicle.local.yaml"), root / "vehicle.local.yaml",
                 root / "vehicle.yaml"):  # local overrides win, as in the manifest load
        if path.is_file() and _read_yaml(path).get("data_dir"):
            data_dir = str(_read_yaml(path)["data_dir"])
            break
    if not data_dir or "{{" in data_dir:
        return []
    runs = Path(data_dir) / "runs"
    return sorted((d.name for d in runs.iterdir() if d.is_dir()), reverse=True) \
        if runs.is_dir() else []


@_soft
def _fleet_vehicles(root_arg, positionals, words):
    import os
    explicit = None  # the standard chain: --fleet > $RIG_FLEET > upward walk (fleet.py)
    for i, tok in enumerate(words):
        if tok == "--fleet" and i + 1 < len(words):
            explicit = words[i + 1]
        elif tok.startswith("--fleet="):
            explicit = tok[len("--fleet="):]
    if explicit:
        path = Path(explicit).expanduser()
    elif os.environ.get("RIG_FLEET"):
        path = Path(os.environ["RIG_FLEET"]).expanduser()
    else:
        cwd = Path.cwd()
        path = next((d / "fleet.yaml" for d in (cwd, *cwd.parents)
                     if (d / "fleet.yaml").is_file()), None)
    if not path or not path.is_file():
        return []
    return [str(v["name"]) for v in _read_yaml(path).get("vehicles") or []
            if isinstance(v, dict) and v.get("name")]


_INSTANCE_VERBS = ("up", "down", "config", "config-render", "config-diff", "pull", "status",
                   "logs", "image-audit", "cleanup", "certify", "doctor")

_POSITIONAL_SOURCES: dict = {
    **{((verb,), "names"): _instances for verb in _INSTANCE_VERBS},
    (("add",), "service"): _add_specs,
    (("vendor",), "service"): _services,
    (("unbake",), "artifact"): _artifacts,
    (("graph",), "run"): _run_ids,
    (("replay",), "run"): _run_ids,
    (("replay",), "names"): _instances,
    (("pkg", "add"), "spec"): _add_specs,
    (("pkg", "remove"), "specs"): _remove_specs,
    (("pkg", "save"), "spec"): _save_specs,
    (("pkg", "upgrade"), "names"): _instances,
    (("pkg", "promote"), "names"): _instances,
    (("pkg", "info"), "ref"): _pkg_refs,
    (("pkg", "yank"), "ref"): _pkg_refs,
    (("pkg", "repin"), "ref"): _pkg_refs,
    (("pkg", "rebase"), "name"): _profile_names,
    (("pkg", "outdated"), "refs"): _pkg_refs,
    (("overlay", "apply"), "instance"): _instances,
    (("overlay", "apply"), "ref"): _overlay_refs,
    (("overlay", "remove"), "instance"): _instances,
    (("overlay", "remove"), "ref"): _bound_overlays,
    (("overlay", "reorder"), "instance"): _instances,
    (("overlay", "reorder"), "refs"): _bound_overlays,
    (("overlay", "list"), "instance"): _instances,
    **{(("registry", verb), "name"): _registry_names
       for verb in ("remove", "pending", "push", "discard")},
    (("registry", "sync"), "names"): _registry_names,
    **{(("fleet", verb), "names"): _fleet_vehicles
       for verb in ("status", "up", "down", "sync")},
}

# NOTE the near-miss: build/bake --registry is a DOCKER registry host, never a configured
# rig registry — absent here by design (file/no fallback beats a wrong menu).
_OPTION_SOURCES: dict = {
    **{(("pkg", verb), "--to"): _registry_names for verb in ("promote", "repin", "rebase")},
    (("pkg", "yank"), "--from"): _registry_names,
    (("pkg", "search"), "--registry"): _registry_names,
    (("pkg", "outdated"), "--registry"): _registry_names,
    (("graph",), "--contract"): _instances,
}


# --- the emitted shell scripts (`rig completion bash|zsh`) -------------------
# Deliberately thin: every TAB calls `_complete` on the binary AS TYPED (a ./rig dev shim
# completes its own checkout's grammar), all logic stays in Python. Regenerated per release
# by the deb build and the brew formula; pipx/source installs eval them at shell startup
# (`rig setup --shell`).

_BASH_SCRIPT = """\
# rig bash completion (generated by rig {version}; regenerate: `rig completion bash`)
_rig_completions() {{
    local IFS=$'\\n'
    COMPREPLY=( $(COMP_LINE="$COMP_LINE" COMP_POINT="$COMP_POINT" \\
        COMP_WORDBREAKS="$COMP_WORDBREAKS" "${{COMP_WORDS[0]}}" _complete --line 2>/dev/null) )
}}
complete -o default -F _rig_completions rig
"""

_ZSH_SCRIPT = """\
#compdef rig
# rig zsh completion (generated by rig {version}; regenerate: `rig completion zsh`)
_rig() {{
    local -a lines matches
    local IFS=$'\\n'
    lines=( $("${{words[1]}}" _complete --describe $((CURRENT-2)) -- "${{(@)words[2,-1]}}" 2>/dev/null) )
    if (( ${{#lines}} == 0 )); then
        _files
        return
    fi
    local line name desc
    for line in "${{lines[@]}}"; do
        name=${{line%%$'\\t'*}}
        desc=${{line#*$'\\t'}}
        if [[ "$desc" == "$line" ]]; then
            matches+=( "${{name//:/\\\\:}}" )
        else
            matches+=( "${{name//:/\\\\:}}:${{desc}}" )
        fi
    done
    _describe -t rig-completions 'rig' matches
}}
if [ "$funcstack[1]" = "_rig" ]; then
    _rig "$@"
else
    # eval'd from a shell rc: a vanilla zsh may not have run compinit yet (macOS ships no
    # default ~/.zshrc) — without it compdef doesn't exist and NO completion works at all.
    if ! (( $+functions[compdef] )); then
        autoload -Uz compinit && compinit -i
    fi
    compdef _rig rig
fi
"""


def script(shell: str) -> str:
    from . import __version__
    template = {"bash": _BASH_SCRIPT, "zsh": _ZSH_SCRIPT}[shell]
    return template.format(version=__version__)
