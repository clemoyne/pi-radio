#!/bin/sh
# Copies the committed files of the working repositories (../sdrpi, ../garage_pi and ../porch_pi) into this one.
# Only committed files come across, so local.json, CLAUDE.local.md and anything else ignored stays behind.
# Review with `git status`, commit, then run ./push.sh to update both the private and public repositories.
set -eu
cd "$(dirname "$0")"
for project in sdrpi garage_pi porch_pi; do
  source="../$project"
  if [ -n "$(git -C "$source" status --porcelain)" ]; then
    echo "$source has uncommitted changes; commit them first" >&2
    exit 1
  fi
  rm -rf "$project"
  mkdir "$project"
  git -C "$source" archive HEAD | tar -x -C "$project"
  echo "$project: $(git -C "$source" log -1 --format='%h %s')"
done
