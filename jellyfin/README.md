# Jellyfin

`docker-compose.yml` defines the service. Copy `.env.example` to `.env` and set
the media directory and host values before deployment.

## Rename episodes

`tools/rename-series.py` sorts `.mkv`, `.mp4`, and `.avi` files in a directory by
name (case insensitive) and proposes names such as `My Series s01e01.mkv`. It
does not search subdirectories.

```sh
python3 jellyfin/tools/rename-series.py --path /path/to/season --series-name "My Series" --season 1
python3 jellyfin/tools/rename-series.py --path /path/to/season --series-name "My Series" --season 1 --apply
python3 jellyfin/tools/rename-series.py --path /path/to/season --undo
```

The first command only previews the changes and creates no files. `--apply`
renames the videos and appends completed changes to `rename.log` in the same
directory. `--undo` restores the original names from the most recent applied
operation. Before renaming, it checks that every target still has the recorded
device, inode, size, and nanosecond modification and change times, and that no
original name exists. Missing files or mismatches stop the undo. These checks
detect ordinary replacements and edits but do not prove content identity; the
script does not hash video contents. Only the latest operation can be undone.
Older log entries without operation boundaries or timestamp metadata remain in
the log but cannot be undone safely by the script.

The directory and series name may also be given as positional arguments. Use
`--start` to set the first episode number. The script rejects nonpositive
numbers and existing destination files; review the preview before applying it.
