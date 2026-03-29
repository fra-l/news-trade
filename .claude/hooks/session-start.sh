#!/bin/bash
set -euo pipefail

# Only run in Claude Code on the web (remote sessions)
if [ "${CLAUDE_CODE_REMOTE:-}" != "true" ]; then
  exit 0
fi

DOTFILES_REPO="https://github.com/fra-l/dotfiles"
DOTFILES_DIR="$HOME/.dotfiles"
SKILLS_SOURCE="$DOTFILES_DIR/claude/skills"
SKILLS_TARGET="$HOME/.claude/skills"

# Clone dotfiles repo if not present, otherwise pull latest
if [ ! -d "$DOTFILES_DIR/.git" ]; then
  git clone --depth=1 "$DOTFILES_REPO" "$DOTFILES_DIR"
else
  git -C "$DOTFILES_DIR" pull --ff-only
fi

# Ensure target skills directory exists
mkdir -p "$SKILLS_TARGET"

# Symlink each skill directory from dotfiles into ~/.claude/skills/
if [ -d "$SKILLS_SOURCE" ]; then
  for skill in "$SKILLS_SOURCE"/*/; do
    skill_name="$(basename "$skill")"
    target="$SKILLS_TARGET/$skill_name"
    if [ ! -e "$target" ]; then
      ln -s "$skill" "$target"
      echo "Linked skill: $skill_name"
    fi
  done
fi
