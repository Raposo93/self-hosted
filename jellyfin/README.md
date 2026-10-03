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
```

The first command only previews the changes and creates no files. `--apply`
renames the videos and appends completed changes to `rename.log` in the same
directory. Use `--start` to set the first episode number. The script rejects
nonpositive numbers and existing destination files; review the preview before
applying it.
