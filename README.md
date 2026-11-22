# portugues

A Claude Code skill for learning Brazilian Portuguese while you work. Each reply in a session you type into is written in English, with the Portuguese words you know swapped in. Words you learn are tracked, and the mistakes in your Portuguese are corrected.

## What it does

- **Mix mode** (the default): known words show in `backticks`, which the terminal colors. New words show in **bold** with the English after them: `**palavra** (= word)`.
- **Your own words**: a word becomes known only when you write it yourself. Reading it does not count.
- **Echo**: every reply starts with your prompt rewritten. Mistakes are struck through and fixed, with a line that names the type of error.
- **Cognates**: easy cognates such as `situação` or `possível` can count as known.
- **Status line**: your streak, the Portuguese prompts you wrote, the words you know and learn, and the reviews due.
- **Commands**: `/portugues revisão` (a quiz of your words and your own mistakes), `leitura` (a short text to read), `semana` (your week), `erros` (your most common errors), `fala` (speak, transcribed locally with Whisper).

The hooks skip sessions you do not type into: `-p` and SDK runs, agents in `~/.claude/agents/`, and `screen` sessions. Portuguese never goes into code, commits, PRs or messages.

## Install

Requires Python 3.9 or later. It uses only the standard library. `fala` also needs `ffmpeg` and `mlx_whisper`.

1. Link the skill:

   ```bash
   ln -s "$PWD" ~/.claude/skills/portugues
   ```

2. Add the hooks and the status line to `~/.claude/settings.json`:

   ```json
   {
     "hooks": {
       "UserPromptSubmit": [
         { "hooks": [{ "type": "command", "command": "python3 ~/.claude/skills/portugues/bin/pt.py prompt-hook" }] }
       ],
       "Stop": [
         { "hooks": [{ "type": "command", "command": "python3 ~/.claude/skills/portugues/bin/pt.py stop-hook" }] }
       ]
     },
     "statusLine": { "type": "command", "command": "python3 ~/.claude/skills/portugues/bin/pt.py statusline" }
   }
   ```

3. Start a new session. The first prompt loads about 210 common words from `bin/seed-words.tsv` as known.

## Settings

```bash
python3 bin/pt.py mode mix|footer|hard|off
python3 bin/pt.py set limit=0 new=2 echo=1 cognates=all
python3 bin/pt.py stats
```

- `limit`: the number of words swapped per reply. 0 means no limit.
- `new`: the number of new bold words per reply.
- `echo`: 1 starts each reply with your corrected prompt.
- `cognates`: `off`, `new` (cognates come first among new words) or `all` (every easy cognate counts as known).

Your data stays in `~/.portugues/` (config, words, log, corrections, cards), outside this repo.

## Terminal tips (macOS Terminal.app)

- **Accents**: run `defaults write com.apple.Terminal ApplePressAndHoldEnabled -bool true` and quit Terminal with ⌘Q. Then hold a letter to pick an accent. Turn off "Use Option as Meta key" in the profile, so that ⌥e and ⌥n make accents too.
- **Base64 text in the prompt**: turn off copy on select (`"copyOnSelect": false` in `~/.claude.json`). To copy, select the text and press Ctrl+C, or hold Fn while you drag and press ⌘C.
