---
"tt-project": minor
---

Check a coordinator reply or notify's own 'landed'/'live' lines before it is sent: a 'landed' line
must name a commit the push branch holds, a 'live' one a commit the running version contains, and
one that names none, or whose commit fails the check, is marked 'not landed'/'not live yet' in
place, with a note to the coordinator in its next digest. The landed/live parse extends
pushclaim's single parser (fail-open), and the hedges now cover "should/may/might/could have".
