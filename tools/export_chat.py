"""Turn Claude Code session files into one readable Markdown chat log.

    python3 tools/export_chat.py AI_CHAT_LOG.md SESSION1.jsonl [SESSION2.jsonl ...] [--exclude TEXT ...]

--append-from TEXT appends one exchange to the existing log instead of rewriting it: the first user message that
starts with TEXT (case-insensitive) and everything Claude said until the next user message,
after the last block already in the file.

--exclude TEXT leaves out every long user message (over 400 characters) that contains TEXT (case-insensitive),
for a pasted message the user asks to keep out of the log; TEXT is scrubbed from everything else.

The session files live under ~/.claude/projects/<project>/. Only the user's and Claude's own words are
kept, nothing else (no tool calls, no notes); the Windows user name in file paths is replaced with <user>.
"""
import json, re, sys

args = sys.argv[1:]
VALUED = ("--exclude", "--append-from")
exclude = [args[i + 1].lower() for i, a in enumerate(args) if a == "--exclude"]
append_from = next((args[i + 1].lower() for i, a in enumerate(args) if a == "--append-from"), None)
args = [a for i, a in enumerate(args) if a not in VALUED and (i == 0 or args[i - 1] not in VALUED)]
out_path, files = args[0], args[1:]
left_out = 0
REMINDER = re.compile(r"<system-reminder>.*?</system-reminder>", re.S)
seen, rows = set(), []


PASTE_OPEN = re.compile(r"<pasted_content[^>]*>\n?")
PASTE_CLOSE = re.compile(r"\n?</pasted_content[^>]*>")


def clean(text):
    # Text the user pasted in keeps its exact content; the app's wrapper tags become a plain-text block.
    text = PASTE_CLOSE.sub("\n```", PASTE_OPEN.sub("```text\n", text))
    return REMINDER.sub("", text).strip()


def redact(text):
    text = re.sub(r"(Users[\\/]+)[^\\/\s<]+", r"\g<1><user>", text)
    for x in exclude:                     # also scrub the phrase from anything that mentions the left-out message
        text = re.sub(r"(the )?" + re.escape(x) + r"( text| message| prompt)?", "the left-out message", text, flags=re.I)
        for word in x.split():            # and any of its words on their own
            if len(word) > 3:
                text = re.sub(r"\b" + re.escape(word) + r"\b", "[left out]", text, flags=re.I)
    return text


for f in files:
    for line in open(f, encoding="utf-8"):
        try:
            o = json.loads(line)
        except ValueError:
            continue
        if o.get("type") not in ("user", "assistant") or o.get("isSidechain") or o.get("isMeta"):
            continue
        if o.get("uuid") in seen:
            continue
        seen.add(o.get("uuid"))
        ts = (o.get("timestamp") or "")[:19].replace("T", " ")
        content = (o.get("message") or {}).get("content")
        if o.get("isCompactSummary"):
            continue                       # the harness's own summary, not part of the conversation
        blocks = [{"type": "text", "text": content}] if isinstance(content, str) else (content or [])
        texts, tools = [], []
        for b in blocks:
            if not isinstance(b, dict):
                continue
            if b.get("type") == "text":
                t = clean(b.get("text", ""))
                if t:
                    texts.append(t)
            elif b.get("type") == "tool_use":
                i = b.get("input") or {}
                what = i.get("description") or i.get("file_path") or i.get("pattern") or i.get("name") or i.get("recap") or ""
                tools.append(f"{b.get('name', '').split('__')[-1]}" + (f": {str(what)[:140]}" if what else ""))
        role = o["type"]
        # Drop the pasted message itself (long). A short message that only mentions it, such as the request
        # to leave it out, stays; the phrase in it is scrubbed by redact() like everywhere else.
        if role == "user" and exclude and any(x in t.lower() and len(t) > 400 for t in texts for x in exclude):
            left_out += 1
            continue
        if texts:                          # only what was said; tool calls are not part of the conversation
            rows.append((ts, role, "\n\n".join(texts)))

rows.sort(key=lambda r: r[0])
merged = []
for ts, role, text in rows:                      # fold consecutive tool-call notes into one block
    if merged and role == "tools" and merged[-1][1] == "tools":
        merged[-1] = (merged[-1][0], "tools", merged[-1][2] + "\n" + text)
    else:
        merged.append((ts, role, text))

def render(blocks):
    label = {"user": "User", "assistant": "Claude", "tools": "Actions", "note": "Note"}
    out = []
    for ts, role, text in blocks:
        if role == "tools":
            out.append(f"<details><summary>Actions ({text.count(chr(10)) + 1})</summary>\n\n{text}\n\n</details>\n")
        elif role == "note":
            out.append(text + "\n")
        else:
            out.append(f"## {label[role]} · {ts}\n\n{text}\n")
    return "\n".join(out)


if append_from:
    start = next(i for i, r in enumerate(merged) if r[1] == "user" and r[2].lower().startswith(append_from))
    end = next((i for i in range(start + 1, len(merged)) if merged[i][1] == "user"), len(merged))
    pick = merged[start:end]
    with open(out_path, "a", encoding="utf-8", newline="\n") as f:
        f.write(redact("\n" + render(pick)))
    print("appended", len(pick), "blocks:", sum(r[1] == "user" for r in pick), "user,",
          sum(r[1] == "assistant" for r in pick), "assistant")
    sys.exit(0)

n_user = sum(r[1] == "user" for r in merged)
n_ai = sum(r[1] == "assistant" for r in merged)
head = ("# AI chat log\n\nThe conversation between the user and Claude Code while building this service: the user's"
        " messages and Claude's replies, word for word, in order. Times are UTC.\n")
open(out_path, "w", encoding="utf-8", newline="\n").write(redact(head + "\n" + render(merged)))
print(len(merged), "blocks;", n_user, "user;", n_ai, "assistant")
