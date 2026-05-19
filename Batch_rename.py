import os
import re
from collections import defaultdict

DRY_RUN = False
CONVERT_TO_SRT = False
VIDEO_EXT = '.mkv'
SUB_EXTS = ('.ass', '.srt')

_NOISE_NUMBERS = frozenset([
    264, 265, 360, 480, 576, 720, 1080, 2160, 4320,
])

_YEAR_RANGE = range(1900, 2100)

_LANG_SUFFIXES = frozenset([
    '.vi', '.en', '.ja', '.jp', '.zh', '.ko', '.th', '.id', '.ms',
    '.de', '.fr', '.es', '.it', '.pt', '.ru', '.ar', '.pl', '.nl',
    '.sv', '.no', '.da', '.fi', '.hu', '.cs', '.ro', '.bg', '.tr',
    '.uk', '.hr', '.el', '.he', '.hi', '.bn', '.ta', '.te',
    '.default', '.forced', '.sdh', '.cc', '.full', '.signs',
    '.und', '.mul', '.zxx',
    '.jpn', '.eng', '.vie', '.chi', '.kor', '.tha', '.ind', '.msa',
    '.deu', '.fra', '.spa', '.ita', '.por', '.rus', '.ara', '.pol',
])

_RE_GROUP_TAG = re.compile(r'^\s*\[(?=[^\]]*[a-zA-Z])[^\]]*\]\s*')
_RE_CRC_HASH = re.compile(r'\[([0-9a-fA-F]{6,8})\]')
_RE_RESOLUTION_WH = re.compile(r'\b\d{3,4}\s*[xX×]\s*\d{3,4}\b')
_RE_RESOLUTION_P = re.compile(r'(?i)\b(1080[pi]?|720[pi]?|480[pi]?|576[pi]?|360[pi]?|2160[pi]?|4[kK])\b')
_RE_CODEC = re.compile(r'(?i)\b(x\.?264|x\.?265|h\.?264|h\.?265|hevc|avc|aac[\d.]*|flac|opus|vorbis|dts|truehd|atmos|eac3|ac3|mp3|lame)\b')
_RE_SOURCE = re.compile(r'(?i)\b(bdrip|dvdrip|webrip|web[\-_.]?dl|hdtv|pdtv|sdtv|bluray|blu[\-_]ray|remux|hdma|hdrip|satrip|dsr|tvrip|vhsrip|ldrip)\b')
_RE_QUALITY = re.compile(r'(?i)\b(10[\-_]?bit|8[\-_]?bit|hi10p?|ma10p|hdr10\+?|hdr|sdr|dolby[\-_]?vision|dv|imax)\b')
_RE_AUDIO_CH = re.compile(r'(?i)\b([257]\.1(?:\.\d)?|mono|stereo|surround)\b')
_RE_VERSION = re.compile(r'(?i)(?:^|[^a-zA-Z\d])v\d+(?=[^a-zA-Z\d]|$)')
_RE_YEAR_BRACKET = re.compile(r'[\[\(]((?:19|20)\d{2})[\]\)]')
_RE_YEAR_STANDALONE = re.compile(r'(?:^|[^a-zA-Z\d])((?:19|20)\d{2})(?:[^a-zA-Z\d]|$)')
_RE_BATCH_RANGE = re.compile(r'(?:^|[^a-zA-Z\d])(\d{1,4})\s*[-~]\s*(\d{1,4})(?:\s*(?:END|end|End|FIN|fin|Fin|COMPLETE|complete))?(?:[^a-zA-Z\d]|$)')


def _strip_extensions(filename):
    name = os.path.splitext(filename)[0]

    while True:
        stem, ext = os.path.splitext(name)
        if ext.lower() in _LANG_SUFFIXES:
            name = stem
        else:
            break

    return name


def _clean_for_number_search(name):
    cleaned = name
    cleaned = _RE_GROUP_TAG.sub('', cleaned)
    cleaned = _RE_CRC_HASH.sub('', cleaned)
    cleaned = _RE_RESOLUTION_WH.sub('', cleaned)
    cleaned = _RE_RESOLUTION_P.sub('', cleaned)
    cleaned = _RE_CODEC.sub('', cleaned)
    cleaned = _RE_SOURCE.sub('', cleaned)
    cleaned = _RE_QUALITY.sub('', cleaned)
    cleaned = _RE_AUDIO_CH.sub('', cleaned)
    cleaned = _RE_VERSION.sub('', cleaned)
    cleaned = _RE_YEAR_BRACKET.sub('', cleaned)
    cleaned = _RE_YEAR_STANDALONE.sub(' ', cleaned)
    return cleaned


def _is_noise_number(val):
    if val in _NOISE_NUMBERS:
        return True
    if val in _YEAR_RANGE:
        return True
    if val <= 0 or val >= 1900:
        return True
    return False


def _episode_code(season, episode):
    if season is not None:
        return f"S{season:02d}E{episode:02d}"
    return f"E{episode:02d}"


def _extract_episode_parts(filename):
    name = _strip_extensions(filename)

    m = re.search(r'(?i)(?:^|[^a-zA-Z\d])s(\d{1,2})[\s._-]*e(\d{1,4})(?=[^a-zA-Z\d]|$)', name)
    if m:
        season = int(m.group(1))
        episode = int(m.group(2))
        if 0 < season < 100 and 0 < episode < 2000:
            return season, episode

    m = re.search(r'(?i)(?:^|[^a-zA-Z\d])season[\s._-]*(\d{1,2})[\s._-]*(?:episode|ep|e)[\s._-]*(\d{1,4})(?=[^a-zA-Z\d]|$)', name)
    if m:
        season = int(m.group(1))
        episode = int(m.group(2))
        if 0 < season < 100 and 0 < episode < 2000:
            return season, episode

    m = re.search(r'(?:^|[^a-zA-Z\d])(\d{1,2})[xX](\d{2,4})(?=[^a-zA-Z\d]|$)', name)
    if m:
        season = int(m.group(1))
        episode = int(m.group(2))
        if 0 < season < 100 and 0 < episode < 2000 and not (season >= 100 and episode >= 100):
            return season, episode

    m = re.search(
        r'(?:^|(?<=[^a-zA-Z]))'
        r'(?:'
            r'[Ee][Pp](?:[Ii][Ss][Oo][Dd][Ee])?'
            r'|'
            r'EPISODE|Episode|episode'
            r'|'
            r'(?<![sS])[Ee](?=[^a-zA-Z]|\d)'
        r')'
        r'[\s._\-]*'
        r'(\d{1,4})'
        r'(?=[^a-zA-Z\d]|$)',
        name
    )
    if m:
        episode = int(m.group(1))
        if 0 < episode < 2000:
            return None, episode

    m = re.search(r'第\s*(\d{1,4})\s*[話话集回章幕期弾弹]', name)
    if m:
        episode = int(m.group(1))
        if 0 < episode < 2000:
            return None, episode

    m = re.search(r'(?:^|[^\d])(\d{1,4})\s*[話话集回章幕]', name)
    if m:
        episode = int(m.group(1))
        if 0 < episode < 2000:
            return None, episode

    m = re.search(r'#\s*(\d{1,4})', name)
    if m:
        episode = int(m.group(1))
        if 0 < episode < 2000:
            return None, episode

    m = re.search(
        r'(?:^|[^a-zA-Z])'
        r'(?:OVA|OAD|ONA|OAV|SP|SPECIAL|Special|special|NCED|NCOP|'
        r'EX|EXTRA|Extra|extra|Bonus|BONUS|bonus|Movie|MOVIE|movie|'
        r'Prologue|PROLOGUE|Epilogue|EPILOGUE)'
        r'[\s._\-]*(\d{1,3})',
        name
    )
    if m:
        episode = int(m.group(1))
        if 0 < episode < 500:
            return None, episode

    m = re.search(
        r'(?:^|[^a-zA-Z])'
        r'(?:Vol(?:ume)?|DVD|BD|Disc|DISC|Part|PART|Chapter|CHAPTER|'
        r'Ch|CH|Arc|ARC|Season|SEASON|Cour|COUR|Movie|Film)'
        r'\.?\s*[-._]?\s*(\d{1,4})'
        r'(?=[^a-zA-Z\d]|$)',
        name
    )
    if m:
        episode = int(m.group(1))
        if 0 < episode < 1000:
            return None, episode

    cleaned = _clean_for_number_search(name)
    candidates = []

    for m in re.finditer(
        r'[\s_]+[-–—]\s*(\d{1,4})(?:[vV]\d)?(?=[\s_.\[\])\'\"\-,;!?]|$)',
        cleaned
    ):
        episode = int(m.group(1))
        if not _is_noise_number(episode):
            candidates.append((episode, 200, m.start()))

    for m in re.finditer(
        r'(?:^|\A)\s*(\d{1,4})\s*(?:[-._\s])',
        cleaned
    ):
        episode = int(m.group(1))
        if not _is_noise_number(episode):
            candidates.append((episode, 180, m.start()))

    m = re.search(r'(?:[\s_.]|^)(\d{1,4})\s*$', cleaned)
    if m:
        episode = int(m.group(1))
        if not _is_noise_number(episode):
            candidates.append((episode, 170, m.start()))

    for m in re.finditer(r'\.(\d{1,4})\.', cleaned):
        episode = int(m.group(1))
        if not _is_noise_number(episode):
            candidates.append((episode, 140, m.start()))

    for m in re.finditer(r'_(\d{1,4})_', cleaned):
        episode = int(m.group(1))
        if not _is_noise_number(episode):
            candidates.append((episode, 140, m.start()))

    for m in re.finditer(r'[\[\(](\d{1,4})[\]\)]', cleaned):
        episode = int(m.group(1))
        if not _is_noise_number(episode):
            candidates.append((episode, 130, m.start()))

    for m in re.finditer(
        r'(?:^|[\s_\[\(\{])(\d{1,4})(?=[\s_\]\)\}.\-,;!?]|$)',
        cleaned
    ):
        episode = int(m.group(1))
        if not _is_noise_number(episode):
            candidates.append((episode, 100, m.start()))

    for m in re.finditer(r'(?:^|[^\d])(\d{1,4})(?=[^\d]|$)', cleaned):
        episode = int(m.group(1))
        if not _is_noise_number(episode):
            candidates.append((episode, 50, m.start()))

    if candidates:
        seen = set()
        unique = []

        for episode, score, pos in candidates:
            key = (episode, pos)
            if key not in seen:
                seen.add(key)
                unique.append((episode, score, pos))

        unique.sort(key=lambda x: (-x[1], x[2]))
        return None, unique[0][0]

    return None


def extract_episode_number(filename):
    parts = _extract_episode_parts(filename)
    if parts is None:
        return None

    season, episode = parts
    return _episode_code(season, episode)


def _build_file_infos(files):
    infos = []

    for filename in files:
        parts = _extract_episode_parts(filename)
        if parts is None:
            infos.append({
                'name': filename,
                'season': None,
                'episode': None,
                'code': None,
            })
            continue

        season, episode = parts
        infos.append({
            'name': filename,
            'season': season,
            'episode': episode,
            'code': _episode_code(season, episode),
        })

    return infos


def _is_same_episode(video_info, sub_info):
    if video_info['episode'] is None or sub_info['episode'] is None:
        return False

    if video_info['episode'] != sub_info['episode']:
        return False

    if video_info['season'] is None or sub_info['season'] is None:
        return True

    return video_info['season'] == sub_info['season']


def _find_matching_sub(video_info, sub_infos, used_subs):
    exact_matches = []
    loose_matches = []

    for sub_info in sub_infos:
        sub_name = sub_info['name']

        if sub_name in used_subs:
            continue

        if not _is_same_episode(video_info, sub_info):
            continue

        if video_info['season'] is not None and sub_info['season'] == video_info['season']:
            exact_matches.append(sub_info)
        else:
            loose_matches.append(sub_info)

    if len(exact_matches) == 1:
        return exact_matches[0], None

    if len(exact_matches) > 1:
        names = ', '.join(item['name'] for item in exact_matches)
        return None, f"  Cảnh báo: Video tập {video_info['code']} có nhiều sub cùng season: {names}"

    if len(loose_matches) == 1:
        return loose_matches[0], None

    if len(loose_matches) > 1:
        names = ', '.join(item['name'] for item in loose_matches)
        return None, f"  Cảnh báo: Video tập {video_info['code']} có nhiều sub có cùng số tập: {names}"

    return None, f"  Cảnh báo: Video tập {video_info['code']} không có sub tương ứng."


def ass_time_to_srt_time(ass_time):
    parts = ass_time.strip().split(':')
    h = int(parts[0])
    m = int(parts[1])
    sec_parts = parts[2].split('.')
    s = int(sec_parts[0])
    cs = int(sec_parts[1].ljust(2, '0')[:2])
    return f"{h:02d}:{m:02d}:{s:02d},{cs * 10:03d}"


def strip_ass_tags(text):
    if re.search(r'\{[^}]*\\p[1-9]', text):
        return ''

    text = re.sub(r'\{[^}]*\}', '', text)
    text = text.replace('\\N', '\n')
    text = text.replace('\\n', '\n')
    text = text.replace('\\h', ' ')
    return text.strip()


def convert_ass_to_srt(ass_filepath):
    lines = None

    for enc in ['utf-8-sig', 'utf-16', 'cp1252', 'latin-1']:
        try:
            with open(ass_filepath, 'r', encoding=enc) as f:
                lines = f.readlines()
            break
        except (UnicodeDecodeError, UnicodeError):
            continue

    if lines is None:
        raise ValueError(f"Không thể đọc file: {ass_filepath}")

    dialogues = []
    format_fields = None

    for line in lines:
        line = line.strip()

        if line.startswith('Format:') and 'Text' in line:
            format_fields = [field.strip() for field in line[7:].split(',')]
        elif line.startswith('Dialogue:'):
            if format_fields is None:
                format_fields = [
                    'Layer', 'Start', 'End', 'Style', 'Name',
                    'MarginL', 'MarginR', 'MarginV', 'Effect', 'Text'
                ]

            parts = line[9:].split(',', len(format_fields) - 1)

            if len(parts) >= len(format_fields):
                start = parts[format_fields.index('Start')].strip()
                end = parts[format_fields.index('End')].strip()
                text = strip_ass_tags(parts[format_fields.index('Text')].strip())

                if text:
                    dialogues.append((start, end, text))

    srt_filepath = os.path.splitext(ass_filepath)[0] + '.srt'

    with open(srt_filepath, 'w', encoding='utf-8') as f:
        for i, (start, end, text) in enumerate(dialogues, 1):
            f.write(f"{i}\n")
            f.write(f"{ass_time_to_srt_time(start)} --> {ass_time_to_srt_time(end)}\n")
            f.write(f"{text}\n\n")

    return srt_filepath


def convert_all_ass_to_srt():
    current_folder = os.getcwd()
    ass_files = sorted(f for f in os.listdir(current_folder) if f.lower().endswith('.ass'))

    if not ass_files:
        print("Không tìm thấy file .ass nào.")
        return

    print(f"Tìm thấy {len(ass_files)} file .ass")

    success = 0

    for ass_file in ass_files:
        try:
            srt_file = convert_ass_to_srt(ass_file)
            os.remove(ass_file)
            print(f"  OK: {ass_file} -> {os.path.basename(srt_file)}")
            success += 1
        except Exception as e:
            print(f"  LỖI: {ass_file} -> {e}")

    print(f"Chuyển đổi xong: {success}/{len(ass_files)} file.\n")


def _print_duplicate_warnings(file_infos, label):
    groups = defaultdict(list)

    for info in file_infos:
        if info['episode'] is None:
            continue

        groups[(info['season'], info['episode'])].append(info['name'])

    for (season, episode), names in sorted(groups.items(), key=lambda item: ((item[0][0] is None), item[0][0] or -1, item[0][1])):
        if len(names) > 1:
            code = _episode_code(season, episode)
            print(f"Cảnh báo: {label} có nhiều file cùng mã {code}: {', '.join(names)}")


def rename_subs():
    current_folder = os.getcwd()
    files = os.listdir(current_folder)

    videos = sorted(f for f in files if f.lower().endswith(VIDEO_EXT))
    subs = sorted(f for f in files if f.lower().endswith(SUB_EXTS))

    print(f"Tìm thấy {len(videos)} file MKV và {len(subs)} file Sub")

    if len(videos) == 0 or len(subs) == 0:
        print("Lỗi: Không tìm thấy video hoặc sub.")
        return

    if len(videos) != len(subs):
        print(f"CẢNH BÁO: Số lượng không khớp (Video: {len(videos)} vs Sub: {len(subs)}).")
        print("Dừng lại để đảm bảo an toàn.")
        return

    video_infos = _build_file_infos(videos)
    sub_infos = _build_file_infos(subs)

    for info in video_infos:
        if info['episode'] is None:
            print(f"Bỏ qua video (không tìm thấy số tập): {info['name']}")

    for info in sub_infos:
        if info['episode'] is None:
            print(f"Bỏ qua sub (không tìm thấy số tập): {info['name']}")

    valid_video_infos = [info for info in video_infos if info['episode'] is not None]
    valid_sub_infos = [info for info in sub_infos if info['episode'] is not None]

    _print_duplicate_warnings(valid_video_infos, 'video')
    _print_duplicate_warnings(valid_sub_infos, 'sub')

    used_subs = set()
    match_count = 0

    for video_info in valid_video_infos:
        sub_info, warning = _find_matching_sub(video_info, valid_sub_infos, used_subs)

        if warning:
            print(warning)

        if sub_info is None:
            continue

        sub_name = sub_info['name']
        used_subs.add(sub_name)

        _, sub_ext = os.path.splitext(sub_name)
        video_name_no_ext, _ = os.path.splitext(video_info['name'])
        new_sub_name = video_name_no_ext + sub_ext

        if sub_name == new_sub_name:
            print(f"  [Bỏ qua] {sub_name} đã đúng tên.")
            continue

        if not DRY_RUN:
            try:
                os.rename(sub_name, new_sub_name)
                print(f"  Đổi tên: {sub_name} -> {new_sub_name}")
                match_count += 1
            except Exception as e:
                print(f"  LỖI: {e}")
        else:
            print(f"  DRY_RUN: {sub_name} -> {new_sub_name}")
            match_count += 1

    print(f"Đổi tên xong: {match_count} file.\n")


def main():
    print("=" * 50)
    if CONVERT_TO_SRT:
        convert_all_ass_to_srt()
    else:
        print("Bỏ qua bước chuyển đổi .ass sang .srt (CONVERT_TO_SRT = False)\n")
    rename_subs()
    print("=" * 50)


if __name__ == "__main__":
    main()
