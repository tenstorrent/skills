"""Packaging invariants for the tt-project plugin."""

from __future__ import annotations

import ast
import json
import os
import pathlib
import re
import sys

import pytest
import yaml

PLUGIN = pathlib.Path(__file__).resolve().parents[1] / "plugins" / "tt-project"
SKILLS = PLUGIN / "skills"
RUNTIME = PLUGIN / "runtime" / "ttp"
MANIFESTS = [PLUGIN / d / "plugin.json" for d in (".claude-plugin", ".codex-plugin", ".cursor-plugin")]

# Generic secret shapes and personal paths that must never ship in the open-source plugin.
FORBIDDEN = [
    r"xox[abprs]-[0-9A-Za-z]", r"sk-ant-", r"\bsk-[A-Za-z0-9]{32,}", r"\bgh[pousr]_[A-Za-z0-9]{30,}",
    r"github_pat_", r"\bglpat-[0-9A-Za-z_-]{20}", r"\bAKIA[0-9A-Z]{16}\b", r"\bAIza[0-9A-Za-z_-]{35}\b",
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----", r"/home/[a-z]+/", r"/Users/[a-z]+/",
]

# Setup-specific patterns (internal domains, host names, user names, project names) never live in
# this repository. Each maintainer keeps them in a local file outside it: $TTP_LEAK_PATTERNS_FILE,
# else $XDG_CONFIG_HOME/tt-project/leak-patterns.txt (default ~/.config/tt-project/leak-patterns.txt).
# One regex per line, compiled on its own (so a leading (?i) works); blank lines and lines starting
# with # are ignored. Without the file that check is skipped with a visible reason; a malformed
# pattern fails it.
LEAK_PATTERNS_ENV = "TTP_LEAK_PATTERNS_FILE"
TESTS = pathlib.Path(__file__).resolve().parent


def local_leak_patterns_file() -> pathlib.Path:
    if os.environ.get(LEAK_PATTERNS_ENV):
        return pathlib.Path(os.environ[LEAK_PATTERNS_ENV]).expanduser()
    config = os.environ.get("XDG_CONFIG_HOME") or pathlib.Path.home() / ".config"
    return pathlib.Path(config) / "tt-project" / "leak-patterns.txt"


def load_leak_patterns(path: pathlib.Path) -> list:
    patterns = []
    for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            patterns.append(re.compile(line))
        except re.error as e:
            raise AssertionError(f"{path}:{n}: malformed leak pattern: {e}") from None
    return patterns


def assert_no_leaks(patterns: list, files) -> None:
    for f in files:
        text = f.read_text(encoding="utf-8", errors="replace")
        for pattern in patterns:
            m = pattern.search(text)
            assert not m, f"{f}: contains {m.group(0)!r}"


def plugin_text_files(root: pathlib.Path = PLUGIN):
    return [f for f in sorted(root.rglob("*"))
            if f.is_file() and f.suffix in {".py", ".md", ".json", ".js", ".html", ".css", ""}]


def frontmatter(path: pathlib.Path) -> dict:
    text = path.read_text(encoding="utf-8")
    assert text.startswith("---\n"), f"{path}: no frontmatter"
    return yaml.safe_load(text[3:text.find("\n---", 3)]) or {}


def test_manifests_agree():
    data = [json.loads(p.read_text()) for p in MANIFESTS]
    assert {d["name"] for d in data} == {"tt-project"}
    assert len({d["version"] for d in data}) == 1
    runtime_version = re.search(r'__version__ = "([^"]+)"',
                                (PLUGIN / "runtime" / "ttp" / "__init__.py").read_text()).group(1)
    assert runtime_version == data[0]["version"], "bump runtime/ttp/__init__.py with the manifests"
    assert all(d["skills"] == "./skills/" for d in data)


def test_skills_are_discoverable_and_self_contained():
    skills = sorted(SKILLS.glob("*/SKILL.md"))
    assert {p.parent.name for p in skills} == {"tt-project", "tt-project-harness"}
    for path in skills:
        fm = frontmatter(path)
        assert fm["name"] == path.parent.name
        assert fm["name"].startswith("tt-"), f"{path}: skill names start with tt-"
        assert len(fm["description"]) > 40
    for md in SKILLS.rglob("*.md"):
        text = md.read_text(encoding="utf-8")
        assert not re.search(r"[`(]\.\./\w", text), f"{md}: relative ../ path"
        for ref in re.findall(r"`([\w-]+\.md)`", text):
            if (md.parent / ref).exists() or ref in {"MEMORY.md", "CHARTER.md", "README.md"}:
                continue
            if ref.startswith(("kind-", "daily-review", "coordinator", "worker")):
                continue   # prompt templates of a project's harness, named for the reader
            raise AssertionError(f"{md}: references missing sibling {ref}")


def test_runtime_is_standard_library_only_and_python39_compatible():
    stdlib = set(sys.stdlib_module_names)
    for py in RUNTIME.rglob("*.py"):
        src = py.read_text(encoding="utf-8")
        tree = ast.parse(src, feature_version=(3, 9))   # rejects syntax newer than 3.9
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [a.name.split(".")[0] for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                names = [node.module.split(".")[0]]
            else:
                continue
            for n in names:
                assert n in stdlib or n == "ttp", f"{py}: imports non-stdlib module {n}"
        if re.search(r"\w\]?\s*\|\s*None\b", src):
            assert "from __future__ import annotations" in src, f"{py}: PEP 604 unions need the future import on 3.9"


def test_no_secrets_or_personal_paths():
    assert_no_leaks([re.compile(p) for p in FORBIDDEN], plugin_text_files())


def test_no_setup_specific_details_from_local_patterns():
    path = local_leak_patterns_file()
    if not path.is_file():
        pytest.skip(f"setup-specific leak check skipped: no local pattern file at {path} "
                    f"(set {LEAK_PATTERNS_ENV} or create it)")
    patterns = load_leak_patterns(path)
    assert_no_leaks(patterns, plugin_text_files() + sorted(TESTS.glob("test_tt_project_*.py")))


def test_local_leak_patterns_are_found_and_checked(tmp_path, monkeypatch):
    monkeypatch.setenv(LEAK_PATTERNS_ENV, str(tmp_path / "patterns.txt"))
    assert local_leak_patterns_file() == tmp_path / "patterns.txt"
    monkeypatch.delenv(LEAK_PATTERNS_ENV)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    assert local_leak_patterns_file() == tmp_path / "cfg" / "tt-project" / "leak-patterns.txt"

    tree = tmp_path / "plugin"
    (tree / "docs").mkdir(parents=True)
    (tree / "docs" / "notes.md").write_text("runs on Example-Box-7 every night\n")
    patterns_file = tmp_path / "patterns.txt"
    patterns_file.write_text("# planted\n\n(?i)example-box-\\d\n")
    patterns = load_leak_patterns(patterns_file)
    assert len(patterns) == 1
    with pytest.raises(AssertionError, match="Example-Box-7"):
        assert_no_leaks(patterns, plugin_text_files(tree))
    (tree / "docs" / "notes.md").write_text("runs on a build box every night\n")
    assert_no_leaks(patterns, plugin_text_files(tree))

    patterns_file.write_text("fine\n(unclosed\n")
    with pytest.raises(AssertionError, match=r"patterns\.txt:2: malformed leak pattern"):
        load_leak_patterns(patterns_file)


def test_launchers_are_executable():
    import os
    for launcher in (PLUGIN / "bin" / "ttp", PLUGIN / "template" / "bin" / "ttp"):
        assert os.access(launcher, os.X_OK), f"{launcher} is not executable"
    assert (PLUGIN / "bin" / "ttp").read_text() == (PLUGIN / "template" / "bin" / "ttp").read_text()


def test_harness_tasks_stay_in_their_own_harness():
    """A harness task never edits the plugin's source or another project's harness; generic lessons
    travel as upstream notes in the hand-off instead."""
    prompts = PLUGIN / "template" / "prompts"
    for path in (prompts / "kind-harness.md", prompts / "worker.md", SKILLS / "tt-project-harness" / "SKILL.md"):
        text = " ".join(path.read_text(encoding="utf-8").split())
        assert "only this project's own harness" in text, f"{path.name}: harness tasks stay in their own harness"
        assert re.search(r"[Nn]ever edit[s]?, or (create|make)s? a worktree or branch in, the tt-project "
                         r"plugin's source repository or (any other|another) project's harness", text), path.name
        assert "upstream note" in text and "`upstream: " in text, f"{path.name}: names the upstream notes"
    coordinator = " ".join((prompts / "coordinator.md").read_text(encoding="utf-8").split())
    assert "upstream notes for the tt-project maintainers, not work for this project" in coordinator
    # A project whose own work is the plugin (its charter says so) may still queue that work.
    assert "unless the charter names that repository as this project's own work" in coordinator
    # Deploying a release with ttp setup/upgrade is not editing another project's harness.
    for path in (prompts / "kind-harness.md", prompts / "worker.md", prompts / "coordinator.md",
                 SKILLS / "tt-project-harness" / "SKILL.md"):
        text = " ".join(path.read_text(encoding="utf-8").split())
        assert "`ttp upgrade <name>`" in text and "is not editing" in text, f"{path.name}: deploying is allowed"
        assert "hand edits to its charter, memory, config, state or code" in text, path.name


def test_harness_tasks_deliver_by_a_local_commit_not_a_push():
    """The harness is a local repo with no remote: a harness task's commit is its delivery. Neither
    the task nor the coordinator's specs ask it to `ttp push --own` or to copy files into a code branch."""
    prompts = PLUGIN / "template" / "prompts"
    harness = " ".join((prompts / "kind-harness.md").read_text(encoding="utf-8").split())
    assert "commit in the harness repo is the delivery" in harness
    assert "do not `ttp push` or `ttp push --own` it" in harness
    assert "never copy harness files into a code branch" in harness
    assert "`delivery.backup_remote` backs up code tasks' branches, not the harness" in harness
    coordinator = " ".join((prompts / "coordinator.md").read_text(encoding="utf-8").split())
    assert "never ask it to push, `ttp push --own` or publish its change on a code branch" in coordinator


def test_prompts_record_live_changes_to_shared_machines_in_the_ledger():
    """Every live change to a shared machine goes in the change ledger with an owner, a reason and an end."""
    prompts = PLUGIN / "template" / "prompts"
    for name in ("worker.md", "coordinator.md"):
        text = " ".join((prompts / name).read_text(encoding="utf-8").split())
        assert "`ttp machines change add <alias>" in text and "--undo" in text and "--expires" in text, name
        assert "a disabled recovery switch, a pause" in text, name
    coordinator = " ".join((prompts / "coordinator.md").read_text(encoding="utf-8").split())
    assert "`machine_condition`" in coordinator and "`ttp machines set <alias> --owner <project>" in coordinator
    operate = (SKILLS / "tt-project" / "operate.md").read_text(encoding="utf-8")
    assert "ttp machines set <alias> --owner" in operate and "ttp machines change add" in operate


def test_coordinator_retires_temporary_instructions_and_does_not_ask_needlessly():
    """Temporary words get an end; a clearly-over restriction is retired, not asked about; a known,
    safe, reversible fix is done and reported, never asked about with a yes recommendation."""
    coordinator = " ".join((PLUGIN / "template" / "prompts" / "coordinator.md").read_text(encoding="utf-8").split())
    for phrase in ("`expires`", "`until`", "`until_probe`", "retire it yourself", "`over`",
                   "never send an ask whose recommendation is yes", "possibly over"):
        assert phrase in coordinator, phrase
    daily = (PLUGIN / "template" / "prompts" / "daily-review.md").read_text(encoding="utf-8")
    assert "stale restriction" in daily and "`replaces`" in daily


def test_coordinator_relays_accepted_reviews_without_a_worker():
    """An accepted review reaches its consumers by a spec-only task_update; no relay-only worker run."""
    coordinator = " ".join((PLUGIN / "template" / "prompts" / "coordinator.md").read_text(encoding="utf-8").split())
    start = coordinator.index("A review that passes is evidence")
    rule = coordinator[start:coordinator.index(" - ", start)]
    for phrase in ("spec-only `task_update`", "accepted commit", "hash-pinned manifest", "verdict",
                   "gates still open", "blocked reason, dependencies and deferral stay",
                   "Never requeue or add a task only to relay a verdict",
                   "Changed source or missing validation is real work", "cannot reach stays blocked",
                   "only queued locally (exit 0) is not delivered"):
        assert phrase in rule, phrase


def test_coordinator_rewrites_a_changed_restriction_in_place():
    """A restriction the user changes is edited in Restrictions in that turn, never left standing
    next to a new section that says otherwise: workers obey the Restrictions block verbatim."""
    coordinator = " ".join((PLUGIN / "template" / "prompts" / "coordinator.md").read_text(encoding="utf-8").split())
    start = coordinator.index("The user changes, narrows, widens or lifts a restriction")
    rule = coordinator[start:coordinator.index(" - ", start)]
    for phrase in ("in that same turn", "`quote` set to the old item", "`replaces`", "no `over`",
                   "Never leave the old item standing next to a new section",
                   "A temporary loosening of a permanent item also `quote`s that item"):
        assert phrase in rule, phrase
    # Next to the other charter_update guidance, under User instructions.
    assert coordinator.index("# User instructions") < start < coordinator.index("# Keep moving")
    assert "merges the permanent Restrictions sections into one block" in rule
    assert "full heading or number" in coordinator and "`Charter sections` line" in coordinator
    start = coordinator.index("If that change is rejected")
    carry = coordinator[start:coordinator.index(" - ", start)]
    for phrase in ("the user's yes stays on record", "without asking again", "same section, target and text",
                   "`coordinator.charter_approval_days`"):
        assert phrase in carry, phrase


# Wording that makes the user do, or approve, what tt-project can do itself.
ASKS_USER_TO_DO = [
    r"\b(ask|tell)(ing)? (the )?user to (run|do|open|install|type|start|restart|set up|enable)\b",
    r"\bwith the user's (OK|okay|approval)\b", r"\bafter the user agrees\b", r"\(ask first\)",
    r"\b(would|do) you (like|want) me to\b", r"\bshall I\b", r"\bwant me to\b", r"\byou can run\b",
    r"\boffer (to|`ttp)", r"\bplease run\b",
]


def user_facing_texts():
    for f in sorted(SKILLS.rglob("*.md")) + sorted((PLUGIN / "template").rglob("*.md")):
        yield f.relative_to(PLUGIN), f.read_text(encoding="utf-8")
    # Strings the runtime prints or shows (status, web app, alerts, help).
    for f in sorted(RUNTIME.rglob("*.py")) + sorted((RUNTIME / "web").glob("*.js")):
        yield f.relative_to(PLUGIN), f.read_text(encoding="utf-8")


def test_no_wording_asks_the_user_to_do_what_tt_project_can_do():
    """Never ask the user to do what tt-project can do, never offer it: do it and say so. A rule that
    forbids such wording may quote it, so items saying "never" and quoted examples are skipped."""
    pattern = re.compile("|".join(ASKS_USER_TO_DO), re.I)
    for rel, text in user_facing_texts():
        # Markdown: one item per paragraph, list entry or table row, so a quote wrapped over lines
        # stays whole. Code: one item per line.
        md = rel.suffix == ".md"
        for item in re.split(r"\n\s*\n|\n(?=\s*[-|#] )", text) if md else text.splitlines():
            item = " ".join(item.split())
            if re.search(r"\bnever\b", item, re.I):
                continue
            if md:   # in code, quotes are the strings themselves
                item = re.sub(r'"[^"]*"', "", item)
            m = pattern.search(item)
            assert not m, f"{rel}: {m.group(0)!r} makes the user do or approve what tt-project can do: {item[:120]}"


def test_local_web_forwards_open_without_asking_and_exposing_tunnels_ask():
    remote = " ".join((SKILLS / "tt-project" / "remote.md").read_text(encoding="utf-8").split())
    assert "ALWAYS ask the user before opening any tunnel" not in remote
    assert "open it without asking and keep it up" in remote
    assert "`ttp web <name> --tunnel --keep`" in remote and "`com.tt-project.tunnel.<name>`" in remote
    assert re.search(r"Ask the user before any tunnel that exposes their machine to others: a reverse forward", remote)
    rows = {row.split("|")[1].strip(): row.split("|")[3].strip()
            for row in (SKILLS / "tt-project" / "remote.md").read_text(encoding="utf-8").splitlines()
            if row.startswith("| ") and "forward" in row}
    assert rows["Laptop reaches a service on a box"] == "no"
    assert rows["Box reaches a service on the laptop"] == "yes"
    assert rows["Box A reaches box B via the laptop"] == "yes"
    skill = (SKILLS / "tt-project" / "SKILL.md").read_text(encoding="utf-8")
    assert "Tunnels need the user's OK first" not in skill and "--tunnel --keep" in skill
    for prompt in ("coordinator.md", "worker.md"):
        text = " ".join((PLUGIN / "template" / "prompts" / prompt).read_text(encoding="utf-8").split())
        assert re.search(r"Never ask (or tell )?the user to do what (you or )?the project can do", text), prompt


def test_new_and_connect_reply_with_a_verified_web_link():
    """After `new` and `connect` the chat replies with the web app link those commands checked, next
    to the locator line, and the relay never gives a link that was not checked."""
    skill = " ".join((SKILLS / "tt-project" / "SKILL.md").read_text(encoding="utf-8").split())
    row = next(r for r in (SKILLS / "tt-project" / "SKILL.md").read_text(encoding="utf-8").splitlines()
               if r.startswith("| 3c."))
    assert "`tt-project://…` line and the verified web app link" in row
    assert "Never give an unverified web app link." in skill and "NOT AVAILABLE" in skill
    assert "show the `tt-project://…` line once, with the verified web app link" in skill
    create = " ".join((SKILLS / "tt-project" / "create.md").read_text(encoding="utf-8").split())
    assert "verified web app link" in create and "give no link" in create
    remote = " ".join((SKILLS / "tt-project" / "remote.md").read_text(encoding="utf-8").split())
    assert "open the same kept forward themselves" in remote


def test_review_lists_every_blocking_finding_and_fixes_test_each_bug_class():
    """One review round finds all blocking bugs; the fix tests each class, not one case."""
    prompts = PLUGIN / "template" / "prompts"
    review = " ".join((prompts / "kind-review.md").read_text(encoding="utf-8").split())
    assert "Find every blocking problem in one pass: after the first, keep going through the whole changed module" in review
    for edge in ("failure paths", "retries", "time windows", "restarts"):
        assert edge in review, edge
    code = " ".join((prompts / "kind-code.md").read_text(encoding="utf-8").split())
    assert "test each class of bug found, not only the reported case" in code


def test_worker_prompt_limits_search_scope():
    text = " ".join((PLUGIN / "template" / "prompts" / "worker.md").read_text(encoding="utf-8").split())
    assert "Never search / or the home folder" in text
    for cmd in ("`find /`", "`find ~`", "`grep -r ~`", "`mdfind`", "git ls-files"):
        assert cmd in text, cmd


def test_prompts_and_charter_template_say_self_heal_then_report():
    def flat(*parts):
        return " ".join((PLUGIN / "template" / pathlib.Path(*parts)).read_text(encoding="utf-8").split())
    coord = flat("prompts", "coordinator.md")
    for part in ("Self-healing: keeping the project stable is yours.", "a hold that never cleared",
                 "(`task_add` priority 1) or a heal check", "notify at severity `low` afterwards",
                 "`ttp note --to <project>`", "`classify`", "`restriction_change`",
                 "`neither`, which is rejected: decide it yourself", "name the reversible alternative you considered",
                 "The class must match `blocking`", "a step only the user can take (physical or on site",
                 "another person's request", "paraphrases get through it", "heal checks cover what it misses"):
        assert part in coord, part
    worker = flat("prompts", "worker.md")
    assert ("is fixed in the task, or handed off with a fix plan in `followups`; never handed off `blocked` as a "
            "question about what to do") in worker
    charter = flat("CHARTER.md")
    assert ("Self-healing: keeping the project stable is a prime directive; check everything it is responsible "
            "for, fix anomalies yourself and report afterwards.") in charter


def test_worker_prompt_bounds_disk_usage_scans():
    text = " ".join((PLUGIN / "template" / "prompts" / "worker.md").read_text(encoding="utf-8").split())
    for part in ("`df <path>`", "Never `du` from / or a home folder", "`du -x`", "`timeout`", "is unknown, not a blocker"):
        assert part in text, part


def test_worker_prompt_ci_wait_wakes_on_a_hung_job():
    text = " ".join((PLUGIN / "template" / "prompts" / "worker.md").read_text(encoding="utf-8").split())
    for part in ("`ttp ci --repo <owner/repo> --branch <branch>`", "exits 0 when a job hangs", "`hung:` line",
                 "`--no-hang`"):
        assert part in text, part


def test_web_js_parses_on_old_node():
    """The web app must stay ES2019 so older system node and browsers can run it."""
    import shutil
    import subprocess
    files = sorted((RUNTIME / "web").glob("*.js"))
    assert files
    for f in files:
        text = f.read_text()
        bad = [m.group(0) for m in re.finditer(r"\?\?|\?\.(?!\d)", text)]
        assert not bad, f"{f.name}: ES2020 syntax {bad} (use == null checks)"
    node = shutil.which("node")
    if not node:
        import pytest
        pytest.skip("node not installed")
    for f in files:
        r = subprocess.run([node, "--check", str(f)], capture_output=True, text=True)
        assert r.returncode == 0, f"{f.name}: {r.stderr}"


# A path token ending in harness/bin made only of literal relative segments: `harness/bin/ttp`,
# `tt-project/harness/bin/ttp`, `./harness/bin`. A token anchored on a root (`/abs/...`, `~/...`,
# `$TTP_PROJECT/...`, `${X}/...`, `{dir}/{FOLDER}/...`, `%s/...`) does not match, and neither does
# code such as `p.harness / "bin"`.
ROOT_RELATIVE_HARNESS_BIN = re.compile(r"(?<![\w$}/.%~-])(?:[\w.-]+/)*harness/bin\b")
# (path relative to the plugin, exact line text stripped) -> why the line may say it. Keep it short.
ROOT_RELATIVE_HARNESS_BIN_ALLOW: dict[tuple[str, str], str] = {}


def shipped_text_files():
    """Tracked plugin files that decode as text: prompts, skills, runtime .py and scripts."""
    import subprocess
    out = subprocess.run(["git", "ls-files", "-z", "--", "."], cwd=PLUGIN, capture_output=True,
                         text=True, check=True).stdout
    for rel in sorted(filter(None, out.split("\0"))):
        path = PLUGIN / rel
        if not path.is_file():
            continue
        try:
            yield rel, path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue


def test_root_relative_harness_bin_pattern():
    for line in ("run tt-project/harness/bin/ttp note x", "`harness/bin/ttp lock`",
                 'cmd = "tt-project/harness/bin/ttp"', "./harness/bin/gh", "x=harness/bin"):
        assert ROOT_RELATIVE_HARNESS_BIN.search(line), line
    for line in ('"$TTP_PROJECT/harness/bin/ttp" note', "${TTP_PROJECT}/harness/bin/ttp",
                 "f\"{entry['dir']}/{FOLDER}/harness/bin/ttp\"", "f\"{root}/tt-project/harness/bin\"",
                 "/abs/proj/tt-project/harness/bin/ttp", "~/proj/tt-project/harness/bin",
                 "'%s/harness/bin' % root", 'ttp = p.harness / "bin" / "ttp"',
                 "f\"{self.p.harness / 'bin'}:{path}\"", "<project>/harness/bin/ttp",
                 "harness/binary"):
        assert not ROOT_RELATIVE_HARNESS_BIN.search(line), line


def test_shipped_text_never_uses_root_relative_harness_bin_paths():
    """Workers run in tt-project/worktrees/<task>, where `tt-project/harness/bin/...` does not
    resolve; shipped prompts, skills, runtime strings and scripts must use
    "$TTP_PROJECT/harness/bin/..." or a path anchored on the project root instead."""
    files = dict(shipped_text_files())
    # The scan must reach runtime code and suffixless scripts, not only prompt files.
    assert "runtime/ttp/cli.py" in files and "template/bin/ttp" in files
    hits = []
    for rel, text in files.items():
        for n, line in enumerate(text.splitlines(), 1):
            if ROOT_RELATIVE_HARNESS_BIN.search(line) and \
                    (rel, line.strip()) not in ROOT_RELATIVE_HARNESS_BIN_ALLOW:
                hits.append(f"{rel}:{n}: {line.strip()}")
    assert not hits, "root-relative harness/bin paths:\n" + "\n".join(hits)


def test_worker_prompt_driver_chain_fails_fast_and_shared_watchers_lock():
    root = PLUGIN
    worker = (root / "template" / "prompts" / "worker.md").read_text()
    assert "pipefail" in worker and "first non-zero" in worker
    harness = (root / "skills" / "tt-project-harness" / "SKILL.md").read_text()
    assert "flock -n" in harness and "condition key" in harness


def test_worker_prompt_driver_marker_uses_exit_trap():
    text = (PLUGIN / "template" / "prompts" / "worker.md").read_text()
    assert "EXIT trap" in text
    assert "trap 'echo $? > \"$marker\"' EXIT" in text


def test_docs_and_prompts_name_only_real_ttp_subcommands():
    """A prompt or skill that names a `ttp <word>` the CLI lacks sends workers to an invalid choice."""
    sys.path.insert(0, str(RUNTIME.parent))
    try:
        from ttp import cli
        import argparse
        seen: list = []
        real = argparse.ArgumentParser.parse_args

        def grab(self, args=None, namespace=None):
            seen.append(self)
            raise SystemExit(0)
        argparse.ArgumentParser.parse_args = grab
        try:
            cli.main(["status"])
        except SystemExit:
            pass
        finally:
            argparse.ArgumentParser.parse_args = real
    finally:
        sys.path.remove(str(RUNTIME.parent))
    sub = next(a for a in seen[0]._actions if isinstance(a, argparse._SubParsersAction))
    commands = set(sub.choices)
    assert {"note", "notify", "push"} <= commands
    files = [f for f in PLUGIN.rglob("*") if f.suffix in (".md", ".py", ".json", ".sh") and f.is_file()]
    bad = sorted({f"{f.relative_to(PLUGIN)}: ttp {m}"
                  for f in files for m in re.findall(r"`ttp ([a-z][a-z-]*)", f.read_text(encoding="utf-8", errors="replace"))
                  if m not in commands})
    assert not bad, bad


def test_worker_prompt_covers_pattern_kills_in_scripts_it_runs():
    text = " ".join((PLUGIN / "template" / "prompts" / "worker.md").read_text(encoding="utf-8").split())
    for part in ("scripts and test stubs you run", "`ttp killscan <script>`", "`ttp killscan --shim <dir>`",
                 "`kill $(pgrep -f ...)`", "a separate session or process group does not protect it"):
        assert part in text, part


def test_suite_keeps_pytest_temp_dirs_small(pytestconfig):
    # The runtime suite makes hundreds of thousands of files under the pytest temp root; on a shared
    # host the default (keep every tmp_path of the last 3 sessions) fills /tmp and slows boot cleanup.
    assert pytestconfig.getini("tmp_path_retention_policy") == "failed"
    assert int(pytestconfig.getini("tmp_path_retention_count")) <= 1


def test_code_and_review_prompts_keep_full_suite_runs_to_ttp_checks():
    prompts = PLUGIN / "template" / "prompts"
    code = " ".join((prompts / "kind-code.md").read_text(encoding="utf-8").split())
    for part in ("While you iterate, run only the tests your change affects", "never the full suite",
                 "Run `ttp checks` once at the end, committed, before the hand-off",
                 "A direct full-suite run is refused",
                 "each command quoted as one word (`-- 'pytest -q tests/a.py' 'ruff check .'`)"):
        assert part in code, part
    review = " ".join((prompts / "kind-review.md").read_text(encoding="utf-8").split())
    for part in ("Rely on the pass the change's task recorded for the head you review",
                 "They run again only when the head differs or no pass is recorded",
                 "never rerun the full suite by hand"):
        assert part in review, part


def test_the_coordinator_defers_on_a_landing_with_landed_id_not_a_raw_sha():
    """Pushing rebases each commit to a new sha, so a raw-sha ancestor probe can go stale for good."""
    prompts = PLUGIN / "template" / "prompts"
    coordinator = " ".join((prompts / "coordinator.md").read_text(encoding="utf-8").split())
    assert "`start_when` `landed:#<id>`" in coordinator
    assert "Never wait on a raw sha" in coordinator and "`ttp landed <sha>`" in coordinator
    review = " ".join((prompts / "daily-review.md").read_text(encoding="utf-8").split())
    assert "`landed:#<id>`" in review


def test_the_forked_test_runner_reports_every_test_once_as_one_process_would(tmp_path):
    """tests/conftest.py runs the tt-project tests in forked workers: every test runs exactly once,
    failures, skips and warnings come back as in one process, other files stay in one worker in
    order, and a worker that dies fails the test it was running instead of losing it."""
    import shutil
    import subprocess
    shutil.copy(pathlib.Path(__file__).with_name("conftest.py"), tmp_path / "conftest.py")
    (tmp_path / "test_tt_project_sample.py").write_text(
        "import os, pathlib, warnings, pytest\n"
        "@pytest.mark.parametrize('i', range(60))\n"
        "def test_ok(i, tmp_path):\n"
        "    pathlib.Path(os.environ['OUT'], f'ok-{i}').write_text(str(os.getpid()))\n"
        "def test_fail(): assert 1 == 2, 'boom'\n"
        "def test_skip(): pytest.skip('not here')\n"
        "def test_warn(): warnings.warn('careful', UserWarning)\n"
        "def test_crash(): os._exit(3)\n")
    (tmp_path / "test_other.py").write_text(
        "import os, pathlib\n"
        "def test_a(): pathlib.Path(os.environ['OUT'], 'a').write_text(str(os.getpid()))\n"
        "def test_b(): assert pathlib.Path(os.environ['OUT'], 'a').read_text() == str(os.getpid())\n")

    def run(jobs):
        out = tmp_path / f"out-{jobs}"
        out.mkdir()
        env = {**os.environ, "TTP_TEST_JOBS": str(jobs), "OUT": str(out)}
        r = subprocess.run([sys.executable, "-m", "pytest", "-q", "-rfEs", "-p", "no:cacheprovider", "-W",
                            "default", str(tmp_path)], cwd=str(tmp_path), env=env, capture_output=True,
                           text=True, timeout=120)
        return r, out

    forked, out = run(3)
    assert forked.returncode == 1, forked.stdout
    assert "2 failed, 63 passed, 1 skipped, 1 warning" in forked.stdout, forked.stdout
    assert "FAILED test_tt_project_sample.py::test_fail - AssertionError: boom" in forked.stdout
    assert "test_tt_project_sample.py::test_crash - the test worker process" in forked.stdout
    assert "SKIPPED [1] test_tt_project_sample.py:" in forked.stdout and "UserWarning: careful" in forked.stdout
    pids = {(out / f"ok-{i}").read_text() for i in range(60)}
    assert len(pids) > 1 and str(os.getpid()) not in pids, "the tests did not run in forked workers"
    # One process runs the same tests the same way, but for the test that ends its process.
    single, _ = run(1)
    assert single.returncode != 0 and "2 failed" not in single.stdout, single.stdout


BOX_PROBE = PLUGIN / "template" / "bin" / "tt-box-clean-probe"
PROBE_NOW = "2026-01-10T12:00:00"


def _box_probe(tmp_path, jobs=None, up_h=100.0, args=(), broker=None, tz="UTC", now=PROBE_NOW):
    """Run the box-clean probe at `now` (local, in zone `tz`) with a fake uptime and either a jobs JSON
    file or a broker."""
    import subprocess
    up = tmp_path / "uptime"
    up.write_text("%.2f 1.00\n" % (up_h * 3600))
    cmd = [sys.executable, str(BOX_PROBE), "--uptime-file", str(up), *(["--now", now] if now else []), *args]
    if jobs is not None:
        f = tmp_path / "jobs.json"
        f.write_text(json.dumps(jobs))
        cmd += ["--jobs-file", str(f)]
    else:
        cmd += ["--broker-python", str(broker or tmp_path / "no-broker-python")]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=60, env=dict(os.environ, TZ=tz))
    return r.returncode, r.stdout.strip()


def _job(hours_ago, owner="alice", status="done", command="pytest tests", job_id="j1"):
    import datetime as dt
    t = dt.datetime.fromisoformat(PROBE_NOW) - dt.timedelta(hours=hours_ago)
    return {"job_id": job_id, "owner": owner, "status": status, "command": command, "queued_at": t.isoformat()}


def test_box_clean_probe_ships_executable_and_documented():
    assert os.access(BOX_PROBE, os.X_OK)
    assert "tt-box-clean-probe" in (SKILLS / "tt-project-harness" / "SKILL.md").read_text()


@pytest.mark.parametrize("job", [
    _job(3, owner="[broker]power-cycle"), _job(3, owner="[broker]bridge-reset"), _job(3, owner="[broker]reboot"),
    _job(3, owner="[broker]pci-rescan"), _job(3, owner="[broker]health-gate"),
    dict(_job(3, owner="[broker]hold", status="started"), finished_at="2026-01-10T09:00:00"),
    _job(3, owner="[broker]fabric-check", status="failed"),
    _job(3, status="broker-kill"), _job(3, status="hung"), _job(3, command="tt-smi -r 0"),
    _job(3, command="python -c 'tt_device_reset()'"),
])
def test_box_clean_probe_counts_each_incident_kind(tmp_path, job):
    ended = _job(2.5, owner="[broker]hold", status="ended")
    rc, out = _box_probe(tmp_path, {"jobs": [_job(1), job, ended, _job(30, owner="[broker]power-cycle")]})
    assert rc == 1, out
    assert out.startswith("NOT CLEAN: newest incident 2026-01-10 09:00:00 (") and "clean at 2026-01-11 09:00" in out


def test_box_clean_probe_ignores_routine_and_old_jobs(tmp_path):
    jobs = [_job(1, owner="[broker]startup"), _job(2, owner="[broker]fabric-check"),
            _job(3, owner="[broker]hold", status="ended"), _job(4, status="failed"),
            _job(5, command="tt-smi -s"), _job(25, owner="[broker]power-cycle"), {"job_id": "x", "owner": "[broker]reboot"}]
    rc, out = _box_probe(tmp_path, {"jobs": jobs})
    assert (rc, out) == (0, "CLEAN: up 100.0 h, no broker incident since 2026-01-09 12:00")
    # A shorter window, aware timestamps and a bare list are read too.
    aware = [dict(_job(3, owner="[broker]reboot"), queued_at="2026-01-10T11:00:00+01:00")]
    assert _box_probe(tmp_path, aware, args=["--window-h", "2"])[0] == 1
    assert _box_probe(tmp_path, aware, args=["--window-h", "1.5"])[0] == 0
    aware[0]["queued_at"] = "2026-01-10T10:30:00Z"
    assert _box_probe(tmp_path, aware, args=["--window-h", "2"])[0] == 1


def test_box_clean_probe_dates_a_job_by_when_it_went_wrong(tmp_path):
    """A job queued and started before the window but hung, killed or running inside it counts."""
    import datetime as dt
    at = lambda h: (dt.datetime.fromisoformat(PROBE_NOW) - dt.timedelta(hours=h)).isoformat()
    hung = dict(_job(26, status="hung"), started_at=at(25), finished_at=at(1))
    rc, out = _box_probe(tmp_path, {"jobs": [_job(0.5), hung]})
    assert rc == 1 and "newest incident 2026-01-10 11:00:00" in out, out
    running = dict(_job(30, owner="[broker]power-cycle", status="running"), started_at=at(29))
    # A hold whose start row (finished when written) has no later "ended" row is still on.
    held = [dict(_job(50, owner="[broker]hold", status="ended")),
            dict(_job(40, owner="[broker]hold", status="started"), started_at=at(40), finished_at=at(40))]
    for jobs in ([running], held):
        rc, out = _box_probe(tmp_path, {"jobs": [_job(0.5), *jobs]})
        assert rc == 1 and "newest incident 2026-01-10 12:00:00" in out, out
    # A routine job that finished inside the window is still routine; the history check uses its queue time.
    done = dict(_job(30), started_at=at(29), finished_at=at(1))
    assert _box_probe(tmp_path, {"jobs": [done]})[0] == 0
    assert _box_probe(tmp_path, {"jobs": [_job(0.5), done]}, args=["--limit", "2"])[0] == 0


def test_box_clean_probe_counts_a_degraded_hold_and_chip_drop_orphans(tmp_path):
    """The broker's live row for a device held degraded (no tenant refused yet) is a hold on now."""
    import datetime as dt
    at = lambda h: (dt.datetime.fromisoformat(PROBE_NOW) - dt.timedelta(hours=h)).isoformat()
    held = {"job_id": "--", "owner": "[broker]", "status": "running", "finished_at": None,
            "queued_at": at(30), "started_at": at(30),
            "command": "device HELD (degraded): chip 3 off the bus - new tenant jobs refused until recovered"}
    rc, out = _box_probe(tmp_path, {"jobs": [held]}, args=["--window-h", "6"])
    assert rc == 1 and "newest incident 2026-01-10 12:00:00" in out, out
    # A tenant job orphaned by a chip drop counts when it went wrong; a plain interrupted one does not.
    orphan = dict(_job(3, status="interrupted", command="[MCP killed: chips left PCIe] pytest tests"),
                  started_at=at(3), finished_at=at(2))
    rc, out = _box_probe(tmp_path, {"jobs": [orphan]}, args=["--window-h", "6"])
    assert rc == 1 and "newest incident 2026-01-10 10:00:00" in out, out
    plain = dict(orphan, command="[interrupted: reboot] pytest tests")
    assert _box_probe(tmp_path, {"jobs": [plain]}, args=["--window-h", "6"])[0] == 0


def test_box_clean_probe_reads_broker_times_as_local(tmp_path):
    """The broker writes naive local times: west of UTC, an incident 1 h ago is still inside a 6 h window."""
    import datetime as dt
    tz = "America/Los_Angeles"
    if not os.path.exists("/usr/share/zoneinfo/" + tz):
        tz = "PST8PDT"
    env = dict(os.environ, TZ=tz)
    import subprocess
    local_now = subprocess.run([sys.executable, "-c", "import datetime; print(datetime.datetime.now().isoformat())"],
                               capture_output=True, text=True, env=env).stdout.strip()
    hour_ago = (dt.datetime.fromisoformat(local_now) - dt.timedelta(hours=1)).isoformat()
    job = {"job_id": "j9", "owner": "[broker]power-cycle", "status": "completed", "command": "power-cycle",
           "queued_at": hour_ago, "started_at": hour_ago, "finished_at": hour_ago}
    rc, out = _box_probe(tmp_path, {"jobs": [job]}, args=["--window-h", "6"], tz=tz, now=None)
    assert rc == 1, out


def test_box_clean_probe_counts_a_recent_boot_and_short_history(tmp_path):
    rc, out = _box_probe(tmp_path, {"jobs": []}, up_h=5)
    assert rc == 1 and "(host boot)" in out and "clean at 2026-01-11 07:00" in out
    rc, out = _box_probe(tmp_path, {"jobs": [_job(1), _job(2)]}, args=["--limit", "2"])
    assert rc == 1 and "does not reach back 24.0 h" in out
    assert _box_probe(tmp_path, {"jobs": [_job(1), _job(30)]}, args=["--limit", "2"])[0] == 0


def test_box_clean_probe_degrades_without_a_broker_and_255_when_unreadable(tmp_path):
    rc, out = _box_probe(tmp_path)
    assert rc == 0 and "no broker installed, uptime only" in out
    assert _box_probe(tmp_path, up_h=3)[0] == 1
    rc, out = _box_probe(tmp_path, args=["--require-broker"])
    assert rc == 255 and out.startswith("UNKNOWN: no broker python at")
    for body in ("exit 3", "echo not json", "echo '{\"error\": \"held\"}'"):
        fake = tmp_path / "broker-python"
        fake.write_text("#!/bin/sh\n%s\n" % body)
        fake.chmod(0o755)
        rc, out = _box_probe(tmp_path, broker=fake)
        assert rc == 255 and out.startswith("UNKNOWN: broker"), out
    (tmp_path / "jobs.json").write_text("{")
    import subprocess
    r = subprocess.run([sys.executable, str(BOX_PROBE), "--jobs-file", str(tmp_path / "jobs.json"),
                        "--uptime-file", str(tmp_path / "missing")], capture_output=True, text=True)
    assert r.returncode == 255 and r.stdout.startswith("UNKNOWN: uptime unreadable")


def test_box_clean_probe_queries_the_broker_python(tmp_path):
    """The broker's python gets the fetch snippet with port, limit and timeout and its JSON is judged."""
    fake = tmp_path / "broker-python"
    seen = tmp_path / "argv"
    payload = json.dumps({"jobs": [_job(2, owner="[broker]health-gate")]})
    fake.write_text("#!/bin/sh\nprintf '%%s\\n' \"$@\" > %s\necho '%s'\n" % (seen, payload))
    fake.chmod(0o755)
    rc, out = _box_probe(tmp_path, broker=fake, args=["--port", "9000", "--limit", "50"])
    assert rc == 1 and "[broker]health-gate" in out, out
    text = seen.read_text()
    assert text.startswith("-c\n") and "tt_device_recent_jobs" in text and text.endswith("\n9000\n50\n40\n")


def test_self_reboot_recipe_never_waits_on_drain_probe_while_paused():
    import re
    root = pathlib.Path(__file__).resolve().parents[1] / "plugins" / "tt-project" / "skills"
    for name in ("tt-project", "tt-project-harness"):
        text = (root / name / "SKILL.md").read_text()
        assert "--wait 240" in text
        assert not re.search(r"retry_when`?\s*`ttp drain", text)
        assert "ttp resume <name>" in " ".join(text.split())
