# Skill Prose Rules

- How tt-buddy skills read.
- Directive voice, abstraction discipline, no cross-skill file references.

## Style

- Skill text follows `tt-buddy:buddy`'s `tone.md` § Style.
- Simple English. High level. Low verbosity.
- Bullets of 10 words or fewer.
- NEVER prose, metaphors, or archeology.
- Tables and code blocks are exempt from the word limit.

## Directness

Skills give orders. Soft language fails to fire.

- **Imperative voice for every action.** "Invoke X." "Run Y." "Record Z."
- **MUST / NEVER / ABSOLUTELY for hard rules.**
- SHOULD only when an alternative is truly acceptable. Default is MUST.
- **Skill invocations are directives.** `Invoke `tt-buddy:foo``, per `tt-buddy:buddy` § Host mapping.
- Never `see tt-buddy:foo` for invocations.
- Reserve `see X` for documentation cross-references.
- **No hedges on required behavior.** No "should also", "consider", "perhaps".
- **Red Flags tables** (`thought → reality`) catch rationalizations.
- **Decision trees and dispatch tables** for branching logic.
- **Name discipline failures.** "Skipping `tt-buddy:note` is a discipline failure."

Tone reference: `superpowers:using-superpowers` and `tt-buddy:buddy`.

---

## Abstraction Discipline

- A skill captures the rule. Nothing else.
- Not the session, test case, PR, or working path.
- Commits hold the journey. Notes hold findings. Skills hold rules.
- Each proper noun in a draft: name the rule it serves.
- Rule stands without the name: drop the name.

Where session detail goes instead:

- Test case that revealed the rule → state the rule.
- PR that motivated the change → point to the rule.
- Specific working path → describe the shape.
- User who flagged the issue → quote no one.

---

## Self-Contained Skills

- A skill references its own siblings only.
- NEVER reference files inside another skill's directory.
- Use another skill's capability by invoking it.
- Within a skill: relative paths to siblings, e.g. `tone.md`.
- The host announces the skill's absolute directory.
- Across skills: invoke the skill.
- Plugin-root resources (`<plugin-root>/recipes/`): shared plugin data.
- Reference them as `<plugin-root>/...`. NEVER use `../` paths.
