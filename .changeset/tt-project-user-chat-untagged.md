---
"tt-project": patch
---

`tt-project`: messages to the user's own chats never end with the AI-authorship tag. Every outbound message (coordinator replies, notifies, asks, a worker's chat reply, alerts; every provider) drops a trailing `delivery.ai_tag`, the default `[authored by AI]` or a similar disclosure, model-free when it is recorded, and a stored hand-off's summary, message and question (and its `on_pass` ones) drop it too. Where the harness adds its own text after a model's (an ask's recommendation and timer note, a blocked or failed reply's "Needs from you", a sign-in step's command and deadline, a step updated in place), each model-written part drops its tag first. A tag inside the text stays. Posts to other people (gh, connector and curl guards) are unchanged and still need it. The chat-reply prompt block says the chat is the user's own.
