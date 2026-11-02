# Notification cases: the web app's browser notifications and the optional Slack channel's state and
# test. Run from tests/test_tt_project_runtime.py (see _include_cases there): helpers, fixtures and
# imports come from that file, and pytest collects these tests under it.
from __future__ import annotations


def _slack_api(fail_method=None, error="invalid_auth"):
    """A stand-in for the Slack Web API at the urlopen level: every call answers ok, except
    `fail_method`, which answers ok=false with `error` ('net': the network is down)."""
    calls = []

    def urlopen(req, timeout=None):
        import io
        import urllib.parse
        method = req.full_url.rsplit("/", 1)[-1]
        params = {k: v[0] for k, v in urllib.parse.parse_qs(req.data.decode()).items()}
        calls.append((method, params))
        if method == fail_method:
            if error == "net":
                raise urllib.error.URLError("connection refused")
            return io.BytesIO(json.dumps({"ok": False, "error": error}).encode())
        body = {"auth.test": {"ok": True, "bot_id": "B1"},
                "users.lookupByEmail": {"ok": True, "user": {"id": "U7"}},
                "conversations.open": {"ok": True, "channel": {"id": "D1"}},
                "chat.postMessage": {"ok": True, "ts": "1.0"}}.get(method, {"ok": True})
        return io.BytesIO(json.dumps(body).encode())
    urlopen.calls = calls
    return urlopen


def test_slack_status_names_each_state_and_says_slack_is_optional(env, monkeypatch):
    import ttp.project
    from ttp import slack
    secrets = {}
    monkeypatch.setattr(ttp.project, "load_secrets", lambda: secrets)
    off = {"notify": {"slack": False}}
    on = {"notify": {"slack": True, "slack_min_severity": "normal"}}
    got = slack.status(off)
    assert got["state"] == "not_configured" and "optional" in got["line"] and "ttp secret slack" in got["line"]
    assert slack.status(on)["state"] == "no_token" and "ttp secret slack" in slack.status(on)["line"]
    secrets["slack"] = {"bot_token": "test-bot-token", "user_id": "U1"}
    got = slack.status(off)
    assert got["state"] == "off" and "notify.slack true" in got["line"]
    got = slack.status(on)
    assert got == {"state": "on", "line": got["line"]} and "normal severity" in got["line"]
    got = slack.status(on, {"ts": 5.0, "error": "chat.postMessage: token_revoked"})
    assert got["state"] == "error" and got["since"] == 5.0 and got["line"].endswith("token_revoked")


def test_slack_send_test_reports_success_or_the_exact_error(env, monkeypatch):
    import ttp.project
    import ttp.slack
    from ttp import slack
    secrets = {}
    monkeypatch.setattr(ttp.project, "load_secrets", lambda: secrets)
    monkeypatch.setattr(ttp.slack.time, "sleep", lambda s: None)
    ok, text = slack.send_test("demo")
    assert not ok and "not configured" in text and "ttp secret slack" in text
    secrets["slack"] = {"bot_token": "test-bot-token", "user_email": "user@example.com"}
    api = _slack_api()
    monkeypatch.setattr(ttp.slack.urllib.request, "urlopen", api)
    ok, text = slack.send_test("demo")
    assert ok and "U7" in text, text
    post = [p for m, p in api.calls if m == "chat.postMessage"]
    assert len(post) == 1 and post[0]["channel"] == "D1" and post[0]["text"].startswith("[demo] Test message")
    for method, error, want in [("auth.test", "invalid_auth", "Slack test failed: auth.test: invalid_auth"),
                                ("users.lookupByEmail", "users_not_found", "users.lookupByEmail: users_not_found"),
                                ("chat.postMessage", "not_in_channel", "chat.postMessage: not_in_channel"),
                                ("auth.test", "net", "could not reach Slack")]:
        monkeypatch.setattr(ttp.slack.urllib.request, "urlopen", _slack_api(method, error))
        ok, text = slack.send_test("demo")
        assert not ok and want in text, (method, text)
    # No user to send to: the reason, not a traceback.
    secrets["slack"] = {"bot_token": "test-bot-token"}
    monkeypatch.setattr(ttp.slack.urllib.request, "urlopen", _slack_api())
    ok, text = slack.send_test("demo")
    assert not ok and "no Slack user id or email configured" in text


def test_notifier_test_slack_prints_the_outcome_and_exits_1_on_failure(env, monkeypatch, capsys):
    from ttp import cli, notifier, slack
    shown = []
    monkeypatch.setattr(notifier, "show", lambda *a, **kw: (shown.append(a), {"backend": "notify-send", "ok": True,
                        "confirmed": True, "error": "", "ts": 0.0})[1])   # show() returns its delivery result
    monkeypatch.setattr(notifier, "ensure_app", lambda: (None, ""))
    monkeypatch.setattr(slack, "send_test", lambda project="tt-project": (False, "Slack test failed: auth.test: invalid_auth"))
    with pytest.raises(SystemExit) as e:
        cli.main(["notifier", "test", "--slack"])
    assert e.value.code == 1 and "invalid_auth" in capsys.readouterr().out and shown == []
    monkeypatch.setattr(slack, "send_test", lambda project="tt-project": (True, "sent a test message to your Slack DM (user U1)"))
    cli.main(["notifier", "test", "--slack"])
    assert "sent a test message" in capsys.readouterr().out and shown == []
    cli.main(["notifier", "test"])                        # without --slack: the desktop test, as before
    assert len(shown) == 1


def test_a_failed_slack_send_shows_until_the_next_good_one(env, monkeypatch):
    import ttp.project
    from ttp import web
    failing = {"on": True}
    p, d = slack_daemon(env, monkeypatch, FakeSlackHTTP(lambda text: "token_revoked" if failing["on"] else None))
    monkeypatch.setattr(ttp.project, "load_secrets", lambda: {"slack": {"bot_token": "test-bot-token", "user_id": "U1"}})
    p.set_config("notify.slack", True)
    assert web.state_payload(p, p.db)["notify"]["slack"]["state"] == "on"
    p.db.post("in", "hi", chat="slack", channel="slack", kind="user", ref="9.0")
    p.db.post("out", "reply", chat="slack", ref="9.0")
    d.deliver_outbound()
    got = web.state_payload(p, p.db)["notify"]["slack"]
    assert got["state"] == "error" and "token_revoked" in got["line"] and got["since"], got
    failing["on"] = False
    d.deliver_outbound()
    assert web.state_payload(p, p.db)["notify"]["slack"]["state"] == "on"


class _CrashySlackDM:
    """A Slack DM at the urlopen level that keeps what was posted and returns it from
    conversations.history/replies as Slack does (& < > escaped). `crash_after_post` makes the next
    chat.postMessage post and then raise SystemExit, as a daemon killed right after posting."""

    def __init__(self):
        self.posts: list[dict] = []
        self.crash_after_post = False

    def sent(self, bot="B1"):
        return [m["text"] for m in self.posts if m["bot_id"] == bot]

    def __call__(self, req, timeout=None):
        import io
        import urllib.parse
        method = req.full_url.rsplit("/", 1)[-1]
        params = {k: v[0] for k, v in urllib.parse.parse_qs(req.data.decode(), keep_blank_values=True).items()}
        if method == "chat.postMessage":
            ts = f"{time.time() + len(self.posts) * 1e-3:.6f}"
            esc = params["text"].replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            self.posts.append({"ts": ts, "bot_id": "B1", "text": esc,
                               **({"thread_ts": params["thread_ts"]} if params.get("thread_ts") else {})})
            if self.crash_after_post:
                self.crash_after_post = False
                raise SystemExit("killed after the post")
            body = {"ok": True, "ts": ts}
        elif method in ("conversations.history", "conversations.replies"):
            thread = params.get("ts") if method == "conversations.replies" else None
            body = {"ok": True, "messages": [m for m in self.posts if float(m["ts"]) >= float(params["oldest"])
                                             and m.get("thread_ts") == thread]}
        else:
            body = {"auth.test": {"ok": True, "bot_id": "B1"},
                    "conversations.open": {"ok": True, "channel": {"id": "D1"}}}.get(method, {"ok": True})
        return io.BytesIO(json.dumps(body).encode())


def test_slack_outbound_posts_each_message_once_when_killed_after_the_post(env, monkeypatch):
    """A daemon killed between a Slack post and recording it finds the post in the DM on restart and
    records it instead of posting again; killed right after the record's commit, it skips the row; a
    marker with no post behind it (killed before posting) still posts. Each message reaches Slack once,
    top-level or in a thread, and another bot's post of the same text does not count."""
    from ttp.daemon import SLACK_SENDING_KEY, Daemon
    from ttp.slack import Slack
    dm = _CrashySlackDM()
    p, d = slack_daemon(env, monkeypatch, dm)

    def restarted():
        d = Daemon(p.base)
        d._slack = Slack("test-bot-token", user_id="U1")
        d.slack = lambda: d._slack
        return d

    p.db.post("in", "hi", chat="slack", channel="slack", kind="user", ref="9.0")
    top = p.db.post("out", "PR <ready> & green", severity="high")
    reply = p.db.post("out", "a  reply", chat="slack", ref="9.0")
    dm.posts.append({"ts": f"{time.time() - 1:.6f}", "bot_id": "B2", "text": f"[{p.name}] PR &lt;ready&gt; &amp; green"})
    for mid in (top, reply):                    # killed after the post, before its bookkeeping
        dm.crash_after_post = True
        with pytest.raises(SystemExit):
            d.deliver_outbound()
        assert p.db.kv(SLACK_SENDING_KEY)["id"] == mid
        assert not p.db.one("SELECT ext_id FROM messages WHERE id=?", (mid,))["ext_id"]
        d = restarted()
    d.deliver_outbound()
    assert dm.sent() == [f"[{p.name}] PR &lt;ready&gt; &amp; green", "a  reply"]
    rows = {r["id"]: r["ext_id"] for r in p.db.q("SELECT id, ext_id FROM messages WHERE id IN (?, ?)", (top, reply))}
    assert rows == {top: dm.posts[1]["ts"], reply: dm.posts[2]["ts"]} and dm.posts[2]["thread_ts"] == "9.0"
    assert int(p.db.kv("slack_last_out", 0)) == reply and not p.db.kv(SLACK_SENDING_KEY)
    assert set(rows.values()) <= set(p.db.kv("slack_threads", []))

    # Killed right after the commit that records the post: the restart moves on without posting.
    after = p.db.post("out", "after the commit", chat="slack", ref="9.0")

    class KillOnPop(dict):
        def pop(self, *a):
            raise SystemExit("killed after the commit")
    d._slack_rejects = KillOnPop()
    with pytest.raises(SystemExit):
        d.deliver_outbound()
    assert int(p.db.kv("slack_last_out", 0)) == after
    p.db.set_kv("slack_last_out", reply)       # a cursor left behind a recorded row (an older daemon)
    d = restarted()
    d.deliver_outbound()
    assert dm.sent().count("after the commit") == 1 and int(p.db.kv("slack_last_out", 0)) == after

    # Killed after setting the marker but before posting: the restart finds nothing and posts once, even
    # with the same text posted and recorded just before (another message's post).
    last = p.db.post("out", "never posted", chat="slack", ref="9.0")
    p.db.set_kv(SLACK_SENDING_KEY, {"id": last, "t": time.time()})
    d = restarted()
    d.deliver_outbound()
    d.deliver_outbound()
    assert dm.sent().count("never posted") == 1 and int(p.db.kv("slack_last_out", 0)) == last
    again = p.db.post("out", "never posted", chat="slack", ref="9.0")
    p.db.set_kv(SLACK_SENDING_KEY, {"id": again, "t": time.time()})
    d = restarted()
    d.deliver_outbound()
    assert dm.sent().count("never posted") == 2 and int(p.db.kv("slack_last_out", 0)) == again
    assert p.db.one("SELECT ext_id FROM messages WHERE id=?", (again,))["ext_id"] == dm.posts[-1]["ts"]


@needs_sockets
def test_web_notify_test_endpoint_returns_the_notification_and_the_slack_outcome(env, monkeypatch):
    p = make(env)
    from ttp import slack, web
    sent = []
    monkeypatch.setattr(slack, "send_test", lambda project: sent.append(project) or (False, "Slack test failed: auth.test: invalid_auth"))
    port = _start_web(p)

    def post(body, token=None):
        req = urllib.request.Request(f"http://127.0.0.1:{port}/api/notify/test", method="POST", data=json.dumps(body).encode(),
                                     headers={"X-TTP-Token": token or web.token(p), "Content-Type": "application/json"})
        for _ in range(50):
            try:
                with urllib.request.urlopen(req, timeout=5) as r:
                    return r.status, json.loads(r.read())
            except urllib.error.HTTPError as e:
                return e.code, json.loads(e.read())
            except OSError:
                time.sleep(0.1)
        raise AssertionError("the web app did not start")
    code, r = post({})
    assert code == 200 and r["title"] == f"{p.name}: test" and r["body"] and "slack" not in r and sent == []
    code, r = post({"slack": True})
    assert code == 200 and r["slack"] == {"ok": False, "text": "Slack test failed: auth.test: invalid_auth"} and sent == [p.name]
    assert post({}, token="wrong")[0] == 401


def test_web_app_notification_state_permission_and_announce(env):
    """The browser's state is told apart (on, ask, blocked, insecure address, unsupported), permission
    is asked only from a click, and each new ask, problem or task ready for review notifies once,
    the first one after a quiet page load too, also from a hidden tab, and stays until closed."""
    import shutil
    import subprocess
    web = RUNTIME / "ttp" / "web"
    js, html = (web / "app.js").read_text(), (web / "index.html").read_text()
    for el in ("notifycard", "notifystate", "notifyask", "notifytest", "notifyresult", "slackline", "notify"):
        assert f'id="{el}"' in html, el
    # A permission request no click started is ignored by browsers: only the buttons' handlers ask.
    assert js.count("requestPermission") == 1 and js.count("allowNotify") == 3
    assert "$(\"#notifyask\").onclick = allowNotify" in js and "return allowNotify()" in js
    # A hidden tab keeps looking by elapsed time (30 s), and looks at once when shown.
    assert "Date.now() - lastLook >= 30000" in js and "ticks" not in js
    assert 'addEventListener("visibilitychange"' in js
    node = shutil.which("node")
    if not node:
        return
    block = js[js.index("// Browser notifications while this page"):js.index('document.addEventListener("visibilitychange"')]
    stub = """
const els = {}; const $ = (s) => els[s] || (els[s] = { hidden: false, html: "", onclick: null, scrollIntoView() {} });
const text = (el, s) => { el.html = s; }, put = text, codes = (s) => s, at = (t) => String(t), api = async () => ({});
const store = {}; const sessionStorage = { getItem: (k) => k in store ? store[k] : null, setItem: (k, v) => { store[k] = String(v); } };
const shown = [], sticky = []; class Notification { constructor(t, o) { shown.push(t + " | " + o.body); sticky.push(o.requireInteraction); } }
Notification.permission = "granted";
const document = { hidden: true, querySelector: () => ({ click() {} }) };
const window = { isSecureContext: true, Notification, focus() {} };
"""
    run = """
const out = {};
out.states = [notifyState(window), notifyState({ isSecureContext: true, Notification: { permission: "denied" } }),
  notifyState({ isSecureContext: true, Notification: { permission: "default" } }),
  notifyState({ isSecureContext: false, Notification: { permission: "denied" } }), notifyState({ isSecureContext: true })];
const st = (attention, tasks) => ({ project: { name: "demo" }, attention, tasks });
announce(st([], [{ id: 4, status: "running", title: "t4" }]));                        // first look: quiet page
out.first = shown.length;
announce(st([{ id: 10, kind: "alert", text: "disk low" }], [{ id: 4, status: "review", title: "t4" }]));
announce(st([{ id: 10, kind: "alert", text: "disk low" }, { id: 11, kind: "ask", text: "merge?" }], [{ id: 4, status: "review", title: "t4" }]));
out.shown = shown.slice(); out.sticky = sticky.slice(); out.unseen = unseen;
Notification.permission = "denied"; notifyCard(); out.blocked = [els["#notifystate"].html, els["#notify"].hidden, els["#notifyask"].hidden];
Notification.permission = "default"; notifyCard(); out.ask = [els["#notify"].hidden, els["#notifyask"].hidden];
console.log(JSON.stringify(out));
"""
    r = subprocess.run([node, "-e", stub + block + run], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout)
    assert out["states"] == ["on", "blocked", "ask", "insecure", "unsupported"]
    assert out["first"] == 0
    assert out["shown"] == ["demo: problem | disk low", "demo: ready for review | #4 t4", "demo: needs you | merge?"]
    assert out["unseen"] == 3
    assert out["sticky"] == [True, True, True]          # each stays until the user closes it
    assert "blocked by this browser" in out["blocked"][0] and "address bar" in out["blocked"][0]
    assert out["blocked"][1:] == [False, True] and out["ask"] == [False, False]


def test_a_desktop_preview_of_a_long_message_says_where_the_full_text_is():
    from ttp import notifier
    assert notifier.preview("short alert") == "short alert"
    long = notifier.preview("word " * 200)
    assert len(long) <= notifier.PREVIEW and long.endswith("(full text in the chat and web)")
    js = (RUNTIME / "ttp" / "web" / "app.js").read_text()
    assert 'preview(body, 240, "full text in the web chat")' in js and ".slice(0, 240)" not in js


def _relay_chat(p, cid="c1", label="relay-a", created_ago=0.0, last_read=0):
    p.db.x("INSERT INTO chats(id,created,label,last_active,last_read) VALUES(?,?,?,?,?)",
           (cid, time.time() - created_ago, label, time.time() - created_ago, last_read))


def _relay_line_clean(line):
    assert "?" not in line and "`" not in line and "ttp listen" not in line, line


def test_an_ask_the_web_apps_visible_tab_showed_reached_the_user(env):
    from ttp.web import health, web_seen
    p = make(env)
    _relay_chat(p)
    problems, ask = _ask(p, "Option A or B for the account?", blocking="access")
    assert problems == []
    assert health(p, p.db, now=ask["ts"] + 3600)["undelivered"]["asks"] == 1
    web_seen(p.db, ask["id"] - 1)
    assert health(p, p.db, now=ask["ts"] + 3600)["undelivered"]["asks"] == 1, "the tab had not shown the ask yet"
    web_seen(p.db, ask["id"] + 10**6)   # never past the newest message
    assert p.db.one("SELECT last_read FROM chats WHERE id='web'")["last_read"] == p.db.one(
        "SELECT MAX(id) m FROM messages")["m"]
    assert health(p, p.db, now=ask["ts"] + 3600)["undelivered"] is None
    web_seen(p.db, 0)
    assert health(p, p.db, now=ask["ts"] + 3600)["undelivered"] is None, "the web cursor went backwards"
    app = (Path(__file__).resolve().parents[1] / "plugins/tt-project/runtime/ttp/web/app.js").read_text()
    assert "/api/state?visible=1&seen=${lastMsg}" in app and "document.hidden ?" in app


def test_the_relay_warning_counts_normal_alerts_and_asks_no_question(env):
    from ttp.cli import status_text
    from ttp.web import health
    p = make(env)
    _relay_chat(p)
    top = p.db.one("SELECT COALESCE(MAX(id),0) m FROM messages")["m"]
    p.db.x("UPDATE chats SET last_read=? WHERE id='c1'", (top,))
    alert = p.db.post("out", "a service restarted twice", kind="alert", severity="normal")
    p.db.post("out", "a status line", kind="alert", severity="low")
    p.db.x("UPDATE messages SET ts=? WHERE id>?", (time.time() - 3600, top))
    u = health(p, p.db)["undelivered"]
    assert (u["asks"], u["alerts"]) == (0, 1), "a normal alert no reader got was not counted, or a low one was"
    assert u["line"].startswith("0 question(s) and 1 alert(s) reached no chat or web view since ")
    assert "; chat relay-a last read " in u["line"] and u["line"].endswith(", no listener running")
    _relay_line_clean(u["line"])
    out = status_text(p)
    (line,) = [ln for ln in out.splitlines() if ln.startswith("chat relay:")]
    _relay_line_clean(line)
    p.db.x("UPDATE chats SET last_read=? WHERE id='c1'", (alert,))
    assert health(p, p.db)["undelivered"] is None


def test_a_chat_with_no_listener_silent_a_day_leaves_the_readers_with_one_status_line(env):
    from ttp import alerts, chatgone
    from ttp.daemon import Daemon
    from ttp.web import health
    p = make(env)
    _relay_chat(p, created_ago=30 * 3600)
    (p.state / "listen-c1.pid").write_text("999999999")   # a listener that is gone
    problems, ask = _ask(p, "Option A or B for the account?", blocking="access")
    assert problems == []
    p.db.x("UPDATE messages SET ts=? WHERE id=?", (time.time() - 25 * 3600, ask["id"]))
    (c,) = chatgone.chats(p, p.db, p.config(), time.time())
    assert c["gone"] and not c["listener"]
    u = health(p, p.db)["undelivered"]
    assert u["asks"] == 1 and "chat relay-a last read" in u["line"] and "no listener running" in u["line"]
    d = Daemon(p.base)
    for _ in range(3):
        d.check_chats()
    (item,) = p.db.q("SELECT * FROM messages WHERE ref='chat-silent:c1'")
    assert item["severity"] == "low" and item["kind"] == "alert" and item["chat"] is None
    assert item["text"].startswith("chat relay-a has not read since ") and "(message #0)" in item["text"]
    assert "asks and alerts reach the web app and desktop alerts" in item["text"]
    _relay_line_clean(item["text"])
    assert not alerts.sweep(p.db)
    assert [m["state"] for m in alerts.feed(p.db, time.time()) if m["id"] == item["id"]] == ["status"]
    # Its next read clears the line, and the chat counts as a reader again.
    p.db.x("UPDATE chats SET last_read=? WHERE id='c1'", (ask["id"],))
    (ep,) = alerts.sweep(p.db)
    assert ep["key"] == "chat-silent:c1"
    assert [m["state"] for m in alerts.feed(p.db, time.time()) if m["id"] == item["id"]] == ["cleared"]
    assert health(p, p.db)["undelivered"] is None
    d.check_chats()
    assert len(p.db.q("SELECT id FROM messages WHERE ref='chat-silent:c1'")) == 1
    assert not p.db.kv(chatgone.KV)


def test_a_chat_with_a_live_listener_stays_a_reader_however_long_it_is_silent(env):
    from ttp import chatgone
    from ttp.daemon import Daemon
    from ttp.web import health
    p = make(env)
    _relay_chat(p, created_ago=30 * 3600)
    (p.state / "listen-c1.pid").write_text(str(os.getpid()))
    problems, ask = _ask(p, "Option A or B for the account?", blocking="access")
    p.db.x("UPDATE messages SET ts=? WHERE id=?", (time.time() - 25 * 3600, ask["id"]))
    (c,) = chatgone.chats(p, p.db, p.config(), time.time())
    assert c["listener"] and not c["gone"]
    Daemon(p.base).check_chats()
    assert not p.db.q("SELECT id FROM messages WHERE ref='chat-silent:c1'")
    assert health(p, p.db)["undelivered"]["line"].endswith(", listener running")
    p.db.x("UPDATE chats SET last_read=? WHERE id='c1'", (ask["id"],))
    assert health(p, p.db)["undelivered"] is None, "a live chat that read the ask still counts as its reader"



@needs_sockets
def test_only_a_visible_web_poll_with_a_plain_id_moves_the_web_read_cursor(env):
    from ttp import web
    p = make(env)
    port = _start_web(p)
    top = p.db.one("SELECT MAX(id) m FROM messages")["m"]

    def poll(query):
        req = urllib.request.Request(f"http://127.0.0.1:{port}/api/state{query}", headers={"X-TTP-Token": web.token(p)})
        assert urllib.request.urlopen(req, timeout=5).status == 200, query
        row = p.db.one("SELECT last_read FROM chats WHERE id='web'")
        return row and row["last_read"]

    for query in ("", f"?seen={top}", f"?visible=0&seen={top}", "?visible=1&seen=x", "?visible=1&seen=-1",
                  "?visible=1"):
        assert not poll(query), f"{query!r} moved the web cursor"
    assert poll(f"?visible=1&seen={top}") == top


def test_gone_after_zero_keeps_every_chat_a_reader(env):
    from ttp import chatgone
    from ttp.daemon import Daemon
    p = make(env)
    _relay_chat(p, created_ago=30 * 3600)
    problems, ask = _ask(p, "Option A or B for the account?", blocking="access")
    p.db.x("UPDATE messages SET ts=? WHERE id=?", (time.time() - 25 * 3600, ask["id"]))
    assert chatgone.chats(p, p.db, p.config(), time.time())[0]["gone"]
    p.set_config("relay.gone_after_s", 0)
    assert not chatgone.chats(p, p.db, p.config(), time.time())[0]["gone"]
    Daemon(p.base).check_chats()
    assert not p.db.q("SELECT id FROM messages WHERE ref='chat-silent:c1'")


def test_web_app_need_you_pill_counts_only_what_needs_the_user():
    """The header pill matches the 'Needs you now' card: open asks plus blocked tasks that wait on
    the user, plus gated tasks an open ask does not already count. A project-owned blocked task or
    an ask naming its gated task is not counted twice."""
    import shutil, subprocess
    js = (RUNTIME / "ttp" / "web" / "app.js").read_text()
    node = shutil.which("node")
    if not node:
        return
    block = js[js.index("const stuck ="):js.index("function board")]
    run = """
const out = {};
const st = (tasks, attention, gate) => ({ attention, tasks: tasks.map((t) => Object.assign(t, gate ? { gate: gate } : {})) });
const ask = (text, id) => ({ kind: "ask", id, text, ts: 0 });
const blocked = (kind) => ({ id: 1, status: "blocked", wait: { kind, text: "you" } });
// 1 ask + 1 project-owned blocked task (waits on the coordinator, not the user) -> 1
out.one = needsYou(st([blocked("coordinator")], [ask("merge?", 1)]));
// 1 ask + 1 user-wait task -> 2
out.two = needsYou(st([blocked("user")], [ask("approve?", 2)]));
// nothing -> 0 (the pill hides)
out.none = needsYou({ attention: [], tasks: [] });
// a gated task with no open ask naming it, with an attention alert (not an ask): alert + gated task
out.gated = needsYou(st([{ id: 3, status: "queued" }], [{ kind: "alert", id: 5, text: "disk" }], { ask: true }));
// an open ask naming the gated task as #3: the ask counts once, the task is not double-counted
out.dedup = needsYou(st([{ id: 3, status: "queued" }], [ask("approve #3?", 4)], { ask: true }));
console.log(JSON.stringify(out));
"""
    r = subprocess.run([node, "-e", block + run], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout)
    # The alert attention (1) plus the gated task (1): the gated task counts when no ask names it.
    assert out == {"one": 1, "two": 2, "none": 0, "gated": 2, "dedup": 1}
    # The pill uses needsYou; the old count of every blocked task plus attention is gone.
    assert "const needs = needsYou(st)" in js and "filter((t) => t.status === \"blocked\").length" not in js


def test_a_say_without_a_chat_replies_to_one_chat_per_caller_that_counts_as_a_reader(env, monkeypatch, capsys):
    from ttp import cli
    from ttp import coordinator as coord
    from ttp.web import health
    p = make(env)
    monkeypatch.setattr(cli, "here", lambda: None)
    monkeypatch.setenv("TTP_CLIENT_HOST", "relay-host")

    def said(*argv):
        capsys.readouterr()
        cli.main(["say", "demo", *argv])
        return (p.db.one("SELECT id, chat FROM messages WHERE direction='in' ORDER BY id DESC LIMIT 1"),
                capsys.readouterr().out)

    m1, out = said("--", "first")
    m2, _ = said("--context", "relayed", "--", "second")
    chat = m1["chat"]
    assert chat and chat.startswith(cli.SAY_CHAT_PREFIX) and m2["chat"] == chat, "one chat per caller, not per call"
    assert f"ttp listen demo --chat {chat} --once" in out
    row = p.db.one("SELECT label, last_read FROM chats WHERE id=?", (chat,))
    assert row["label"] == "ttp say from relay-host" and row["last_read"] < m1["id"], "the first message's reply is unread"
    assert p.db.one("SELECT COUNT(*) n FROM chats WHERE id LIKE 'say-%'")["n"] == 1
    # Another caller (an explicit --caller, or another project) gets its own chat.
    m3, _ = said("--caller", "project other", "--", "third")
    assert m3["chat"] not in (None, chat)
    monkeypatch.setattr(cli, "here", lambda: type("P", (), {"name": "other"})())
    m4, _ = said("--", "fourth")
    assert m4["chat"] == m3["chat"], "a calling project names its chat"
    # --chat behaves as before: no new chat, the given one, the old confirmation.
    m5, out = said("--chat", "c9", "--", "fifth")
    assert m5["chat"] == "c9" and "ttp listen" not in out and "answers in this chat" in out
    assert p.db.one("SELECT COUNT(*) n FROM chats")["n"] == 2
    # The coordinator's reply to the message goes to that chat (the turn's default chat is the message's).
    monkeypatch.setattr(cli, "here", lambda: None)
    m6, _ = said("--", "sixth")
    assert coord.apply(p, [{"type": "reply", "text": "done: the fix landed"}], default_chat=m6["chat"]) == []
    reply = p.db.one("SELECT id, chat FROM messages WHERE kind='reply' ORDER BY id DESC LIMIT 1")
    assert reply["chat"] == chat
    # Unread for an hour, the reply reached nobody; once the chat reads past it, it did.
    p.db.x("UPDATE messages SET ts=? WHERE id=?", (time.time() - 3600, reply["id"]))
    u = health(p, p.db)["undelivered"]
    assert (u["asks"], u["alerts"]) == (0, 1), u
    p.db.x("UPDATE chats SET last_read=? WHERE id=?", (reply["id"], chat))
    assert health(p, p.db)["undelivered"] is None
    # A reply to an ordinary chat is still not counted, and the say chat reads broadcasts too.
    p.db.post("out", "for c9", chat="c9", kind="reply")
    alert = p.db.post("out", "a service restarted twice", kind="alert", severity="normal")
    p.db.x("UPDATE messages SET ts=? WHERE id>?", (time.time() - 3600, reply["id"]))
    assert health(p, p.db)["undelivered"]["alerts"] == 1
    p.db.x("UPDATE chats SET last_read=? WHERE id=?", (alert, chat))
    assert health(p, p.db)["undelivered"] is None


def test_a_new_say_chat_has_not_read_older_broadcasts_until_its_listen_reads_past_them(env, monkeypatch, capsys):
    """Making a say chat puts its cursor at the newest message: that is no read. An older unread
    broadcast alert stays counted until that chat's `ttp listen` reads past it."""
    from ttp import chatgone, cli
    from ttp.web import health
    p = make(env)
    _relay_chat(p)
    alert = p.db.post("out", "a service restarted twice", kind="alert", severity="normal")
    p.db.x("UPDATE messages SET ts=? WHERE id=?", (time.time() - 3600, alert))
    assert health(p, p.db)["undelivered"]["alerts"] == 1
    monkeypatch.setattr(cli, "here", lambda: None)
    cli.main(["say", "demo", "--", "hello"])
    chat = p.db.one("SELECT chat FROM messages WHERE direction='in' ORDER BY id DESC LIMIT 1")["chat"]
    assert chat.startswith(cli.SAY_CHAT_PREFIX) and chatgone.read_from(p.db, chat) >= alert
    assert health(p, p.db)["undelivered"]["alerts"] == 1, "making the chat read nothing"
    cli.main(["say", "demo", "--", "again"])   # reusing it keeps its creation cursor
    assert health(p, p.db)["undelivered"]["alerts"] == 1
    # An alert after the chat was made counts as read once its listen reads past it.
    later = p.db.post("out", "a disk filled up", kind="alert", severity="normal")
    p.db.x("UPDATE messages SET ts=? WHERE id=?", (time.time() - 3600, later))
    assert health(p, p.db)["undelivered"]["alerts"] == 2
    capsys.readouterr()
    cli.main(["listen", "demo", "--chat", chat, "--once"])
    assert p.db.one("SELECT last_read FROM chats WHERE id=?", (chat,))["last_read"] >= later
    assert health(p, p.db)["undelivered"]["alerts"] == 1, "only the alert from before the chat stays counted"
    p.db.x("UPDATE chats SET last_read=? WHERE id='c1'", (alert,))
    assert health(p, p.db)["undelivered"] is None


def test_a_remote_say_sends_the_callers_label_and_drops_it_for_an_older_runtime(env, monkeypatch):
    from ttp import cli
    sent = []

    def fake(entry, argv, capture=False):
        sent.append(list(argv))
        unknown = any(x.startswith("--caller") for x in argv) and len(sent) == 1
        return subprocess.CompletedProcess(argv, 2 if unknown else 0, "", "unrecognized arguments: --caller x"
                                           if unknown else "")
    monkeypatch.setattr(cli, "_ssh", fake)
    monkeypatch.setattr(cli, "flush_outbox", lambda name, entry: None)
    monkeypatch.setattr(cli.outbox, "entries", lambda name: [])
    rc, _ = cli._say_remote("demo", {"host": "far", "dir": "/x"}, "hi", None, caller="project other")
    assert rc == 0 and "--caller" in sent[0] and sent[-1] == ["say", "--client-id=" + sent[-1][1][12:], "demo",
                                                              "--", "hi"]
    sent.clear()
    cli._say_remote("demo", {"host": "far", "dir": "/x"}, "hi", "c1", caller="project other")
    assert "--caller" not in sent[0], "a chat given: no caller label"
