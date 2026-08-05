import os
import re
import unicodedata
import sys
from collections import defaultdict

DRY_RUN = False
CONVERT_TO_SRT = False

VIDEO_EXTS = {'.mkv', '.mp4', '.avi', '.mov', '.wmv', '.flv', '.webm', '.m4v', '.ts', '.m2ts'}
SUB_EXTS = {'.ass', '.srt', '.ssa', '.vtt', '.sub', '.idx'}
PREFERRED_LANGS = ['vi', 'vie', 'en', 'eng', 'ja', 'jp', 'jpn']

_SPECIAL_RE = re.compile(r'(?i)^(.*?)(?:^|[^a-zA-Z])(OVA|OAD|ONA|OAV|SP|SPECIAL|NCED|NCOP|PV|CM|EX|EXTRA|BONUS|PROLOGUE|EPILOGUE|OP|ED|TRAILER|TEASER|PREVIEW|MENU)(?:[^a-zA-Z]|$)')

_NON_EP_STEMS = frozenset(['opening', 'ending', 'trailer', 'menu', 'scan', 'fonts', 'creditless'])

_CN_NUMERALS = {
    '零': 0, '〇': 0, '一': 1, '二': 2, '三': 3, '四': 4, '五': 5,
    '六': 6, '七': 7, '八': 8, '九': 9, '十': 10, '百': 100, '千': 1000,
    '壱': 1, '弐': 2, '参': 3, '肆': 4, '伍': 5, '陸': 6, '漆': 7, '捌': 8, '玖': 9, '拾': 10,
    '壹': 1, '貳': 2, '參': 3, '兩': 2,
}

_CN_COUNTER_WORDS = frozenset(['話', '话', '集', '回', '章', '幕', '期', '弾', '弹', '話目', '话目'])

_VIET_NUMERAL_WORDS = {
    'mot': 1, 'hai': 2, 'ba': 3, 'bon': 4, 'nam': 5,
    'sau': 6, 'bay': 7, 'tam': 8, 'chin': 9, 'muoi': 10,
    'mot': 1, 'hai': 2, 'ba': 3, 'tu': 4, 'lam': 5,
    'một': 1, 'hai': 2, 'ba': 3, 'bốn': 4, 'năm': 5,
    'sáu': 6, 'bảy': 7, 'tám': 8, 'chín': 9, 'mườ': 10,
    'mườ': 10, 'mướ': 10,
}

_VIET_ORDINAL_WORDS = {
    'mot': 1, 'hai': 2, 'ba': 3, 'bon': 4, 'nam': 5,
    'sau': 6, 'bay': 7, 'tam': 8, 'chin': 9, 'muoi': 10,
    'mườ': 10, 'mướ': 10, 'mườ': 10,
    'mườihai': 12, 'mườimot': 11, 'mườiba': 13,
    'mườibốn': 14, 'mườinăm': 15,
    'mườisáu': 16, 'mườibảy': 17, 'mườitám': 18, 'mườichín': 19,
    'haimươi': 20, 'haimươimốt': 21, 'haimươihai': 22,
}

_ENGLISH_NUMBERS = {
    'one': 1, 'two': 2, 'three': 3, 'four': 4, 'five': 5,
    'six': 6, 'seven': 7, 'eight': 8, 'nine': 9, 'ten': 10,
    'eleven': 11, 'twelve': 12, 'thirteen': 13, 'fourteen': 14, 'fifteen': 15,
    'sixteen': 16, 'seventeen': 17, 'eighteen': 18, 'nineteen': 19, 'twenty': 20,
    'thirty': 30, 'forty': 40, 'fifty': 50,
}

_ROMAN_NUMERALS = {
    'i': 1, 'ii': 2, 'iii': 3, 'iv': 4, 'v': 5,
    'vi': 6, 'vii': 7, 'viii': 8, 'ix': 9, 'x': 10,
    'xi': 11, 'xii': 12, 'xiii': 13, 'xiv': 14, 'xv': 15,
    'xvi': 16, 'xvii': 17, 'xviii': 18, 'xix': 19, 'xx': 20,
    'xxx': 30, 'xl': 40, 'l': 50,
    'I': 1, 'II': 2, 'III': 3, 'IV': 4, 'V': 5,
    'VI': 6, 'VII': 7, 'VIII': 8, 'IX': 9, 'X': 10,
    'XI': 11, 'XII': 12, 'XIII': 13, 'XIV': 14, 'XV': 15,
    'XVI': 16, 'XVII': 17, 'XVIII': 18, 'XIX': 19, 'XX': 20,
    'XXX': 30, 'XL': 40, 'L': 50,
}

_LANG_SUFFIXES = frozenset([
    '.vi', '.en', '.ja', '.jp', '.zh', '.ko', '.th', '.id', '.ms',
    '.de', '.fr', '.es', '.it', '.pt', '.ru', '.ar', '.pl', '.nl',
    '.sv', '.no', '.da', '.fi', '.hu', '.cs', '.ro', '.bg', '.tr',
    '.uk', '.hr', '.el', '.he', '.hi', '.bn', '.ta', '.te',
    '.default', '.forced', '.sdh', '.cc', '.full', '.signs',
    '.und', '.mul', '.zxx',
    '.jpn', '.eng', '.vie', '.chi', '.kor', '.tha', '.ind', '.msa',
    '.deu', '.fra', '.spa', '.ita', '.por', '.rus', '.ara', '.pol',
    '.zh-hans', '.zh-hant', '.zh_cn', '.zh_tw', '.zh-chs', '.zh-cht',
    '.big5', '.gb',
])

_NOISE_NUMBERS = frozenset([264, 265, 360, 480, 576, 720, 1080, 2160, 4320])
_YEAR_RANGE = range(1900, 2100)

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

_RE_FILESIZE = re.compile(r'(?i)\b\d+\.?\d*\s*(gb|mb|tb|kb|b)\b')

_RE_VIET_TAP = re.compile(r'(?i)(?:^|[^a-zA-Z\d])(t[aă][aă]?p)\s*(\d{1,4})(?=[^a-zA-Z\d]|$)')
_RE_VIET_TAP_UNICODE = re.compile(r'(?:^|[^\w])(T[aă][aă]?p)\s*(\d{1,4})(?=[^\w]|$)')

_FULLWIDTH_TO_ASCII = str.maketrans(
    '０１２３４５６７８９ＡＢＣＤＥＦＧＨＩＪＫＬＭＮＯＰＱＲＳＴＵＶＷＸＹＺａｂｃｄｅｆｇｈｉｊｋｌｍｎｏｐｑｒｓｔｕｖｗｘｙｚ',
    '0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz'
)


class MediaInfo:
    __slots__ = ('filename', 'stem', 'ext', 'season', 'episode', 'absolute_episode',
                 'special_type', 'part', 'cour', 'version', 'is_batch', 'range_start',
                 'range_end', 'title_key', 'language', 'warnings', 'confidence')

    def __init__(self, filename):
        self.filename = filename
        self.stem, self.ext = _split_all_extensions(filename)
        self.season = None
        self.episode = None
        self.absolute_episode = None
        self.special_type = None
        self.part = None
        self.cour = None
        self.version = None
        self.is_batch = False
        self.range_start = None
        self.range_end = None
        self.title_key = None
        self.language = None
        self.warnings = []
        self.confidence = 0
        self._parse()

    def _parse(self):
        name = self.stem
        norm = normalize_unicode(name)
        no_fw = norm.translate(_FULLWIDTH_TO_ASCII)

        self.title_key = _extract_title_key(no_fw)
        self.special_type = _detect_special_type(no_fw)
        self.is_batch = _detect_batch(no_fw)
        self.part = _extract_part(no_fw)
        self.cour = _extract_cour(no_fw)
        self.language = _detect_language(norm, self.ext, self.stem)

        parts = _extract_episode_parts(no_fw, norm, self.special_type)
        if parts is not None:
            self.season, self.episode = parts
            self.confidence = 100

        if self.special_type in ('PV', 'CM', 'OP', 'ED', 'TRAILER', 'TEASER', 'PREVIEW', 'MENU'):
            self.warnings.append('non_episode_file')

    def episode_code(self):
        if self.season is not None and self.episode is not None:
            return 'S{:02d}E{:02d}'.format(self.season, self.episode)
        if self.episode is not None:
            return 'E{:02d}'.format(self.episode)
        if self.special_type and self.episode is not None:
            return '{}_{:02d}'.format(self.special_type, self.episode)
        return None

    def is_renamable(self):
        if self.episode is None and self.special_type is None:
            return False
        if self.is_batch:
            return False
        if self.special_type in ('PV', 'CM', 'OP', 'ED', 'TRAILER', 'TEASER', 'PREVIEW', 'MENU'):
            return False
        return True


def _split_all_extensions(filename):
    root = filename
    exts = []

    while True:
        stem, ext = os.path.splitext(root)

        if ext.lower() in _LANG_SUFFIXES:
            exts.append(ext)
            root = stem
        else:
            if ext.lower() in VIDEO_EXTS or ext.lower() in SUB_EXTS:
                exts.append(ext)
                return stem, ''.join(reversed(exts))

            return root, ''.join(reversed(exts)) if exts else ext

    return root, ''.join(reversed(exts))


def normalize_unicode(text):
    return unicodedata.normalize('NFKC', text)


def _remove_brackets(text):
    text = re.sub(r'\[[^\]]*\]', ' ', text)
    text = re.sub(r'\([^)]*\)', ' ', text)
    text = re.sub(r'\{[^}]*\}', ' ', text)
    text = re.sub(r'【[^】]*】', ' ', text)
    text = re.sub(r'〖[^〗]*〗', ' ', text)
    text = re.sub(r'〔[^〕]*〕', ' ', text)
    text = re.sub(r'「[^」]*」', ' ', text)
    text = re.sub(r'『[^』]*』', ' ', text)
    text = re.sub(r'《[^》]*》', ' ', text)
    text = re.sub(r'〈[^〉]*〉', ' ', text)
    text = re.sub(r'［[^］]*］', ' ', text)
    return text


def _remove_noise(text):
    text = _RE_GROUP_TAG.sub('', text)
    text = _RE_CRC_HASH.sub('', text)
    text = _RE_RESOLUTION_WH.sub('', text)
    text = _RE_RESOLUTION_P.sub('', text)
    text = _RE_CODEC.sub('', text)
    text = _RE_SOURCE.sub('', text)
    text = _RE_QUALITY.sub('', text)
    text = _RE_AUDIO_CH.sub('', text)
    text = _RE_VERSION.sub('', text)
    text = _RE_YEAR_BRACKET.sub('', text)
    text = _RE_YEAR_STANDALONE.sub(' ', text)
    text = _RE_FILESIZE.sub('', text)
    return text


def _is_noise_number(val):
    if val in _NOISE_NUMBERS:
        return True
    if val in _YEAR_RANGE:
        return True
    if val <= 0 or val >= 1900:
        return True
    return False


def _parse_chinese_numeral(text):
    i = 0
    total = 0
    prev = 0

    while i < len(text) and text[i] in _CN_NUMERALS:
        ch = text[i]
        val = _CN_NUMERALS[ch]

        if val >= 10:
            if prev == 0:
                prev = 1
            total += prev * val
            prev = 0
        else:
            prev = val

        i += 1

    total += prev

    if total > 0:
        return total, i

    return None, 0


def parse_english_number_words(text):
    text = text.lower()

    compound = re.search(r'(?:^|[^a-zA-Z])(twenty\s+(one|two|three|four|five|six|seven|eight|nine))(?:[^a-zA-Z]|$)', text)
    if compound:
        total = 20 + _ENGLISH_NUMBERS.get(compound.group(2), 0)
        if total > 0:
            return total

    compound = re.search(r'(?:^|[^a-zA-Z])(thirty\s+(one|two|three|four|five|six|seven|eight|nine))(?:[^a-zA-Z]|$)', text)
    if compound:
        total = 30 + _ENGLISH_NUMBERS.get(compound.group(2), 0)
        if total > 0:
            return total

    for word, num in sorted(_ENGLISH_NUMBERS.items(), key=lambda x: -len(x[0])):
        if re.search(r'(?:^|[^a-zA-Z])' + word + r'(?:[^a-zA-Z]|$)', text):
            return num

    return None


def _extract_title_key(text):
    cleaned = _remove_brackets(text)
    cleaned = _remove_noise(cleaned)
    cleaned = re.sub(r'[\s._\-–—：:]+', ' ', cleaned)
    cleaned = re.sub(r'(?i)\b(ep|episode|e)\b', ' ', cleaned)
    cleaned = re.sub(r'第\s*\d+\s*[話话集回]', ' ', cleaned)
    cleaned = re.sub(r'\b\d+\b', ' ', cleaned)
    cleaned = cleaned.lower().strip()

    exclusion = {
        'season', 'cour', 'part', 'episode', 'ep', 'ova', 'oad', 'ona',
        'special', 'movie', 'the', 'and', 'of', 'no', 'to', 'in', 'at',
        'vietsub', 'english', 'sub', 'dub', 'raw', 'uncensored',
        'vostfr', 'bd', 'dvd', 'web', 'dl', 'rip', 'avc', 'aac',
        'webdl', 'webrip', 'bdmv', 'batch', 'complete',
    }

    tokens = []
    for t in cleaned.split():
        if len(t) >= 2 and t not in exclusion:
            tokens.append(t)
        elif len(t) == 1 and t.isalpha() and t not in exclusion:
            tokens.append(t)

    return ' '.join(sorted(set(tokens)))


def _detect_special_type(text):
    m = re.search(r'(?i)(?:^|[^a-zA-Z])(OVA|OAD|ONA|OAV|NCED|NCOP|PV|CM|EX|EXTRA|BONUS|PROLOGUE|EPILOGUE)(?:[^a-zA-Z]|$)', text)
    if m:
        return m.group(1).upper()

    m = re.search(r'(?i)(?:^|[^a-zA-Z])SP(?:[^a-zA-Z]|$)', text)
    if m:
        return 'SP'

    m = re.search(r'(?i)(?:^|[^a-zA-Z])SPECIAL(?:[^a-zA-Z]|$)', text)
    if m:
        return 'SPECIAL'

    m = re.search(r'(?i)(?:^|[^a-zA-Z])(OP|ED)\d*(?:[^a-zA-Z]|$)', text)
    if m:
        return m.group(1).upper()

    m = re.search(r'(?i)(?:^|[^a-zA-Z])TRAILER(?:[^a-zA-Z]|$)', text)
    if m:
        return 'TRAILER'

    m = re.search(r'(?i)(?:^|[^a-zA-Z])TEASER(?:[^a-zA-Z]|$)', text)
    if m:
        return 'TEASER'

    m = re.search(r'(?i)(?:^|[^a-zA-Z])PREVIEW(?:[^a-zA-Z]|$)', text)
    if m:
        return 'PREVIEW'

    m = re.search(r'(?i)(?:^|[^a-zA-Z])MENU(?:[^a-zA-Z]|$)', text)
    if m:
        return 'MENU'

    return None


def _detect_batch(text):
    """
    Đã sửa lỗi:
    Trước đây "Season 2 - 04" bị coi là batch range 2-04.
    Bây giờ các dạng "Season X - Y" hoặc "SX - Y" sẽ được coi là
    season/episode bình thường, trừ khi nó là một range dài hơn như
    "Season 2 - 04-12".
    """
    for m in _RE_BATCH_RANGE.finditer(text):
        start = int(m.group(1))
        end = int(m.group(2))

        if end > start and end - start >= 2:
            before = text[:m.start(1)]
            after = text[m.end(2):]

            looks_like_season_episode = False

            # "Season 2 - 04"
            if re.search(r'(?i)\bseason\s*$', before):
                looks_like_season_episode = True

            # "S2 - 04"
            elif re.search(r'(?i)(?:^|[^a-zA-Z0-9])s\s*$', before):
                looks_like_season_episode = True

            if looks_like_season_episode:
                # Nếu phía sau vẫn còn một range khác, ví dụ:
                # "Season 2 - 04-12" thì vẫn coi là batch/multi-range.
                if re.match(r'\s*[-~]\s*\d{1,4}', after):
                    return True

                # Bỏ qua vì đây nhiều khả năng là Season X - Episode Y
                continue

            return True

    if re.search(r'(?i)\b(batch|complete|full\s*series|all\s*episodes)\b', text):
        return True

    return False


def _extract_part(text):
    m = re.search(r'(?i)(?:^|[^a-zA-Z])Part\s*([12])\b', text)
    if m:
        return int(m.group(1))

    m = re.search(r'(?i)(?:^|[^a-zA-Z])([12])\s*Part\b', text)
    if m:
        return int(m.group(1))

    m = re.search(r'(?i)(?:^|[^a-zA-Z\d])(\d+)(?:st|nd|rd|th)\s+(?:Season|Part|Cour)\b', text)
    if m:
        return int(m.group(1))

    return None


def _extract_cour(text):
    m = re.search(r'(?i)(?:^|[^a-zA-Z])Cour\s*(\d{1,2})\b', text)
    if m:
        return int(m.group(1))

    m = re.search(r'(?i)(?:^|[^a-zA-Z])(\d{1,2})(?:st|nd|rd|th)\s+Cour\b', text)
    if m:
        return int(m.group(1))

    m = re.search(r'(?i)(?:^|[^a-zA-Z])第\s*(\d{1,2})\s*クール', text)
    if m:
        return int(m.group(1))

    return None


def _detect_language(norm_text, ext, stem):
    low = norm_text.lower()
    ext_low = ext.lower()
    stem_low = stem.lower()

    if '.vi' in ext_low or '.vie' in ext_low:
        return 'vi'

    if '.en' in ext_low or '.eng' in ext_low:
        return 'en'

    if '.ja' in ext_low or '.jp' in ext_low or '.jpn' in ext_low:
        return 'ja'

    if '.zh' in ext_low or '.chi' in ext_low:
        return 'zh'

    if '.ko' in ext_low or '.kor' in ext_low:
        return 'ko'

    for suffix in ['.vi', '.vie', '.en', '.eng', '.ja', '.jp', '.jpn', '.zh', '.zh-hans', '.zh-hant', '.zh_cn', '.zh_tw', '.chi', '.ko', '.kor']:
        if stem_low.endswith(suffix):
            return suffix.lstrip('.')[:2]

    if 'vietsub' in low or 'phu de' in low or 'phụ đề' in low:
        return 'vi'

    if 'english' in low or ' eng ' in low:
        return 'en'

    if 'japanese' in low or 'raw' in low:
        return 'ja'

    return None


def _extract_episode_parts(no_fw_text, orig_text, special_type):
    name = no_fw_text

    m = re.search(r'(?i)(?:^|[^a-zA-Z\d])s(\d{1,2})[\s._\-]*e(\d{1,4})(?=[^a-zA-Z\d]|$)', name)
    if m:
        season = int(m.group(1))
        episode = int(m.group(2))
        if 0 < season < 100 and 0 < episode < 2000:
            return season, episode

    m = re.search(r'(?i)(?:^|[^a-zA-Z\d])season[\s._\-]*(\d{1,2})[\s._\-]*(?:episode|ep|e)[\s._\-]*(\d{1,4})(?=[^a-zA-Z\d]|$)', name)
    if m:
        season = int(m.group(1))
        episode = int(m.group(2))
        if 0 < season < 100 and 0 < episode < 2000:
            return season, episode

    m = re.search(r'(?i)(?:^|[^a-zA-Z\d])season\s*(\d{1,2})\s*[\-–—]\s*(\d{1,4})(?=[^a-zA-Z\d]|$)', name)
    if m:
        season = int(m.group(1))
        episode = int(m.group(2))
        if 0 < season < 100 and 0 < episode < 2000 and not _is_noise_number(episode):
            return season, episode

    m = re.search(r'(?i)(?:^|[^a-zA-Z\d])s(\d{1,2})\s*[\-–—]\s*(\d{1,4})(?=[^a-zA-Z\d]|$)', name)
    if m:
        season = int(m.group(1))
        episode = int(m.group(2))
        if 0 < season < 100 and 0 < episode < 2000 and not _is_noise_number(episode):
            text_between = name[m.end(1):m.start(2)]
            if re.match(r'\s*[\-–—]\s*', text_between):
                return season, episode

    m = re.search(r'(?:^|[^a-zA-Z\d])(\d{1,2})[xX](\d{2,4})(?=[^a-zA-Z\d]|$)', name)
    if m:
        season = int(m.group(1))
        episode = int(m.group(2))
        if 0 < season < 100 and 0 < episode < 2000:
            return season, episode

    m = re.search(r'(?i)(?:^|[^a-zA-Z])Season\s+([IVX]+)\s+Episode\s+([IVX]+|\d+)(?:[^a-zA-Z\d]|$)', name)
    if m:
        rs = m.group(1).upper()
        re_ = m.group(2).upper()

        season = _ROMAN_NUMERALS.get(rs)

        if re_ in _ROMAN_NUMERALS:
            episode = _ROMAN_NUMERALS[re_]
        else:
            episode = int(re_) if re_.isdigit() and 0 < int(re_) < 2000 else None

        if season and episode:
            return season, episode

    m = _RE_VIET_TAP.search(name)
    if m:
        episode = int(m.group(2))
        if 0 < episode < 2000:
            return None, episode

    m = re.search(
        r'(?:^|(?<=[^a-zA-Z]))'
        r'(?:[Ee][Pp](?:[Ii][Ss][Oo][Dd][Ee])?'
        r'|EPISODE|Episode|episode'
        r'|(?<![sS])[Ee](?=[^a-zA-Z]|\d))'
        r'[\s._\-]*'
        r'(\d{1,4})'
        r'(?=[^a-zA-Z\d]|$)',
        name
    )
    if m:
        episode = int(m.group(1))
        if 0 < episode < 2000:
            return None, episode

    m = re.search(r'第\s*(\d{1,4})\s*[話话集回章幕弾弹]', orig_text)
    if m:
        episode = int(m.group(1))
        if 0 < episode < 2000:
            return None, episode

    m = re.search(r'第\s*(\d{1,4})\s*[話话集回章幕弾弹]', no_fw_text)
    if m:
        episode = int(m.group(1))
        if 0 < episode < 2000:
            return None, episode

    cn_match = re.search(r'([零〇一二三四五六七八九十百千壱弐参肆伍陸漆捌玖拾壹貳參兩]+)\s*[話话集回章幕弾弹]', orig_text)
    if cn_match:
        num, _ = _parse_chinese_numeral(cn_match.group(1))
        if num and 0 < num < 2000:
            return None, num

    m = re.search(r'(?:^|[^\d])(\d{1,4})\s*[話话集回章幕弾弹](?![a-zA-Z])', orig_text)
    if m:
        episode = int(m.group(1))
        if 0 < episode < 2000 and not _is_noise_number(episode):
            return None, episode

    en_num = parse_english_number_words(name)
    if en_num and 0 < en_num < 2000:
        return None, en_num

    m = re.search(r'#\s*(\d{1,4})', name)
    if m:
        episode = int(m.group(1))
        if 0 < episode < 2000:
            return None, episode

    if special_type:
        m = re.search(
            r'(?:^|[^a-zA-Z])'
            + re.escape(special_type)
            + r'[\s._\-]*(\d{1,3})',
            name, re.IGNORECASE
        )
        if m:
            episode = int(m.group(1))
            if 0 < episode < 500:
                return None, episode

    m = re.search(
        r'(?i)(?:^|[^a-zA-Z])'
        r'(?:Vol(?:ume)?|DVD|BD|Disc|DISC|Part|PART|Chapter|CHAPTER|'
        r'Ch\.?\s*\d|Arc|ARC|Movie|Film)'
        r'\.?\s*[-._]?\s*(\d{1,4})'
        r'(?=[^a-zA-Z\d]|$)',
        name
    )
    if m:
        val = int(m.group(1))
        part_end = m.end()
        after = name[part_end:]
        has_later_ep = re.search(r'[-–—]\s*\d{1,4}', after)

        if has_later_ep:
            pass
        elif 0 < val < 1000 and not _is_noise_number(val):
            return None, val

    cleaned = _clean_for_number_search(name)
    candidates = []

    for m in re.finditer(
        r'[\s_]+[-–—]\s*(\d{1,4})(?:[vV]\d)?(?=[\s_.\[\])\'"\-,;!?]|$)',
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
    cleaned = _RE_FILESIZE.sub('', cleaned)

    cleaned = re.sub(r'(?i)(?:^|[^a-zA-Z])s\d{1,2}(?:[^a-zA-Z\d]|$)', ' ', cleaned)
    cleaned = re.sub(r'(?i)season\s*\d{1,2}', ' ', cleaned)
    cleaned = re.sub(r'(?i)\b\d{2}s\b', ' ', cleaned)
    cleaned = re.sub(r'\b\d+\.\d+\b', ' ', cleaned)

    return cleaned


def _title_similarity(a_key, b_key):
    if not a_key or not b_key:
        return 0.0

    a_tokens = set(a_key.split())
    b_tokens = set(b_key.split())

    if not a_tokens or not b_tokens:
        return 0.0

    inter = a_tokens & b_tokens
    union = a_tokens | b_tokens

    if not union:
        return 0.0

    return len(inter) / len(union)


def _lang_score(lang):
    if lang is None:
        return 0.5

    low = lang.lower()

    for i, preferred in enumerate(PREFERRED_LANGS):
        if low == preferred:
            return 1.0 - i * 0.05

    return 0.4


def _score_match(video, sub):
    score = 0.0
    reasons = []

    if video.episode is not None and sub.episode is not None:
        if video.episode == sub.episode:
            score += 50.0
            reasons.append('episode_match')
        else:
            return -1.0, ['episode_mismatch']

    if video.season is not None and sub.season is not None:
        if video.season == sub.season:
            score += 25.0
            reasons.append('season_match')
        else:
            return -1.0, ['season_mismatch']
    elif video.season is not None or sub.season is not None:
        score -= 5.0
        reasons.append('partial_season')

    if video.special_type and sub.special_type:
        if video.special_type == sub.special_type:
            score += 20.0
            reasons.append('special_match')
        else:
            return -1.0, ['special_mismatch']
    elif video.special_type or sub.special_type:
        if video.special_type not in (None, 'SP', 'SPECIAL') and sub.special_type not in (None, 'SP', 'SPECIAL'):
            score -= 10.0
            reasons.append('partial_special')

    if video.part is not None and sub.part is not None:
        if video.part == sub.part:
            score += 5.0
            reasons.append('part_match')

    if video.cour is not None and sub.cour is not None:
        if video.cour == sub.cour:
            score += 5.0
            reasons.append('cour_match')

    title_sim = _title_similarity(video.title_key, sub.title_key)
    if title_sim > 0:
        score += title_sim * 15.0
        reasons.append('title_{:.2f}'.format(title_sim))

    lang_s = _lang_score(sub.language)
    score += lang_s * 5.0
    reasons.append('lang_{}'.format(sub.language or 'none'))

    if video.is_batch or sub.is_batch:
        score -= 30.0
        reasons.append('batch_penalty')

    return score, reasons


def _find_best_matches(video_infos, sub_infos):
    videos = [v for v in video_infos if v.is_renamable()]
    subs = [s for s in sub_infos if s.is_renamable()]

    candidates = []

    for vi, video in enumerate(videos):
        for si, sub in enumerate(subs):
            score, reasons = _score_match(video, sub)
            if score >= 20.0:
                candidates.append((score, vi, si, reasons))

    candidates.sort(key=lambda x: -x[0])

    matched_videos = set()
    matched_subs = set()
    matches = []
    warnings = []

    for score, vi, si, reasons in candidates:
        if vi in matched_videos or si in matched_subs:
            continue

        video = videos[vi]
        sub = subs[si]

        other_scores = []

        for c2 in candidates:
            if c2[1] == vi and c2[2] != si and c2[2] not in matched_subs:
                other_scores.append(c2[0])

        for c2 in candidates:
            if c2[2] == si and c2[1] != vi and c2[1] not in matched_videos:
                other_scores.append(c2[0])

        ambiguous = False
        for os_score in other_scores:
            if abs(score - os_score) < 3.0:
                ambiguous = True
                break

        if ambiguous:
            vcode = video.episode_code() or video.filename
            scode = sub.filename
            warnings.append("  Cảnh báo: Cặp {} - {} quá mơ hồ (điểm {:.1f}), bỏ qua.".format(vcode, sub.filename, score))
            continue

        matches.append((video, sub, score))
        matched_videos.add(vi)
        matched_subs.add(si)

    for vi, video in enumerate(videos):
        if vi not in matched_videos:
            vcode = video.episode_code() or video.filename
            warnings.append("  Cảnh báo: Video tập {} không có sub tương ứng.".format(vcode))

    for si, sub in enumerate(subs):
        if si not in matched_subs:
            warnings.append("  Cảnh báo: Sub {} không có video tương ứng.".format(sub.filename))

    return matches, warnings


def rename_subs():
    current_folder = os.getcwd()
    files = os.listdir(current_folder)

    videos = sorted(f for f in files if any(f.lower().endswith(ext) for ext in VIDEO_EXTS))
    subs = sorted(f for f in files if any(f.lower().endswith(ext) for ext in SUB_EXTS))

    print("Tìm thấy {} file video và {} file phụ đề".format(len(videos), len(subs)))

    if len(videos) == 0 or len(subs) == 0:
        print("Lỗi: Không tìm thấy video hoặc phụ đề.")
        return

    video_infos = [MediaInfo(f) for f in videos]
    sub_infos = [MediaInfo(f) for f in subs]

    for v in video_infos:
        if v.warnings:
            for w in v.warnings:
                if w == 'non_episode_file':
                    print("  Bỏ qua video (không phải tập thường): {}".format(v.filename))

    if len(videos) != len(subs):
        print("  Cảnh báo: Số lượng không khớp (Video: {} vs Phụ đề: {}).".format(len(videos), len(subs)))
        print("  Tiếp tục xử lý các cặp khớp được...")

    matches, warnings = _find_best_matches(video_infos, sub_infos)

    for w in warnings:
        print(w)

    match_count = 0
    renamed = []

    for video, sub, score in matches:
        video_name_no_ext, _ = os.path.splitext(video.filename)
        new_sub_name = video_name_no_ext + sub.ext

        if sub.filename == new_sub_name:
            print("  [Bỏ qua] {} đã đúng tên.".format(sub.filename))
            continue

        if os.path.exists(new_sub_name):
            existing_lower = new_sub_name.lower()
            sub_lower = sub.filename.lower()

            if existing_lower == sub_lower:
                print("  [Bỏ qua] {} đã đúng tên.".format(sub.filename))
                continue

            print("  Cảnh báo: Không thể đổi tên {} thành {} vì file đích đã tồn tại.".format(sub.filename, new_sub_name))
            continue

        if DRY_RUN:
            print("  DRY_RUN: {} -> {} (điểm: {:.1f})".format(sub.filename, new_sub_name, score))
            match_count += 1
        else:
            try:
                temp_name = sub.filename + '.tmp_rename'

                if os.path.exists(temp_name):
                    temp_name = sub.filename + '.tmp_rename_' + str(os.getpid())

                os.rename(sub.filename, temp_name)
                os.rename(temp_name, new_sub_name)

                renamed.append((sub.filename, new_sub_name))
                print("  Đổi tên: {} -> {}".format(sub.filename, new_sub_name))
                match_count += 1

            except Exception as e:
                print("  Lỗi khi đổi tên {}: {}".format(sub.filename, e))

    print("Đổi tên xong: {} file.\n".format(match_count))


def ass_time_to_srt_time(ass_time):
    parts = ass_time.strip().split(':')
    h = int(parts[0])
    m = int(parts[1])

    sec_parts = parts[2].split('.')
    s = int(sec_parts[0])
    cs = int(sec_parts[1].ljust(2, '0')[:2])

    return "{:02d}:{:02d}:{:02d},{:03d}".format(h, m, s, cs * 10)


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
        raise ValueError("Không thể đọc file: {}".format(ass_filepath))

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
            f.write("{}\n".format(i))
            f.write("{} --> {}\n".format(ass_time_to_srt_time(start), ass_time_to_srt_time(end)))
            f.write("{}\n\n".format(text))

    return srt_filepath


def convert_all_ass_to_srt():
    current_folder = os.getcwd()
    ass_files = sorted(f for f in os.listdir(current_folder) if f.lower().endswith('.ass'))

    if not ass_files:
        print("Không tìm thấy file .ass nào.")
        return

    print("Tìm thấy {} file .ass".format(len(ass_files)))

    success = 0

    for ass_file in ass_files:
        try:
            srt_file = convert_ass_to_srt(ass_file)
            os.remove(ass_file)
            print("  OK: {} -> {}".format(ass_file, os.path.basename(srt_file)))
            success += 1
        except Exception as e:
            print("  Lỗi: {} -> {}".format(ass_file, e))

    print("Chuyển đổi xong: {}/{} file.\n".format(success, len(ass_files)))


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
