"""Every package an install of rocky pulls in is pinned (no API, no network, no uv).

pyproject.toml used to pin only the direct packages, so an install took whatever
version of each sub-package was newest that day. The whole tree is pinned now,
written from uv.lock by scripts/lock_closure.py. Here uv.lock is walked for every
platform and Python rocky supports, for the default install and each extra:
what it reaches must be pinned at that version, under a marker that holds there,
and nothing else may be. A marker this test cannot read fails it, on purpose.
"""
import itertools
import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
lock = tomllib.loads((ROOT / "uv.lock").read_text(encoding="utf-8"))
project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
packages = {}
for package in lock["package"]:
    assert package["name"] not in packages, "one name locked at two versions: teach this test to follow both"
    packages[package["name"]] = package


def holds(marker: str, env: dict) -> bool:
    """Evaluate comparisons joined by and / or, with brackets."""
    tokens = re.findall(r"\(|\)|'[^']*'|==|!=|<=|>=|<|>|\w+", marker)
    assert re.sub(r"\s", "", marker) == re.sub(r"\s", "", "".join(tokens)), f"cannot read marker: {marker}"

    def primary():
        token = tokens.pop(0)
        if token == "(":
            value = either()
            assert tokens.pop(0) == ")", f"cannot read marker: {marker}"
            return value
        op, right = tokens.pop(0), tokens.pop(0).strip("'")
        assert token in env, f"cannot read marker: {marker}"
        left = env[token]
        if token == "python_full_version":
            left, right = [[*map(int, v.split(".")), 0, 0][:3] for v in (left, right)]
        else:
            assert op in ("==", "!="), f"cannot read marker: {marker}"
        return {"==": left == right, "!=": left != right, "<": left < right, "<=": left <= right,
                ">": left > right, ">=": left >= right}[op]

    def both():
        value = primary()
        while tokens and tokens[0] == "and":
            tokens.pop(0)
            value = primary() and value
        return value

    def either():
        value = both()
        while tokens and tokens[0] == "or":
            tokens.pop(0)
            value = both() or value
        return value

    result = either()
    assert not tokens, f"cannot read marker: {marker}"
    return result


def reached(env: dict, extra: str | None) -> set:
    """What an install pulls in: follow every locked edge whose marker holds."""
    seen, todo = set(), [("rockycode", None)] + ([("rockycode", extra)] if extra else [])
    while todo:
        key = todo.pop()
        if key in seen:
            continue
        seen.add(key)
        package, part = packages[key[0]], key[1]
        edges = package.get("optional-dependencies", {}).get(part, []) if part else package.get("dependencies", [])
        for edge in edges:
            if "marker" not in edge or holds(edge["marker"], env):
                todo += [(edge["name"], None)] + [(edge["name"], e) for e in edge.get("extra", ())]
    return {(name, packages[name]["version"]) for name, _ in seen} - {("rockycode", packages["rockycode"]["version"])}


def pins(requirements: list, env: dict) -> set:
    found = set()
    for requirement in requirements:
        match = re.match(r"^([A-Za-z0-9._-]+)==([\w.]+)\s*(?:;\s*(.+))?$", requirement)
        assert match, f"not an exact pin (name==version): {requirement}"
        if not match[3] or holds(match[3].replace('"', "'"), env):
            found.add((re.sub(r"[-_.]+", "-", match[1]).lower(), match[2]))
    return found


# uv.lock's own edges are rocky's metadata, which holds the pins: walking them would
# prove the pins by themselves. Start from the packages the block is NOT for.
text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
own = re.sub(r"    # ── sub-packages.*?# ── end of the pinned sub-packages ──\n", "", text, flags=re.S)
declared = tomllib.loads(own)["project"]
direct = {None: declared["dependencies"], **declared["optional-dependencies"]}


def roots(requirements: list) -> set:
    return {re.sub(r"[-_.]+", "-", re.match(r"[\w.-]+?(?===)", r)[0]).lower() for r in requirements}


rocky = packages["rockycode"]
rocky["dependencies"] = [e for e in rocky["dependencies"] if e["name"] in roots(direct[None])]
rocky["optional-dependencies"] = {x: [e for e in edges if e["name"] in roots(direct[x])]
                                  for x, edges in rocky["optional-dependencies"].items()}
assert len(rocky["dependencies"]) == len(direct[None]), "uv.lock is older than pyproject.toml: run uv lock"

checked = 0
for platform, machine, python in itertools.product(
        ("darwin", "linux", "win32"), ("arm64", "x86_64", "AMD64"), ("3.11.0", "3.12.0", "3.13.0", "3.14.0", "3.15.0")):
    env = {"sys_platform": platform, "platform_machine": machine, "python_full_version": python, "python_version": python[:4],
           "implementation_name": "cpython", "platform_python_implementation": "CPython"}
    for extra in direct:
        wanted = reached(env, extra)
        pinned = pins(project["dependencies"] + (project["optional-dependencies"][extra] if extra else []), env)
        where = f"{extra or 'default'} install on {platform} {machine} Python {python}"
        assert not wanted - pinned, f"{where}: not pinned (scripts/lock_closure.py, then uv lock): {sorted(wanted - pinned)}"
        assert not pinned - wanted, f"{where}: pinned but not in the locked tree: {sorted(pinned - wanted)}"
        checked += 1

# A pin without a ready-made wheel makes pip build from source, which needs compilers.
PLATFORMS = {("darwin", "arm64"): r"macosx_\w*(arm64|universal2)", ("darwin", "x86_64"): r"macosx_\w*(x86_64|universal2)",
             ("linux", "x86_64"): r"manylinux\w*x86_64", ("linux", "aarch64"): r"manylinux\w*aarch64", ("win32", "AMD64"): "win_amd64"}


def has_wheel(package: dict, minor: int, platform: str) -> bool:
    for wheel in package.get("wheels", []):
        python, abi, where = wheel["url"].rsplit("/", 1)[1].removesuffix(".whl").split("-")[-3:]
        fits = any(tag == "py3" or tag == f"cp3{minor}" or (abi == "abi3" and tag.startswith("cp3") and int(tag[3:]) <= minor)
                   for tag in python.split("."))
        if fits and (where == "any" or re.search(platform, where)):
            return True
    return False


for (system, machine), pattern in PLATFORMS.items():
    for minor in range(11, 16):
        env = {"sys_platform": system, "platform_machine": machine, "python_full_version": f"3.{minor}.0", "python_version": f"3.{minor}",
               "implementation_name": "cpython", "platform_python_implementation": "CPython"}
        bare = sorted(name for name, _ in reached(env, None) if not has_wheel(packages[name], minor, pattern))
        assert not bare, f"no wheel on {system} {machine} Python 3.{minor}: {bare}"

print(f"LOCKED CLOSURE SMOKE OK — {len(project['dependencies'])} pin in default install, {checked} install checked. amaze!")
