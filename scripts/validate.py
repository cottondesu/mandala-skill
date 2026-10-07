#!/usr/bin/env python3
from __future__ import annotations

from pathlib import Path
import json
import re
import sys
from typing import Final

if __package__:
    from .live_eval_cases import GRADERS
    from .package_safety import require_real_directory_path
else:
    from live_eval_cases import GRADERS
    from package_safety import require_real_directory_path


ROOT = Path(__file__).resolve().parents[1]
PACKAGES = (ROOT / "src" / "mandala", ROOT / "dist" / "mandala")
EXPECTED = {Path("SKILL.md"), Path("references/cli-contract.md")}
SKILL_NAME: Final = "mandala"
# Agent Skills specification: name and description metadata constraints.
NAME_MAX: Final = 64
DESCRIPTION_MAX: Final = 1024
SKILL_NAME_PATTERN: Final = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
ACTIVATION_FIELDS: Final = {"id", "category", "locale", "prompt", "should_activate"}
ACTIVATION_LOCALES: Final = {"en", "ja"}
REQUIRED_ACTIVATION_CATEGORIES: Final = {
    "explicit-tracking", "existing-mandala-tracking", "generic-gap-analysis",
    "generic-task-management", "unrelated-mandala",
}
LINK = re.compile(r"(?<!!)\[[^]]+\]\(([^)]+)\)")
FENCE_OPEN = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")
LOCAL_PATH = re.compile(rb"(?:/(?:Users|home)/[^\s`'\"<>/]+/|[A-Za-z]:[\\/]+Users[\\/]+[^\s`'\"<>\\/]+[\\/])")
CONTRACTS_PATH: Final = ROOT / "tests" / "evals" / "contracts.json"
FIELD_USAGE_PATH: Final = ROOT / "tests" / "fixtures" / "field_usage" / "version_contracts.json"
FIELD_USAGE_TYPE: Final = "mandala-field-usage-example"
FIELD_USAGE_SOURCE: Final = "sanitized-real-world-usage"
CONTRACT_COUNT: Final = 23
CONTRACT_ID: Final = re.compile(r"[A-Z]+-[0-9]{3}")
CONTRACT_AREAS: Final = {"authorization", "state", "clean", "cli", "completion", "capacity", "verification"}
CONTRACT_FIELDS: Final = {"id", "slug", "area", "clause"}
REQUIRED_SCENARIOS = {
    "generic-gap-analysis", "explicit-tracking", "contextual-update", "zero-gaps",
    "reset-request", "gaps-exit-one-manual", "missing-cli-manual",
    "explicit-non-use-manual", "generic-task-management", "explicit-clean",
    "completion-state-changed",
    "capacity-full-child", "capacity-full-tree", "capacity-final-child",
}
TURN_SCENARIOS = {"contextual-update", "zero-gaps", "completion-state-changed"}
LIVE_CASES: Final = {
    "R1": "reset-request", "R2": "explicit-clean", "M1": "contextual-update",
    "C1": "zero-gaps", "C2": "completion-state-changed",
    "B4": "capacity-full-child", "B5": "capacity-final-child", "B6": "capacity-full-tree",
}
LIVE_SUITES: Final = {
    "focused": ("R1", "R2", "M1", "C1", "C2"),
    "capacity": ("B4", "B5", "B6"),
}
CLI_BASELINE: Final = "Mandala CLI v0.3.0"
CLI_CHECK: Final = "mandala --version"
VERSION_OUTPUT: Final = "mandala v0.3.0"
PINNED_INSTALL = "go install github.com/cottondesu/mandala/cmd/mandala@v0.3.0"
OBSOLETE_VERSION: Final = re.compile(
    r"Do not (?:assume|rely on)[^\n.]*--version|no public[^\n.]*--version"
    r"|There is no required[^\n.]*--version|--version[^\n。]*(?:必須確認に使いません|前提にしません)",
    re.IGNORECASE,
)


def validate_contract_catalog(catalog: object) -> dict[str, dict[str, str]]:
    """Validate the stable safety contract catalog and return contracts keyed by ID."""
    if not isinstance(catalog, dict) or catalog.get("schema_version") != 1:
        raise ValueError("contract catalog needs schema_version 1")
    contracts = catalog.get("contracts")
    if not isinstance(contracts, list) or not contracts:
        raise ValueError("contract catalog needs a non-empty contracts list")
    by_id: dict[str, dict[str, str]] = {}
    slugs = set()
    for contract in contracts:
        if not isinstance(contract, dict) or set(contract) != CONTRACT_FIELDS or not all(
            isinstance(contract[field], str) and contract[field].strip() for field in CONTRACT_FIELDS
        ):
            raise ValueError(f"invalid safety contract entry: {contract!r:.80}")
        if not CONTRACT_ID.fullmatch(contract["id"]):
            raise ValueError(f"invalid safety contract ID: {contract['id']}")
        if contract["area"] not in CONTRACT_AREAS:
            raise ValueError(f"unknown safety contract area: {contract['id']}: {contract['area']}")
        if contract["id"] in by_id:
            raise ValueError(f"duplicate safety contract ID: {contract['id']}")
        if contract["slug"] in slugs:
            raise ValueError(f"duplicate safety contract slug: {contract['slug']}")
        by_id[contract["id"]] = contract
        slugs.add(contract["slug"])
    if len(by_id) != CONTRACT_COUNT:
        raise ValueError(f"expected {CONTRACT_COUNT} safety contracts, found {len(by_id)}")
    return by_id


def load_contracts(path: Path = CONTRACTS_PATH) -> dict[str, dict[str, str]]:
    return validate_contract_catalog(json.loads(path.read_text(encoding="utf-8")))


try:
    SAFETY_CONTRACTS = load_contracts()
    CATALOG_ERROR: Exception | None = None
except (ValueError, OSError, json.JSONDecodeError) as exc:  # reported by main() without a traceback
    SAFETY_CONTRACTS = {}
    CATALOG_ERROR = exc
# Stable contract ID -> active-prose clause.
SAFETY_CONTRACT: Final = {contract_id: contract["clause"] for contract_id, contract in SAFETY_CONTRACTS.items()}


def validate_cli_baseline(text: str, name: str) -> None:
    if CLI_BASELINE not in text or CLI_CHECK not in text or VERSION_OUTPUT not in text:
        raise ValueError(f"missing CLI baseline/version check: {name}")
    if "v0.2.0" in text or OBSOLETE_VERSION.search(text):
        raise ValueError(f"obsolete CLI baseline/version guidance: {name}")


def active_instruction_text(markdown: str) -> str:
    lines = markdown.splitlines()
    if lines and lines[0] == "---":
        try:
            end = lines.index("---", 1)
        except ValueError as exc:
            raise ValueError("unterminated YAML frontmatter") from exc
        lines = lines[end + 1:]
    without_comments = re.sub(r"<!--.*?(?:-->|\Z)", "\n", "\n".join(lines), flags=re.DOTALL)
    active = []
    fence = ""
    for line in without_comments.splitlines():
        if fence:
            closing = rf" {{0,3}}{re.escape(fence[0])}{{{len(fence)},}}[ \t]*"
            if re.fullmatch(closing, line):
                fence = ""
            continue
        opening = FENCE_OPEN.fullmatch(line)
        if opening:
            marker, info = opening.groups()
            if marker[0] == "~" or "`" not in info:
                fence = marker
                continue
        active.append(line)
    return "\n".join(active)


def validate_safety_contract(skill: str, contracts: dict[str, dict[str, str]] = SAFETY_CONTRACTS) -> None:
    if not contracts:
        raise ValueError("no safety contracts loaded")
    active_prose = active_instruction_text(skill)
    for contract_id, contract in contracts.items():
        if contract["clause"] not in active_prose:
            raise ValueError(f"missing safety contract: {contract_id} ({contract['slug']})")


def validate_eval_metadata(cases: list[dict], contracts: dict[str, dict[str, str]] = SAFETY_CONTRACTS) -> None:
    slugs = {contract["slug"] for contract in contracts.values()}
    ids = set()
    covered = set()
    for case in cases:
        if not isinstance(case, dict) or not isinstance(case.get("id"), str) or not case["id"] or not isinstance(case.get("prompt"), str) or not case["prompt"] or not isinstance(case.get("expected"), list) or not case["expected"] or not all(isinstance(item, str) and item for item in case["expected"]):
            raise ValueError("invalid eval case schema")
        references = case.get("contracts")
        if not isinstance(references, list) or not references or not all(isinstance(item, str) for item in references):
            raise ValueError(f"invalid eval contract metadata: {case['id']}")
        for item in references:
            if item in slugs:
                raise ValueError(f"legacy contract slug in eval fixture: {case['id']}: {item}")
            if item not in contracts:
                raise ValueError(f"unknown contract ID in eval fixture: {case['id']}: {item}")
        if len(set(references)) != len(references):
            raise ValueError(f"duplicate contract reference in eval fixture: {case['id']}")
        if "setup" in case and (not isinstance(case["setup"], str) or not case["setup"]):
            raise ValueError(f"invalid eval setup: {case['id']}")
        if "turns" in case or case["id"] in TURN_SCENARIOS:
            turns = case.get("turns")
            if not isinstance(turns, list) or len(turns) < 2 or not all(
                isinstance(turn, dict) and isinstance(turn.get("prompt"), str) and turn["prompt"]
                and isinstance(turn.get("expected"), list) and turn["expected"]
                and all(isinstance(item, str) and item for item in turn["expected"])
                for turn in turns
            ):
                raise ValueError(f"invalid eval turn sequence: {case['id']}")
            if turns[-1]["prompt"] != case["prompt"]:
                raise ValueError(f"eval final turn prompt mismatch: {case['id']}")
        if "between_turns" in case or case["id"] == "completion-state-changed":
            if not isinstance(case.get("between_turns"), str) or not case["between_turns"] or "turns" not in case:
                raise ValueError(f"invalid eval between-turn setup: {case['id']}")
        ids.add(case["id"])
        covered.update(references)
    if len(ids) != len(cases):
        raise ValueError("duplicate eval case id")
    if not REQUIRED_SCENARIOS <= ids:
        raise ValueError(f"missing manual evaluation scenarios: {sorted(REQUIRED_SCENARIOS - ids)}")
    if covered != set(contracts):
        raise ValueError(f"eval contract coverage incomplete: {sorted(set(contracts) - covered)}")


def validate_live_suites(manifest: object, cases: list[dict], graders: object = GRADERS) -> dict:
    """Validate the live-eval manifest against behavioral fixtures; prompts stay in cases.json."""
    if not isinstance(manifest, dict) or manifest.get("schema_version") != 1 or set(manifest) != {"schema_version", "cases", "suites"}:
        raise ValueError("live suite manifest needs schema_version 1, cases, and suites")
    live_cases, suites = manifest["cases"], manifest["suites"]
    if not isinstance(live_cases, dict) or not live_cases or not isinstance(suites, dict) or not suites:
        raise ValueError("live suite manifest needs non-empty cases and suites objects")
    fixture_ids = {case["id"] for case in cases}
    for alias, entry in live_cases.items():
        if not re.fullmatch(r"[A-Z][0-9]+", alias):
            raise ValueError(f"invalid live case alias: {alias}")
        if not isinstance(entry, dict) or set(entry) != {"fixture", "grader"}:
            raise ValueError(f"invalid live case entry: {alias}")
        if entry["fixture"] not in fixture_ids:
            raise ValueError(f"unknown live fixture ID: {alias}: {entry['fixture']}")
        if entry["grader"] not in graders:
            raise ValueError(f"unknown live grader: {alias}: {entry['grader']}")
    for alias, fixture in LIVE_CASES.items():
        if live_cases.get(alias, {}).get("fixture") != fixture:
            raise ValueError(f"live case {alias} must use fixture {fixture}")
    for name, members in suites.items():
        if not isinstance(members, list) or not members or not all(isinstance(alias, str) for alias in members):
            raise ValueError(f"invalid live suite: {name}")
        if len(set(members)) != len(members):
            raise ValueError(f"duplicate live alias in suite: {name}")
        unknown = sorted(set(members) - set(live_cases))
        if unknown:
            raise ValueError(f"unknown live alias in suite {name}: {unknown}")
    for name, expected in LIVE_SUITES.items():
        if set(suites.get(name, ())) != set(expected):
            raise ValueError(f"live suite {name} must contain exactly {list(expected)}")
    release = set(suites.get("release", ()))
    union = {alias for name in LIVE_SUITES for alias in suites[name]}
    if release != union:
        raise ValueError(f"live suite release must be the union of focused and capacity; missing {sorted(union - release)}")
    return manifest


def validate_documentation(root: Path) -> None:
    for name in ("INSTALLATION.md", "INSTALLATION.ja.md"):
        guide = (root / "docs" / name).read_text(encoding="utf-8")
        if PINNED_INSTALL not in guide or "go install github.com/cottondesu/mandala/cmd/mandala@latest" in guide:
            raise ValueError(f"installation guide does not pin the tested CLI baseline: {name}")
        validate_cli_baseline(guide, name)
        if f"{PINNED_INSTALL}\n{CLI_CHECK}" not in guide:
            raise ValueError(f"installation guide does not verify the installed version: {name}")
    for name, manual in (("README.md", "tests/README.md"), ("README.ja.md", "tests/README.ja.md")):
        if manual not in (root / name).read_text(encoding="utf-8"):
            raise ValueError(f"README does not link the manual behavioral evaluation: {name}")
    for name in ("README.md", "README.ja.md"):
        guide = (root / name).read_text(encoding="utf-8")
        validate_cli_baseline(guide, name)
        if PINNED_INSTALL not in guide:
            raise ValueError(f"README omits pinned CLI installation: {name}")
    for name in ("README.md", "README.ja.md"):
        guide = (root / "tests" / name).read_text(encoding="utf-8")
        validate_cli_baseline(guide, f"tests/{name}")
        if not all(re.search(rf"^\| {number} \|", guide, re.MULTILINE) for number in range(1, 11)):
            raise ValueError(f"manual evaluation guide omits a required scenario: {name}")
        if not all(re.search(rf"^\| {scenario} \|", guide, re.MULTILINE) for scenario in ("R1", "R2", "M1", "C1", "C2")):
            raise ValueError(f"manual evaluation guide omits a focused freshness scenario: {name}")
        if not all(re.search(rf"^\| {scenario} \|", guide, re.MULTILINE) for scenario in ("B4", "B6", "B5")):
            raise ValueError(f"manual evaluation guide omits a capacity-workaround scenario: {name}")


def _nonempty(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def validate_field_usage_example(example: object) -> None:
    """Task-level coverage example (task_contracts); deliberately separate from the 23 Skill safety contracts."""
    if not isinstance(example, dict) or example.get("schema_version") != 1:
        raise ValueError("field usage example needs schema_version 1")
    if example.get("artifact_type") != FIELD_USAGE_TYPE:
        raise ValueError(f"field usage example needs artifact_type {FIELD_USAGE_TYPE}")
    if "contracts" in example:
        raise ValueError("field usage example must use task_contracts, not contracts (safety contracts live in tests/evals/contracts.json)")
    if example.get("source") != FIELD_USAGE_SOURCE:
        raise ValueError(f"field usage example source must be {FIELD_USAGE_SOURCE}")
    if not _nonempty(example.get("id")) or not _nonempty(example.get("goal")):
        raise ValueError("field usage example needs a non-empty id and goal")
    task_contracts = example.get("task_contracts")
    if not isinstance(task_contracts, list) or not task_contracts:
        raise ValueError("field usage example needs a non-empty task_contracts list")
    ids = set()
    for item in task_contracts:
        if not isinstance(item, dict) or not all(_nonempty(item.get(field)) for field in ("id", "label", "value")):
            raise ValueError(f"invalid task contract: {item!r:.80}")
        if set(item) - {"id", "label", "value", "verification"}:
            raise ValueError(f"unknown task contract field: {item['id']}")
        if "verification" in item and (not isinstance(item["verification"], list) or not item["verification"] or not all(_nonempty(step) for step in item["verification"])):
            raise ValueError(f"task contract verification must be a non-empty string list: {item['id']}")
        if item["id"] in ids:
            raise ValueError(f"duplicate task contract ID: {item['id']}")
        ids.add(item["id"])


def frontmatter(skill: str) -> dict[str, str]:
    lines = skill.splitlines()
    if not lines or lines[0] != "---":
        raise ValueError("missing YAML frontmatter")
    try:
        end = lines.index("---", 1)
    except ValueError as exc:
        raise ValueError("unterminated YAML frontmatter") from exc
    values: dict[str, str] = {}
    for line in lines[1:end]:
        if not re.fullmatch(r"(?:name|description): [^\r\n]+", line):
            raise ValueError("frontmatter must contain simple name and description scalars")
        key, value = line.split(": ", 1)
        if key in values:
            raise ValueError(f"duplicate frontmatter key: {key}")
        values[key] = value.strip()
    # Repository policy: the Agent Skills specification allows more optional fields.
    if set(values) != {"name", "description"}:
        raise ValueError("frontmatter needs only name and description")
    return values


def validate_agent_skills_metadata(metadata: dict[str, str], directory_name: str) -> None:
    """Check the Agent Skills specification constraints on name and description."""
    name = metadata.get("name", "")
    if not 1 <= len(name) <= NAME_MAX:
        raise ValueError(f"Skill name must be 1-{NAME_MAX} characters: {name!r}")
    if not SKILL_NAME_PATTERN.fullmatch(name):
        raise ValueError(f"Skill name must use lowercase a-z, 0-9, and single inner hyphens: {name!r}")
    if name != directory_name:
        raise ValueError(f"Skill name must match its directory: {name!r} != {directory_name!r}")
    description = metadata.get("description", "")
    if not 1 <= len(description) <= DESCRIPTION_MAX or not description.strip():
        raise ValueError(f"Skill description must be 1-{DESCRIPTION_MAX} non-empty characters")


def validate_activation_metadata(cases: list[dict]) -> None:
    """Check activation-routing fixture structure; this does not prove agent routing."""
    if not isinstance(cases, list):
        raise ValueError("activation fixture must be a list")
    ids = set()
    outcomes: set[tuple[bool, str]] = set()
    categories = set()
    for case in cases:
        if not isinstance(case, dict) or set(case) != ACTIVATION_FIELDS:
            raise ValueError("invalid activation case schema")
        if not all(isinstance(case[field], str) and case[field].strip() for field in ("id", "category", "prompt")):
            raise ValueError(f"invalid activation case schema: {case.get('id')!r}")
        if case["locale"] not in ACTIVATION_LOCALES:
            raise ValueError(f"invalid activation locale: {case['id']}")
        if not isinstance(case["should_activate"], bool):
            raise ValueError(f"activation should_activate must be boolean: {case['id']}")
        if case["id"] in ids:
            raise ValueError(f"duplicate activation case id: {case['id']}")
        ids.add(case["id"])
        categories.add(case["category"])
        outcomes.add((case["should_activate"], case["locale"]))
    if not REQUIRED_ACTIVATION_CATEGORIES <= categories:
        raise ValueError(f"missing activation categories: {sorted(REQUIRED_ACTIVATION_CATEGORIES - categories)}")
    for should_activate in (True, False):
        if not any(outcome == should_activate for outcome, _ in outcomes):
            raise ValueError(f"activation fixtures need {'positive' if should_activate else 'negative'} cases")
    missing = sorted(
        f"{'positive' if outcome else 'negative'}/{locale}"
        for outcome in (True, False) for locale in sorted(ACTIVATION_LOCALES)
        if (outcome, locale) not in outcomes
    )
    if missing:
        raise ValueError(f"activation locale/outcome coverage incomplete: {missing}")


def package_files(package: Path, root: Path = ROOT) -> dict[Path, bytes]:
    require_real_directory_path(root, package)
    files: dict[Path, bytes] = {}
    for path in package.rglob("*"):
        if path.is_symlink():
            raise ValueError(f"symlink in package: {path}")
        if path.is_file():
            relative = path.relative_to(package)
            if path.stat().st_mode & 0o111:
                raise ValueError(f"executable file in package: {path}")
            files[relative] = path.read_bytes()
        elif not path.is_dir():
            raise ValueError(f"non-regular entry in package: {path}")
    if set(files) != EXPECTED:
        raise ValueError(f"unexpected package files in {package}: {sorted(map(str, files))}")
    if sum(path.name == "SKILL.md" for path in files) != 1:
        raise ValueError(f"exactly one SKILL.md required: {package}")
    for relative, content in files.items():
        if b"\x00" in content or LOCAL_PATH.search(content):
            raise ValueError(f"binary data or machine-specific user-home path: {package / relative}")
        text = content.decode("utf-8")
        for target in LINK.findall(text):
            if "://" in target or target.startswith("#"):
                continue
            destination = (package / relative.parent / target.split("#", 1)[0]).resolve()
            if package.resolve() not in destination.parents or not destination.is_file():
                raise ValueError(f"broken or escaping reference: {package / relative}: {target}")
    skill = files[Path("SKILL.md")].decode("utf-8")
    metadata = frontmatter(skill)
    validate_agent_skills_metadata(metadata, package.name)
    # Repository policy: this repository ships exactly one Skill, named mandala.
    if metadata["name"] != SKILL_NAME:
        raise ValueError(f"invalid Skill identity: {package}")
    # Repository policy, following the Agent Skills recommendation to keep SKILL.md short.
    if len(skill.splitlines()) >= 500:
        raise ValueError(f"SKILL.md must be under 500 lines: {package}")
    validate_safety_contract(skill)
    validate_cli_baseline(skill, str(package / "SKILL.md"))
    validate_cli_baseline(files[Path("references/cli-contract.md")].decode("utf-8"), str(package / "references/cli-contract.md"))
    return files


def main() -> None:
    for package in PACKAGES:
        require_real_directory_path(ROOT, package, allow_missing=True)
    if CATALOG_ERROR is not None:
        raise ValueError(f"invalid safety contract catalog: {CATALOG_ERROR}")
    snapshots = [package_files(package) for package in PACKAGES]
    if snapshots[0] != snapshots[1]:
        raise ValueError("generated package differs from canonical source")
    cases = json.loads((ROOT / "tests" / "evals" / "cases.json").read_text(encoding="utf-8"))
    if not isinstance(cases, list):
        raise ValueError("eval fixture must be a list")
    validate_eval_metadata(cases)
    manifest = json.loads((ROOT / "tests" / "evals" / "live_suites.json").read_text(encoding="utf-8"))
    validate_live_suites(manifest, cases)
    activation = json.loads((ROOT / "tests" / "evals" / "activation.json").read_text(encoding="utf-8"))
    validate_activation_metadata(activation)
    validate_field_usage_example(json.loads(FIELD_USAGE_PATH.read_text(encoding="utf-8")))
    validate_documentation(ROOT)
    print(
        f"Mandala Skill packages valid and current; {len(SAFETY_CONTRACTS)} safety contracts, "
        f"{len(cases)} behavioral fixtures, {len(manifest['cases'])} live cases, "
        f"{len(activation)} activation-routing fixtures, and 1 task-level field usage example validated; live agents are not run"
    )


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, UnicodeError, json.JSONDecodeError) as exc:
        print(f"validation failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
