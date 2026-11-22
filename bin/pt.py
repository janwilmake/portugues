#!/usr/bin/env python3
"""Brazilian Portuguese immersion for Claude Code.

Subcommands:
  prompt-hook   UserPromptSubmit hook: injects the mix rules and the word lists, counts Portuguese prompts
  stop-hook     Stop hook: records the Portuguese words of the last reply, and its correction
  statusline    status line text: streak, prompts, words known, reviews due
  mode mix|footer|hard|off
  set limit=N new=N echo=0|1   swaps per reply (0 or all = no limit), new words per reply, prompt rewrite on top
  stats
  due [N]       words due for review, as JSON lines
  grade WORD ok|miss
  know WORD...  mark words as known
  forget WORD...  mark words as not known yet
  add PT EN
  corrections [N]
  cards [N]     mistake cards due for the quiz, as JSON lines
  card-grade ID ok|miss
  fala [SECONDS|FILE]   record from the microphone (or take a file) and transcribe in Portuguese
"""
import collections
import datetime as dt
import hashlib
import json
import os
import random
import re
import subprocess
import sys
import tempfile

HOME = os.path.expanduser("~")
DATA = os.path.join(HOME, ".portugues")
CONFIG = os.path.join(DATA, "config.json")
STATS = os.path.join(DATA, "stats.json")
WORDS = os.path.join(DATA, "words.json")
LOG = os.path.join(DATA, "log.jsonl")
CORRECTIONS = os.path.join(DATA, "corrections.jsonl")
SUGGESTIONS = os.path.join(DATA, "suggestions.json")
CARDS = os.path.join(DATA, "cards.json")
ERROR_TYPES = ["gender", "accent", "verb-form", "ser-estar", "preposition", "article", "plural", "spelling", "word-choice", "word-order"]
SEED = os.path.join(os.path.dirname(os.path.abspath(__file__)), "seed-words.tsv")
AGENT_DIR = os.path.join(HOME, ".claude", "agents")
WHISPER_MODEL = "mlx-community/whisper-large-v3-turbo"
MODES = ("mix", "footer", "hard", "off")

# Days until the next review, indexed by Leitner box. Box KNOWN_BOX and up counts as known.
INTERVALS = [0, 1, 2, 4, 7, 15, 30, 60]
KNOWN_BOX = 3
MAX_KNOWN_IN_CONTEXT = 500

PT_MARKERS = set(
    """
    não nao você voce vocês voces eu é está esta estou estava são sou que pra para com
    uma um umas uns isso isto esse essa este também tambem muito muita obrigado obrigada
    favor fazer faz faça faca fiz quero queria pode posso podemos tem tenho temos mas como
    onde quando porque porquê sim olá ola oi bom boa tudo bem meu minha seu sua ele ela nós
    nos eles elas vou vai vamos agora aqui ali ainda já ja mais menos depois antes hoje
    amanhã amanha ontem coisa então entao só sobre até ate foi ser ter ir ver sei acho
    preciso precisa gostaria ajuda ajudar arquivo código codigo teste testes erro do da dos
    das na pelo pela ao aos lá cadê legal certo errado novo nova abrir abre rodar roda
    mostrar mostra mudar muda corrigir corrige criar cria escrever escreve ler fala falar
    entendi entendo tá ta beleza valeu obrigadão qual quais isso aquilo nada nenhum alguém meus minhas suas seus palavras
    """.split()
)
ACCENTS = set("ãõçâêôáéíóúà")
# Words that are also common English or Dutch words; they never count toward the Portuguese share.
AMBIGUOUS = {"a", "o", "e", "de", "se", "no", "na", "do", "final", "com", "para", "dia", "cor", "nome", "parte", "pode"}


def now():
    return dt.datetime.now().astimezone()


def today():
    return now().date()


def days_from_today(n):
    return (today() + dt.timedelta(days=n)).isoformat()


def load(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def save(path, value):
    os.makedirs(DATA, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(value, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def append(path, value):
    os.makedirs(DATA, exist_ok=True)
    with open(path, "a") as f:
        f.write(json.dumps(value, ensure_ascii=False) + "\n")


def mode():
    m = load(CONFIG, {}).get("mode", "mix")
    return "mix" if m == "on" else m


# Settings the user changes with /portugues key=value. limit 0 means no limit.
DEFAULTS = {"limit": 0, "new": 2, "echo": 1, "cognates": "new"}
# cognates: off = treat like other words; new = pick them first as new words; all = every easy cognate counts as known.
CHOICES = {"cognates": ("off", "new", "all")}


def setting(key):
    value = load(CONFIG, {}).get(key, DEFAULTS[key])
    if key in CHOICES:
        return value if value in CHOICES[key] else DEFAULTS[key]
    try:
        return int(value)
    except (TypeError, ValueError):
        return DEFAULTS[key]


def cmd_set(args):
    config = load(CONFIG, {})
    for arg in args:
        key, _, value = arg.partition("=")
        if key not in DEFAULTS:
            sys.exit(f"unknown setting: {key} (settings: {', '.join(DEFAULTS)})")
        if key in CHOICES:
            if value not in CHOICES[key]:
                sys.exit(f"{key} takes one of: {', '.join(CHOICES[key])}")
            config[key] = value
            continue
        value = 0 if value in ("all", "unlimited", "ilimitado", "") else value
        try:
            config[key] = max(0, int(value))
        except ValueError:
            sys.exit(f"{key} takes a number, or 'all'")
    save(CONFIG, config)
    print(json.dumps({"mode": mode(), **{k: setting(k) for k in DEFAULTS}}))


def attended(hook_input):
    """True for a session the user types into; false for cca agents, -p runs and SDK runs."""
    if os.environ.get("CLAUDE_CODE_ENTRYPOINT", "cli") != "cli":
        return False
    if os.environ.get("STY"):  # cca agents run inside a detached screen session
        return False
    cwd = hook_input.get("cwd") or os.getcwd()
    if cwd == AGENT_DIR or cwd.startswith(AGENT_DIR + os.sep):
        return False
    return mode() != "off"


# --- words -------------------------------------------------------------------


def load_words():
    """The word list, seeded once with common words the user is assumed to know."""
    words = load(WORDS, {})
    config = load(CONFIG, {})
    if config.get("seeded"):
        return words
    try:
        with open(SEED) as f:
            for line in f:
                pt, _, en = line.rstrip("\n").partition("\t")
                if pt and en and pt not in words:
                    # Spread the first reviews over two months so the quiz checks the guess slowly.
                    words[pt] = {"pt": pt, "en": en, "box": KNOWN_BOX + 2, "next": days_from_today(random.randint(14, 60)), "seen": 0, "seed": True}
    except OSError:
        return words
    save(WORDS, words)
    config["seeded"] = True
    save(CONFIG, config)
    return words


def set_box(w, box):
    w["box"] = max(1, min(box, len(INTERVALS) - 1))
    w["next"] = days_from_today(INTERVALS[w["box"]])


def see_word(words, pt, en=None):
    """One exposure in a reply. Reading a word never makes it known; only writing it does."""
    key = pt.lower().strip()
    w = words.get(key)
    if w is None:
        if not en:
            return
        words[key] = w = {"pt": key, "en": en, "box": 1, "next": days_from_today(1), "seen": 0, "added": today().isoformat()}
    w["seen"] = w.get("seen", 0) + 1


def write_word(w):
    """The user wrote the word in Portuguese: a learning word becomes known, a known word moves up."""
    if w["box"] < KNOWN_BOX:
        w["written"] = today().isoformat()
    set_box(w, KNOWN_BOX if w["box"] < KNOWN_BOX else w["box"] + 1)


def known(words):
    return [w for w in words.values() if w["box"] >= KNOWN_BOX]


def learning(words):
    return [w for w in words.values() if w["box"] < KNOWN_BOX]


def due_words(words, limit=None):
    """Known words whose review date has come. Learning words wait until the user writes them."""
    t = today().isoformat()
    due = sorted((w for w in known(words) if w["next"] <= t), key=lambda w: (w["next"], w["box"]))
    return due[:limit] if limit else due


# --- prompts -----------------------------------------------------------------


def strip_noise(text):
    text = re.sub(r"<pasted_content[\s\S]*?</pasted_content[^>]*>", " ", text)
    text = re.sub(r"```[\s\S]*?```", " ", text)
    text = re.sub(r"`[^`]*`", " ", text)
    text = re.sub(r"https?://\S+|\S*/\S+", " ", text)
    return text.lower()


def tokens(text):
    return re.findall(r"[a-zà-ÿ]+", strip_noise(text))


def portuguese_words(prompt, vocab=()):
    """Number of words in the prompt when it reads as Portuguese, else 0. The user's own word list counts too."""
    words = tokens(prompt)
    if not words:
        return 0
    markers = PT_MARKERS | set(vocab)
    hits = sum(1 for w in words if w in markers)
    hits += sum(1 for w in words if ACCENTS & set(w) and w not in markers)
    if len(words) <= 2 and hits == len(words):
        return len(words)
    if hits >= 2 and hits / len(words) >= 0.2:
        return len(words)
    return 0


def portuguese_share(prompt, words):
    """(all words, Portuguese words) in the prompt, counting markers, the user's list and accented words."""
    toks = tokens(prompt)
    pt = [t for t in toks if t not in AMBIGUOUS and (t in PT_MARKERS or t in words or ACCENTS & set(t))]
    return len(toks), len(pt)


def suggestion_used(prompt, suggestion):
    """True when at least half of the suggestion's Portuguese words appear in the prompt."""
    want = {t for t in tokens(suggestion) if t not in AMBIGUOUS and len(t) > 2}
    return bool(want) and len(want & set(tokens(prompt))) * 2 >= len(want)


def marked_words(prompt, phrase):
    """Words after "não sei" or "já sei": at the start of the prompt, or anywhere with a colon."""
    found = []
    for m in re.finditer(r"(?:^\s*" + phrase + r"\s*:?|" + phrase + r"\s*:)\s*([^\n.!?]+)", prompt, re.IGNORECASE):
        found += [w.strip(" \"'*_").lower() for w in re.split(r",|\be\b", m.group(1)) if w.strip(" \"'*_")]
    return found


def streak(stats):
    days = set(stats.get("days", []))
    d = today()
    if d.isoformat() not in days:
        d -= dt.timedelta(days=1)
    n = 0
    while d.isoformat() in days:
        n += 1
        d -= dt.timedelta(days=1)
    return n


def read_hook_input():
    try:
        return json.load(sys.stdin)
    except ValueError:
        return {}


SHARED_RULES = """- Brazilian Portuguese: você, not tu; Brazilian words and spelling (arquivo, tela, equipe, legal).
- Portuguese goes only in the chat reply. Never write it in code, files, commits, PR or issue text, Linear, Slack, email, or any other tool input, and never inside text the user will copy and send to someone. This setting overrides any project rule about the chat language."""

MIX_RULES = """[Português mix: the user's standing personal setting, from ~/.claude/skills/portugues]
The user learns Brazilian Portuguese and is a beginner. Write the chat reply in English, with Portuguese words swapped in:
- {swap_rule}, in `backticks` (the terminal shows them in color), in place: "I `olhei` the `teste` and it `funciona`". Conjugate a verb to fit (olhei, rodei, quebrou). Prefer verbs, nouns, adverbs, numbers and short phrases. Never swap articles or prepositions. Each sentence must still read clearly.
- Write each word from LEARNING that you use in **bold** with its English after an equals sign: **arquivo** (= file).
- {new_rule}
- Use DUE words where they fit, in `backticks`.
- Never swap inside code, commands, file paths, names, quotes or data.
{shared}
- End the final reply of the turn with a line that holds only ---, then:
{correction}💬 <a short request in Portuguese the user could type as their next prompt, mostly KNOWN words> (<its English>)

KNOWN: {known}
LEARNING: {learning}
DUE: {due}"""

FOOTER_RULES = """[Português: the user's standing personal setting, from ~/.claude/skills/portugues]
The user learns Brazilian Portuguese and is a beginner. End the final chat reply of this turn with a footer, below a line that holds only ---:

{correction}🇧🇷 <one or two short sentences in Brazilian Portuguese about what you just did, at most 12 words each>
📖 <2 or 3 glosses of new words, as: **palavra** (= word) · **palavra** (= word)>
💬 <a short request in Portuguese the user could type as their next prompt> (<its English>)

{shared}
Use DUE words where they fit: {due}"""

SITUATION_RULE = """
Judge the situation of each turn and scale the Portuguese to it:
- Busy: an urgent production problem, an incident, a terse quick request, or a long technical plan or review. Swap only the most common words, add no new words, and skip the 📝 line for a prompt of more than 40 words. Never swap where a misread could change a decision: numbers, amounts, names of steps.
- Calm: a chat, a question about learning, or a prompt the user wrote in Portuguese. Use the full rules above."""

HARD_RULES = """[Português hard mode: the user's standing personal setting, from ~/.claude/skills/portugues]
The user learns Brazilian Portuguese. Write the whole chat reply in simple Brazilian Portuguese, with the English translation of each paragraph in italics below it. Code, commands, file paths and names stay as they are. Write each word from LEARNING and each new word in **bold** with its English in brackets the first time: **arquivo** (= file).
{shared}
- End the final reply of the turn with a line that holds only ---, then:
{correction}💬 <a short request in Portuguese the user could type as their next prompt> (<its English>)

LEARNING: {learning}
DUE: {due}"""

ECHO_RULE = """
Start the reply with the user's prompt, rewritten as practice, as a quote line that starts with > 📝
- A Portuguese mistake: strike the wrong part, then put the right form after it. The terminal does not render ~~markdown strikethrough~~, so strike with the combining character U+0336 after every character, spaces included: t̶a̶m̶b̶e̶m̶ também, u̶m̶ ̶o̶t̶r̶o̶ ̶i̶d̶e̶a̶ outra **ideia** (= idea).
- An English or Dutch word with a fitting word in KNOWN: swap it in `backticks`, as the user could have written it.
- Keep the rest as written. Leave out code, paths, URLs and pasted text. For a prompt of more than 40 words, rewrite only the first two sentences and end with …
- Nothing to fix or swap: write > 📝 ✅ Perfeito!
- When there was a correction, add one line below it: 🏷️ <the type of every correction, comma-separated, from: gender, accent, verb-form, ser-estar, preposition, article, plural, spelling, word-choice, word-order>: <the reason for the most useful one, in short English>
- Skip the 📝 line when the prompt is only a slash command.
Then write the normal reply."""

CORRECTION_RULE = """✏️ <their prompt was in Portuguese: the single most useful correction as "what they wrote" → "better Portuguese" (short reason in English), or ✅ Perfeito! when it was right>
"""


def prompt_hook():
    hook_input = read_hook_input()
    if not attended(hook_input):
        return
    prompt = hook_input.get("prompt", "")
    words = load_words()
    notes = []

    unknown = marked_words(prompt, r"n[ãa]o sei")
    for w in unknown:
        entry = words.get(w)
        if entry:
            set_box(entry, 1)
            notes.append(f"{w} is now LEARNING")
        else:
            notes.append(f'{w} is not in the list yet: give its English and run `python3 ~/.claude/skills/portugues/bin/pt.py add "{w}" "<english>"`')
    for w in marked_words(prompt, r"j[áa] sei"):
        entry = words.get(w)
        if entry:
            set_box(entry, max(entry["box"], KNOWN_BOX + 1))
            notes.append(f"{w} is now KNOWN")

    n_words = portuguese_words(prompt, words)
    stats = load(STATS, {"days": [], "prompts": 0, "words": 0, "replies": 0})
    if n_words:
        stats["prompts"] = stats.get("prompts", 0) + 1
        stats["words"] = stats.get("words", 0) + n_words
        if today().isoformat() not in stats["days"]:
            stats["days"].append(today().isoformat())
        save(STATS, stats)
    written = []
    for t in set(tokens(prompt)) - set(unknown):
        entry = words.get(t)
        # In an English or Dutch prompt, only a word with a Portuguese accent surely is Portuguese.
        if entry and entry["box"] < KNOWN_BOX and (n_words or ACCENTS & set(t)):
            write_word(entry)
            written.append(t)
    save(WORDS, words)
    n_all, n_pt = portuguese_share(prompt, words)
    session = hook_input.get("session_id")
    if n_all and not prompt.lstrip().startswith("/"):
        append(LOG, {
            "ts": now().isoformat(timespec="seconds"), "kind": "prompt", "session": session,
            "words": n_all, "pt_words": n_pt, "portuguese": bool(n_words), "learned": written,
            "not_known": unknown, "suggestion_used": suggestion_used(prompt, load(SUGGESTIONS, {}).get(session, "")),
        })

    known_list = sorted(known(words), key=lambda w: -w.get("seen", 0))[:MAX_KNOWN_IN_CONTEXT]
    fields = {
        "shared": SHARED_RULES,
        "correction": CORRECTION_RULE if n_words and not setting("echo") else "",
        "known": ", ".join(w["pt"] for w in known_list) or "(none yet)",
        "learning": ", ".join(f"{w['pt']} ({w['en']})" for w in learning(words)) or "(none yet)",
        "due": ", ".join(f"{w['pt']} ({w['en']})" for w in due_words(words, 5)) or "(none)",
    }
    limit, new = setting("limit"), setting("new")
    fields["swap_rule"] = (
        "Swap every English word that has a fitting word in KNOWN, with no limit per reply"
        if limit == 0
        else f"Swap up to {limit} English words per reply for fitting words from KNOWN"
    )
    cognates = setting("cognates")
    fields["new_rule"] = "Add no new words." if new == 0 else f"Add at most {new} new words per reply, also in **bold** with the English after an equals sign."
    if new and cognates == "new":
        fields["new_rule"] += " Pick words that look like their English word first (situação, problema, versão, importante), then everyday words."
    if cognates == "all":
        fields["new_rule"] += (
            "\n- Easy cognates count as KNOWN, also when they are not on the list: a Portuguese word that an English reader surely"
            " recognizes at a glance (situação, problema, versão, sistema, importante, possível, automático, configuração, informação)."
            " Swap them in `backticks` without a gloss. A word that only looks similar but means something else (a false friend) is not a cognate."
        )
    rules = {"mix": MIX_RULES, "footer": FOOTER_RULES, "hard": HARD_RULES}[mode()]
    context = rules.format(**fields)
    weak = top_error_types()
    if weak:
        context += "\nThe user's most common mistakes in the last two weeks: " + ", ".join(f"{t} ({n})" for t, n in weak) + ". Where it fits, use correct examples of these structures in your Portuguese, so the user sees the right form."
    if mode() != "footer":
        context += SITUATION_RULE
    if setting("echo"):
        context += ECHO_RULE
    if n_words:
        context += f"\nThe user's prompt was in Portuguese (streak: {streak(stats)} days, counting today). Do the task as usual. Make the 💬 suggestion a little harder than what they wrote."
    if notes:
        context += "\nWord list updated from the prompt: " + "; ".join(notes) + ". Confirm it in one short line."
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": context}}, ensure_ascii=False))


# --- replies -----------------------------------------------------------------


def last_reply_text(hook_input):
    text = hook_input.get("last_assistant_message")
    if isinstance(text, str) and text:
        return text
    path = hook_input.get("transcript_path")
    if not path:
        return ""
    try:
        with open(path) as f:
            lines = f.readlines()[-400:]
    except OSError:
        return ""
    texts = []
    for line in reversed(lines):
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        content = (entry.get("message") or {}).get("content")
        if entry.get("type") == "user":
            if isinstance(content, str) or any(isinstance(c, dict) and c.get("type") == "text" for c in content or []):
                break
            continue
        if entry.get("type") == "assistant" and isinstance(content, list):
            texts += [c.get("text", "") for c in content if isinstance(c, dict) and c.get("type") == "text"]
    return "\n".join(reversed(texts))


# A gloss is "**palavra** (= word)"; the equals sign keeps ordinary English "**bold** (note)" out of the list.
GLOSSED = re.compile(r"\*\*([^*\n]{1,40}?)\*\*\s*\(=\s*([^)\n]{1,40})\)")
BOLD = re.compile(r"\*\*([^*\n]{1,40}?)\*\*")
ITALIC = re.compile(r"(?<![*\w])[*_]([^*_\s][^*_\n]{0,38}?)[*_](?![*\w])")


def parse_corrections(text):
    """The 📝 lines that hold a correction, each with the types and reason of the 🏷️ line below it."""
    entries, current = [], None
    for line in text.splitlines():
        line = line.strip().lstrip(">").strip()
        if line.startswith("📝") and ("\u0336" in line or "~~" in line):
            current = {"text": line[1:].strip().replace("`", ""), "types": [], "reason": ""}
            entries.append(current)
        elif line.startswith("🏷") and current is not None and not current["types"]:
            head, _, reason = line.lstrip("🏷️").strip().partition(":")
            current["types"] = [t.strip().lower() for t in head.split(",") if t.strip().lower() in ERROR_TYPES]
            current["reason"] = reason.strip().replace("`", "")
        elif line.startswith("✏️") and "→" in line:
            entries.append({"text": line.lstrip("✏️").strip(), "types": [], "reason": ""})
    return entries


def load_corrections(days=None):
    since = (today() - dt.timedelta(days=days)).isoformat() if days else ""
    out = []
    try:
        with open(CORRECTIONS) as f:
            for line in f:
                try:
                    c = json.loads(line)
                except ValueError:
                    continue
                if c.get("ts", "")[:10] >= since:
                    c["id"] = hashlib.sha1(c["text"].encode()).hexdigest()[:8]
                    out.append(c)
    except OSError:
        pass
    return out


def top_error_types(days=14, n=3):
    counts = collections.Counter(t for c in load_corrections(days) for t in c.get("types", []))
    return counts.most_common(n)


def stop_hook():
    hook_input = read_hook_input()
    if not attended(hook_input):
        return
    text = last_reply_text(hook_input)
    text = re.sub(r"```[\s\S]*?```", " ", text)
    corrections = parse_corrections(text)
    words = load_words()
    before = set(words)
    seen = set()
    session = hook_input.get("session_id")
    suggestion = next((l.split("💬", 1)[1].replace("`", "") for l in reversed(text.splitlines()) if "💬" in l), None)
    if suggestion and session:
        suggestions = load(SUGGESTIONS, {})
        suggestions[session] = suggestion.strip()
        save(SUGGESTIONS, dict(list(suggestions.items())[-50:]))
    # Known words are written in backticks; real code in backticks is never on the word list.
    for pt in re.findall(r"`([^`\n]{1,40})`", text):
        key = pt.lower().strip()
        if key in words and key not in seen:
            see_word(words, key)
            seen.add(key)
    text = re.sub(r"`[^`\n]*`", " ", text)
    for pt, en in GLOSSED.findall(text):
        # A gloss is a short word or phrase; longer bold text with brackets is ordinary English emphasis.
        if len(pt.split()) > 3 or len(en.split()) > 4 or not re.fullmatch(r"[a-zà-ÿ' -]+", pt.lower()):
            continue
        see_word(words, pt, en.strip())
        seen.add(pt.lower().strip())
    rest = GLOSSED.sub(" ", text)
    for pt in BOLD.findall(rest) + ITALIC.findall(BOLD.sub(" ", rest)):
        key = pt.lower().strip()
        if key in words and key not in seen:
            see_word(words, key)
            seen.add(key)
    struck = 0
    for c in corrections:
        struck += len(re.findall(r"(?:[^\u0336]\u0336)+", c["text"])) + len(re.findall(r"~~[^~]+~~", c["text"]))
        append(CORRECTIONS, {"ts": now().isoformat(timespec="seconds"), **c})
    if not seen:
        return
    save(WORDS, words)
    known_shown = sum(1 for k in seen if words[k]["box"] >= KNOWN_BOX)
    append(LOG, {
        "ts": now().isoformat(timespec="seconds"), "kind": "reply", "session": session,
        "known_shown": known_shown, "learning_shown": len(seen) - known_shown,
        "new_words": sorted(set(words) - before), "corrections": struck,
    })
    stats = load(STATS, {"days": [], "prompts": 0, "words": 0, "replies": 0})
    stats["replies"] = stats.get("replies", 0) + 1
    save(STATS, stats)


# --- status line and commands ------------------------------------------------


def statusline():
    read_hook_input()
    if mode() == "off":
        print("🇧🇷 off")
        return
    stats = load(STATS, {"days": []})
    words = load(WORDS, {})
    n = streak(stats)
    label = f"{n} dia" if n == 1 else f"{n} dias"
    done_today = today().isoformat() in stats.get("days", [])
    flame = f"🔥 {label}" if done_today else (f"⏳ {label}, escreva hoje!" if n else "escreva em português hoje!")
    parts = [f"🇧🇷 {flame}", f"{stats.get('prompts', 0)} frases", f"{len(known(words))} sei", f"{len(learning(words))} aprendendo"]
    due = len(due_words(words))
    if due:
        parts.append(f"{due} para revisar")
    if mode() != "mix":
        parts.append(f"modo {mode()}")
    print(" · ".join(parts))


def cmd_mode(args):
    if not args or args[0] not in MODES + ("on",):
        print(mode())
        return
    config = load(CONFIG, {})
    config["mode"] = "mix" if args[0] == "on" else args[0]
    save(CONFIG, config)
    print(f"mode: {config['mode']}")


def week_metrics(days=7):
    """Usage over the last days, from the prompt and reply log."""
    since = (today() - dt.timedelta(days=days - 1)).isoformat()
    prompts, replies = [], []
    try:
        with open(LOG) as f:
            for line in f:
                try:
                    e = json.loads(line)
                except ValueError:
                    continue
                if e.get("ts", "")[:10] >= since:
                    (prompts if e.get("kind") == "prompt" else replies if e.get("kind") == "reply" else []).append(e)
    except OSError:
        pass
    words = load(WORDS, {})
    all_words = sum(e["words"] for e in prompts)
    pt_words = sum(e["pt_words"] for e in prompts)
    added = sum(1 for w in words.values() if w.get("added", "") >= since)
    learned = sum(1 for w in words.values() if w.get("written", "") >= since)
    return {
        "days": days,
        "active_days": len({e["ts"][:10] for e in prompts if e["pt_words"]}),
        "prompts": len(prompts),
        "portuguese_prompts": sum(1 for e in prompts if e["portuguese"]),
        "portuguese_share": round(pt_words / all_words, 3) if all_words else 0,
        "portuguese_words_written": pt_words,
        "new_words_shown": added,
        "words_learned_by_writing": learned,
        "read_to_written": round(learned / added, 2) if added else None,
        "corrections": sum(e.get("corrections", 0) for e in replies),
        "errors_per_portuguese_word": round(sum(e.get("corrections", 0) for e in replies) / pt_words, 3) if pt_words else None,
        "suggestions_used": sum(1 for e in prompts if e.get("suggestion_used")),
        "marked_not_known": sum(len(e.get("not_known", [])) for e in prompts),
        "top_error_types": dict(top_error_types(days, 5)),
    }


def cmd_stats():
    stats = load(STATS, {"days": []})
    words = load_words()
    print(
        json.dumps(
            {
                "mode": mode(),
                **{k: setting(k) for k in DEFAULTS},
                "streak_days": streak(stats),
                "days_practised": len(stats.get("days", [])),
                "portuguese_prompts": stats.get("prompts", 0),
                "portuguese_words_written": stats.get("words", 0),
                "replies_with_portuguese": stats.get("replies", 0),
                "known": len(known(words)),
                "known_from_seed_list_unchecked": sum(1 for w in known(words) if w.get("seed") and not w.get("seen")),
                "learning": len(learning(words)),
                "due_for_review": len(due_words(words)),
                "last_7_days": week_metrics(),
            },
            ensure_ascii=False,
            indent=1,
        )
    )


def cmd_due(args):
    for w in due_words(load_words(), int(args[0]) if args else 10):
        print(json.dumps(w, ensure_ascii=False))


def cmd_grade(args):
    if len(args) != 2 or args[1] not in ("ok", "miss"):
        sys.exit("usage: pt.py grade WORD ok|miss")
    words = load_words()
    w = words.get(args[0].lower())
    if not w:
        sys.exit(f"unknown word: {args[0]}")
    if args[1] == "ok":
        write_word(w)
    else:
        set_box(w, 1)
    save(WORDS, words)
    print(json.dumps(w, ensure_ascii=False))


def cmd_mark(args, box):
    words = load_words()
    for a in args:
        w = words.get(a.lower())
        if w:
            set_box(w, box(w))
            print(json.dumps(w, ensure_ascii=False))
        else:
            print(f"unknown word: {a} (add it with: pt.py add PT EN)")
    save(WORDS, words)


def cmd_add(args):
    if len(args) != 2:
        sys.exit("usage: pt.py add PT EN")
    words = load_words()
    see_word(words, args[0], args[1])
    save(WORDS, words)
    print(json.dumps(words[args[0].lower()], ensure_ascii=False))


def cmd_cards(args):
    """Mistake cards that are due: corrections never asked first, then the ones whose date has come."""
    state = load(CARDS, {})
    t = today().isoformat()
    unique = {c["id"]: c for c in load_corrections() if "\u0336" in c["text"] or "~~" in c["text"]}
    due = [c for c in unique.values() if state.get(c["id"], {}).get("next", "") <= t]
    due.sort(key=lambda c: (c["id"] in state, state.get(c["id"], {}).get("next", ""), c["ts"]))
    for c in due[: int(args[0]) if args else 3]:
        print(json.dumps({k: c.get(k) for k in ("id", "text", "types", "reason")}, ensure_ascii=False))


def cmd_card_grade(args):
    if len(args) != 2 or args[1] not in ("ok", "miss"):
        sys.exit("usage: pt.py card-grade ID ok|miss")
    state = load(CARDS, {})
    card = state.get(args[0], {"box": 0})
    card["box"] = min(card["box"] + 1, len(INTERVALS) - 1) if args[1] == "ok" else 1
    card["next"] = days_from_today(INTERVALS[card["box"]])
    state[args[0]] = card
    save(CARDS, state)
    print(json.dumps({"id": args[0], **card}))


def cmd_corrections(args):
    try:
        with open(CORRECTIONS) as f:
            lines = f.readlines()
    except OSError:
        return
    for line in lines[-(int(args[0]) if args else 10):]:
        print(line.rstrip())


def cmd_fala(args):
    out_dir = tempfile.mkdtemp(prefix="fala-")
    if args and os.path.isfile(args[0]):
        audio = args[0]
    else:
        seconds = args[0] if args else "30"
        audio = os.path.join(out_dir, "fala.wav")
        print(f"Recording {seconds} s from the default microphone. Fale agora!", file=sys.stderr)
        subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "avfoundation", "-i", ":0", "-t", seconds, "-ac", "1", "-ar", "16000", audio],
            check=True,
        )
    subprocess.run(
        ["mlx_whisper", audio, "--model", WHISPER_MODEL, "--language", "pt", "--output-dir", out_dir, "--output-format", "txt"],
        check=True,
        stdout=subprocess.DEVNULL,
    )
    name = os.path.splitext(os.path.basename(audio))[0] + ".txt"
    with open(os.path.join(out_dir, name)) as f:
        print(f.read().strip())


def main():
    cmd, args = (sys.argv[1], sys.argv[2:]) if len(sys.argv) > 1 else ("stats", [])
    commands = {
        "prompt-hook": prompt_hook,
        "stop-hook": stop_hook,
        "statusline": statusline,
        "stats": cmd_stats,
        "mode": lambda: cmd_mode(args),
        "set": lambda: cmd_set(args),
        "due": lambda: cmd_due(args),
        "grade": lambda: cmd_grade(args),
        "know": lambda: cmd_mark(args, lambda w: max(w["box"], KNOWN_BOX + 1)),
        "forget": lambda: cmd_mark(args, lambda w: 1),
        "add": lambda: cmd_add(args),
        "corrections": lambda: cmd_corrections(args),
        "cards": lambda: cmd_cards(args),
        "card-grade": lambda: cmd_card_grade(args),
        "fala": lambda: cmd_fala(args),
    }
    if cmd not in commands:
        sys.exit(__doc__)
    try:
        commands[cmd]()
    except Exception as e:  # a hook must never break the session
        if cmd in ("prompt-hook", "stop-hook", "statusline"):
            print(f"pt.py {cmd}: {e}", file=sys.stderr)
            sys.exit(0)
        raise


if __name__ == "__main__":
    main()
