# What a task that answers a chat posts there (runtime/ttp/replymsg.py, Daemon._finish_worker, pushq._reply).
# Run from test_tt_project_runtime.py.

# Task #3304's hand-off: the steps it was asked to send were only in its tool output.
_SELF_DESCRIBED = ("Extracted the MAC RERUN steps from run 7021 result.json and returned them as the message for "
                   "the user. No code, branch or state changed.")


def _reply_hand_off(env, p, result: dict, chat="c1", kind="work"):
    from ttp.daemon import Daemon
    from ttp.providers.base import RunUsage as Usage
    tid = p.db.add_task("send the steps", "spec", kind=kind, tier="light", origin="coordinator")
    p.db.update_task(tid, status="running", reply_chat=chat)
    run_dir = env["tmp"] / f"run-reply-{tid}"
    run_dir.mkdir()
    (run_dir / "result.json").write_text(json.dumps(result))
    d = Daemon(p.base)
    with d.p.db.tx():
        d._finish_worker({"task": tid}, Usage(cost_usd=1.0), "ok", run_dir)
    replies = [m["text"] for m in p.db.q("SELECT text FROM messages WHERE kind='reply' AND chat=?", (chat,))]
    return p.db.task(tid), replies


def test_a_reply_posts_the_hand_offs_message_not_its_summary(env):
    from ttp.db import load_result
    p = make(env)
    steps = "1. Run `ttp notifier reset`.\n2. Run `ttp notifier check` and paste back what it prints."
    task, replies = _reply_hand_off(env, p, {"status": "done", "summary": "Sent the user the rerun steps.",
                                             "message": steps})
    assert task["status"] == "done" and replies == [steps]
    assert load_result(task["result"])["message"] == steps, "the record keeps what was sent"


def test_a_reply_without_a_deliverable_is_not_posted_and_goes_to_review(env):
    from ttp.db import load_result
    p = make(env)
    for result in ({"status": "done", "summary": _SELF_DESCRIBED},
                   {"status": "done", "summary": "Done.", "message": "The steps are written out above, ready to send."},
                   {"status": "done", "summary": ""}):
        task, replies = _reply_hand_off(env, p, result)
        assert replies == [], result
        assert task["status"] == "review" and "nothing was posted to the chat" in task["blocked_reason"], result
        assert load_result(task["result"])["undelivered"] in task["blocked_reason"]
        [ev] = p.db.q("SELECT * FROM events WHERE task=? AND kind='task_review'", (task["id"],))
        assert ev["status"] == "queued" and "Not posted to the chat" in ev["text"], "the coordinator sees it"


def test_ordinary_answers_still_post(env):
    p = make(env)
    for summary in ("Yes: the daemon restarts after a reboot (daemon.py:3791).",
                    "The notifier sent the user a toast at 09:30 PDT; its log shows it was delivered.",
                    "The user's config sets review.auto to false, so no review was queued.",
                    "Run it again with --strict.",
                    "Sent the fix to the push queue; it landed as 1a2b3c4.",
                    "Coverage is 92%; it is above the 90% target.",
                    "Yes, the release note is ready to send.",
                    "Sent the user the steps: 1) git pull 2) ttp setup 3) ttp notifier test."):
        task, replies = _reply_hand_off(env, p, {"status": "done", "summary": summary}, chat=f"c{len(summary)}")
        assert task["status"] == "done" and replies == [summary], summary
    # A failed or blocked run describing itself is still reported: its summary says what went wrong.
    task, _ = _reply_hand_off(env, p, {"status": "blocked", "summary": _SELF_DESCRIBED, "question": "which box?"},
                              chat="cb")
    assert task["status"] == "blocked"


def test_replymsg_flags_only_short_self_descriptions():
    from ttp import replymsg
    for t in (_SELF_DESCRIBED, "Returned the Mac rerun steps to the chat.", "Relayed them to the user.",
              "Gave the user the steps.", "The message is above.", "Drafted it; ready to send."):
        assert replymsg.undelivered({"summary": t}), t
    long_steps = _SELF_DESCRIBED + "\nSteps:\n1. `git checkout abc`\n2. `ttp notifier check`"
    for t in (long_steps, "You returned them as the message for the user; that is fine.\n" * 12,
              "Yes, it is.", "Sent you the steps by mail.", "As shown in the log, the job passed.",
              # a comparison, not the reply placed above
              "Coverage is 92%; it is above the 90% target.", "Temperature: it is above 80C on chip 3.",
              "Spend this week: they are above budget on both boxes.", "The answer is above 1000 for every run.",
              # a named deliverable's state, not the reply's
              "Yes, the release note is ready to send.",
              # the deliverable follows the phrase
              "Sent the user the steps: 1) git pull 2) ttp setup 3) ttp notifier test.",
              "Mac rerun steps (ready to paste): git pull; ttp setup; ttp notifier test.",
              "Returned them as the message for the user: run ttp setup, then ttp notifier test."):
        assert not replymsg.undelivered({"summary": t}), t
    for t in ("The steps are written out above.", "The steps are written out above, ready to send.",
              "Done: returned them as the message for the user."):
        assert replymsg.undelivered({"summary": t}), t
    assert replymsg.text({"summary": "s", "message": "  "}) == "s", "an empty message falls back to the summary"
    assert replymsg.text({"summary": "s", "message": "m"}) == "m"


def test_a_landed_reply_posts_its_message_with_how_it_landed():
    from ttp import pushq
    assert pushq._done_text({"message": "Here are the steps."}, "s (landed)", "landed") == "Here are the steps. (landed)"
    assert pushq._done_text({"summary": "s"}, "s (landed)", "landed") == "s (landed)"


def test_a_reply_chat_worker_prompt_says_the_message_is_relayed_verbatim(env):
    from ttp.prompts import worker_task
    p = make(env)
    tid = p.db.add_task("send the steps", "spec", kind="work", tier="light", origin="coordinator")
    assert "## Reply to the chat" not in worker_task(p, p.db.task(tid), str(p.root), None)
    p.db.update_task(tid, reply_chat="c1")
    text = worker_task(p, p.db.task(tid), str(p.root), None)
    assert "## Reply to the chat" in text and "`message` there verbatim" in text and "deliverable" in text


# A chat relay shows 6000 chars of one message: a longer reply or notify was cut there silently
# (upstream note b8f4ad25236ac40d). msgparts posts it whole as numbered parts, or rejects a document.
_PART = re.compile(r"^\((\d+)/(\d+)\) ")


def _parts(p, kind, chat=None):
    rows = p.db.q("SELECT text FROM messages WHERE direction='out' AND kind=? AND chat IS ? ORDER BY id", (kind, chat))
    return [r["text"] for r in rows]


def _unmarked(parts):
    marks = [_PART.match(t) for t in parts]
    assert all(marks) and [int(m.group(1)) for m in marks] == list(range(1, len(parts) + 1))
    assert {int(m.group(2)) for m in marks} == {len(parts)}
    return [t[m.end():] for t, m in zip(parts, marks)]


def test_a_long_reply_is_posted_whole_as_ordered_parts_split_at_paragraphs(env):
    from ttp import coordinator as coord, msgparts
    p = make(env)
    paras = [f"Paragraph {i}. " + "word " * 300 for i in range(12)]   # ~1.5k chars each, ~18k in all
    text = "\n\n".join(paras)
    assert coord.apply(p, [{"type": "reply", "chat": "c1", "text": text}]) == []
    parts = _parts(p, "reply", "c1")
    assert len(parts) > 1 and all(len(t) <= msgparts.LIMIT for t in parts)
    bodies = _unmarked(parts)
    assert "".join(bodies) == text, "no character lost or repeated: the history holds it once"
    assert all(b.endswith("\n\n") for b in bodies[:-1]), "each part ends at a paragraph break"


def test_the_digest_lists_a_long_reply_once_by_its_first_part(env):
    from ttp import coordinator as coord
    p = make(env)
    for i in range(3):
        p.db.post("out", f"earlier reply {i}", chat="c1", kind="reply")
    text = "\n\n".join(f"Paragraph {i}. " + "word " * 300 for i in range(12))
    assert coord.apply(p, [{"type": "reply", "chat": "c1", "text": text}]) == []
    sent = coord.digest(p, {}, [], []).split("## Recently sent to the user")[1].split("\n## ")[0]
    assert "(1/" in sent and "(2/" not in sent and all(f"earlier reply {i}" in sent for i in range(3))


def test_a_single_overlong_paragraph_is_cut_with_nothing_lost():
    from ttp import msgparts
    for text in ("x" * 13000, "Sentence one goes here. " * 700, "line\n" * 3000, "x" * (9 * 5995)):
        parts = msgparts.split(text)
        assert len(parts) > 1 and all(len(t) <= msgparts.LIMIT for t in parts), len(text)
        assert "".join(_unmarked(parts)) == text
    sentences = _unmarked(msgparts.split("Sentence one goes here. " * 700))
    assert all(b.endswith(". ") for b in sentences[:-1]), "no line break: it cuts at a sentence end"


def test_a_long_notify_is_posted_whole_as_parts(env):
    from ttp import coordinator as coord, msgparts
    p = make(env)
    text = "\n".join(f"- host {i} is fine " + "detail " * 40 for i in range(60))   # one paragraph of lines
    assert coord.apply(p, [{"type": "notify", "text": text, "severity": "normal"}]) == []
    parts = _parts(p, "alert")
    assert len(parts) > 1 and all(len(t) <= msgparts.LIMIT for t in parts)
    bodies = _unmarked(parts)
    assert "".join(bodies) == text and all(b.endswith("\n") for b in bodies[:-1])


def test_a_reply_or_notify_over_the_part_cap_is_rejected_naming_the_limit(env):
    from ttp import coordinator as coord, msgparts
    p = make(env)
    text = "word " * (msgparts.MAX_CHARS // 5 + 1)
    problems = coord.apply(p, [{"type": "reply", "chat": "c1", "text": text},
                               {"type": "notify", "text": text, "severity": "normal"}])
    assert len(problems) == 2, problems
    for prob, what in zip(problems, ("reply", "notify")):
        assert f"{what} rejected" in prob and str(len(text)) in prob and str(msgparts.MAX_CHARS) in prob
        assert str(msgparts.LIMIT) in prob
    assert not p.db.q("SELECT id FROM messages WHERE direction='out'"), "nothing of it was posted"


def test_a_reply_under_the_limit_is_one_message_unchanged(env):
    from ttp import coordinator as coord, msgparts
    p = make(env)
    for text in ("short answer", "a" * msgparts.LIMIT):
        assert coord.apply(p, [{"type": "reply", "chat": "c1", "text": text}]) == []
        assert _parts(p, "reply", "c1")[-1] == text
    assert len(_parts(p, "reply", "c1")) == 2


def test_a_long_worker_reply_is_posted_as_parts_and_cut_only_past_the_cap_saying_where(env):
    from ttp import msgparts
    p = make(env)
    long = "\n\n".join("Step. " + "x " * 700 + "end." for _ in range(6))   # ~8.4k chars
    task, replies = _reply_hand_off(env, p, {"status": "done", "summary": "s", "message": long})
    assert len(replies) == 2 and "".join(_unmarked(replies)) == long
    huge = "y " * msgparts.MAX_CHARS
    cut = msgparts.cut(huge, "task #7's hand-off")
    assert len(cut) <= msgparts.MAX_CHARS and huge.startswith(cut[:cut.index("\n\n[")])
    assert f"{len(huge) - cut.index(chr(10) + chr(10) + '[')} more chars not posted here" in cut
    assert "task #7's hand-off" in cut



def test_a_reply_whose_hand_off_ends_with_the_ai_tag_reaches_the_chat_without_it(env):
    """Reply #1834: a worker's chat `message` (in its on_pass too) ended with '[authored by AI]' and was
    posted verbatim. The chat is the user's own, so the tag is dropped there and from the stored hand-off."""
    from ttp.db import load_result
    p = make(env)
    body = ("The always-on coordinator is now the default.\n\n- **Next:** a review, then the push queue.")
    tagged = body + "\n\n[authored by AI]\n"
    task, replies = _reply_hand_off(env, p, {"status": "done", "summary": "Default on. [authored by AI]",
                                             "message": tagged,
                                             "on_pass": {"status": "done", "message": tagged, "summary": "x"}})
    assert task["status"] == "done" and replies == [body]
    stored = load_result(task["result"])
    assert stored["message"] == body and stored["summary"] == "Default on." and stored["on_pass"]["message"] == body
    # Every message to the user's chats drops it: a coordinator reply, a notify, an ask, a broadcast,
    # the project's own configured tag too; an inbound note (another project's channel) keeps its text.
    p.db.ai_tag = "(bot)"
    for kind, chat, text in (("reply", "c1", "Tests are green.\n_Written by AI_"), ("info", None, "Pushed. (bot)"),
                             ("ask", None, "Merge it?\n\n[Authored by AI]")):
        mid = p.db.post("out", text, chat=chat, kind=kind)
        assert "AI" not in p.db.one("SELECT text FROM messages WHERE id=?", (mid,))["text"].split("\n")[-1]
        assert "(bot)" not in p.db.one("SELECT text FROM messages WHERE id=?", (mid,))["text"]
    mid = p.db.post("in", "A note for you.\n\n[authored by AI]", chat="peer", kind="note")
    assert p.db.one("SELECT text FROM messages WHERE id=?", (mid,))["text"].endswith("[authored by AI]")
