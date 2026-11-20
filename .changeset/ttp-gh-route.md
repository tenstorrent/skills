---
"tt-project": patch
---

`tt-project`: a host with no GitHub login still does its gh work, and nobody is asked to log gh in there. The new setting `delivery.gh_route` (an ssh alias of a machine where gh is logged in; unset by default) runs each gh call of workers, pr-watch and `ttp ci` on that machine with `-R` for the checkout's repo, after the same local guards. With no route, or the route down, public reads still run, PR watching shows as degraded, gh writes are held (exit 75) and retried, and the host reports one harness condition naming the setting and what was skipped. An https GitHub push with no https login here goes over the remote's ssh form with the forwarded ssh agent; the stored remote is never edited and no credential is written. If that fails, the push queue holds the batch and says which setting to look at. The ask gate refuses an ask or notify telling the user to log gh in on a remote host.
