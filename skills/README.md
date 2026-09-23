# Firecube Skills

Agent skills for the Firecube hackathon: `firecube` (build and run Firecube plugins) and `eumdac` (find and download EUMETSAT Data Store products). They are plain `SKILL.md` folders that work with Claude Code, Codex, VS Code Copilot, OpenCode, and other agents that support Agent Skills. Installing a skill installs no software and configures no credentials; it only teaches your agent how to use the tools you install yourself.

| Skill | Use it for | Grounded on |
| --- | --- | --- |
| [firecube](firecube/SKILL.md) | Installing Firecube, running ingestions, designing and implementing a plugin for your dataset, testing, packaging, Zarr/Parquet checks, crash recovery, performance tuning | `firecube==0.1.7` |
| [eumdac](eumdac/SKILL.md) | Searching collections, checking product count and size, authorised downloads to a directory you name, Data Tailor, subscriptions | `eumdac==3.1.1` |

The `firecube` skill never downloads data itself. When you have no source files, it hands over to the `eumdac` skill, which asks for your authorisation before any download. Install both.

## Install

Clone the hackathon repository, then expose the two skill folders in your agent's skill directory. Symlinks keep the skills in sync with the checkout; copy the folders instead where symlinks are unavailable (Windows without developer mode).

**Claude Code**

```bash
mkdir -p "$HOME/.claude/skills"
for skill in firecube eumdac; do
  ln -s "$PWD/skills/$skill" "$HOME/.claude/skills/$skill"
done
```

For one project only, use `<project>/.claude/skills/` instead of `$HOME/.claude/skills/`.

**Codex and VS Code Copilot**

```bash
mkdir -p "$HOME/.agents/skills"
for skill in firecube eumdac; do
  ln -s "$PWD/skills/$skill" "$HOME/.agents/skills/$skill"
done
```

For one project only, use `<project>/.agents/skills/`. In VS Code, check the Skills list under Chat Customizations.

**OpenCode**

```bash
mkdir -p "$HOME/.config/opencode/skills"
for skill in firecube eumdac; do
  ln -s "$PWD/skills/$skill" "$HOME/.config/opencode/skills/$skill"
done
```

For one project only, use `<project>/.opencode/skills/`.

**Windows PowerShell** (copies instead of linking; re-copy after updating the checkout)

```powershell
$destination = Join-Path $HOME '.agents/skills'   # or .claude/skills for Claude Code
New-Item -ItemType Directory -Force $destination | Out-Null
foreach ($skill in 'firecube', 'eumdac') {
    Copy-Item -Recurse (Join-Path 'skills' $skill) (Join-Path $destination $skill)
}
```

Run the commands from the repository root (the directory that contains `skills/`). `ln` reports an error if a destination already exists; inspect that installation before replacing it. Each skill must be exposed directly as `<name>/SKILL.md`; a clone nested inside the skill directory is not itself a skill.

**Or let the agent install them.** From the repository root, ask:

```text
Install the firecube and eumdac skills from ./skills into my current agent's skill
directory using the instructions in skills/README.md. Keep any existing installation
unless I authorise replacing it, then verify that <skill-dir>/firecube/SKILL.md and
<skill-dir>/eumdac/SKILL.md resolve.
```

Verify, then start a new agent session so the skills are picked up:

```bash
test -f "$HOME/.claude/skills/firecube/SKILL.md" && echo "firecube: installed"
test -f "$HOME/.claude/skills/eumdac/SKILL.md" && echo "eumdac: installed"
```

## Prerequisites the skills expect

- Firecube on your `PATH`, pinned to the version the skill covers: `uv tool install --python 3.12 'firecube==0.1.7'` (the skill's [install reference](firecube/references/install.md) has the alternatives). `uv` itself must be on `PATH`; `firecube plugins install` uses it.
- EUMDAC only if you download from the Data Store: `uv tool install 'eumdac==3.1.1'`, plus your own Data Store credentials from the EUMETSAT user portal. The agent never asks you to paste credentials into the chat.
- Source data you bring, or a collection ID the agent can search with `eumdac`.

## Prompts to start with

```text
Use the firecube skill. Run the Firecube quickstart plugin end to end and show me the resulting Zarr cube.
```

```text
Use the firecube skill. I want to build a plugin for the NetCDF files in <directory>. Inspect two products first and propose a cube design before writing any code.
```

```text
Use the firecube skill. I want a plugin for collection <EO:EUM:DAT:...> and I have no files yet.
```

```text
Use the eumdac skill. How many products does collection <id> have for <time range>, and how big are they? Do not download anything yet.
```

The `firecube` skill asks before scaffolding a project: plugin name, author, licence, and target directory. It writes a plan file at a path you name and stops for your go-ahead before implementing. Read-only commands run without asking; anything that writes, deletes, installs, or downloads is confirmed with you first.

## What is in here

```text
skills/
├── firecube/      SKILL.md, references/, scripts/ (optional helper scripts), agents/
├── eumdac/        SKILL.md, references/, agents/
├── examples/      Worked prompt/response scenarios for each skill
└── README.md
```

`SKILL.md` is what the agent loads first; it routes to the files under `references/` on demand. The [firecube helper scripts](firecube/scripts/README.md) are optional and run in your plugin project's environment.
