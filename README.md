# GDrive VideoLoader

**GDrive VideoLoader** is a Python-based tool to download videos from Google Drive effortlessly, **including those marked as _view-only_** (no download option). It supports resumable downloads, progress bars, parallel downloading, and various customization options for video fetching and downloading.

## Features

- Download videos even if marked as *view-only* (without a download button)
- Supports resumable downloads (continue from where it stopped)
- Supports parallel downloading of a single file (multithreading)
- Displays a progress bar for ongoing downloads
- Allows custom chunk sizes for downloading
- Optionally specify a custom output file name
- Verbose mode for detailed logs during execution
- Recursive folder download while preserving Google Drive folder structure
- Download status tracking in JSON (`queued`, `downloading`, `completed`, `skipped`, `failed`)
- Skip already downloaded files automatically to avoid duplicates

## Installation

### Prerequisites

- Python 3.7+
- Pip (Python package manager)

### Dependencies

Install the required Python packages using the following command:

```bash
pip install -r requirements.txt
```

## Usage

### Basic Command

To download a video, you can provide either the Google Drive file ID or a full Google Drive URL:

**Using a file ID:**
```bash
python gdrive_videoloader.py <video_id>
```

**Using a full Google Drive URL:**
```bash
python gdrive_videoloader.py https://drive.google.com/file/d/<video_id>/view
```

The script will automatically extract the file ID from the URL if you provide a full link.

### Options

| Parameter                | Description                                                       | Default Value         |
|--------------------------|-------------------------------------------------------------------|-----------------------|
| `<video_id>`             | The video ID from Google Drive or a full Google Drive URL (required). The script automatically extracts the ID from URLs like `https://drive.google.com/file/d/ID/view`. | N/A                   |
| `-o`, `--output`         | Custom output file name for the downloaded video.                | Video name in GDrive  |
| `-c`, `--chunk_size`     | Chunk size (in bytes) for downloading the video.                 | 1024 bytes            |
| `-t`, `--threads`        | Number of threads for parallel downloading, improves speed       | 4                     |
| `-v`, `--verbose`        | Enable verbose mode for detailed logs.                           | Disabled              |
| `--cookie-file`          | Path to exported browser cookie JSON for private/shared-with-me access (single mode and folder fallback). | N/A |
| `--folder`               | Google Drive folder ID or folder URL to download recursively.    | N/A                   |
| `--output-dir`           | Base local output directory for folder mode.                     | `.`                   |
| `--auth-client-secrets`  | OAuth client secrets JSON path (folder mode).                    | `client_secret.json`  |
| `--auth-token-file`      | OAuth token cache path (folder mode).                            | `token.json`          |
| `--status-file`          | JSON file storing download progress status for folder mode.      | `download_status.json`|
| `--show-status`          | Print status summary from `--status-file` and exit.              | Disabled              |
| `--version`              | Display the script version.                                      | N/A                   |
| `-h`, `--help`           | Display the help message.                                        | N/A                   |

### Download Entire Folder (Recursive)

This mode preserves the exact folder hierarchy from Google Drive and supports resume/skip to avoid duplicate downloads.

### OAuth Setup (Required For First Folder Login)

To use `--folder`, you need an OAuth client secret file for the first login.

1. Go to Google Cloud Console and create OAuth credentials with type **Desktop app**.
2. In the same Google Cloud project, enable **Google Drive API** from APIs & Services -> Library.
3. Download the credentials JSON file.
4. Rename it to `client_secret.json` (or keep original name and pass `--auth-client-secrets <path>`).
5. Put this file in the project root.

Then run folder download:

```bash
python gdrive_videoloader.py \
	--folder "https://drive.google.com/drive/folders/<folder_id>" \
	--output-dir "downloads" \
	--status-file download_status.json \
	-c 1048576 -v
```

On first run, a browser login window appears and token is saved to `token.json`.
From next runs, script reuses `token.json` automatically.

If some files fail in folder mode via API with HTTP `403` or `404` (for example restricted/shared files), you can pass `--cookie-file` to retry those files through browser-cookie fallback automatically.

### Check Download Status

```bash
python gdrive_videoloader.py --show-status --status-file download_status.json
```

Status values:
- `queued`: waiting to start
- `downloading`: currently in progress
- `completed`: finished successfully
- `skipped`: already exists or unsupported mime type
- `failed`: download failed (can retry by rerunning)

Each file entry in `download_status.json` also includes `download_method` (`api` or `cookie`) when a transfer path is used.

## TODO

### Features
- Add support for downloading subtitles.
- Add support for multiple downloads (list or file of video IDs).
- Allow selection of video quality.
- Implement temporary file naming during download.

### UX
- Safely handle interruptions (KeyboardInterrupt).
- Display custom error messages based on request responses.

### Organization
- Modularize the project into separate files (`downloader.py`, `cli.py`, `utils.py`).
- Add logging support using the `logging` module.

### Code Quality
- Create automated tests for core functions.
- Add detailed documentation using `pdoc` or `Sphinx`.

## Contributing
Contributions are always welcome! If you have suggestions for improving the script or adding new features, feel free to fork the repository and submit a pull request.

## License
This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.

python gdrive_videoloader.py --folder "https://drive.google.com/drive/u/0/folders/1gwObgm2Eg4FjcnlquL719h4LojvW79Lw" --output-dir "downloads" --auth-client-secrets "client_secret.json" --auth-token-file "token.json" --status-file "download_status.json" --cookie-file "cookie.json" -c 1048576 -v