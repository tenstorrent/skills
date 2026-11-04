# Why maintainers ask for what they ask for

The examples are generic. Always confirm the current mechanism in the repository.

**1. Trustworthy output.** Triage output becomes evidence in someone else's investigation. A
reviewer's first question is "what if that assumption is false on a real failed system?" What if
the device is not the first one? What if a device the runtime used is not discoverable after
the failure? What if the user narrowed the visible devices, so the runtime's numbering no
longer matches the debugger's? An ID taken from one source and used against another is a
classic wrong-answer bug.

**2. Fail loudly.** A `try/except` that turns a missing symbol into "N/A" keeps CI green while
the script quietly stops working. Maintainers prefer an exception that a test turns red. They
accept guarded code only where the guarded condition is the failure being diagnosed. Repeating
the same warning on hundreds of cores for one device-level cause buries the signal: raise once
at the right level. When a fallback catches an error, keep or log the original.

**3. The framework owns the plumbing.** Each repeated piece (a loop over devices with its own
error handling, a hand-built table, a local colour flag, re-parsed arguments) misses later
framework improvements such as parallelism, broken-device skipping and new output formats. It
also becomes the template for the next script. The usual request is "use the framework's
facility", or "add it to the framework, then use it".

**4. One source of truth.** Hard-coded core counts, address maps, block lists or
per-architecture tables go stale when hardware or firmware changes. Maintainers ask for them
from the debugger library, the firmware's own symbols or the runtime's metadata. If the
library lacks the API, they ask for an issue there rather than a local copy. Duplicated logic
drifts, so when one script changes a shared pattern, its siblings should change too.

**5. Public abstractions.** Reaching into private members or guessing the architecture from
class types and strings breaks when the library is refactored. Architecture checks list the
architectures that behave a given way, so a new architecture is handled on purpose, not by
accident.

**6. Observe, don't perturb.** Halting a core or patching memory to read something must be undone
in `finally`. Otherwise triage itself leaves the system worse than it found it. What triage
changed should be visible in its own report.

**7. Output for the reader.** Logs are shared with experts who were not there. The default view
answers "what is wrong and where". Internal counters and raw pointers belong at higher
verbosity. Scripts return data, not formatted text, so every output format (console,
machine-readable, database) works. Mixed-type fields break typed outputs.

**8. Explicit code.** `has_x` that actually checks `y`, a path converted to a string and back,
or an unexplained `24` each make the reader stop and reverse-engineer the code. Precise names,
one type, named constants and a "why" comment on a non-obvious choice remove that cost. Module
docs are printed as help, so a generic description ("handles IDs") is a defect.

**9. Only necessary code.** Maintainers push back on code that handles cases they believe cannot
happen, on accessors nobody calls, and on layers of indirection around simple logic. Reviewers
have explicitly called out over-complex, generated-looking changes. "Remove it until we see the
case" is a common outcome.

**10. Placement.** CI glue in the tool makes local runs carry CI concerns. Hidden environment
variables are undiscoverable, while options show in help. A free function that manipulates one
class's internals belongs on that class.

**11. Efficiency.** Systems have many devices and hundreds of cores. Reading a whole structure to
use one field, re-reading what another script already cached, or scanning every core to find a
few known ones multiplies the cost. A fixed sleep can often be replaced by ordering the work.

**12. Tests.** The test suite runs the scripts against real failures. That catches drift between
the tool and the firmware or runtime. Unit tests of trivial helpers do not, and they add churn.

**13. Shipped together.** The tool reads runtime metadata from the same build, so
compatibility code for older formats is dead weight. A change to that metadata must change its
producer and every reader.
