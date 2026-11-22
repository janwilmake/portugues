---
name: portugues
description: The user's Brazilian Portuguese practice (beginner). Use when the user says "/portugues", asks for a review quiz ("revisão"), a reading text ("leitura"), their streak, stats or known words, wants to mark words as known or unknown, wants to switch the mode (mix, footer, hard, off), or asks how the Portuguese in replies works.
---

# Português

The user learns **Brazilian Portuguese** and is a beginner. Two global hooks do the daily work, so this skill is only for the commands below:

- **UserPromptSubmit** (`bin/pt.py prompt-hook`) gives each turn the rules of the current mode and the word lists: KNOWN, LEARNING and DUE. It counts each prompt the user writes in Portuguese, and it reads `não sei: X, Y` and `já sei: X` from any prompt.
- **Stop** (`bin/pt.py stop-hook`) reads the Portuguese words of the reply into `~/.portugues/words.json`. A `**word** (gloss)` is new or learning; a word in backticks (or italics) that is on the list is one more exposure. It keeps each `✏️` correction in `~/.portugues/corrections.jsonl`.
- **Status line** (`bin/pt.py statusline`) shows the streak, the Portuguese prompts, the words known and learning, and the reviews due.

The hooks skip sessions the user does not type into: `cca` agents (`~/.claude/agents/`, screen sessions) and `-p` or SDK runs. Portuguese never goes into code, commits, PRs, tickets, Slack or email.

## How a word becomes known

A word becomes KNOWN only when the user **writes** it. Reading it in replies never makes it known. Each word sits in a Leitner box (`INTERVALS` in `pt.py`). Box 3 and up is KNOWN, and the box sets the next review of a known word.

- A new word starts in box 1 as LEARNING. It stays **bold** with its English in every reply until the user writes it.
- A word the user writes in a Portuguese prompt becomes KNOWN at once, and is then shown in backticks (colored in the terminal) without English. A KNOWN word written again moves up a box.
- `não sei X, Y` sends words back to LEARNING; `já sei X` makes it KNOWN. The hook reads this at the start of a prompt (accent optional), or anywhere when a colon follows (`não sei: X`).
- `/portugues revisão` grades words: a right answer counts as writing it, a wrong one sends it back to LEARNING. Ask LEARNING words only English → Portuguese, so the user writes the word.
- A starter list of about 230 common words (`bin/seed-words.tsv`) counts as KNOWN from day one. The quiz checks them slowly over two months.

`PT=~/.claude/skills/portugues/bin/pt.py`

## Commands

| User says | Do |
|---|---|
| `/portugues mix`, `footer`, `hard`, `off` | `python3 $PT mode <mode>`. **mix** (default): English with known words swapped in, in backticks (colored in the terminal), and learning words in **bold** with English. **footer**: plain English plus a Portuguese footer. **hard**: all Portuguese with English below. **off**: none. |
| `/portugues limit=N`, `new=N`, `echo=0\|1`, `cognates=off\|new\|all` | `python3 $PT set limit=N new=N echo=1`. **limit**: words swapped per reply (`all` or 0 = no limit, the default). **new**: new bold words per reply (default 2, 0 = none). **echo**: start each reply with the user's prompt rewritten, mistakes ~~struck~~ and fixed, known words in backticks (default 1). **cognates**: `off`, `new` (pick cognates first as new words, the default) or `all` (every easy cognate, such as situação or sistema, counts as known and is swapped without a gloss). Several can go in one command. Confirm the new values in one line. |
| `/portugues semana` | `python3 $PT stats` and report `last_7_days`: Portuguese share of the prompts, words learned by writing against new words shown, errors per Portuguese word, 💬 suggestions used, words marked "não sei". Give one concrete tip from the weakest number. |
| `/portugues` or `stats` | `python3 $PT stats`. Report it in two lines, in mix style. |
| `/portugues palavras` | List the LEARNING words, and the KNOWN words grouped by topic, from `~/.portugues/words.json`. |
| `/portugues sei X Y` / `não sei X Y` | `python3 $PT know X Y` / `python3 $PT forget X Y`. Add a missing word first with `python3 $PT add "<pt>" "<en>"`. |
| `/portugues revisão` | Quiz, see below. |
| `/portugues leitura [topic]` | Write 5 to 8 short sentences on the topic (default: what this session is doing), at beginner level. Then ask 2 questions about it in Portuguese. The user answers in Portuguese. Correct the single most useful mistake. Add new words with `python3 $PT add "<pt>" "<en>"`. |
| `/portugues erros` | `python3 $PT stats` (`last_7_days.top_error_types`) and `python3 $PT corrections 15`. Each correction has `types` (gender, accent, verb-form, ser-estar, preposition, article, plural, spelling, word-choice, word-order). Name the top 3 types with one real example of each from the list, and one tip for the most common type. |
| `/portugues fala [seconds\|file]` | `python3 $PT fala 30` records the default microphone and prints a Portuguese transcript, or transcribes a file. Correct it like a written prompt. A transcript does not show pronunciation; say that a misheard word may point to a sound to practise. |

## Revisão (review quiz)

1. `python3 $PT due 8`. If nothing is due, quiz the LEARNING words. Then `python3 $PT cards 3`: the user's own mistakes.
2. Ask one word at a time. Mix directions: Portuguese → English, and English → Portuguese. Use a short sentence as context, never a bare word list.
3. Wait for each answer. Then `python3 $PT grade "<pt>" ok` or `miss`. Accept a missing accent as `ok` and show the right spelling.
4. For each mistake card: show the corrected sentence with the corrected part as ___, plus its English meaning. The user types the missing part. Then `python3 $PT card-grade <id> ok` or `miss`. Here an accent counts: the card is often about the accent.
5. End with the score and one encouraging line in Portuguese.

## Level

Beginner: sentences of 12 words or fewer, present tense and pretérito perfeito, `você`, everyday words. In mix mode, swap more words as KNOWN grows. Raise the level of hard mode once `stats` shows more than 500 words known outside the starter list.
