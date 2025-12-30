import requests
import json
import argparse
import sys
import os
import re
from tenacity import retry, wait_fixed, wait_exponential, retry_if_exception_type, stop_after_attempt
from bs4 import BeautifulSoup
from urllib.parse import urlparse, unquote
from tqdm import tqdm
from base64 import b64decode
from math import floor
from datetime import datetime
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

BUNKR_VS_API_URL_FOR_SLUG = "https://bunkr.cr/api/vs"
SECRET_KEY_BASE = "SECRET_KEY_"
MIN_FILE_SIZE = 1024 * 1024  
MAX_RETRIES = 10
file_lock = threading.Lock()

def clean_filename(name):
    if not name: return "untitled_video"
    name = re.sub(r'[<>:"/\\|?*]', '-', name)
    name = re.sub(r'[\0-\31]', '', name)
    return name.strip()

def log_success_handler(download_path, original_url):
    success_path = os.path.join(download_path, 'success.txt')
    failed_path = os.path.join(download_path, 'failed.txt')

    with file_lock:
        with open(success_path, 'a', encoding='utf-8') as f:
            f.write(f"{original_url}\n")

        if os.path.exists(failed_path):
            with open(failed_path, 'r', encoding='utf-8') as f:
                lines = f.readlines()
            
            new_lines = [line for line in lines if line.strip() != original_url]
            
            if len(new_lines) != len(lines):
                with open(failed_path, 'w', encoding='utf-8') as f:
                    f.writelines(new_lines)

def log_failed_handler(download_path, original_url):
    failed_path = os.path.join(download_path, 'failed.txt')

    if os.path.exists(failed_path):
        with file_lock:
            with open(failed_path, 'r', encoding='utf-8') as f:
                if original_url in f.read(): return

    with file_lock:
        with open(failed_path, 'a', encoding='utf-8') as f:
            f.write(f"{original_url}\n")

def get_success_set(download_path):
    path = os.path.join(download_path, 'success.txt')
    if not os.path.exists(path): return set()
    with open(path, 'r', encoding='utf-8') as f:
        return set(line.strip() for line in f if line.strip())

def process_single_item(session, item, download_path, success_set, extensions_list, only_export, direct_link):
    viewing_url = item['url']

    if viewing_url in success_set:
        return

    if only_export:
        write_url_to_list(viewing_url, download_path)
        return

    if not direct_link:
        real_item = get_real_download_url(session, viewing_url, True, item['name'])
        if real_item is None:
            print(f"\t[-] Cannot get link for: {viewing_url}")
            log_failed_handler(download_path, viewing_url)
            return

        ext = get_url_data(real_item['url'])['extension']
        ext = ext.replace('.', '')
        if len(extensions_list) > 0 and ext not in extensions_list:
            return

        download_advanced(session, real_item['url'], download_path, viewing_url, real_item['name'])
    else:
        real_item = get_real_download_url(session, viewing_url, True, item['name'])
        if real_item:
             download_advanced(session, real_item['url'], download_path, viewing_url, real_item['name'])

def get_items_list(session, url, extensions, only_export, custom_path=None, is_last_page=True, date_before=None, date_after=None):
    extensions_list = extensions.split(',') if extensions is not None else []
    
    if '/f/' in url or '/v/' in url:
        print(f"[-] Detected Single Video: {url}")
        
        vid_name = "Unknown_Video"
        try:
            r = session.get(url, timeout=20)
            if r.status_code == 200:
                soup = BeautifulSoup(r.content, 'html.parser')
                title_tag = soup.find('h1', {'class': 'text-[20px]'}) or \
                            soup.find('h1', {'class': 'truncate'}) or \
                            soup.find('h1', id='title')
                if title_tag: vid_name = title_tag.text.strip()
        except: pass

        safe_album_name = "Single_Videos"
        download_path = get_and_prepare_download_path(custom_path, safe_album_name)
        
        success_set = get_success_set(download_path)
        if url in success_set:
            print(f"\t[Skip] Already in success list.")
            return

        real_item = get_real_download_url(session, url, True, vid_name)
        if real_item:
            download_advanced(session, real_item['url'], download_path, url, real_item['name'])
        else:
            print(f"\t[-] Cannot resolve link: {url}")
            log_failed_handler(download_path, url)
        
        return

    print(f"[-] Fetching Album page: {url}")
    try:
        r = session.get(url, timeout=20)
        if r.status_code != 200:
            print(f"[-] HTTP error {r.status_code}")
            return
    except Exception as e:
        print(f"[-] Connection error: {e}")
        return

    soup = BeautifulSoup(r.content, 'html.parser')
    
    is_bunkr = True 
    album_name = soup.find('h1', {'class': 'text-[20px]'})
    if album_name is None:
        album_name = soup.find('h1', {'class': 'truncate'})
    
    if not album_name:
        album_name = soup.find('h1', id='title')

    safe_album_name = clean_filename(album_name.text if album_name else "Unknown_Album")
    download_path = get_and_prepare_download_path(custom_path, safe_album_name)
    
    success_set = get_success_set(download_path)

    items = []
    
    direct_link = soup.find('span', {'class': 'ic-videos'}) is not None or soup.find('div', {'class': 'lightgallery'}) is not None
    
    if direct_link:
        real_url_data = get_real_download_url(session, url, True)
        if real_url_data: items.append(real_url_data)
    else:
        theItems = soup.find_all('div', {'class': 'theItem'})
        if not theItems:
             theItems = soup.find_all('div', class_=re.compile(r'item|grid'))

        if theItems:
            for theItem in theItems:
                if date_before is not None or date_after is not None:
                    date_span = theItem.find('span', {'class': 'ic-clock'})
                    if date_span and not is_date_in_range(date_span.text, date_before, date_after):
                        continue
                
                a_tag = theItem.find('a')
                if not a_tag: continue
                
                link = a_tag.get('href')
                name_tag = theItem.find('p') or theItem.find('span', {'class': 'name'})
                name_text = name_tag.text if name_tag else None
                items.append({'url': link, 'size': -1, 'name': name_text})
        
        if not items:
            potential_links = soup.find_all('a', href=re.compile(r'/f/[a-zA-Z0-9]+|/v/[a-zA-Z0-9]+'))
            seen_links = set()
            for a in potential_links:
                href = a['href']
                if href in seen_links: continue
                seen_links.add(href)
                
                name = a.get_text(strip=True)
                if not name:
                    img = a.find('img')
                    if img and 'alt' in img.attrs:
                        name = img['alt']
                
                items.append({'url': href, 'size': -1, 'name': name})

    with ThreadPoolExecutor(max_workers=5) as executor:
        futures = [
            executor.submit(
                process_single_item, 
                session, 
                item, 
                download_path, 
                success_set, 
                extensions_list, 
                only_export,
                direct_link
            ) 
            for item in items
        ]
        for future in as_completed(futures):
            try:
                future.result()
            except Exception as e:
                pass

    pagination = soup.find('nav', {'class': 'pagination'})
    if pagination is not None:
        current_page_tag = pagination.find('span', {'class': 'active'})
        if current_page_tag:
            try:
                current_page = int(current_page_tag.text)
            except ValueError:
                current_page = 1

            page_links = pagination.find_all('a')
            
            page_numbers = []
            for link in page_links:
                try:
                    page_numbers.append(int(link.text))
                except ValueError:
                    continue
            
            last_page = max(page_numbers) if page_numbers else current_page

            if int(current_page) < int(last_page):
                print(f"[!] Moving to page ({int(current_page)+1}/{last_page})")
                if re.search(r'([?&])page=\d+', url):
                    url_next_page = re.sub(r'([?&])page=\d+', r'\1page={}'.format(current_page+1), url)
                else:
                    url_next_page = f"{url}{'&' if '?' in url else '?'}page={(current_page+1)}"
                
                get_items_list(session, url_next_page, extensions, only_export, custom_path=custom_path, is_last_page=(int(current_page) == int(last_page)), date_before=date_before, date_after=date_after)

    if is_last_page:
        print(f"\t[+] Process Completed.")
    return

def get_real_download_url(session, url, is_bunkr=True, item_name=None):
    url = url if 'https' in url else f'https://bunkr.cr{url}'
    
    try:
        match = re.search(r'\/f\/(.*?)$', url)
        if not match: return {'url': url, 'size': -1, 'name': item_name} 
        
        slug = unquote(match.group(1))

        enc_data = get_encryption_data(session, slug)
        if not enc_data: return None
        
        decrypted = decrypt_encrypted_url(enc_data)
        return {'url': decrypted, 'size': -1, 'name': item_name}
    except Exception as e:
        print(f"\t[-] Error resolving slug: {e}")
        return None

@retry(stop=stop_after_attempt(10), wait=wait_fixed(2), retry=retry_if_exception_type((requests.exceptions.RequestException, OSError)))
def get_encryption_data(session, slug):
    try:
        r = session.post(BUNKR_VS_API_URL_FOR_SLUG, json={'slug': slug})
        if r.status_code != 200: return None
        return json.loads(r.content)
    except: return None

def decrypt_encrypted_url(encryption_data):
    try:
        secret_key = f"{SECRET_KEY_BASE}{floor(encryption_data['timestamp'] / 3600)}"
        encrypted_url_bytearray = list(b64decode(encryption_data['url']))
        secret_key_byte_array = list(secret_key.encode('utf-8'))
        decrypted_url = ""
        for i in range(len(encrypted_url_bytearray)):
            decrypted_url += chr(encrypted_url_bytearray[i] ^ secret_key_byte_array[i % len(secret_key_byte_array)])
        return decrypted_url
    except: return None

@retry(stop=stop_after_attempt(10), wait=wait_exponential(multiplier=1, min=2, max=10), retry=retry_if_exception_type((requests.exceptions.RequestException, OSError)))
def download_advanced(session, direct_url, download_path, original_viewing_url, display_name=None):

    parsed_path = urlparse(direct_url).path
    ext = os.path.splitext(parsed_path)[1] or ".mp4"

    if display_name:
        safe_name = clean_filename(display_name)
        if safe_name.lower().endswith(ext.lower()):
            file_name = safe_name
        else:
            file_name = f"{safe_name}{ext}"
    else:
        file_name = os.path.basename(parsed_path)

    final_path = os.path.join(download_path, file_name)

    if os.path.exists(final_path):
        file_size = os.path.getsize(final_path)
        if file_size > MIN_FILE_SIZE:
            print(f"\t[Skip] File OK ({file_size//1024} KB): {file_name}")
            log_success_handler(download_path, original_viewing_url)
            return
        else:
            print(f"\t[Fix] Found broken/maintenance file ({file_size} B). Deleting: {file_name}")
            try:
                os.remove(final_path)
            except: pass

    try:
        with session.get(direct_url, stream=True, timeout=20) as r:
            if r.status_code != 200:
                print(f"\t[Err] HTTP {r.status_code}: {file_name}")
                log_failed_handler(download_path, original_viewing_url)
                return

            if "maintenance" in r.url or "deleted" in r.url:
                print(f"\t[Err] Maintenance Redirect: {file_name}")
                log_failed_handler(download_path, original_viewing_url)
                return

            try:
                total_size = int(r.headers.get('content-length', 0))
            except: total_size = 0
            
            if total_size > 0 and total_size < MIN_FILE_SIZE:
                print(f"\t[Err] File too small ({total_size} B) - Maintenance detected: {file_name}")
                log_failed_handler(download_path, original_viewing_url)
                return

            print(f"\t[Down] {file_name} ...")
            with open(final_path, 'wb') as f:
                with tqdm(total=total_size, unit='B', unit_scale=True, desc=file_name[:20], leave=False) as pbar:
                    for chunk in r.iter_content(chunk_size=8192):
                        if chunk:
                            f.write(chunk)
                            pbar.update(len(chunk))
    
    except Exception as e:
        print(f"\t[Err] Network: {e}")
        log_failed_handler(download_path, original_viewing_url)
        return
        
    if os.path.exists(final_path):
        actual_size = os.path.getsize(final_path)
        if actual_size < MIN_FILE_SIZE:
            print(f"\t[Fail] Downloaded file is broken ({actual_size} B). Deleting.")
            os.remove(final_path)
            log_failed_handler(download_path, original_viewing_url)
        else:
            log_success_handler(download_path, original_viewing_url)
    else:
        log_failed_handler(download_path, original_viewing_url)

def create_session():
    session = requests.Session()
    session.headers.update({
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36',
        'Referer': 'https://bunkr.cr/',
    })
    return session

def get_url_data(url):
    parsed_url = urlparse(url)
    return {'file_name': os.path.basename(parsed_url.path), 'extension': os.path.splitext(parsed_url.path)[1], 'hostname': parsed_url.hostname}

def get_and_prepare_download_path(custom_path, album_name):
    final_path = 'downloads' if custom_path is None else custom_path
    final_path = os.path.join(final_path, album_name) if album_name is not None else 'downloads'
    if not os.path.isdir(final_path):
        os.makedirs(final_path)
    return final_path

def write_url_to_list(item_url, download_path):
    list_path = os.path.join(download_path, 'url_list.txt')
    with open(list_path, 'a', encoding='utf-8') as f:
        f.write(f"{item_url}\n")

def date_argument(date_string):
    try:
        return datetime.strptime(date_string, '%Y-%m-%dT%H:%M:%S')
    except ValueError:
        raise argparse.ArgumentTypeError("Invalid date format. Use: yyyy-mm-ddThh:mm:ss")

def is_date_in_range(date_string, date_before, date_after):
    try:
        bunkr_date = datetime.strptime(date_string.strip(), '%H:%M:%S %d/%m/%Y')
        date_before = datetime.max if date_before is None else date_before
        date_after = datetime.min if date_after is None else date_after
        return bunkr_date <= date_before and bunkr_date >= date_after
    except:
        return True 

if __name__ == '__main__':
    parser = argparse.ArgumentParser(sys.argv[1:])
    parser.add_argument("-u", help="Url to fetch", type=str, required=False, default=None)
    parser.add_argument("-f", help="File to list of URLs to download", required=False, type=str, default=None)
    parser.add_argument("-r", help="Amount of retries", type=int, required=False, default=10)
    parser.add_argument("-e", help="Extensions to download (comma separated)", type=str)
    parser.add_argument("-p", help="Path to custom downloads folder", default="downloads")
    parser.add_argument("-w", help="Export url list", action="store_true")
    parser.add_argument("--before", help="Export only files before this date", type=date_argument, default=None)
    parser.add_argument("--after", help="Export only files after this date", type=date_argument, default=None)

    args = parser.parse_args()
    sys.stdout.reconfigure(encoding='utf-8')

    if args.u is None and args.f is None:
        print("[-] Error: Bạn chưa nhập link (-u) hoặc file (-f)")
        sys.exit(1)

    session = create_session()
    MAX_RETRIES = args.r

    if args.f is not None:
        if os.path.exists(args.f):
            print(f"[-] Đang đọc file danh sách: {args.f}")
            try:
                with open(args.f, 'r', encoding='utf-8') as f:
                    urls = f.read().splitlines()
                
                count = 0
                for url in urls:
                    if not url.strip(): continue
                    count += 1
                    print(f"\n[-] --- Xử lý Album số {count}: {url.strip()} ---")
                    get_items_list(session, url.strip(), args.e, args.w, args.p, date_before=args.before, date_after=args.after)
            except Exception as e:
                print(f"[-] Lỗi khi đọc file {args.f}: {e}")
        else:
            print(f"[-] Lỗi: Không tìm thấy file '{args.f}'")
            print(f"[-] Hãy chắc chắn file '{args.f}' nằm cùng thư mục với script hoặc điền đường dẫn đầy đủ.")
    
    elif args.u is not None:
        get_items_list(session, args.u, args.e, args.w, args.p, date_before=args.before, date_after=args.after)