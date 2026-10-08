# archiver-tui

A resumable replacement for the monthly "md5sum everything, tar it, rsync it
to the NAS" script, with a terminal UI to watch it and a headless mode for
cron/systemd.

```
discover  →  checksum  →  archive  →  transfer  →  verify
list files   md5 manifest   tar.gz      rsync to NAS   re-hash tarball,
                                                       compare NAS copy
```

Every stage records its progress to disk as it goes. If a run dies at
`transfer` (NAS asleep, network blip, laptop lid closed), re-running the same
command skips straight back to `transfer`; nothing is re-hashed or re-tarred.

## Install

```sh
pip install --user ".[tui]"     # or: pipx install ".[tui]"
```

The engine and `archiver run` use only the standard library; Textual is
needed only for `archiver tui`. You need `rsync` 3.1 or newer on the
machine running the archive (for `--info=progress2`).

## Use

```sh
# Interactive: r = run/resume, c = cancel, x = reset run, q = quit
archiver tui /srv/data/to-archive nas:/volume1/archives/

# Headless (cron, systemd, ssh session)
archiver run /srv/data/to-archive nas:/volume1/archives/

# What happened this month?
archiver status
archiver status --all

# Re-send to the NAS without re-hashing anything
archiver reset --from transfer
```

The destination is anything rsync accepts: a mounted share
(`/mnt/nas/archives/`) or an SSH target (`nas:/volume1/archives/`). Extra
rsync options go in one quoted string:

```sh
archiver run SRC nas:/volume1/archives/ --rsync-opts "-e 'ssh -p 2222' --bwlimit=50m"
```

### Options

| Option | Default | Notes |
|---|---|---|
| `--run-id` | current month, e.g. `2026-10` | Names the state file, tarball and manifest. Use `--run-id 2026-09` if you archive September on 1 October. |
| `--work-dir` | `~/.cache/archiver/<run-id>/` | Where the manifest and tarball are built. Needs room for the whole tarball; must not be inside the source dir. |
| `--compression` | `gz` | `none` for data that's already compressed (photos, video), `xz` for best ratio. |
| `--rsync` | `rsync` (or `$ARCHIVER_RSYNC`) | Command used for rsync, e.g. `"sudo rsync"`. |
| `--state-dir` | `~/.local/state/archiver/` | Global option, goes before the subcommand. |

### Exit codes

`0` complete · `1` a stage failed (message on stderr; re-run to resume) ·
`2` bad arguments or config · `3` another process is running this month ·
`130` cancelled.

## Scheduling

`contrib/` has a systemd user service and timer that run on the 1st of each
month at 02:00 (and catch up at next boot if the machine was off). Edit the
paths in `archiver.service`, then:

```sh
cp contrib/archiver.{service,timer} ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now archiver.timer
```

Cron works too: `0 2 1 * * $HOME/.local/bin/archiver run SRC DEST >> ~/archiver.log 2>&1`.

A per-run lock means the timer and the TUI can't both run the same month;
open `archiver tui` with the same arguments to watch status or resume a
failed scheduled run.

## What ends up on the NAS

```
2026-10.tar.gz      every file, under a 2026-10/ directory, plus 2026-10/MANIFEST.md5
2026-10.md5         the same manifest as a sidecar, readable without extracting
```

The manifest is standard `md5sum` format, so a restore can be checked with
nothing but coreutils:

```sh
tar xzf 2026-10.tar.gz
cd 2026-10 && md5sum -c MANIFEST.md5
```

## How it stays safe

- **Atomic outputs.** The state file, file list, manifest and tarball are
  written to a temp name and renamed into place, so a crash never leaves a
  half-written file that a resumed run would trust.
- **Change detection.** `discover` records each file's size and mtime;
  `checksum` and `archive` refuse to continue if a file changed underneath
  them, instead of archiving data that doesn't match its checksum.
- **Real verification.** `verify` re-reads the finished tarball and checks
  every member against the manifest, then asks rsync to compare the NAS copy
  by checksum. Differences in permissions only (common on SMB/NFS shares) are
  logged and ignored; differences in content fail the run.
- **Self-repair on resume.** A stage marked done whose output has vanished
  (e.g. the cache was cleaned) is re-run along with everything after it.
- **Symlinks and special files are skipped** and logged, so the archive holds
  regular files only.

## Layout

```
src/archiver/
  config.py     RunConfig: paths, run id, rsync command
  state.py      crash-safe JSON state + per-run flock
  stages.py     the five stages; no UI code
  pipeline.py   runs stages against state, emits progress events
  cli.py        `archiver run|tui|status|reset`
  tui.py        Textual frontend (worker thread + call_from_thread)
tests/          unittest suite; fake_rsync.py stands in when rsync is absent
contrib/        systemd service + timer
```

The engine never imports Textual. Both frontends subscribe to the same
`Event` stream from `Pipeline.run()`, so anything the TUI can show, the
console reporter can print.

## Development

```sh
pip install -e ".[dev]"
python -m unittest discover -s tests -v     # or: pytest
```

Tests that need the real `rsync` binary or Textual are skipped when those
aren't installed; CI installs both.
