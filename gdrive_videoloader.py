from urllib.parse import unquote
import requests
import argparse
import sys
from tqdm import tqdm
import os
import re
import threading
import math
import shutil
import json
from datetime import datetime, timezone

try:
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build
    from googleapiclient.errors import HttpError
except ImportError:
    Credentials = None
    Request = None
    InstalledAppFlow = None
    build = None
    HttpError = None

thread_errors = []
DRIVE_FOLDER_MIME = "application/vnd.google-apps.folder"
GOOGLE_APPS_MIME_PREFIX = "application/vnd.google-apps"
DRIVE_SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def sanitize_filename(filename: str) -> str:
    safe = re.sub(r'[<>:"/\\|?*\x00-\x1F]', '', filename)
    safe = re.sub(r'[. ]+$', '', safe)
    return safe if safe else "unnamed"

def load_cookies_from_file(cookie_file: str, verbose: bool = False) -> requests.cookies.RequestsCookieJar:
    """Loads cookies from a browser-exported JSON file into a Requests cookie jar."""
    jar = requests.cookies.RequestsCookieJar()
    if not cookie_file:
        return jar

    if not os.path.exists(cookie_file):
        print(f"[WARN] Cookie file not found: {cookie_file}")
        return jar

    try:
        with open(cookie_file, 'r', encoding='utf-8') as f:
            cookies_data = json.load(f)
    except Exception as e:
        print(f"[WARN] Could not read cookie file: {e}")
        return jar

    if not isinstance(cookies_data, list):
        print("[WARN] Cookie file format is invalid. Expected a JSON array of cookie objects.")
        return jar

    loaded = 0
    for item in cookies_data:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        value = item.get("value")
        if not name or value is None:
            continue
        domain = item.get("domain")
        path = item.get("path", "/")
        secure = bool(item.get("secure", False))
        jar.set(name, value, domain=domain, path=path, secure=secure)
        loaded += 1

    if verbose:
        print(f"[INFO] Loaded {loaded} cookies from {cookie_file}")

    return jar


class DownloadStatusTracker:
    """Persists file-level status so users can inspect progress across runs."""

    def __init__(self, status_file: str):
        self.status_file = status_file
        self.data = {
            "updated_at": utc_now_iso(),
            "summary": {
                "total": 0,
                "queued": 0,
                "downloading": 0,
                "completed": 0,
                "skipped": 0,
                "failed": 0,
            },
            "files": {}
        }
        self._load()

    def _load(self) -> None:
        if not os.path.exists(self.status_file):
            return
        try:
            with open(self.status_file, 'r', encoding='utf-8') as f:
                loaded = json.load(f)
            if isinstance(loaded, dict) and isinstance(loaded.get("files", {}), dict):
                self.data = loaded
        except Exception:
            pass

    def _recompute_summary(self) -> None:
        files = self.data.get("files", {})
        summary = {
            "total": len(files),
            "queued": 0,
            "downloading": 0,
            "completed": 0,
            "skipped": 0,
            "failed": 0,
        }
        for _, info in files.items():
            status = info.get("status", "queued")
            if status in summary:
                summary[status] += 1
        self.data["summary"] = summary
        self.data["updated_at"] = utc_now_iso()

    def save(self) -> None:
        self._recompute_summary()
        with open(self.status_file, 'w', encoding='utf-8') as f:
            json.dump(self.data, f, ensure_ascii=False, indent=2)

    def set_file(self, key: str, **kwargs) -> None:
        current = self.data.setdefault("files", {}).get(key, {})
        current.update(kwargs)
        current["updated_at"] = utc_now_iso()
        self.data["files"][key] = current
        self.save()


def print_status_summary(status_file: str) -> None:
    if not os.path.exists(status_file):
        print(f"Status file not found: {status_file}")
        return
    try:
        with open(status_file, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except Exception as e:
        print(f"Could not read status file: {e}")
        return

    summary = data.get("summary", {})
    print(f"Status file: {status_file}")
    print(f"Updated at : {data.get('updated_at', 'unknown')}")
    print(f"Total      : {summary.get('total', 0)}")
    print(f"Queued     : {summary.get('queued', 0)}")
    print(f"Downloading: {summary.get('downloading', 0)}")
    print(f"Completed  : {summary.get('completed', 0)}")
    print(f"Skipped    : {summary.get('skipped', 0)}")
    print(f"Failed     : {summary.get('failed', 0)}")

def extract_drive_id(input_str: str) -> str:
    """Extracts the Google Drive file ID from a URL or returns the input if it's already an ID."""
    pattern = r'/file/d/([a-zA-Z0-9_-]+)'
    match = re.search(pattern, input_str)
    if match:
        return match.group(1)
    return input_str


def extract_folder_id(input_str: str) -> str:
    """Extracts a Google Drive folder ID from URL or returns input if already an ID."""
    pattern = r'/folders/([a-zA-Z0-9_-]+)'
    match = re.search(pattern, input_str)
    if match:
        return match.group(1)
    return input_str

def get_video_url(page_content: str, verbose: bool) -> tuple[str, str]:
    """Extracts the video playback URL and title from the page content."""
    if verbose:
        print("[INFO] Parsing video playback URL and title.")
    contentList = page_content.split("&")
    video, title = None, None
    for content in contentList:
        if content.startswith('title=') and not title:
            title = unquote(content.split('=')[-1])
        elif "videoplayback" in content and not video:
            video = unquote(content).split("|")[-1]
        if video and title:
            break

    if verbose:
        print(f"[INFO] Video URL: {video}")
        print(f"[INFO] Video Title: {title}")

    return video, title


def ensure_drive_api_available() -> bool:
    if all((Credentials, Request, InstalledAppFlow, build)):
        return True
    print("[ERROR] Missing Google Drive API dependencies.")
    print("Install them with: pip install google-api-python-client google-auth google-auth-oauthlib")
    return False


def print_google_api_error(err: Exception) -> None:
    msg = str(err)
    lower_msg = msg.lower()

    if "accessnotconfigured" in lower_msg or "has not been used in project" in lower_msg:
        print("[ERROR] Google Drive API is disabled for your Google Cloud project.")
        print("Enable it in Google Cloud Console, then wait a few minutes and retry.")
        print("Hint: open APIs & Services -> Library -> Google Drive API -> Enable")
        return

    if "insufficientpermissions" in lower_msg or "insufficient permissions" in lower_msg:
        print("[ERROR] Your token does not have enough permission for this folder/file.")
        print("Delete token file and login again to refresh OAuth consent scopes.")
        return

    if "filenotfound" in lower_msg or "not found" in lower_msg:
        print("[ERROR] Folder not found or account does not have access.")
        print("Verify the folder URL/ID and ensure the logged-in account can open it.")
        return

    print(f"[ERROR] Google Drive API request failed: {err}")


def get_google_credentials(client_secrets_file: str, token_file: str, verbose: bool):
    creds = None
    if os.path.exists(token_file):
        try:
            creds = Credentials.from_authorized_user_file(token_file, DRIVE_SCOPES)
        except Exception:
            creds = None

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not os.path.exists(client_secrets_file):
                raise FileNotFoundError(
                    f"OAuth client secrets file not found: {client_secrets_file}. "
                    "Create Desktop OAuth credentials in Google Cloud and download JSON file."
                )
            flow = InstalledAppFlow.from_client_secrets_file(client_secrets_file, DRIVE_SCOPES)
            creds = flow.run_local_server(port=0)

        with open(token_file, 'w', encoding='utf-8') as token:
            token.write(creds.to_json())
            if verbose:
                print(f"[INFO] OAuth token saved to {token_file}")

    return creds


def list_drive_files_recursive(service, root_folder_id: str, verbose: bool):
    try:
        root_meta = service.files().get(
            fileId=root_folder_id,
            fields="id,name,mimeType",
            supportsAllDrives=True
        ).execute()
    except Exception as err:
        if HttpError and isinstance(err, HttpError):
            print_google_api_error(err)
            return None
        raise

    if root_meta.get("mimeType") != DRIVE_FOLDER_MIME:
        raise ValueError("The provided --folder is not a Google Drive folder.")

    root_name = sanitize_filename(root_meta.get("name", root_folder_id))
    queue = [(root_folder_id, root_name)]
    collected = []

    while queue:
        folder_id, relative_dir = queue.pop(0)
        page_token = None

        while True:
            try:
                resp = service.files().list(
                    q=f"'{folder_id}' in parents and trashed=false",
                    fields="nextPageToken, files(id,name,mimeType,size,md5Checksum,resourceKey,capabilities(canDownload))",
                    pageSize=1000,
                    pageToken=page_token,
                    supportsAllDrives=True,
                    includeItemsFromAllDrives=True,
                ).execute()
            except Exception as err:
                if HttpError and isinstance(err, HttpError):
                    print_google_api_error(err)
                    return None
                raise

            for item in resp.get("files", []):
                item_name = sanitize_filename(item.get("name", item.get("id", "unnamed")))
                item_mime = item.get("mimeType", "")

                if item_mime == DRIVE_FOLDER_MIME:
                    queue.append((item["id"], os.path.join(relative_dir, item_name)))
                else:
                    size_raw = item.get("size")
                    size_int = int(size_raw) if size_raw and str(size_raw).isdigit() else None
                    collected.append({
                        "id": item["id"],
                        "name": item_name,
                        "mimeType": item_mime,
                        "size": size_int,
                        "md5Checksum": item.get("md5Checksum"),
                        "resourceKey": item.get("resourceKey"),
                        "canDownload": item.get("capabilities", {}).get("canDownload", True),
                        "relative_path": os.path.join(relative_dir, item_name),
                    })

            page_token = resp.get("nextPageToken")
            if not page_token:
                break

    if verbose:
        print(f"[INFO] Found {len(collected)} files under folder '{root_name}'")

    return collected

def get_file_size(url: str, cookies) -> int:
    """Gets the total file size via a HEAD request."""
    response = requests.head(url, cookies=cookies, allow_redirects=True)
    size = int(response.headers.get('content-length', 0))
    return size


def extract_confirm_token(html: str) -> str:
    """Extracts Google Drive confirm token from interstitial HTML when present."""
    patterns = [
        r'confirm=([0-9A-Za-z_\-]+)',
        r'name="confirm"\s+value="([0-9A-Za-z_\-]+)"',
    ]
    for pattern in patterns:
        match = re.search(pattern, html)
        if match:
            return match.group(1)
    return None


def looks_like_html_file(path: str) -> bool:
    """Detects if a local partial file is actually HTML instead of media bytes."""
    if not os.path.exists(path) or os.path.getsize(path) == 0:
        return False
    try:
        with open(path, 'rb') as f:
            head = f.read(2048).lower()
        return b"<!doctype html" in head or b"<html" in head
    except Exception:
        return False


def looks_like_text_payload(path: str) -> bool:
    """Heuristic check for text-like payload accidentally saved as binary media."""
    if not os.path.exists(path) or os.path.getsize(path) == 0:
        return False
    try:
        with open(path, 'rb') as f:
            head = f.read(8192)
        if not head:
            return False
        printable = sum((32 <= b <= 126) or b in (9, 10, 13) for b in head)
        return (printable / len(head)) > 0.9
    except Exception:
        return False

def download_part(url: str, cookies, thread_lock, start: int, end: int, part_num: int, part_filename: str, chunk_size: int, pbar: tqdm, gpbar: tqdm, verbose: bool) -> None:
    """Downloads a specific byte range of the file and writes it to a part file."""
    headers = {'Range': f'bytes={start}-{end}'}

    # Support resuming individual parts
    downloaded = 0
    if os.path.exists(part_filename):
        downloaded = os.path.getsize(part_filename)
        if downloaded > 0:
            headers['Range'] = f'bytes={start + downloaded}-{end}'

            # Update Progress
            with thread_lock:
                gpbar.update(downloaded)
                pbar.update(downloaded)
            
            if verbose:
                print(f"[INFO] Resuming part {part_filename} from byte {start + downloaded}")

    # Check Part already fully downloaded
    if downloaded >= (end - start + 1):
        return
        
    s = requests.Session()
    response = s.get(url, stream=True, cookies=cookies, headers=headers)
    if response.status_code not in (200, 206):
        raise Exception(f"[ERROR] Failed to download part {part_filename}, status: {response.status_code}")
    
    file_mode = 'ab' if os.path.exists(part_filename) and os.path.getsize(part_filename) > 0 else 'wb'
    with open(part_filename, file_mode) as f:
        for chunk in response.iter_content(chunk_size=chunk_size):
            f.write(chunk)
            with thread_lock:
                gpbar.update(len(chunk))
                pbar.update(len(chunk))
            downloaded += len(chunk)

            # Check Part fully downloaded
            if downloaded >= (end - start + 1):
                break

def download_part_wrapper(*args):
    try:
        download_part(*args)
    except Exception as e:
        thread_errors.append(e)

def merge_parts(part_files: list[str], output_filename: str, verbose: bool) -> None:
    """Merges all part files into the final output file."""
    if verbose:
        print(f"[INFO] Merging {len(part_files)} parts into {output_filename}")

    missing = [pf for pf in part_files if not os.path.exists(pf)]
    if missing:
        print(f"[ERROR] Missing parts: {missing}")
        return

    with open(output_filename, 'wb') as outfile:
        for part_file in part_files:
            if verbose:
                print("Merging " + part_file)
            with open(part_file, 'rb') as pf:
                shutil.copyfileobj(pf, outfile)
    
    for part_file in part_files: # Cleanup
        os.remove(part_file)

    if verbose:
        print(f"[INFO] Merge complete. Cleaned up part files.")

def download_file(url: str, cookies, filename: str, chunk_size: int, num_threads: int, verbose: bool) -> None:
    """Downloads the file using multiple threads, each handling a byte-range segment."""

    thread_errors.clear()

    total_size = get_file_size(url, cookies)
    if num_threads == 1:
        download_single_threaded(url, cookies, filename, chunk_size, verbose)
        return
    if total_size == 0:
        print("[WARN] Could not determine file size. Falling back to single-threaded download.")
        download_single_threaded(url, cookies, filename, chunk_size, verbose)
        return

    if verbose:
        print(f"[INFO] Total file size: {total_size} bytes")
        print(f"[INFO] Downloading with {num_threads} threads")

    part_size = math.ceil(total_size / num_threads)
    part_files = []
    threads = []

    gpBar = tqdm(
        unit='B', unit_scale=True,
        desc="Download Progress",
        total=total_size,
        position=0
    )

    pbars = [
        tqdm(
            unit='B', unit_scale=True,
            desc="Downloading Part " + str(i+1),
            total=min((i * part_size) + part_size - 1, total_size - 1) - (i * part_size) + 1,
            position=i+1
        )
        for i in range(num_threads)
    ]

    thread_lock = threading.Lock()

    for i in range(num_threads):
        start = i * part_size
        end = min(start + part_size - 1, total_size - 1)
        part_filename = f"{filename}.part{i}"
        part_files.append(part_filename)

        t = threading.Thread(
            target=download_part_wrapper,
            args=(url, cookies, thread_lock, start, end, i, part_filename, chunk_size, pbars[i], gpBar, verbose),
            daemon=True
        )
        threads.append(t)
        t.start()

    for t in threads:
        t.join()

    gpBar.close()
    for pbar in pbars:
        pbar.close()
    
    if(len(thread_errors) > 0):
        print(f"[ERROR] One of the parts failed. Check the console for details. Exiting...")
        return

    # Verify all parts downloaded correctly
    downloaded_total = sum(os.path.getsize(pf) for pf in part_files if os.path.exists(pf))
    if downloaded_total < total_size:
        print(f"[ERROR] Download incomplete: got {downloaded_total}/{total_size} bytes.")
        return
    

    merge_parts(part_files, filename, verbose)
    print(f"\n{filename} downloaded successfully.")

def download_single_threaded(url: str, cookies, filename: str, chunk_size: int, verbose: bool) -> None:
    """Fallback single-threaded download (original behavior)."""
    headers = {}
    file_mode = 'wb'
    downloaded_size = 0

    if os.path.exists(filename):
        downloaded_size = os.path.getsize(filename)
        headers['Range'] = f"bytes={downloaded_size}-"
        file_mode = 'ab'

    if verbose:
        print(f"[INFO] Starting single-threaded download from {url}")

    response = requests.get(url, stream=True, cookies=cookies, headers=headers)
    if response.status_code in (200, 206):  # 200 for new downloads, 206 for partial content
        total_size = int(response.headers.get('content-length', 0)) + downloaded_size
        with open(filename, file_mode) as file:
            with tqdm(total=total_size, initial=downloaded_size, unit='B', unit_scale=True, desc=filename, file=sys.stdout) as pbar:
                for chunk in response.iter_content(chunk_size=chunk_size):
                    if chunk:
                        file.write(chunk)
                        pbar.update(len(chunk))
        print(f"\n{filename} downloaded successfully.")
    else:
        print(f"Error downloading {filename}, status code: {response.status_code}")


def download_drive_api_file(file_info: dict, access_token: str, local_path: str, chunk_size: int, verbose: bool, tracker: DownloadStatusTracker) -> tuple[bool, int | None]:
    """Downloads one Drive file via API with resume support and status tracking."""
    rel = file_info["relative_path"]
    remote_size = file_info.get("size")
    file_id = file_info["id"]
    resource_key = file_info.get("resourceKey")

    os.makedirs(os.path.dirname(local_path), exist_ok=True)

    if os.path.exists(local_path) and remote_size is not None:
        local_size = os.path.getsize(local_path)
        if local_size == remote_size:
            tracker.set_file(rel, status="skipped", bytes_downloaded=local_size, total_bytes=remote_size, reason="already_exists", download_method="api")
            if verbose:
                print(f"[INFO] Skipped existing file: {rel}")
            return True, None

    downloaded_size = os.path.getsize(local_path) if os.path.exists(local_path) else 0
    if remote_size is not None and downloaded_size > remote_size:
        downloaded_size = 0
        with open(local_path, 'wb'):
            pass

    tracker.set_file(
        rel,
        id=file_id,
        status="downloading",
        bytes_downloaded=downloaded_size,
        total_bytes=remote_size,
        local_path=local_path,
        mime_type=file_info.get("mimeType"),
        download_method="api",
    )

    url = f"https://www.googleapis.com/drive/v3/files/{file_id}?alt=media&supportsAllDrives=true"
    if resource_key:
        url += f"&resourceKey={resource_key}"
    headers = {
        "Authorization": f"Bearer {access_token}",
    }
    if downloaded_size > 0:
        headers["Range"] = f"bytes={downloaded_size}-"

    response = requests.get(url, headers=headers, stream=True)

    # Retry once after auth refresh (caller refreshes token between files).
    if response.status_code == 401:
        tracker.set_file(rel, status="failed", error="unauthorized (token expired)")
        return False, response.status_code

    if response.status_code == 416 and remote_size is not None:
        tracker.set_file(rel, status="completed", bytes_downloaded=remote_size, total_bytes=remote_size, download_method="api")
        return True, response.status_code

    if response.status_code not in (200, 206):
        tracker.set_file(rel, status="failed", error=f"http_{response.status_code}")
        return False, response.status_code

    file_mode = 'ab' if downloaded_size > 0 else 'wb'
    total_size = remote_size if remote_size is not None else int(response.headers.get('content-length', 0)) + downloaded_size

    with open(local_path, file_mode) as file:
        with tqdm(total=total_size, initial=downloaded_size, unit='B', unit_scale=True, desc=rel, file=sys.stdout) as pbar:
            current = downloaded_size
            for chunk in response.iter_content(chunk_size=chunk_size):
                if not chunk:
                    continue
                file.write(chunk)
                current += len(chunk)
                pbar.update(len(chunk))
                tracker.set_file(rel, status="downloading", bytes_downloaded=current, total_bytes=total_size)

    final_size = os.path.getsize(local_path)
    if remote_size is not None and final_size != remote_size:
        tracker.set_file(
            rel,
            status="failed",
            bytes_downloaded=final_size,
            total_bytes=remote_size,
            error=f"size_mismatch local={final_size} remote={remote_size}",
        )
        return False, None

    tracker.set_file(rel, status="completed", bytes_downloaded=final_size, total_bytes=total_size, download_method="api")
    return True, response.status_code


def download_drive_cookie_file(file_info: dict, cookie_jar, local_path: str, chunk_size: int, verbose: bool, tracker: DownloadStatusTracker) -> tuple[bool, int | None]:
    """Downloads one Drive file via browser cookies (fallback path for restricted files)."""
    rel = file_info["relative_path"]
    remote_size = file_info.get("size")
    file_id = file_info["id"]
    resource_key = file_info.get("resourceKey")

    os.makedirs(os.path.dirname(local_path), exist_ok=True)

    if os.path.exists(local_path) and remote_size is not None:
        local_size = os.path.getsize(local_path)
        if local_size == remote_size:
            tracker.set_file(rel, status="skipped", bytes_downloaded=local_size, total_bytes=remote_size, reason="already_exists", download_method="cookie")
            if verbose:
                print(f"[INFO] Skipped existing file: {rel}")
            return True, None

    downloaded_size = os.path.getsize(local_path) if os.path.exists(local_path) else 0
    is_video = file_info.get("mimeType", "").startswith("video/")
    if downloaded_size > 0 and (looks_like_html_file(local_path) or (is_video and looks_like_text_payload(local_path))):
        if verbose:
            print(f"[INFO] Detected placeholder response file, restarting cookie download: {rel}")
        downloaded_size = 0
        with open(local_path, 'wb'):
            pass

    if remote_size is not None and downloaded_size > remote_size:
        downloaded_size = 0
        with open(local_path, 'wb'):
            pass

    tracker.set_file(
        rel,
        id=file_id,
        status="downloading",
        bytes_downloaded=downloaded_size,
        total_bytes=remote_size,
        local_path=local_path,
        mime_type=file_info.get("mimeType"),
        download_method="cookie",
    )

    url = "https://drive.google.com/uc"
    params = {"export": "download", "id": file_id}
    if resource_key:
        params["resourcekey"] = resource_key
    headers = {}
    if downloaded_size > 0:
        headers["Range"] = f"bytes={downloaded_size}-"

    session = requests.Session()
    session.cookies.update(cookie_jar)
    response = None

    # For video files, prefer resolving the direct playback URL first.
    if file_info.get("mimeType", "").startswith("video/"):
        info_url = f"https://drive.google.com/u/0/get_video_info?docid={file_id}&drive_originator_app=303"
        if resource_key:
            info_url += f"&resourcekey={resource_key}"
        info_resp = session.get(info_url, allow_redirects=True)

        merged_cookies = requests.cookies.RequestsCookieJar()
        merged_cookies.update(cookie_jar)
        merged_cookies.update(info_resp.cookies)

        video_url, _ = get_video_url(info_resp.text, verbose=False)
        if video_url:
            response = requests.get(video_url, stream=True, cookies=merged_cookies, headers=headers)

    if response is None:
        response = session.get(url, params=params, headers=headers, stream=True, allow_redirects=True)

        if response.status_code in (200, 206):
            content_type = response.headers.get("content-type", "")
            disposition = response.headers.get("content-disposition", "")
            if "text/html" in content_type.lower() and "attachment" not in disposition.lower():
                html = response.text
                response.close()

                token = extract_confirm_token(html)
                if not token:
                    for key, value in session.cookies.items():
                        if key.startswith("download_warning"):
                            token = value
                            break

                if token:
                    params["confirm"] = token
                    response = session.get(url, params=params, headers=headers, stream=True, allow_redirects=True)

    if response.status_code == 416 and remote_size is not None:
        tracker.set_file(rel, status="completed", bytes_downloaded=remote_size, total_bytes=remote_size, download_method="cookie")
        return True, response.status_code

    if response.status_code not in (200, 206):
        tracker.set_file(rel, status="failed", error=f"cookie_http_{response.status_code}")
        return False, response.status_code

    response_content_type = response.headers.get("content-type", "").lower()
    if "text/html" in response_content_type:
        tracker.set_file(rel, status="failed", error="cookie_interstitial_html")
        return False, response.status_code

    file_mode = 'ab' if downloaded_size > 0 else 'wb'
    total_size = remote_size if remote_size is not None else int(response.headers.get('content-length', 0)) + downloaded_size

    with open(local_path, file_mode) as file:
        with tqdm(total=total_size, initial=downloaded_size, unit='B', unit_scale=True, desc=rel, file=sys.stdout) as pbar:
            current = downloaded_size
            for chunk in response.iter_content(chunk_size=chunk_size):
                if not chunk:
                    continue
                file.write(chunk)
                current += len(chunk)
                pbar.update(len(chunk))
                tracker.set_file(rel, status="downloading", bytes_downloaded=current, total_bytes=total_size)

    final_size = os.path.getsize(local_path)
    if remote_size is not None and final_size != remote_size:
        tracker.set_file(
            rel,
            status="failed",
            bytes_downloaded=final_size,
            total_bytes=remote_size,
            error=f"size_mismatch local={final_size} remote={remote_size}",
        )
        return False, None

    tracker.set_file(rel, status="completed", bytes_downloaded=final_size, total_bytes=total_size, download_method="cookie")
    return True, response.status_code


def download_drive_folder(folder_input: str, output_dir: str, chunk_size: int, verbose: bool, client_secrets_file: str, token_file: str, status_file: str, cookie_file: str = None) -> None:
    """Recursively downloads all files in a Google Drive folder while preserving hierarchy."""
    if not ensure_drive_api_available():
        return

    folder_id = extract_folder_id(folder_input)
    creds = get_google_credentials(client_secrets_file, token_file, verbose)
    service = build('drive', 'v3', credentials=creds, cache_discovery=False)
    tracker = DownloadStatusTracker(status_file)
    cookie_jar = load_cookies_from_file(cookie_file, verbose) if cookie_file else None

    files = list_drive_files_recursive(service, folder_id, verbose)
    if files is None:
        return

    if not files:
        print("No files found in the folder.")
        return

    for item in files:
        rel = item["relative_path"]
        tracker.set_file(
            rel,
            id=item["id"],
            status="queued",
            bytes_downloaded=0,
            total_bytes=item.get("size"),
            local_path=os.path.join(output_dir, rel),
            mime_type=item.get("mimeType"),
        )

    completed = 0
    failed = 0
    skipped = 0

    for index, item in enumerate(files, start=1):
        rel = item["relative_path"]
        mime_type = item.get("mimeType", "")
        local_path = os.path.join(output_dir, rel)

        if verbose:
            print(f"[INFO] ({index}/{len(files)}) {rel}")

        if mime_type.startswith(GOOGLE_APPS_MIME_PREFIX):
            tracker.set_file(rel, status="skipped", reason=f"unsupported_mime:{mime_type}")
            skipped += 1
            continue

        if creds.expired and creds.refresh_token:
            creds.refresh(Request())

        ok, api_status_code = download_drive_api_file(item, creds.token, local_path, chunk_size, verbose, tracker)
        if not ok and cookie_jar and api_status_code in (403, 404):
            if verbose:
                print(f"[INFO] API failed ({api_status_code}), retrying with cookies: {rel}")
            ok, _ = download_drive_cookie_file(item, cookie_jar, local_path, chunk_size, verbose, tracker)
        elif not ok and cookie_jar and verbose:
            print(f"[INFO] API failed ({api_status_code}), skipping cookie fallback for: {rel}")

        current_state = tracker.data.get("files", {}).get(rel, {}).get("status")

        if ok and current_state == "completed":
            completed += 1
        elif current_state == "skipped":
            skipped += 1
        else:
            failed += 1

    print("\nFolder download summary")
    print(f"Completed: {completed}")
    print(f"Skipped  : {skipped}")
    print(f"Failed   : {failed}")
    print(f"Status   : {status_file}")


def download_single_video(video_id_or_url: str, output_file: str = None, chunk_size: int = 1024, num_threads: int = 4, verbose: bool = False, cookie_file: str = None) -> None:
    """Main function to process video ID or URL and download the video file."""
    video_id = extract_drive_id(video_id_or_url)
    
    if verbose:
        print(f"[INFO] Extracted video ID: {video_id}")
    
    drive_url = f'https://drive.google.com/u/0/get_video_info?docid={video_id}&drive_originator_app=303'
    
    if verbose:
        print(f"[INFO] Accessing {drive_url}")

    request_cookies = load_cookies_from_file(cookie_file, verbose)

    response = requests.get(drive_url, cookies=request_cookies)
    page_content = response.text
    cookies = requests.cookies.RequestsCookieJar()
    cookies.update(request_cookies)
    cookies.update(response.cookies)

    video, title = get_video_url(page_content, verbose)

    filename = output_file if output_file else title

    if not filename:
        print("Unable to determine output filename.")
        return

    valid_filename = sanitize_filename(filename)
    
    if video:
        download_file(video, cookies, valid_filename, chunk_size, num_threads, verbose)
    else:
        print("Unable to retrieve the video URL. Ensure the video ID is correct and accessible.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Script to download videos from Google Drive.")
    parser.add_argument("video_id", nargs="?", type=str, help="The video ID from Google Drive or a full Google Drive URL (e.g., 'abc-Qt12kjmS21kjDm2kjd' or 'https://drive.google.com/file/d/ID/view').")
    parser.add_argument("-o", "--output", type=str, help="Optional output file name for the downloaded video (default: video name in gdrive).")
    parser.add_argument("-c", "--chunk_size", type=int, default=1024, help="Optional chunk size (in bytes) for downloading the video. Default is 1024 bytes.")
    parser.add_argument("-t", "--threads", type=int, default=4, choices=range(1, 17), help="Number of parallel download threads (1-16). Default is 4.")
    parser.add_argument("-v", "--verbose", action="store_true", help="Enable verbose mode.")
    parser.add_argument("--cookie-file", type=str, help="Path to browser-exported cookie JSON file for private/shared-with-me files.")
    parser.add_argument("--folder", type=str, help="Google Drive folder ID or full folder URL to download recursively.")
    parser.add_argument("--output-dir", type=str, default=".", help="Base output directory for --folder mode.")
    parser.add_argument("--auth-client-secrets", type=str, default="client_secret.json", help="Path to OAuth client secret JSON file for folder mode.")
    parser.add_argument("--auth-token-file", type=str, default="token.json", help="Path to OAuth token cache JSON file for folder mode.")
    parser.add_argument("--status-file", type=str, default="download_status.json", help="Path to JSON status file used by --folder mode.")
    parser.add_argument("--show-status", action="store_true", help="Print summary from --status-file and exit.")
    parser.add_argument("--version", action="version", version="%(prog)s 1.1.0")

    args = parser.parse_args()

    if args.show_status:
        print_status_summary(args.status_file)
        if not args.folder and not args.video_id:
            sys.exit(0)

    if args.folder:
        download_drive_folder(
            folder_input=args.folder,
            output_dir=args.output_dir,
            chunk_size=args.chunk_size,
            verbose=args.verbose,
            client_secrets_file=args.auth_client_secrets,
            token_file=args.auth_token_file,
            status_file=args.status_file,
            cookie_file=args.cookie_file,
        )
    elif args.video_id:
        download_single_video(args.video_id, args.output, args.chunk_size, args.threads, args.verbose, args.cookie_file)
    else:
        parser.error("Provide either video_id for single-file download or --folder for recursive folder download.")
