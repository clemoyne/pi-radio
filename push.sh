#!/bin/sh
# Pushes main to the private repository with its full history, and replays each new commit onto the public
# repository's separate, clean history (same files, message, author and date). refs/mirror/last records the
# last main commit replayed. Run after sync.sh and a commit on main.
# Refuses the public push if it would publish RadioReference data or local files, or match any regex in
# .leakcheck (one per line; not published, since the patterns are the private details themselves).
set -eu
cd "$(dirname "$0")"
[ "$(git branch --show-current)" = main ] || { echo "check out main first" >&2; exit 1; }
[ -z "$(git status --porcelain)" ] || { echo "commit or discard changes first" >&2; exit 1; }
last=$(git rev-parse refs/mirror/last)

tip=$(git rev-parse public)
for commit in $(git rev-list --reverse "$last..main"); do
  tip=$(git log -1 --format=%B "$commit" | \
    GIT_AUTHOR_NAME="$(git log -1 --format=%an "$commit")" GIT_AUTHOR_EMAIL="$(git log -1 --format=%ae "$commit")" \
    GIT_AUTHOR_DATE="$(git log -1 --format=%aD "$commit")" git commit-tree "$commit^{tree}" -p "$tip")
done

# Public commits carry only the GitHub no-reply address (since 23 Nov 2026), never a personal email.
for commit in $(git rev-list "$(git rev-parse public)..$tip"); do
  case "$(git log -1 --format='%ae %ce' "$commit")" in
    *@users.noreply.github.com*@users.noreply.github.com) ;;
    *) echo "not pushing to public: commit $commit isn't by the no-reply address (git config user.email)" >&2; exit 1 ;;
  esac
done

bad=$(git ls-tree -r --name-only "$tip" | grep -i -E '\.csv$|channels\.json$|(^|/)places\.json$|(^|/)local\.json$|\.local\.md$|\.db$|(^|/)key$' || true)
if [ -f .leakcheck ]; then
  bad="$bad$(grep -v -e '^#' -e '^$' .leakcheck | while read -r pattern; do git grep -l -i -E "$pattern" "$tip" || true; done)"
fi
if [ -n "$bad" ]; then
  echo "not pushing to public; these would be published:" >&2
  echo "$bad" >&2
  exit 1
fi

git push private main
if [ "$tip" != "$(git rev-parse public)" ]; then
  git push public "$tip:main"
  git branch -f public "$tip"
  git update-ref refs/mirror/last main
fi
