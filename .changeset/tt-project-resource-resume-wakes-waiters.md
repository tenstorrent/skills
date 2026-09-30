---
"tt-project": patch
---

`tt-project`: resuming a paused resource wakes the tasks that handed off `waiting` because of the
pause (for example after `ttp lock` refused it with exit 75), so they start again soon instead of
after their full `retry_after_s` or `retry_when` wait. The web app's health line no longer counts a
task on a paused resource as ready to start; it is listed under the paused resource only. Pausing
or resuming a resource from the web app no longer fails on the web server's thread.
