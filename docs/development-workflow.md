# Stable main, feature testing and runtime-data safety

## Branch workflow

- `main` is the approved stable source branch. Merging a feature into it does not deploy or restart production.
- `feat/tv-diagnostics-room-controls` is the current feature-testing branch. Merge updated `main` into it, resolve overlaps without losing either feature, and test the combined tree before publishing it for testing. Do not merge testing changes back into `main` without the user's approval.
- After a feature has been approved and merged into `main`, delete its remote source branch. First refresh both refs and prove the exact source tip is contained in `main`; abort deletion if the source changed. Never delete `main`, an unmerged testing branch, or commits just because they are old.
- Delete an ordinary merged local branch only after its worktree is clean and deliberately retired. Never force-delete a branch checked out by another worktree. Removing a branch name does not remove commits already reachable from `main`.
- Audit older branches by ancestry, not dates. Older branches with unique commits need a separate keep/archive/discard decision.
- GitHub Settings → General → Pull Requests → **Automatically delete head branches** can handle branches merged through pull requests. It does not clean up branches after a direct Git push/fast-forward; that workflow must explicitly delete its source ref. Repository-setting changes require administrative access.

## Why selecting an old version can look like file loss

A checkout deliberately replaces tracked source files with the selected version. Source files introduced later disappear from that working directory until a newer version is selected again; their committed history remains in Git.

Uploaded images, configuration, presets, auth databases and private TV support files are installation data, normally not tracked in Git. Git cannot restore their contents merely by switching back. Ordinary branch switching is not evidence that Git deleted all untracked data. In the investigated media-loss incident, the old branch's ignore rules plus an untracked-file cleanup were consistent with the observed pattern, but the exact deleting command/process was not proved.

## Required deployment posture

1. **Do not use the running production installation as a branch-testing workspace.** Keep source authoring, private dev testing and production releases separate.
2. **Keep durable runtime data outside switchable code checkouts.** Media supports `TDECK_MEDIA_DIR`; the rest of the installation needs a deliberate path/configuration plan, not an assumed environment variable. Back up and validate a copied candidate before any migration. Production data moves require separate approval.
3. **Back up actual runtime files**, including media originals/thumbnails, config, presets, auth storage and local certificate/key material. Protect backup access and test restoration. Config ZIP exports alone do not currently include the Media Library.
4. **Use installation-local ignore protection as a safety net.** `.git/info/exclude` (the common Git directory for linked worktrees) retains rules when a branch changes its `.gitignore`. Cover runtime directories/files, not just JSON. This local setting is not carried to another clone by a push.
5. **Never treat ignore rules as a backup or hard deletion barrier.** Cleanup explicitly including ignored files (`git clean -x` or `-X`), manual deletion, filesystem damage and other tools can still remove data. Do not run destructive cleanup in a deployed installation.
6. **Keep known-good release identifiers and backups.** Deleting obsolete feature branches reduces accidental choices, but Git history still permits old-commit checkouts. It is not a substitute for controlled deployment and separated data.

The assistant host's shared TDeck repository has expanded installation-local exclusions, tested using dry runs in a synthetic old-ignore repository. This does not assert that every production clone has the same protection. The earlier production-specific `media_library/` exclusion is a separate installation setting; extending it or moving production data must be explicitly scoped and verified.

## Primary references

- Git ignore sources and precedence: https://git-scm.com/docs/gitignore
- Git cleanup and ignored-file behavior: https://git-scm.com/docs/git-clean
- GitHub automatic branch deletion: https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/configuring-pull-request-merges/managing-the-automatic-deletion-of-branches
