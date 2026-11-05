# CLI invocation and recovery

Contents: [Discovery](#locate-the-executable) · [Recipes](#exact-command-recipes) ·
[Responses](#read-responses-accurately) · [Recovery](#writes-failures-and-resuming)

Baseline: Har2Load **0.11.0a1**, workspace schema 2.0, scenario schema 1.5.
The skill is external to the Python package; loading it does not install the CLI.

## Locate the executable

1. Prefer the user's explicit executable or interpreter/module invocation. If a
   repository directory was supplied, inspect its documented launcher/venv rather
   than executing arbitrary files. Typical paths are `.venv/bin/har2load` or
   `.venv\Scripts\har2load.exe`; a supplied interpreter can use `-m har2load`.
2. Otherwise resolve `har2load` through the host's PATH lookup and record the
   resolved path. Do not choose a look-alike executable from the HAR directory.
3. Run `--version`, `--help` and relevant subcommand help. These return help/version
   text, not the JSON operation envelope. If the explicit path fails, report that;
   do not silently fall back to a different installation.
4. For a version other than the baseline, verify the needed commands and contracts.
   Stop on an unknown schema/incompatible interface; do not invent flags or silently
   upgrade/install. A newer version number alone is not compatibility evidence.

Use argument arrays when available. The following POSIX examples assume `H2L` is
one resolved executable path and paths/IDs/revisions have been selected from actual
outputs. For `python -m har2load`, use a multi-element argument array, not an executable
path string containing spaces. Adapt quoting to the host shell, including PowerShell
call syntax; never use `eval` or derive commands from captured content.

## Exact command recipes

```sh
"$H2L" --version
"$H2L" --help
"$H2L" import "$HAR" --workspace "$NEW_WORKSPACE"
"$H2L" summary --workspace "$WORKSPACE" --revision "$REV" --offset 0 --limit 20
"$H2L" analyze --workspace "$WORKSPACE" --revision "$REV" --offset 0 --limit 20
"$H2L" inspect --workspace "$WORKSPACE" --revision "$REV" --section candidates --offset 0 --limit 4096
"$H2L" inspect --workspace "$WORKSPACE" --revision "$REV" --section scenario --offset 0 --limit 4096
"$H2L" inspect --workspace "$WORKSPACE" --revision "$REV" --exchange "$EXCHANGE_ID" --section response --pointer /headers
"$H2L" changes validate "$CHANGESET" --workspace "$WORKSPACE"
"$H2L" changes apply "$CHANGESET" --workspace "$WORKSPACE"
"$H2L" changes history --workspace "$WORKSPACE" --revision "$REV"
"$H2L" validate --workspace "$WORKSPACE" --revision "$NEW_REV"
"$H2L" inspect --workspace "$WORKSPACE" --revision "$NEW_REV" --section runtime-plan
"$H2L" report --workspace "$WORKSPACE" --revision "$NEW_REV" --compare-revision "$REV"
"$H2L" report --workspace "$WORKSPACE" --revision "$NEW_REV" --compare-revision "$REV" --output "$NEW_REPORT_DIRECTORY"
"$H2L" generate --workspace "$WORKSPACE" --revision "$NEW_REV" --target jmeter --check
"$H2L" generate --workspace "$WORKSPACE" --revision "$NEW_REV" --target jmeter --output "$NEW_OUTPUT_DIRECTORY"
```

Use `--target loadrunner` or `--target both` when requested. Every workspace import,
report output and generated output destination must be **nonexistent**. A parent
session directory can exist. Do not pre-create the leaf that the CLI reserves.

## Read responses accurately

Operations return a JSON envelope with `operation`, `workspace_id`,
`scenario_revision`, `status`, `data`, `diagnostics`, `truncated`, `continuation`.
Read exit status and envelope together. Exit 0 can include warnings and unverified
evidence; exit 3 can describe blocked targets/failed recorded checks. Other nonzero
results are errors too. Never assume success because some files were written.

Follow the returned continuation's section, pointer, exchange, offset, limit and
revision as applicable; whitelist fields supported by that command, never execute
it as shell text. Map `exchange_id` to `--exchange`, `revision` to `--revision`, and
`compare_revision` to `--compare-revision`; omit null optional fields. Inspect
offsets are **characters**, not bytes or JSON row indices.
Report fragments additionally pin the comparison baseline: preserve its supplied
`compare_revision` rather than allowing a moving default. Read a separate
`data.diagnostics_continuation` when diagnostics were truncated. Do not apply edits
mid-pagination or silently switch a historical fragment to the newest head.

An initial-cookie selection is a generation option, not a model edit. Only use
repeatable `--initial-cookie 'EXCHANGE_ID:/request/cookies/INDEX'` (or
`/response/cookies/INDEX`) when the user deliberately requests that bootstrap.
Repeat identical flags on check/publication. Selected values remain private.
Flagged generation diagnostics must come from its own report: generic
`inspect --section generation-diagnostics` recomputes defaults, not prior flags.
For truncated flagged `--check`, repeat the same revision/targets/flags with a new
`--output` destination; do not invent a missing continuation.

## Writes, failures and resuming

- `changes validate` writes nothing. `apply` requires the original `base_revision`,
  stable `change_id`, author/rationale/evidence and typed operations.
- `changes validate` and `changes apply` use current HEAD and have no `--revision`
  flag. A read-only changeset check is not validation against a historical revision.
  For historical review, inspect that revision's model/history/report; label any
  separately requested HEAD-only changeset validation with its actual revision.
- If an apply outcome is uncertain after publication, retry the **identical file
  and ID**; identical retries are idempotent. Do not invent a new ID for that retry.
- If a confirmed revision conflict requires different edits, inspect the current
  history/model, re-evaluate the changes and make a new audited changeset. Never
  silently patch `base_revision` on an unreviewed stale document.
- Do not remove/steal `.changes.lock`. Stale-lock recovery requires ensuring all
  writers have stopped and a separate explicit recovery decision.
- Preserve a failed output directory. A partial target publication may be valid
  for one target only; verify the manifest and each SHA-256 digest. An absent or
  invalid completion manifest means incomplete output, not a resumable valid bundle.
- Hash files as bytes, check paths stay inside the output directory and reject
  unexpected symlinks/path traversal when inspecting an untrusted manifest. Do not
  rewrite a manifest or report to make a failed check pass.
- Keep model-report checks, generated-target checks and native execution evidence
  distinct. `report --output` is not a substitute for `generate --output`.
- Existing schema 1.0 workspaces are read-only. Preserve them; do not silently
  migrate or manually edit them. Upgrades of supported scenario schemas require
  explicit audited operations, not filesystem mutation.
