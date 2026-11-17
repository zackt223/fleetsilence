"""Turn Claude Code session files into one readable Markdown chat log.

    python3 tools/export_chat.py AI_CHAT_LOG.md SESSION1.jsonl [SESSION2.jsonl ...]

The session files live under ~/.claude/projects/<project>/. Only the user's and Claude's own words are
kept, plus one line per tool call; the Windows user name in file paths is replaced with <user>.
"""
import json, re, sys

out_path, files = sys.argv[1], sys.argv[2:]
REMINDER = re.compile(r"<system-reminder>.*?</system-reminder>", re.S)
seen, rows = set(), []


def clean(text):
    return REMINDER.sub("", text).strip()


def redact(text):
    return re.sub(r"(Users[\\/]+)[^\\/\s<]+", r"\g<1><user>", text)


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
            rows.append((ts, "note", "_The conversation was compacted here; the harness replaced earlier turns with an"
                                     " automatic summary (not reproduced). The turns above are the originals._"))
            continue
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
        if texts:
            rows.append((ts, role, "\n\n".join(texts)))
        if tools and role == "assistant":
            rows.append((ts, "tools", "\n".join(f"- {t}" for t in tools)))

rows.sort(key=lambda r: r[0])
merged = []
for ts, role, text in rows:                      # fold consecutive tool-call notes into one block
    if merged and role == "tools" and merged[-1][1] == "tools":
        merged[-1] = (merged[-1][0], "tools", merged[-1][2] + "\n" + text)
    else:
        merged.append((ts, role, text))

n_user = sum(r[1] == "user" for r in merged)
n_ai = sum(r[1] == "assistant" for r in merged)
head = f"""# AI chat log

Every message between the user and Claude Code (model: Claude Fable 5.2) in the session that built this
service, in order, exported from the session's saved transcript. Times are UTC.

- **User** blocks are the user's messages word for word. The first one is the task brief, which was handed
  to this session by the user's planning session rather than typed here.
- **Claude** blocks are Claude's replies word for word.
- **Actions** blocks list the tools Claude ran between replies (one line each). Tool output, file contents
  and the harness's automatic reminders are left out.

{n_user} user messages, {n_ai} Claude replies, {merged[0][0]} to {merged[-1][0]}. This is a snapshot: anything
said after the last entry is not in it. Re-run `tools/export_chat.py` to refresh.

---
"""
label = {"user": "User", "assistant": "Claude", "tools": "Actions", "note": "Note"}
body = []
for ts, role, text in merged:
    if role == "tools":
        body.append(f"<details><summary>Actions ({text.count(chr(10)) + 1})</summary>\n\n{text}\n\n</details>\n")
    elif role == "note":
        body.append(text + "\n")
    else:
        body.append(f"## {label[role]} · {ts}\n\n{text}\n")
open(out_path, "w", encoding="utf-8", newline="\n").write(head + "\n" + "\n".join(body))
print(len(merged), "blocks;", n_user, "user;", n_ai, "assistant")
