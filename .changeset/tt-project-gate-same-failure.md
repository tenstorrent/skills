---
"tt-project": patch
---

`tt-project`:

- `ttp checks`: the tests gate calls a head failure pre-existing only when the base fails the test the same way (the same first error line or summary message, and the same failing statement); a base that fails it another way, or a base rerun that errors or selects nothing, blocks like a new failure. The gate's rerun no longer leaks its test selection into a pytest a test starts itself, which made such a test fail the same wrong way on the head and the base and hid a real regression as pre-existing (#6793)
