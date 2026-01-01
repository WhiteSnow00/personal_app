import os
import sys
import io
import time
import re
import json
import random
import string
import math
import shutil
import threading
import socket
import codecs
import subprocess
import tempfile
import hashlib
import concurrent.futures
import requests
from requests.exceptions import RequestException, ChunkedEncodingError
from bs4 import BeautifulSoup
import tkinter as tk
from tkinter import ttk, messagebox, filedialog
import sv_ttk
from urllib.parse import urljoin, unquote, urlparse
from base64 import b64decode
from math import floor
from tenacity import retry, wait_exponential, stop_after_attempt
from concurrent.futures import ThreadPoolExecutor
from collections import deque

if sys.stdout is not None and getattr(sys.stdout, "encoding", "").lower() != "utf-8":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

STOP_EVENT = threading.Event()
FILE_LOCK = threading.Lock()
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36",
    "Accept": "*/*",
    "Accept-Encoding": "identity",
    "Connection": "keep-alive"
}

SUPPORTED_FORMATS = [
    "jpg", "jpeg", "png", "gif", "webp", "mp4", "ts", "webm", "mpg", "mov",
    "zip", "rar", "pdf", "m4v", "mkv", "mp3", "mpeg", "avi", "jfif", "wmv",
    "7z", "tar", "flv", "m4a"
]

BUNKR_VS_API_URL = "https://bunkr.cr/api/vs"
SECRET_KEY_BASE = "SECRET_KEY_"

SUPPORTED_SITES = {
    "bunkr": ["bunkr.cr", "bunkr.ru", "bunkr.red", "bunkr.black", "bunkr.sk", "bunkr.media", "bunkr.ws", "bunkr.fi", "bunkr.ac", "bunkr.site", "bunkr.ph", "bunkr.pk", "bunkr.si"],
    "videzz": ["videzz.net", "vidoza.net"],
    "gofile": ["gofile.io"],
    "pixeldrain": ["pixeldrain.com"],
    "streamtape": ["streamtape.com"],
    "vtube": ["vtube.network", "vtbe.to"],
    "rubyvid": ["rubyvid.com", "stmruby.com", "rubystm.com"]
}

root = None
title_label = None
subtitle_label = None
url_frame = None
folder_frame = None
progress_frame = None
stats_frame = None
folder_button = None
download_button = None
theme_button = None
status_label = None
stats_text = None
url_entry = None
folder_entry = None
progress_var = None
last_clipboard = ""
clipboard_var = None
cancel_button = None
clipboard_status = None
clipboard_toggle = None
download_queue = deque()
is_downloading = False
prev_url_type = None

def ui_call(func, *args, **kwargs):
    if root:
        try:
            root.after(0, lambda: func(*args, **kwargs))
        except Exception:
            pass

def set_status(text):
    if status_label:
        ui_call(status_label.config, text=text)

def set_progress(value):
    if progress_var is not None:
        ui_call(progress_var.set, value)

def set_button_state(button, state):
    if button:
        ui_call(button.config, state=state)

def show_error(title, message):
    ui_call(messagebox.showerror, title, message)

def update_stats(stats_widget, message):
    def apply():
        stats_widget.config(state="normal")
        stats_widget.delete(1.0, tk.END)
        stats_widget.insert(tk.END, message)
        if download_queue:
            current_url = url_entry.get()
            queue_message = "\n\n" + "=" * 30 + "\n" + f"Current download:\n{current_url}\n\nQueued downloads: {len(download_queue)} files"
            stats_widget.insert(tk.END, queue_message)
        stats_widget.config(state="disabled")
    if stats_widget:
        ui_call(apply)

def update_theme_colors(stats_widget):
    current_theme = sv_ttk.get_theme()
    if current_theme == "dark":
        stats_widget.configure(bg="#2b2b2b", fg="#e0e0e0")
    else:
        stats_widget.configure(bg="#ffffff", fg="#000000")

def toggle_theme(stats_widget):
    sv_ttk.toggle_theme()
    update_theme_colors(stats_widget)

def make_progress_callback(label, update_progress=True):
    def _callback(downloaded, total, filename, speed=0):
        if STOP_EVENT.is_set():
            return
        if speed < 1024:
            speed_str = f"{speed:.2f} B/s"
        elif speed < 1024 * 1024:
            speed_str = f"{speed / 1024:.2f} KB/s"
        else:
            speed_str = f"{speed / (1024 * 1024):.2f} MB/s"
        if total > 0:
            if update_progress:
                set_progress(min(100, (downloaded / total) * 100))
            mb_downloaded = downloaded / (1024 * 1024)
            mb_total = total / (1024 * 1024)
            set_status(f"Downloading {label} ({mb_downloaded:.2f} / {mb_total:.2f} MB) - {speed_str}")
        else:
            mb_downloaded = downloaded / (1024 * 1024)
            set_status(f"Downloading {label} ({mb_downloaded:.2f} MB) - {speed_str}")
    return _callback

def smart_download(url, folder, filename, progress_callback_func, headers=None):
    try:
        os.makedirs(folder, exist_ok=True)
        final_path = os.path.join(folder, filename)
        temp_path = final_path + ".part"
        for attempt in range(5):
            try:
                request_headers = dict(HEADERS)
                if headers:
                    request_headers.update(headers)
                resume_byte_pos = 0
                mode = "wb"
                if os.path.exists(temp_path):
                    resume_byte_pos = os.path.getsize(temp_path)
                    if resume_byte_pos > 0:
                        request_headers["Range"] = f"bytes={resume_byte_pos}-"
                        mode = "ab"
                with requests.get(url, headers=request_headers, stream=True, timeout=(60, 3600)) as r:
                    if r.status_code == 416:
                        if os.path.exists(temp_path):
                            if os.path.exists(final_path):
                                os.remove(final_path)
                            os.rename(temp_path, final_path)
                        return True
                    if r.status_code not in (200, 206):
                        raise Exception(f"HTTP {r.status_code}")
                    total_size = int(r.headers.get("content-length", 0))
                    if r.status_code == 206:
                        content_range = r.headers.get("content-range")
                        if content_range and "/" in content_range:
                            total_size = int(content_range.split("/")[-1])
                        else:
                            total_size += resume_byte_pos
                    downloaded = resume_byte_pos
                    start_time = time.time()
                    session_downloaded = 0
                    with open(temp_path, mode) as f:
                        for chunk in r.iter_content(chunk_size=8192):
                            if STOP_EVENT.is_set():
                                return False
                            if chunk:
                                f.write(chunk)
                                downloaded += len(chunk)
                                session_downloaded += len(chunk)
                                elapsed_time = time.time() - start_time
                                speed = session_downloaded / elapsed_time if elapsed_time > 0 else 0
                                if progress_callback_func:
                                    progress_callback_func(downloaded, total_size, filename, speed)
                break
            except (RequestException, ChunkedEncodingError) as e:
                print(f"Download error: {e}")
                time.sleep(2)
                continue
        if os.path.exists(temp_path):
            if os.path.exists(final_path):
                os.remove(final_path)
            os.rename(temp_path, final_path)
            return True
        return False
    except Exception as e:
        print(f"Download error: {e}")
        return False

def clean_filename(name):
    if not name:
        return "untitled"
    name = re.sub(r'[<>:"/\\|?*]', "-", name)
    name = re.sub(r"[\0-\31]", "", name)
    return name.strip()

def extract_album_title(url):
    try:
        headers = {
            "User-Agent": HEADERS["User-Agent"],
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Accept-Encoding": "gzip, deflate, br",
            "Referer": "https://bunkr.cr/",
            "Connection": "keep-alive"
        }
        response = requests.get(url, headers=headers, timeout=30)
        soup = BeautifulSoup(response.text, "html.parser")
        is_single_file = "/f/" in url or "/v/" in url
        if is_single_file:
            media_element = soup.find(["img", "video"], class_="max-h-full")
            if media_element:
                if media_element.name == "video":
                    source = media_element.find("source")
                    src = source["src"] if source else media_element.get("src")
                else:
                    src = media_element.get("src")
                if src:
                    file_name = src.split("/")[-1].split(".")[0]
                    return clean_filename(unquote(file_name))
        title_tag = soup.find("title")
        if title_tag:
            title = title_tag.text.split("|")[0].strip()
            title = unquote(title)
            return clean_filename(title)
        return "Video"
    except Exception as e:
        print(f"Error extracting album title: {e}")
        return "Video"

def get_url_data(url):
    parsed_url = urlparse(url)
    return {
        "file_name": os.path.basename(parsed_url.path),
        "extension": os.path.splitext(parsed_url.path)[1].lower().lstrip("."),
        "hostname": parsed_url.hostname
    }

@retry(stop=stop_after_attempt(5), wait=wait_exponential(multiplier=1, min=2, max=10))
def get_encryption_data(session, slug):
    response = session.post(BUNKR_VS_API_URL, json={"slug": slug})
    if response.status_code != 200:
        raise RequestException(f"HTTP {response.status_code}")
    return json.loads(response.content)

def decrypt_encrypted_url(encryption_data):
    try:
        secret_key = f"{SECRET_KEY_BASE}{floor(encryption_data['timestamp'] / 3600)}"
        encrypted_url_bytearray = list(b64decode(encryption_data["url"]))
        secret_key_byte_array = list(secret_key.encode("utf-8"))
        decrypted_url = ""
        for i in range(len(encrypted_url_bytearray)):
            decrypted_url += chr(encrypted_url_bytearray[i] ^ secret_key_byte_array[i % len(secret_key_byte_array)])
        return decrypted_url
    except Exception:
        return None

@retry(stop=stop_after_attempt(5), wait=wait_exponential(multiplier=1, min=2, max=10))
def get_real_download_url(session, url, item_name=None):
    url = url if "https" in url else f"https://bunkr.cr{url}"
    match = re.search(r"/[fv]/(.*?)$", url)
    if not match:
        return {"url": url, "name": item_name}
    slug = unquote(match.group(1))
    enc_data = get_encryption_data(session, slug)
    decrypted = decrypt_encrypted_url(enc_data)
    if not decrypted:
        raise RequestException("Failed to decrypt URL")
    return {"url": decrypted, "name": item_name}

def extract_links(url, session):
    try:
        items = []
        if "/f/" in url or "/v/" in url:
            vid_name = None
            try:
                r = session.get(url, timeout=20)
                if r.status_code == 200:
                    soup = BeautifulSoup(r.content, "html.parser")
                    title_tag = soup.find("h1", {"class": "text-[20px]"}) or soup.find("h1", {"class": "truncate"}) or soup.find("h1", id="title")
                    if title_tag:
                        vid_name = title_tag.text.strip()
            except Exception:
                pass
            real_item = get_real_download_url(session, url, vid_name)
            if real_item and real_item.get("url"):
                ext = get_url_data(real_item["url"])["extension"]
                if ext in SUPPORTED_FORMATS or not ext:
                    items.append(real_item)
            return items
        response = session.get(url, timeout=20)
        if response.status_code != 200:
            return []
        soup = BeautifulSoup(response.content, "html.parser")
        direct_link = soup.find("span", {"class": "ic-videos"}) is not None or soup.find("div", {"class": "lightgallery"}) is not None
        if direct_link:
            real_item = get_real_download_url(session, url, None)
            if real_item and real_item.get("url"):
                ext = get_url_data(real_item["url"])["extension"]
                if ext in SUPPORTED_FORMATS or not ext:
                    items.append(real_item)
            return items
        the_items = soup.find_all("div", {"class": "theItem"})
        if not the_items:
            the_items = soup.find_all("div", class_=re.compile(r"item|grid"))
        if the_items:
            for the_item in the_items:
                a_tag = the_item.find("a")
                if not a_tag:
                    continue
                link = a_tag.get("href")
                name_tag = the_item.find("p") or the_item.find("span", {"class": "name"})
                name_text = name_tag.text if name_tag else None
                real_item = get_real_download_url(session, link, name_text)
                if real_item and real_item.get("url"):
                    ext = get_url_data(real_item["url"])["extension"]
                    if ext in SUPPORTED_FORMATS or not ext:
                        items.append(real_item)
        if not items:
            potential_links = soup.find_all("a", href=re.compile(r"/f/[a-zA-Z0-9]+|/v/[a-zA-Z0-9]+"))
            seen_links = set()
            for a in potential_links:
                href = a.get("href")
                if not href or href in seen_links:
                    continue
                seen_links.add(href)
                name = a.get_text(strip=True)
                if not name:
                    img = a.find("img")
                    if img and "alt" in img.attrs:
                        name = img["alt"]
                real_item = get_real_download_url(session, href, name)
                if real_item and real_item.get("url"):
                    ext = get_url_data(real_item["url"])["extension"]
                    if ext in SUPPORTED_FORMATS or not ext:
                        items.append(real_item)
        return items
    except Exception as e:
        print(f"Error extracting links: {e}")
        return []

def load_history(folder):
    path = os.path.join(folder, "success.txt")
    if not os.path.exists(path):
        return set()
    with FILE_LOCK:
        with open(path, "r", encoding="utf-8") as f:
            return set(line.strip() for line in f if line.strip())

def append_history(folder, url):
    path = os.path.join(folder, "success.txt")
    with FILE_LOCK:
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"{url}\n")

def build_download_filename(name, direct_url):
    parsed_path = urlparse(direct_url).path
    ext = os.path.splitext(parsed_path)[1]
    if name:
        safe_name = clean_filename(name)
        if ext and not safe_name.lower().endswith(ext.lower()):
            return f"{safe_name}{ext}"
        return safe_name
    file_name = os.path.basename(parsed_path)
    return file_name if file_name else f"file{ext}"

def get_file_size(url, headers):
    try:
        response = requests.head(url, headers=headers, timeout=20)
        if response.status_code == 200:
            return int(response.headers.get("content-length", 0))
        return 0
    except Exception:
        return 0

def download_from_bunkr(url, base_folder):
    try:
        session = requests.Session()
        session.headers.update({
            "User-Agent": HEADERS["User-Agent"],
            "Referer": "https://bunkr.cr/",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Accept-Encoding": "gzip, deflate, br",
            "Connection": "keep-alive"
        })
        album_title = clean_filename(extract_album_title(url))
        bunkr_folder = os.path.join(base_folder, "Bunkr")
        os.makedirs(bunkr_folder, exist_ok=True)
        folder = os.path.join(bunkr_folder, album_title)
        os.makedirs(folder, exist_ok=True)
        set_status(f"Collecting links for '{album_title}'...")
        items = extract_links(url, session)
        if not items:
            set_status("No files found to download.")
            set_button_state(download_button, "normal")
            set_button_state(cancel_button, "disabled")
            return
        history = load_history(folder)
        image_exts = {"jpg", "jpeg", "png", "gif", "webp", "jfif"}
        video_exts = {"mp4", "ts", "webm", "mpg", "mov", "m4v", "mkv", "mpeg", "avi", "wmv", "flv"}
        pending = []
        images = 0
        videos = 0
        others = 0
        for item in items:
            item_url = item.get("url")
            if not item_url or item_url in history:
                continue
            ext = get_url_data(item_url)["extension"]
            if ext in image_exts:
                images += 1
            elif ext in video_exts:
                videos += 1
            else:
                others += 1
            pending.append((item, ext))
        if not pending:
            set_status("No new files to download.")
            set_button_state(download_button, "normal")
            set_button_state(cancel_button, "disabled")
            return
        total_files = len(pending)
        completed = 0
        successful = 0
        failed = 0
        def download_item(item, ext):
            if STOP_EVENT.is_set():
                return False, item.get("url"), ext
            item_url = item.get("url")
            filename = build_download_filename(item.get("name"), item_url)
            headers = {"Referer": "https://bunkr.cr/"}
            ok = smart_download(item_url, folder, filename, make_progress_callback(filename, update_progress=False), headers=headers)
            return ok, item_url, ext
        with ThreadPoolExecutor(max_workers=3) as executor:
            futures = [executor.submit(download_item, item, ext) for item, ext in pending]
            for future in concurrent.futures.as_completed(futures):
                if STOP_EVENT.is_set():
                    break
                try:
                    ok, item_url, ext = future.result()
                except Exception as e:
                    print(f"Error downloading item: {e}")
                    ok = False
                    item_url = None
                completed += 1
                set_progress((completed / total_files) * 100)
                if ok:
                    successful += 1
                    if item_url:
                        append_history(folder, item_url)
                else:
                    failed += 1
        set_button_state(download_button, "normal")
        set_button_state(cancel_button, "disabled")
        if STOP_EVENT.is_set():
            set_status("Download cancelled")
            return
        set_status("Download completed!")
        update_stats(stats_text, f"Download completed.\nTotal Images: {images}\nTotal Videos: {videos}\nSuccessful: {successful}\nFailed: {failed}")
        download_complete_callback()
    except Exception as e:
        print(f"Error downloading from Bunkr: {e}")
        set_status(f"Error: {str(e)}")
        set_button_state(download_button, "normal")
        set_button_state(cancel_button, "disabled")

def parse_gofile_content_id(url):
    parsed = urlparse(url)
    if "/d/" in parsed.path:
        parts = parsed.path.strip("/").split("/")
        for i, part in enumerate(parts):
            if part == "d" and i + 1 < len(parts):
                return parts[i + 1]
    return url.strip().split("/")[-1]

def get_gofile_token():
    headers = {
        "User-Agent": HEADERS["User-Agent"],
        "Accept-Encoding": "gzip, deflate, br",
        "Accept": "*/*",
        "Connection": "keep-alive"
    }
    try:
        response = requests.post("https://api.gofile.io/accounts", headers=headers).json()
        if response.get("status") == "ok":
            return response["data"]["token"]
        return None
    except Exception as e:
        print(f"Error getting GoFile token: {e}")
        return None

def get_gofile_content_data(content_id, token, password=None):
    api_url = f"https://api.gofile.io/contents/{content_id}?cache=true&sortField=createTime&sortDirection=1"
    if password:
        hashed = hashlib.sha256(password.encode()).hexdigest()
        api_url = f"{api_url}&password={hashed}"
    headers = {
        "User-Agent": HEADERS["User-Agent"],
        "Accept-Encoding": "gzip, deflate, br",
        "Accept": "*/*",
        "Connection": "keep-alive",
        "Authorization": f"Bearer {token}",
        "Referer": "https://gofile.io/",
        "Origin": "https://gofile.io"
    }
    response = requests.get(api_url, headers=headers, timeout=30).json()
    if response.get("status") != "ok":
        return None
    return response.get("data")

def recursive_extract_gofile_links(content_id, token, password=None, parent_path="", is_root=False):
    data = get_gofile_content_data(content_id, token, password)
    if not data:
        return []
    if "password" in data and data.get("passwordStatus") and data.get("passwordStatus") != "passwordOk":
        return []
    links = []
    if data.get("type") != "folder":
        links.append({"url": data.get("link"), "name": data.get("name"), "rel_path": parent_path})
        return links
    current_path = parent_path
    if not is_root:
        folder_name = data.get("name") or ""
        current_path = os.path.join(parent_path, folder_name) if parent_path else folder_name
    children = data.get("children", {})
    for child in children.values():
        if child.get("type") == "folder":
            links.extend(recursive_extract_gofile_links(child.get("id"), token, password, current_path, False))
        else:
            links.append({"url": child.get("link"), "name": child.get("name"), "rel_path": current_path})
    return links

def show_password_dialog():
    password_window = tk.Toplevel(root)
    password_window.title("GoFile password required")
    window_width = 420
    window_height = 160
    screen_width = root.winfo_screenwidth()
    screen_height = root.winfo_screenheight()
    center_x = int(screen_width / 2 - window_width / 2)
    center_y = int(screen_height / 2 - window_height / 2)
    password_window.geometry(f"{window_width}x{window_height}+{center_x}+{center_y}")
    password_window.resizable(False, False)
    password_window.grab_set()
    sv_ttk.set_theme(sv_ttk.get_theme())
    container = ttk.Frame(password_window, padding=20)
    container.pack(fill=tk.BOTH, expand=True)
    password_label = ttk.Label(
        container,
        text="This GoFile link is password protected. Enter password:",
        font=("Segoe UI", 10)
    )
    password_label.pack(pady=(0, 10))
    password_var = tk.StringVar()
    password_entry = ttk.Entry(
        container,
        font=("Segoe UI", 10),
        show="*",
        textvariable=password_var
    )
    password_entry.pack(fill=tk.X, pady=(0, 20))
    password_entry.focus_set()
    button_frame = ttk.Frame(container)
    button_frame.pack(fill=tk.X)
    result = [None]
    def on_ok():
        result[0] = password_var.get()
        password_window.destroy()
    def on_cancel():
        password_window.destroy()
    ok_button = ttk.Button(
        button_frame,
        text="OK",
        command=on_ok,
        style="Accent.TButton",
        width=15
    )
    ok_button.pack(side=tk.RIGHT, padx=5)
    cancel_btn = ttk.Button(
        button_frame,
        text="Cancel",
        command=on_cancel,
        style="Secondary.TButton",
        width=15
    )
    cancel_btn.pack(side=tk.RIGHT, padx=5)
    password_entry.bind("<Return>", lambda event: on_ok())
    password_window.wait_window()
    return result[0]

def download_from_gofile(url, base_folder, status_label=None, progress_var=None):
    token = get_gofile_token()
    if not token:
        set_status("Failed to get GoFile access token")
        set_button_state(download_button, "normal")
        set_button_state(cancel_button, "disabled")
        return
    content_id = parse_gofile_content_id(url)
    if not content_id:
        set_status("Invalid GoFile URL format")
        set_button_state(download_button, "normal")
        set_button_state(cancel_button, "disabled")
        return
    set_status("Collecting links from GoFile...")
    data = get_gofile_content_data(content_id, token, None)
    password = None
    if not data:
        set_status("No files found to download from GoFile")
        set_button_state(download_button, "normal")
        set_button_state(cancel_button, "disabled")
        return
    if "password" in data and data.get("passwordStatus") and data.get("passwordStatus") != "passwordOk":
        password = show_password_dialog()
        if not password:
            set_status("Password required to continue")
            set_button_state(download_button, "normal")
            set_button_state(cancel_button, "disabled")
            return
        data = get_gofile_content_data(content_id, token, password)
        if not data:
            set_status("No files found to download from GoFile")
            set_button_state(download_button, "normal")
            set_button_state(cancel_button, "disabled")
            return
    root_name = clean_filename(data.get("name") or "GoFile")
    file_links = recursive_extract_gofile_links(content_id, token, password, "", True)
    if not file_links:
        set_status("No files found to download from GoFile")
        set_button_state(download_button, "normal")
        set_button_state(cancel_button, "disabled")
        return
    main_gofile_folder = os.path.join(base_folder, "GoFile")
    os.makedirs(main_gofile_folder, exist_ok=True)
    if len(file_links) <= 2:
        target_folder = main_gofile_folder
    else:
        target_folder = os.path.join(main_gofile_folder, root_name)
        os.makedirs(target_folder, exist_ok=True)
    history = load_history(target_folder)
    total_files = len(file_links)
    completed = 0
    successful = 0
    failed = 0
    pending_links = []
    for file_info in file_links:
        file_url = file_info.get("url")
        if file_url and file_url in history:
            successful += 1
            completed += 1
        else:
            pending_links.append(file_info)
    if total_files > 0 and completed:
        set_progress((completed / total_files) * 100)
    download_headers = {
        "Cookie": f"accountToken={token}",
        "Referer": "https://gofile.io/",
        "Origin": "https://gofile.io"
    }
    def download_item(file_info):
        file_url = file_info.get("url")
        if STOP_EVENT.is_set():
            return False, file_url
        rel_path = file_info.get("rel_path") or ""
        folder = target_folder
        if rel_path and len(file_links) > 2:
            folder = os.path.join(target_folder, rel_path)
            os.makedirs(folder, exist_ok=True)
        filename = clean_filename(file_info.get("name") or "file")
        ok = smart_download(file_url, folder, filename, make_progress_callback(filename, update_progress=False), headers=download_headers)
        return ok, file_url
    with ThreadPoolExecutor(max_workers=5) as executor:
        futures = [executor.submit(download_item, file_info) for file_info in pending_links]
        for future in concurrent.futures.as_completed(futures):
            if STOP_EVENT.is_set():
                break
            ok = False
            file_url = None
            try:
                ok, file_url = future.result()
            except Exception as e:
                print(f"Error downloading GoFile item: {e}")
                ok = False
            completed += 1
            set_progress((completed / total_files) * 100)
            if ok:
                successful += 1
                if file_url:
                    append_history(target_folder, file_url)
            else:
                failed += 1
    set_button_state(download_button, "normal")
    set_button_state(cancel_button, "disabled")
    if STOP_EVENT.is_set():
        set_status("Download cancelled")
        return
    set_status("Download completed!")
    update_stats(stats_text, f"Download completed.\nTotal Files: {total_files}\nSuccessful: {successful}\nFailed: {failed}")
    download_complete_callback()

def extract_videzz_video(url, headers=None):
    if not headers:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.5",
            "Connection": "keep-alive"
        }
    try:
        response = requests.get(url, headers=headers)
        if response.status_code != 200:
            print(f"Error fetching videzz URL: {response.status_code}")
            return None
        video_pattern = r'sourcesCode:\s*\[\{\s*src:\s*"([^"]+)",\s*type:'
        match = re.search(video_pattern, response.text)
        if match:
            video_url = match.group(1)
            return video_url
        soup = BeautifulSoup(response.text, "html.parser")
        scripts = soup.find_all("script")
        for script in scripts:
            if script.string and "sourcesCode" in script.string:
                video_pattern = r'sourcesCode:\s*\[\{\s*src:\s*"([^"]+)",\s*type:'
                match = re.search(video_pattern, script.string)
                if match:
                    video_url = match.group(1)
                    return video_url
        return None
    except Exception as e:
        print(f"Error extracting videzz video: {e}")
        return None

def is_videzz_url(url):
    parsed_url = urlparse(url)
    return any(domain in parsed_url.netloc for domain in SUPPORTED_SITES["videzz"])

def is_bunkr_url(url):
    parsed_url = urlparse(url)
    return any(domain in parsed_url.netloc for domain in SUPPORTED_SITES["bunkr"])

def is_gofile_url(url):
    parsed_url = urlparse(url)
    return any(domain in parsed_url.netloc for domain in SUPPORTED_SITES["gofile"])

def generate_random_filename(extension):
    letters = string.ascii_lowercase + string.digits
    random_name = "".join(random.choice(letters) for i in range(12))
    return f"{random_name}.{extension}"

def download_from_videzz(url, base_folder):
    set_status("Extracting video from videzz.net...")
    vidoza_folder = os.path.join(base_folder, "Vidoza")
    if not os.path.exists(vidoza_folder):
        os.makedirs(vidoza_folder)
    video_url = extract_videzz_video(url)
    if not video_url:
        set_status("Could not extract video URL")
        set_button_state(download_button, "normal")
        set_button_state(cancel_button, "disabled")
        download_complete_callback()
        return
    original_extension = video_url.split("/")[-1].split(".")[-1]
    random_filename = generate_random_filename(original_extension)
    set_status(f"Downloading video to {random_filename}")
    if smart_download(video_url, vidoza_folder, random_filename, make_progress_callback(random_filename)):
        set_status("Video downloaded successfully")
        update_stats(stats_text, f"Download completed.\nVideo: {random_filename}\nSaved to: Vidoza folder")
    else:
        set_status("Error downloading video")
    set_button_state(download_button, "normal")
    set_button_state(cancel_button, "disabled")
    download_complete_callback()

def is_pixeldrain_url(url):
    parsed_url = urlparse(url)
    return "pixeldrain.com" in parsed_url.netloc

def extract_pixeldrain_info(url):
    headers = {
        "User-Agent": HEADERS["User-Agent"],
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "keep-alive",
        "Referer": "https://pixeldrain.com/",
        "sec-ch-ua": '"Brave";v="135", "Not-A.Brand";v="8", "Chromium";v="135"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-User": "?1",
        "Sec-GPC": "1",
        "Upgrade-Insecure-Requests": "1"
    }
    try:
        url_id = url.split("/")[-1]
        response = requests.get(url, headers=headers)
        response.raise_for_status()
        pattern = r"window\.viewer_data\s*=\s*({.*?});"
        match = re.search(pattern, response.text, re.DOTALL)
        if match:
            viewer_data = json.loads(match.group(1))
            api_response = viewer_data.get("api_response", {})
            if "files" in api_response:
                album_id = api_response.get("id")
                album_title = api_response.get("title", "Untitled Album")
                files = api_response.get("files", [])
                file_urls = []
                for file in files:
                    file_id = file.get("id")
                    file_name = file.get("name")
                    if file_id:
                        file_url = f"https://pixeldrain.com/api/file/{file_id}"
                        file_name = file_name.replace("+", " ")
                        file_urls.append({
                            "url": file_url,
                            "name": file_name
                        })
                return {
                    "type": "album",
                    "album_id": album_id,
                    "album_title": album_title,
                    "file_urls": file_urls
                }
            file_id = api_response.get("id")
            file_name = api_response.get("name", "Untitled File")
            if file_id:
                file_url = f"https://pixeldrain.com/api/file/{file_id}"
                file_name = file_name.replace("+", " ")
                return {
                    "type": "file",
                    "file_id": file_id,
                    "file_name": file_name,
                    "file_url": file_url
                }
        else:
            soup = BeautifulSoup(response.text, "html.parser")
            title_tag = soup.find("title")
            if title_tag:
                title = title_tag.text.split("|")[0].strip()
                return {
                    "type": "file",
                    "file_id": url_id,
                    "file_name": title,
                    "file_url": f"https://pixeldrain.com/api/file/{url_id}"
                }
            return None
    except Exception as e:
        print(f"Error extracting PixelDrain info: {e}")
        return None

def download_from_pixeldrain(url, base_folder, status_label=None, progress_var=None):
    try:
        pixeldrain_folder = os.path.join(base_folder, "Pixeldrain")
        if not os.path.exists(pixeldrain_folder):
            os.makedirs(pixeldrain_folder)
        result = extract_pixeldrain_info(url)
        if not result:
            set_status("Could not extract information from PixelDrain URL")
            set_button_state(download_button, "normal")
            set_button_state(cancel_button, "disabled")
            return
        if result["type"] == "album":
            album_folder = os.path.join(pixeldrain_folder, result["album_title"])
            os.makedirs(album_folder, exist_ok=True)
            total_files = len(result["file_urls"])
            successful = 0
            failed = 0
            for i, file_info in enumerate(result["file_urls"], 1):
                if STOP_EVENT.is_set():
                    set_status("Download cancelled")
                    break
                set_status(f"Downloading from PixelDrain ({i}/{total_files}): {file_info['name']}")
                set_progress(0)
                if smart_download(file_info["url"], album_folder, file_info["name"], make_progress_callback(file_info["name"])):
                    successful += 1
                else:
                    failed += 1
                set_progress((i / total_files) * 100)
            set_status("Download completed!")
            update_stats(stats_text, f"Download completed.\nTotal Files: {total_files}\nSuccessful: {successful}\nFailed: {failed}")
        else:
            set_status(f"Downloading from PixelDrain: {result['file_name']}")
            set_progress(0)
            if smart_download(result["file_url"], pixeldrain_folder, result["file_name"], make_progress_callback(result["file_name"])):
                set_status("Download completed!")
                update_stats(stats_text, f"Download completed.\nFile: {result['file_name']}")
            else:
                set_status("Error downloading file")
        set_button_state(download_button, "normal")
        set_button_state(cancel_button, "disabled")
        download_complete_callback()
    except Exception as e:
        print(f"Error downloading from PixelDrain: {e}")
        set_status(f"Error: {str(e)}")
        set_button_state(download_button, "normal")
        set_button_state(cancel_button, "disabled")

def is_streamtape_url(url):
    parsed_url = urlparse(url)
    return any(domain in parsed_url.netloc for domain in SUPPORTED_SITES["streamtape"])

def download_chunk(video_url, start, end, chunk_num, headers):
    range_headers = dict(headers)
    range_headers["Range"] = f"bytes={start}-{end}"
    response = requests.get(video_url, headers=range_headers, stream=True)
    response.raise_for_status()
    return chunk_num, response.content

def download_from_streamtape(url, base_folder, status_label=None, progress_var=None, retry_count=0):
    try:
        streamtape_folder = os.path.join(base_folder, "Streamtape")
        if not os.path.exists(streamtape_folder):
            os.makedirs(streamtape_folder)
        headers = {
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.6",
            "Connection": "keep-alive",
            "Host": "streamtape.com",
            "sec-ch-ua": '"Brave";v="135", "Not-A.Brand";v="8", "Chromium";v="135"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Windows"',
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "none",
            "Sec-Fetch-User": "?1",
            "Sec-GPC": "1",
            "Upgrade-Insecure-Requests": "1",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36"
        }
        set_status("Extracting video from Streamtape...")
        response = requests.get(url, headers=headers)
        response.raise_for_status()
        video_id = url.split("/")[-1]
        soup = BeautifulSoup(response.text, "html.parser")
        scripts = soup.find_all("script")
        video_url_part = None
        for script in scripts:
            if script.string and "get_video" in script.string:
                match = re.search(r"get_video\?id=([^&']+)&expires=([^&']+)&ip=([^&']+)&token=([^&']+)", script.string)
                if match:
                    video_id, expires, ip, token = match.groups()
                    video_id = video_id.replace('" + \'\'+ (\'xcd', "").replace("\')", "")
                    video_url_part = f"/get_video?id={video_id}&expires={expires}&ip={ip}&token={token}"
                    break
        if not video_url_part:
            alt_url = url.replace("/e/", "/v/") if "/e/" in url else url.replace("/v/", "/e/")
            alt_response = requests.get(alt_url, headers=headers)
            alt_response.raise_for_status()
            soup = BeautifulSoup(alt_response.text, "html.parser")
            scripts = soup.find_all("script")
            for script in scripts:
                if script.string and "get_video" in script.string:
                    match = re.search(r"get_video\?id=([^&']+)&expires=([^&']+)&ip=([^&']+)&token=([^&']+)", script.string)
                    if match:
                        video_id, expires, ip, token = match.groups()
                        video_id = video_id.replace('" + \'\'+ (\'xcd', "").replace("\')", "")
                        video_url_part = f"/get_video?id={video_id}&expires={expires}&ip={ip}&token={token}"
                        break
        if not video_url_part:
            if retry_count < 1:
                set_status("Could not extract video URL, retrying...")
                time.sleep(2)
                return download_from_streamtape(url, base_folder, status_label, progress_var, retry_count + 1)
            set_status("Could not extract video URL from Streamtape after retry")
            set_button_state(download_button, "normal")
            set_button_state(cancel_button, "disabled")
            return
        video_url = f"https://streamtape.com{video_url_part}&stream=1"
        title_meta = soup.find("meta", {"name": "og:title"})
        if title_meta:
            filename = title_meta["content"]
        else:
            filename = "video.mp4"
        set_status(f"Downloading: {filename}")
        head_response = requests.head(video_url, headers=headers)
        total_size = int(head_response.headers.get("content-length", 0))
        if total_size == 0:
            if retry_count < 1:
                set_status("Could not determine video size, retrying...")
                time.sleep(2)
                return download_from_streamtape(url, base_folder, status_label, progress_var, retry_count + 1)
            set_status("Could not determine video size, falling back to single download")
            video_response = requests.get(video_url, headers=headers, stream=True)
            video_response.raise_for_status()
            file_path = os.path.join(streamtape_folder, filename)
            downloaded = 0
            start_time = time.time()
            last_update_time = start_time
            with open(file_path, "wb") as f:
                for chunk in video_response.iter_content(chunk_size=8192):
                    if chunk:
                        f.write(chunk)
                        downloaded += len(chunk)
                        current_time = time.time()
                        if current_time - last_update_time > 0.5:
                            last_update_time = current_time
                            mb = downloaded / (1024 * 1024)
                            elapsed = current_time - start_time
                            speed = mb / elapsed if elapsed > 0 else 0
                            set_status(f"Downloading: {filename} - {mb:.2f} MB ({speed:.2f} MB/s)")
            set_status(f"Download completed! File saved as: {filename}")
            return
        num_chunks = 8
        chunk_size = math.ceil(total_size / num_chunks)
        set_status(f"Downloading {filename} ({total_size / (1024*1024):.2f} MB) using {num_chunks} parallel connections")
        temp_files = []
        for i in range(num_chunks):
            start_byte = i * chunk_size
            end_byte = min(start_byte + chunk_size - 1, total_size - 1)
            temp_files.append((start_byte, end_byte, tempfile.NamedTemporaryFile(delete=False)))
        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=num_chunks) as executor:
                future_to_chunk = {
                    executor.submit(download_chunk, video_url, start, end, i, headers): i
                    for i, (start, end, _) in enumerate(temp_files)
                }
                for future in concurrent.futures.as_completed(future_to_chunk):
                    chunk_num, chunk_data = future.result()
                    with open(temp_files[chunk_num][2].name, "wb") as f:
                        f.write(chunk_data)
                    set_progress(((chunk_num + 1) / num_chunks) * 100)
            set_status("Merging chunks...")
            file_path = os.path.join(streamtape_folder, filename)
            with open(file_path, "wb") as f:
                for _, _, temp_file in temp_files:
                    with open(temp_file.name, "rb") as chunk_file:
                        f.write(chunk_file.read())
            set_status(f"Download completed! File saved as: {filename}")
        except Exception as e:
            if retry_count < 1:
                set_status(f"Error during download, retrying... ({str(e)})")
                time.sleep(2)
                return download_from_streamtape(url, base_folder, status_label, progress_var, retry_count + 1)
            raise e
        finally:
            for _, _, temp_file in temp_files:
                try:
                    os.unlink(temp_file.name)
                except Exception:
                    pass
        set_button_state(download_button, "normal")
        set_button_state(cancel_button, "disabled")
        download_complete_callback()
    except Exception as e:
        if retry_count < 1:
            set_status(f"Error downloading from Streamtape, retrying... ({str(e)})")
            time.sleep(2)
            return download_from_streamtape(url, base_folder, status_label, progress_var, retry_count + 1)
        print(f"Error downloading from Streamtape: {e}")
        set_status(f"Error: {str(e)}")
        set_button_state(download_button, "normal")
        set_button_state(cancel_button, "disabled")

def sanitize_url(url):
    try:
        url = url.strip()
        if not url.startswith(("http://", "https://")):
            url = "https://" + url
        parsed = urlparse(url)
        path = parsed.path
        query = f"?{parsed.query}" if parsed.query else ""
        sanitized_url = f"{parsed.scheme}://{parsed.netloc}{path}{query}"
        return sanitized_url
    except Exception as e:
        print(f"URL sanitization error: {e}")
        return url

def start_download_thread():
    download_thread = threading.Thread(target=start_download)
    download_thread.daemon = True
    download_thread.start()

def on_start_button_click():
    STOP_EVENT.clear()

    if download_queue:
        if not is_downloading:
            if not folder_entry.get().strip():
                show_error("Error", "Please select a main folder.")
                return
            process_download_queue()
        return

    start_download_thread()

def cancel_download_process():
    STOP_EVENT.set()
    set_status("Download cancelled")
    set_button_state(download_button, "normal")
    set_button_state(cancel_button, "disabled")

def process_download_queue():
    global is_downloading, prev_url_type
    if is_downloading or not download_queue:
        return
    is_downloading = True
    url = download_queue.popleft()
    if is_gofile_url(url):
        curr_type = "gofile"
    elif is_bunkr_url(url):
        curr_type = "bunkr"
    elif is_videzz_url(url):
        curr_type = "videzz"
    elif is_pixeldrain_url(url):
        curr_type = "pixeldrain"
    else:
        curr_type = "other"
    if prev_url_type == "gofile" and curr_type == "gofile":
        for remaining in range(2, 0, -1):
            msg = f"Waiting {remaining} seconds before next GoFile download..."
            set_status(msg)
            update_stats(stats_text, msg)
            time.sleep(1)
    prev_url_type = curr_type
    url_entry.delete(0, tk.END)
    url_entry.insert(0, url)
    current_text = stats_text.get("1.0", tk.END).strip()
    if current_text:
        update_stats(stats_text, current_text)
    download_thread = threading.Thread(target=start_download)
    download_thread.daemon = True
    download_thread.start()

def download_complete_callback():
    global is_downloading
    is_downloading = False
    set_button_state(download_button, "normal")
    set_button_state(cancel_button, "disabled")
    if download_queue:
        def schedule():
            root.after(1000, process_download_queue)
        ui_call(schedule)
    else:
        set_status("Download queue is empty")
        update_stats(stats_text, "Download queue is empty")

def start_download():
    STOP_EVENT.clear()
    url = sanitize_url(url_entry.get())
    if not url:
        show_error("Error", "Please enter a URL.")
        return
    base_folder = folder_entry.get()
    if not base_folder:
        show_error("Error", "Please select a main folder.")
        return
    set_button_state(download_button, "disabled")
    set_button_state(cancel_button, "normal")
    set_progress(0)
    if is_bunkr_url(url):
        download_from_bunkr(url, base_folder)
    elif is_videzz_url(url):
        download_from_videzz(url, base_folder)
    elif is_gofile_url(url):
        download_from_gofile(url, base_folder, status_label, progress_var)
    elif is_pixeldrain_url(url):
        download_from_pixeldrain(url, base_folder, status_label, progress_var)
    elif is_streamtape_url(url):
        download_from_streamtape(url, base_folder, status_label, progress_var)
    elif is_vtube_url(url):
        download_from_vtube(url, base_folder, status_label, progress_var)
    elif is_rubyvid_url(url):
        download_from_rubyvid(url, base_folder, status_label, progress_var)
    else:
        set_status("Unsupported URL type")
        set_button_state(download_button, "normal")
        set_button_state(cancel_button, "disabled")

def select_folder(folder_entry):
    folder = filedialog.askdirectory()
    if folder:
        folder_entry.delete(0, tk.END)
        folder_entry.insert(0, folder)

def clear_download_queue():
    download_queue.clear()
    set_status("Download queue cleared.")
    update_queue_status()
    update_stats(stats_text, "Queue cleared")

def load_batch_file():
    file_path = filedialog.askopenfilename(
        filetypes=[("Text Files", "*.txt"), ("All Files", "*.*")]
    )
    if not file_path:
        return
    url_entry.delete(0, tk.END)
    url_entry.insert(0, file_path)
    try:
        with open(file_path, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
    except Exception as e:
        print(f"Error reading batch file: {e}")
        set_status("Failed to read batch file")
        return
    urls = re.findall(r'https?://[^\s,;"\'<>]+', content)
    added_count = 0
    skipped_count = 0
    for raw_url in urls:
        sanitized = sanitize_url(raw_url)
        if not sanitized:
            continue
        if sanitized not in download_queue:
            download_queue.append(sanitized)
            added_count += 1
        else:
            skipped_count += 1
    set_status(f"Added {added_count} links ({skipped_count} duplicates skipped).")
    update_queue_status()
    if not folder_entry.get().strip():
        return
    if not is_downloading:
        process_download_queue()

def check_clipboard():
    try:
        global last_clipboard, clipboard_var
        try:
            if not root.winfo_exists():
                return
        except Exception:
            return
        current_clipboard = root.clipboard_get()
        if current_clipboard != last_clipboard:
            lines = current_clipboard.strip().split("\n")
            valid_urls = []
            for line in lines:
                line = line.strip()
                supported_url = False
                if any(domain in line for domain in SUPPORTED_SITES["bunkr"]) and ("https://" in line or "http://" in line):
                    supported_url = True
                elif any(domain in line for domain in SUPPORTED_SITES["videzz"]) and ("https://" in line or "http://" in line):
                    supported_url = True
                elif any(domain in line for domain in SUPPORTED_SITES["gofile"]) and ("https://" in line or "http://" in line):
                    supported_url = True
                elif "pixeldrain.com" in line and ("https://" in line or "http://" in line):
                    supported_url = True
                elif any(domain in line for domain in SUPPORTED_SITES["streamtape"]) and ("https://" in line or "http://" in line):
                    supported_url = True
                elif any(domain in line for domain in SUPPORTED_SITES["vtube"]) and ("https://" in line or "http://" in line):
                    supported_url = True
                elif any(domain in line for domain in SUPPORTED_SITES["rubyvid"]) and ("https://" in line or "http://" in line):
                    supported_url = True
                if supported_url:
                    valid_urls.append(line)
            if valid_urls:
                print(f"Found {len(valid_urls)} valid URLs in clipboard")
                if folder_entry.get():
                    for url in valid_urls:
                        download_queue.append(url)
                    update_queue_status()
                    if not is_downloading:
                        process_download_queue()
                else:
                    set_status("Please select a main folder.")
                last_clipboard = current_clipboard
        if root.winfo_exists() and clipboard_var and clipboard_var.get():
            root.after(1000, check_clipboard)
    except tk.TclError:
        if root.winfo_exists() and clipboard_var and clipboard_var.get():
            root.after(1000, check_clipboard)
    except Exception as e:
        print(f"Clipboard error: {e}")
        if root.winfo_exists() and clipboard_var and clipboard_var.get():
            root.after(1000, check_clipboard)

def update_queue_status():
    if download_queue:
        queue_text = f"Downloads in queue: {len(download_queue)}"
        set_status(queue_text)
        current_text = stats_text.get("1.0", tk.END).strip()
        if current_text:
            update_stats(stats_text, current_text)
    else:
        set_status("Download queue is empty")

def create_gui():
    global root, title_label, subtitle_label, url_frame, folder_frame
    global progress_frame, stats_frame, folder_button, download_button
    global theme_button, status_label, stats_text
    global last_clipboard, clipboard_var, cancel_button
    global clipboard_status, clipboard_toggle, url_entry, folder_entry, progress_var
    root = tk.Tk()
    try:
        last_clipboard = root.clipboard_get()
    except Exception:
        last_clipboard = ""
    root.title("Bunkr & GoFile Downloader")
    window_width = 1000
    window_height = 600
    screen_width = root.winfo_screenwidth()
    screen_height = root.winfo_screenheight()
    center_x = int(screen_width / 2 - window_width / 2)
    center_y = int(screen_height / 2 - window_height / 2)
    root.geometry(f"{window_width}x{window_height}+{center_x}+{center_y}")
    root.minsize(800, 500)
    sv_ttk.set_theme("dark")
    container = ttk.Frame(root)
    container.pack(fill=tk.BOTH, expand=True, padx=20, pady=20)
    left_panel = ttk.Frame(container)
    left_panel.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(0, 10))
    header_frame = ttk.Frame(left_panel)
    header_frame.pack(fill=tk.X, pady=(0, 20))
    logo_label = ttk.Label(
        header_frame,
        text="BD",
        font=("Segoe UI", 24, "bold")
    )
    logo_label.pack(side=tk.LEFT, padx=(0, 10))
    title_frame = ttk.Frame(header_frame)
    title_frame.pack(side=tk.LEFT)
    title_label = ttk.Label(
        title_frame,
        text="Bunkr & GoFile Downloader",
        font=("Segoe UI", 24, "bold")
    )
    title_label.pack(anchor="w")
    subtitle_label = ttk.Label(
        title_frame,
        text="Fast and Secure Download Tool",
        font=("Segoe UI", 10)
    )
    subtitle_label.pack(anchor="w")
    url_frame = ttk.LabelFrame(left_panel, text="Download Link", padding=15)
    url_frame.pack(fill=tk.X, pady=(0, 15))
    batch_button = ttk.Button(
        url_frame,
        text="📂 Batch",
        command=load_batch_file,
        style="Secondary.TButton",
        width=8
    )
    batch_button.pack(side=tk.RIGHT, padx=(5, 0))
    url_entry = ttk.Entry(
        url_frame,
        font=("Segoe UI", 10)
    )
    url_entry.pack(side=tk.LEFT, fill=tk.X, expand=True)
    folder_frame = ttk.LabelFrame(left_panel, text="Main Folder (Album subfolder will be created automatically)", padding=15)
    folder_frame.pack(fill=tk.X, pady=(0, 15))
    folder_entry = ttk.Entry(
        folder_frame,
        font=("Segoe UI", 10)
    )
    folder_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 10))
    folder_button = ttk.Button(
        folder_frame,
        text="Browse",
        command=lambda: select_folder(folder_entry),
        style="Accent.TButton",
        width=15
    )
    folder_button.pack(side=tk.RIGHT)
    button_frame = ttk.Frame(left_panel)
    button_frame.pack(pady=(0, 20))
    download_button = ttk.Button(
        button_frame,
        text="Start Download",
        command=on_start_button_click,
        style="Accent.TButton",
        width=25
    )
    download_button.pack(side=tk.LEFT, padx=5)
    clear_button = ttk.Button(
        button_frame,
        text="🗑 Clear Queue",
        command=clear_download_queue,
        style="Secondary.TButton",
        width=15
    )
    clear_button.pack(side=tk.LEFT, padx=5)
    cancel_button = ttk.Button(
        button_frame,
        text="Cancel",
        command=cancel_download_process,
        style="Secondary.TButton",
        width=15,
        state="disabled"
    )
    cancel_button.pack(side=tk.LEFT, padx=5)
    right_panel = ttk.Frame(container)
    right_panel.pack(side=tk.RIGHT, fill=tk.BOTH, expand=True, padx=(10, 0))
    progress_frame = ttk.LabelFrame(right_panel, text="Download Progress", padding=15)
    progress_frame.pack(fill=tk.X, pady=(0, 15))
    progress_var = tk.DoubleVar()
    progress_bar = ttk.Progressbar(
        progress_frame,
        variable=progress_var,
        maximum=100,
        mode="determinate",
        style="Accent.Horizontal.TProgressbar"
    )
    progress_bar.pack(fill=tk.X, pady=(5, 10))
    status_label = ttk.Label(
        progress_frame,
        text="Ready",
        font=("Segoe UI", 10)
    )
    status_label.pack(anchor="w")
    stats_frame = ttk.LabelFrame(right_panel, text="Download Statistics", padding=15)
    stats_frame.pack(fill=tk.BOTH, expand=True)
    stats_text = tk.Text(
        stats_frame,
        font=("Cascadia Code", 10),
        wrap=tk.WORD,
        state="disabled",
        bg="#2b2b2b",
        fg="#e0e0e0",
        relief="flat",
        height=10
    )
    stats_text.pack(fill=tk.BOTH, expand=True)
    footer_frame = ttk.Frame(root)
    footer_frame.pack(fill=tk.X, padx=20, pady=10)
    marquee_frame = ttk.Frame(footer_frame)
    marquee_frame.pack(side=tk.LEFT, fill=tk.X, expand=True)
    supported_sites_text = "    Supported Sites: Bunkr | Vidoza | GoFile | Pixeldrain | RubyVid | vTube | Streamtape"
    marquee_label = ttk.Label(
        marquee_frame,
        text=supported_sites_text,
        font=("Segoe UI", 8),
        foreground="gray"
    )
    marquee_label.pack(side=tk.LEFT)
    def animate_marquee():
        text = marquee_label.cget("text")
        text = text[1:] + text[0]
        marquee_label.config(text=text)
        root.after(100, animate_marquee)
    root.after(100, animate_marquee)
    theme_button = ttk.Button(
        footer_frame,
        text="Toggle Theme",
        command=lambda: toggle_theme(stats_text),
        style="Secondary.TButton",
        width=15
    )
    theme_button.pack(side=tk.RIGHT)
    style = ttk.Style()
    style.configure("Accent.TButton", font=("Segoe UI", 10))
    style.configure("Secondary.TButton", font=("Segoe UI", 9))
    theme_button.pack(side=tk.RIGHT, padx=5)
    clipboard_frame = ttk.Frame(right_panel)
    clipboard_frame.pack(fill=tk.X, pady=(0, 15))
    clipboard_status = ttk.Label(
        clipboard_frame,
        text="Auto-download active",
        font=("Segoe UI", 9),
        foreground="#00aa00"
    )
    clipboard_status.pack(side=tk.LEFT)
    clipboard_var = tk.BooleanVar(value=True)
    def toggle_clipboard():
        if clipboard_var.get():
            global last_clipboard
            try:
                last_clipboard = root.clipboard_get()
            except Exception:
                last_clipboard = ""
            root.after(1000, check_clipboard)
            clipboard_status.configure(
                text="Auto-download active",
                foreground="#00aa00"
            )
        else:
            clipboard_status.configure(
                text="Auto-download disabled",
                foreground="#aa0000"
            )
    clipboard_toggle = ttk.Checkbutton(
        clipboard_frame,
        text="Auto Download",
        variable=clipboard_var,
        command=toggle_clipboard,
        style="Switch.TCheckbutton"
    )
    clipboard_toggle.pack(side=tk.RIGHT)
    if clipboard_var.get():
        root.after(1000, check_clipboard)
    return root, url_entry, folder_entry, progress_var, status_label, download_button, stats_text

def is_vtube_url(url):
    parsed_url = urlparse(url)
    return any(domain in parsed_url.netloc for domain in SUPPORTED_SITES["vtube"])

def download_from_vtube(url, base_folder, status_label=None, progress_var=None):
    try:
        vtube_folder = os.path.join(base_folder, "vTube")
        if not os.path.exists(vtube_folder):
            os.makedirs(vtube_folder)
        set_status("Extracting video from vtube.network...")
        m3u8_url = extract_video_url(url)
        if not m3u8_url:
            set_status("Could not extract video URL")
            set_button_state(download_button, "normal")
            set_button_state(cancel_button, "disabled")
            download_complete_callback()
            return
        output_filename = create_random_filename()
        output_path = os.path.join(vtube_folder, output_filename)
        try:
            subprocess.run(["ffmpeg", "-version"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
        except (subprocess.SubprocessError, FileNotFoundError):
            set_status("FFmpeg not found. Please install FFmpeg and add it to PATH")
            set_button_state(download_button, "normal")
            set_button_state(cancel_button, "disabled")
            download_complete_callback()
            return
        set_status(f"Downloading: {output_filename}")
        command = [
            "ffmpeg",
            "-i", m3u8_url,
            "-c", "copy",
            "-bsf:a", "aac_adtstoasc",
            "-movflags", "faststart",
            output_path
        ]
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            universal_newlines=True
        )
        for line in process.stdout:
            if STOP_EVENT.is_set():
                process.terminate()
                set_status("Download cancelled")
                break
            line = line.strip()
            if "time=" in line:
                set_status(f"Downloading: {output_filename} - {line}")
                match = re.search(r"time=(\d+:\d+:\d+\.\d+)", line)
                if match:
                    time_str = match.group(1)
                    h, m, s = map(float, time_str.split(":"))
                    total_seconds = h * 3600 + m * 60 + s
                    progress = min(100, (total_seconds / 600) * 100)
                    set_progress(progress)
        process.wait()
        if process.returncode == 0 and not STOP_EVENT.is_set():
            set_status("Download completed!")
            update_stats(stats_text, f"Download completed.\nVideo: {output_filename}")
        else:
            set_status("Error downloading video")
        set_button_state(download_button, "normal")
        set_button_state(cancel_button, "disabled")
        download_complete_callback()
    except Exception as e:
        print(f"Error downloading from vtube.network: {e}")
        set_status(f"Error: {str(e)}")
        set_button_state(download_button, "normal")
        set_button_state(cancel_button, "disabled")
        download_complete_callback()

def create_random_filename(prefix="video_", extension=".mp4"):
    random_string = "".join(random.choices(string.ascii_lowercase + string.digits, k=8))
    return f"{prefix}{random_string}{extension}"

def extract_video_url(url):
    user_agents = [
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:126.0) Gecko/20100101 Firefox/126.0",
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36"
    ]
    user_agent = random.choice(user_agents)
    headers = {
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
        "Accept-Encoding": "gzip, deflate, br",
        "Accept-Language": "en-US,en;q=0.9",
        "Connection": "keep-alive",
        "Host": url.split("/")[2],
        "User-Agent": user_agent,
        "Referer": "https://www.google.com/",
        "sec-ch-ua": '"Chromium";v="135", "Not-A.Brand";v="99"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-User": "?1",
        "Upgrade-Insecure-Requests": "1"
    }
    session = requests.Session()
    try:
        response = session.get(url, headers=headers, timeout=30)
        if response.status_code == 200:
            html_content = response.text
            pattern = r"\|urlset\|(.*?)\|hls\|"
            matches = re.findall(pattern, html_content)
            if matches:
                video_id = matches[0]
                m3u8_url = f"https://str12.vtube.network/hls/{video_id}/master.m3u8"
                return m3u8_url
            else:
                with open("vtube_response.html", "w", encoding="utf-8") as f:
                    f.write(html_content)
                return None
        else:
            return None
    except Exception:
        return None

def is_rubyvid_url(url):
    parsed_url = urlparse(url)
    return any(domain in parsed_url.netloc for domain in SUPPORTED_SITES["rubyvid"])

def extract_rubyvid_m3u8(html_content):
    pattern = r"eval\(function\(p,a,c,k,e,d\){while\(c--\)if\(k\[c\]\)p=p\.replace\(new RegExp\(\'\\\\b\'+c\.toString\(a\)+\'\\\\b\',\'g\'\),k\[c\]\);return p}\(\'(.*?)\',(\d+),(\d+),\'(.*?)\'.split\(\'\|\'\)\)\)"
    match = re.search(pattern, html_content, re.DOTALL)
    if not match:
        return []
    p = match.group(1)
    a = int(match.group(2))
    k = match.group(4).split("|")
    def replace(match):
        num = int(match.group(0), a)
        return k[num] if 0 <= num < len(k) and k[num] else match.group(0)
    decoded = re.sub(r"\b\w+\b", replace, p)
    url_patterns = [
        r'https?://[^"\']*?\.m3u8[^"\']*'
    ]
    for pattern in url_patterns:
        url_matches = re.findall(pattern, decoded)
        clean_urls = []
        for url in url_matches:
            url = re.sub(r"([/_])([a-z,]*h[a-z,]*)[,.]", r"\1h,.", url)
            clean_urls.append(url)
        if clean_urls:
            return clean_urls
    return []

def fetch_rubyvid_html(url):
    headers = {
        "User-Agent": HEADERS["User-Agent"],
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.8",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "keep-alive",
        "Referer": "https://www.google.com/",
        "sec-ch-ua": '"Brave";v="135", "Not-A.Brand";v="8", "Chromium";v="135"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-User": "?1",
        "Sec-GPC": "1",
        "Upgrade-Insecure-Requests": "1"
    }
    try:
        response = requests.get(url, headers=headers)
        response.raise_for_status()
        return response.text
    except requests.RequestException as e:
        print(f"Error fetching RubyVid URL: {e}")
        return None

def download_from_rubyvid(url, base_folder, status_label=None, progress_var=None):
    try:
        rubyvid_folder = os.path.join(base_folder, "RubyVid")
        if not os.path.exists(rubyvid_folder):
            os.makedirs(rubyvid_folder)
        set_status("Extracting video from rubyvid.com...")
        html = fetch_rubyvid_html(url)
        if not html:
            set_status("Could not fetch RubyVid page")
            set_button_state(download_button, "normal")
            set_button_state(cancel_button, "disabled")
            download_complete_callback()
            return
        m3u8_urls = extract_rubyvid_m3u8(html)
        if not m3u8_urls:
            m3u8_urls = re.findall(r'https?://[^"\']*?\.m3u8[^"\']*', html)
            if m3u8_urls:
                m3u8_urls = [re.sub(r"([/_])([a-z,]*h[a-z,]*)[,.]", r"\1h.", url) for url in m3u8_urls]
        if not m3u8_urls:
            set_status("Could not extract video URL")
            set_button_state(download_button, "normal")
            set_button_state(cancel_button, "disabled")
            download_complete_callback()
            return
        m3u8_url = m3u8_urls[0]
        output_filename = create_random_filename()
        output_path = os.path.join(rubyvid_folder, output_filename)
        try:
            subprocess.run(["ffmpeg", "-version"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
        except (subprocess.SubprocessError, FileNotFoundError):
            set_status("FFmpeg not found. Please install FFmpeg and add it to PATH")
            set_button_state(download_button, "normal")
            set_button_state(cancel_button, "disabled")
            return
        set_status(f"Downloading: {output_filename}")
        command = [
            "ffmpeg",
            "-i", m3u8_url,
            "-c", "copy",
            "-bsf:a", "aac_adtstoasc",
            "-movflags", "faststart",
            output_path
        ]
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            universal_newlines=True
        )
        for line in process.stdout:
            if STOP_EVENT.is_set():
                process.terminate()
                set_status("Download cancelled")
                break
            line = line.strip()
            if "time=" in line:
                set_status(f"Downloading: {output_filename} - {line}")
                match = re.search(r"time=(\d+:\d+:\d+\.\d+)", line)
                if match:
                    time_str = match.group(1)
                    h, m, s = map(float, time_str.split(":"))
                    total_seconds = h * 3600 + m * 60 + s
                    progress = min(100, (total_seconds / 600) * 100)
                    set_progress(progress)
        process.wait()
        if process.returncode == 0 and not STOP_EVENT.is_set():
            set_status("Download completed!")
            update_stats(stats_text, f"Download completed.\nVideo: {output_filename}")
        else:
            set_status("Error downloading video")
        set_button_state(download_button, "normal")
        set_button_state(cancel_button, "disabled")
        download_complete_callback()
    except Exception as e:
        print(f"Error downloading from rubyvid.com: {e}")
        set_status(f"Error: {str(e)}")
        set_button_state(download_button, "normal")
        set_button_state(cancel_button, "disabled")
        download_complete_callback()

if __name__ == "__main__":
    root, url_entry, folder_entry, progress_var, status_label, download_button, stats_text = create_gui()
    root.mainloop()
