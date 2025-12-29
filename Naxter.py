import sys
import os
import json
import time
import subprocess
import shutil
import requests
import concurrent.futures
import re
from PIL import Image
from bs4 import BeautifulSoup
from PyQt5.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout,
                            QHBoxLayout, QLabel, QLineEdit, QPushButton,
                            QProgressBar, QTextEdit, QMessageBox, QFrame,
                            QDialog, QRadioButton, QButtonGroup, QListWidget,
                            QListWidgetItem, QAbstractItemView)
from PyQt5.QtCore import QThread, pyqtSignal, Qt, QPropertyAnimation, QEasingCurve

FFMPEG_DATEI = "ffmpeg.exe"

ROSA_STIL = """
QMainWindow, QWidget {
background-color: #1c1520;
color: #f0e6ef;
font-family: 'Segoe UI', Arial;
}
QLabel {
color: #f8c8dc;
}
QLineEdit {
background-color: #2a1f2d;
border: 2px solid #d4749a;
border-radius: 10px;
padding: 10px 14px;
color: #fff;
font-size: 13px;
}
QLineEdit:focus {
border-color: #f4a5c7;
background-color: #2f2333;
}
QLineEdit:hover {
border-color: #e890b0;
}
QPushButton {
background-color: #c44d80;
color: white;
border: none;
border-radius: 10px;
padding: 10px 18px;
font-weight: bold;
font-size: 12px;
}
QPushButton:hover {
background-color: #d65d90;
}
QPushButton:pressed {
background-color: #a33d68;
}
QPushButton:disabled {
background-color: #3d3040;
color: #6a5a68;
}
QPushButton#stopBtn {
background-color: #5a4560;
}
QPushButton#stopBtn:hover {
background-color: #6a5570;
}
QPushButton#stopBtn:disabled {
background-color: #3d3040;
}
QPushButton#compressBtn {
background-color: #7b4b94;
}
QPushButton#compressBtn:hover {
background-color: #8b5ba4;
}
QPushButton#compressBtn:disabled {
background-color: #3d3040;
}
QPushButton#pdfBtn {
background-color: #4b7b94;
}
QPushButton#pdfBtn:hover {
background-color: #5b8ba4;
}
QPushButton#pdfBtn:disabled {
background-color: #3d3040;
}
QProgressBar {
border: 2px solid #d4749a;
border-radius: 8px;
background-color: #2a1f2d;
text-align: center;
color: white;
font-weight: bold;
}
QProgressBar::chunk {
background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
stop:0 #c44d80, stop:0.5 #d4749a, stop:1 #e890b0);
border-radius: 6px;
}
QTextEdit {
background-color: #2a1f2d;
border: 2px solid #4a3a4d;
border-radius: 10px;
color: #e8d0e0;
padding: 10px;
font-size: 11px;
line-height: 1.4;
}
QTextEdit:focus {
border-color: #d4749a;
}
QScrollBar:vertical {
background-color: #2a1f2d;
width: 10px;
border-radius: 5px;
margin: 2px;
}
QScrollBar::handle:vertical {
background-color: #d4749a;
border-radius: 5px;
min-height: 30px;
}
QScrollBar::handle:vertical:hover {
background-color: #e890b0;
}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
height: 0px;
}
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {
background: none;
}
QDialog {
background-color: #1c1520;
border-radius: 12px;
}
QRadioButton {
color: #f8c8dc;
spacing: 10px;
font-size: 13px;
padding: 5px;
}
QRadioButton::indicator {
width: 16px;
height: 16px;
}
QRadioButton::indicator:unchecked {
border: 2px solid #d4749a;
border-radius: 8px;
background-color: #2a1f2d;
}
QRadioButton::indicator:checked {
border: 2px solid #f4a5c7;
border-radius: 8px;
background-color: #c44d80;
}
QRadioButton::indicator:hover {
border-color: #f4a5c7;
}
QMessageBox {
background-color: #1c1520;
}
QMessageBox QLabel {
color: #f8c8dc;
font-size: 13px;
}
QMessageBox QPushButton {
min-width: 80px;
min-height: 30px;
}
QListWidget {
background-color: #2a1f2d;
border: 2px solid #d4749a;
border-radius: 10px;
color: #e8d0e0;
padding: 5px;
font-size: 12px;
}
QListWidget::item {
padding: 8px;
border-radius: 5px;
}
QListWidget::item:selected {
background-color: #c44d80;
color: white;
}
QListWidget::item:hover {
background-color: #3a2f3d;
}
"""


class FormatAuswahl(QDialog):
    def __init__(self, eltern=None):
        super().__init__(eltern)
        self.setWindowTitle("Output Format")
        self.setFixedSize(320, 220)
        self.setModal(True)

        aufbau = QVBoxLayout(self)
        aufbau.setSpacing(12)
        aufbau.setContentsMargins(25, 20, 25, 20)
        
        ueberschrift = QLabel("Select output format:")
        ueberschrift.setStyleSheet("font-size: 14px; font-weight: bold; color: #f4a5c7;")
        aufbau.addWidget(ueberschrift)
        
        self.knopf_gruppe = QButtonGroup(self)
        
        self.option_png = QRadioButton("PNG - Lossless Quality")
        self.option_jpg = QRadioButton("JPG - Maximum Quality")
        self.option_png.setChecked(True)
        
        self.knopf_gruppe.addButton(self.option_png)
        self.knopf_gruppe.addButton(self.option_jpg)
        
        aufbau.addWidget(self.option_png)
        aufbau.addWidget(self.option_jpg)
        aufbau.addSpacing(5)
        
        bestaetigen = QPushButton("Continue")
        bestaetigen.setMinimumHeight(36)
        bestaetigen.clicked.connect(self.accept)
        aufbau.addWidget(bestaetigen)

    def gewaehltes_format(self):
        return "png" if self.option_png.isChecked() else "jpg"


class EpisodenAuswahl(QDialog):
    def __init__(self, episoden_liste, aktuelle_episode, eltern=None):
        super().__init__(eltern)
        self.setWindowTitle("Select Episode")
        self.setFixedSize(500, 400)
        self.setModal(True)
        self.gewaehlte_episode = None

        aufbau = QVBoxLayout(self)
        aufbau.setSpacing(12)
        aufbau.setContentsMargins(25, 20, 25, 20)
        
        ueberschrift = QLabel("Select episode to download:")
        ueberschrift.setStyleSheet("font-size: 14px; font-weight: bold; color: #f4a5c7;")
        aufbau.addWidget(ueberschrift)
        
        info_text = "Current: Selection Required"
        if aktuelle_episode:
            info_text = f"Current: Episode {aktuelle_episode.get('number', '?')} - {aktuelle_episode.get('title', '')}"
        
        aktuell_label = QLabel(info_text)
        aktuell_label.setStyleSheet("font-size: 11px; color: #9a8a98; margin-bottom: 5px;")
        aufbau.addWidget(aktuell_label)
        
        self.listen_widget = QListWidget()
        self.listen_widget.setSelectionMode(QAbstractItemView.SingleSelection)
        
        for ep in episoden_liste:
            status = " [LOCKED]" if not ep.get('is_active', True) else ""
            titel = ep.get('title') or "No Title"
            nummer = ep.get('number') or "?"
            text = f"Episode {nummer}: {titel}{status}"
            item = QListWidgetItem(text)
            item.setData(Qt.UserRole, ep)
            if not ep.get('is_active', True):
                item.setForeground(Qt.gray)
            self.listen_widget.addItem(item)
            if aktuelle_episode and ep.get('id') == aktuelle_episode.get('id'):
                self.listen_widget.setCurrentItem(item)
        
        aufbau.addWidget(self.listen_widget)
        
        knopf_layout = QHBoxLayout()
        
        if aktuelle_episode:
            aktuell_knopf = QPushButton("Use Current Episode")
            aktuell_knopf.setMinimumHeight(36)
            aktuell_knopf.clicked.connect(self.aktuelle_verwenden)
            knopf_layout.addWidget(aktuell_knopf)
        
        bestaetigen = QPushButton("Download Selected")
        bestaetigen.setMinimumHeight(36)
        bestaetigen.clicked.connect(self.auswahl_bestaetigen)
        
        knopf_layout.addWidget(bestaetigen)
        aufbau.addLayout(knopf_layout)
        
        self.aktuelle_episode = aktuelle_episode

    def aktuelle_verwenden(self):
        self.gewaehlte_episode = self.aktuelle_episode
        self.accept()

    def auswahl_bestaetigen(self):
        aktuelles_item = self.listen_widget.currentItem()
        if aktuelles_item:
            self.gewaehlte_episode = aktuelles_item.data(Qt.UserRole)
            self.accept()

    def hole_gewaehlte_episode(self):
        return self.gewaehlte_episode


class ComicWalkerParser:
    def __init__(self, url):
        self.url = url
        self.daten = {}
        self.episode = {}
        self._daten_parsen()

    def _daten_parsen(self):
        antwort = requests.get(
            self.url,
            headers={
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36'
            }
        )
        antwort.raise_for_status()
        suppe = BeautifulSoup(antwort.text, 'html.parser')
        skript_tag = suppe.find('script', {'id': '__NEXT_DATA__'})
        if not skript_tag:
            raise ValueError("ComicWalker __NEXT_DATA__ script tag not found")
        json_daten = json.loads(skript_tag.string)
        werk = json_daten['props']['pageProps']['dehydratedState']['queries'][0]['state']['data']['work']
        erste_episoden = json_daten['props']['pageProps']['dehydratedState']['queries'][0]['state']['data']['firstEpisodes']
        episode = json_daten['props']['pageProps']['dehydratedState']['queries'][2]['state']['data']['episode']
        if not werk or not erste_episoden or not episode:
            raise ValueError('Missing essential ComicWalker data for parsing')
        self.daten = {'work': werk, 'firstEpisodes': erste_episoden, 'episode': episode}

    def episode_setzen(self, ep_nummer=None):
        if ep_nummer is None:
            self.episode = self.daten['episode']
            return
        self.episode = next((ep for ep in self.daten['firstEpisodes']['result'] if ep['internal']['episodeNo'] == ep_nummer), None)

    def episodenliste_holen(self, nur_aktive=False):
        ep_liste = []
        for ep in self.daten['firstEpisodes']['result']:
            if nur_aktive and not ep['isActive']:
                continue
            ep_liste.append({
                'number': ep['internal']['episodeNo'],
                'title': ep['title'],
                'is_active': ep['isActive'],
                'id': ep['id']
            })
        return ep_liste

    def aktuelle_episode_holen(self):
        return {
            'number': self.daten['episode']['internal']['episodeNo'],
            'title': self.daten['episode']['title'],
            'id': self.daten['episode']['id']
        }

    def werk_titel_holen(self):
        return self.daten['work'].get('title', 'Unknown_Comic')


class MangaDexParser:
    def __init__(self, url):
        self.url = url
        self.api_basis = "https://api.mangadex.org"
        self.titel_daten = {}
        self.kapitel_liste = []
        self.manga_id = None
        self._url_analysieren()

    def _url_analysieren(self):
        muster = r"mangadex\.org\/title\/([a-f0-9\-]+)"
        treffer = re.search(muster, self.url)
        if treffer:
            self.manga_id = treffer.group(1)
        else:
            raise ValueError("Invalid MangaDex Title URL")

    def metadaten_laden(self):
        if not self.manga_id:
            return
        
        try:
            antwort = requests.get(f"{self.api_basis}/manga/{self.manga_id}", timeout=10)
            antwort.raise_for_status()
            json_daten = antwort.json()
            attr = json_daten['data']['attributes']
            titel_dict = attr.get('title', {})
            self.titel_daten['title'] = titel_dict.get('en') or next(iter(titel_dict.values()), 'Unknown Manga')
        except Exception:
            self.titel_daten['title'] = 'Unknown Manga'

        try:
            params = {
                'limit': 100,
                'manga': self.manga_id,
                'order[chapter]': 'desc'
            }
            antwort = requests.get(f"{self.api_basis}/chapter", params=params, timeout=10)
            antwort.raise_for_status()
            self.kapitel_liste = antwort.json().get('data', [])
        except Exception as e:
            raise ValueError(f"Failed to fetch chapters: {str(e)}")

    def episodenliste_holen(self):
        liste = []
        for kap in self.kapitel_liste:
            attr = kap['attributes']
            sprache = attr.get('translatedLanguage', 'xx')
            kap_num = attr.get('chapter', '0')
            titel = attr.get('title') or ""
            
            anzeige_titel = f"[{sprache.upper()}] {titel}" if titel else f"[{sprache.upper()}] Chapter {kap_num}"
            
            liste.append({
                'number': kap_num,
                'title': anzeige_titel,
                'is_active': True,
                'id': kap['id'],
                'lang': sprache
            })
        return liste

    def werk_titel_holen(self):
        return self.titel_daten.get('title', 'MangaDex_Download')


class MangaDexThread(QThread):
    signal_protokoll = pyqtSignal(str)
    signal_fortschritt = pyqtSignal(int)
    signal_status = pyqtSignal(str)
    signal_fertig = pyqtSignal(bool, str)
    signal_fehler = pyqtSignal(str)

    def __init__(self, ffmpeg_pfad, ausgabe_format, kapitel_daten, werk_titel):
        super().__init__()
        self.ffmpeg_pfad = ffmpeg_pfad
        self.ausgabe_format = ausgabe_format
        self.kapitel_daten = kapitel_daten
        self.werk_titel = werk_titel
        self.ist_aktiv = True
        self.sitzung = requests.Session()
        self.sitzung.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
        })
        self.alles_erfolgreich = True
        self.speicher_ort = ""
        self.max_arbeiter = min(os.cpu_count() or 4, 8)
        self.abgeschlossen_counter = 0
        self.gesamt_dateien = 0

    def stoppen(self):
        self.ist_aktiv = False

    def dateiname_bereinigen(self, name):
        return "".join(c for c in name if c.isalnum() or c in " ._-").strip()

    def task_konvertieren(self, eingabe_pfad, ausgabe_pfad, seite_nummer):
        if not self.ist_aktiv:
            return False

        start_info = None
        if os.name == 'nt':
            start_info = subprocess.STARTUPINFO()
            start_info.dwFlags |= subprocess.STARTF_USESHOWWINDOW

        if self.ausgabe_format == "png":
            befehl = [
                self.ffmpeg_pfad, '-y', '-v', 'error',
                '-i', eingabe_pfad,
                '-pred', 'mixed',
                '-compression_level', '8', 
                ausgabe_pfad
            ]
        else:
            befehl = [
                self.ffmpeg_pfad, '-y', '-v', 'error',
                '-i', eingabe_pfad,
                '-q:v', '1', 
                ausgabe_pfad
            ]
        
        try:
            subprocess.run(befehl, startupinfo=start_info, check=True, timeout=60)
            if os.path.exists(eingabe_pfad):
                os.remove(eingabe_pfad)
            return True
        except Exception:
            return False

    def bild_herunterladen(self, url, pfad):
        try:
            with self.sitzung.get(url, stream=True, timeout=20) as antwort:
                antwort.raise_for_status()
                with open(pfad, 'wb') as f:
                    for stueck in antwort.iter_content(chunk_size=8192):
                        if not self.ist_aktiv:
                            return False
                        f.write(stueck)
            return True
        except:
            return False

    def on_task_done_wrapper(self, future, seite_nummer):
        try:
            if future.result():
                self.signal_protokoll.emit(f"Page {seite_nummer}: Ready.")
            else:
                self.signal_protokoll.emit(f"Page {seite_nummer}: Convert Error.")
                self.alles_erfolgreich = False
        except:
            self.alles_erfolgreich = False
        
        self.abgeschlossen_counter += 1
        if self.gesamt_dateien > 0:
            pct = int((self.abgeschlossen_counter / self.gesamt_dateien) * 100)
            self.signal_fortschritt.emit(pct)

    def run(self):
        pool = concurrent.futures.ThreadPoolExecutor(max_workers=self.max_arbeiter)
        
        try:
            kapitel_id = self.kapitel_daten.get('id')
            kapitel_num = self.kapitel_daten.get('number', '0')
            kapitel_titel = self.dateiname_bereinigen(self.kapitel_daten.get('title', 'Chapter'))
            werk_name = self.dateiname_bereinigen(self.werk_titel)

            self.signal_protokoll.emit(f"Source: MangaDex")
            self.signal_protokoll.emit(f"Manga: {self.werk_titel}")
            self.signal_protokoll.emit(f"Chapter: {kapitel_num}")
            
            self.signal_status.emit("Fetching At-Home data...")
            
            try:
                antwort = self.sitzung.get(f"https://api.mangadex.org/at-home/server/{kapitel_id}", timeout=15)
                antwort.raise_for_status()
                at_home_daten = antwort.json()
                base_url = at_home_daten['baseUrl']
                hash_wert = at_home_daten['chapter']['hash']
                dateien = at_home_daten['chapter']['data']
            except Exception as e:
                self.signal_fehler.emit(f"API Error: {str(e)}")
                return

            self.gesamt_dateien = len(dateien)
            if self.gesamt_dateien == 0:
                self.signal_fehler.emit("No pages found.")
                return

            ordner_name = f"{werk_name} - Ch{kapitel_num} - {kapitel_titel}"
            basis_verzeichnis = os.path.dirname(self.ffmpeg_pfad)
            self.speicher_ort = os.path.join(basis_verzeichnis, ordner_name)
            os.makedirs(self.speicher_ort, exist_ok=True)

            self.signal_protokoll.emit(f"Output: {self.speicher_ort}")
            self.signal_protokoll.emit(f"Total pages: {self.gesamt_dateien}")
            self.signal_protokoll.emit("-" * 40)

            for index, datei_name in enumerate(dateien):
                if not self.ist_aktiv:
                    break

                seite_nummer = index + 1
                bild_url = f"{base_url}/data/{hash_wert}/{datei_name}"
                ext = os.path.splitext(datei_name)[1]
                temp_name = f"{seite_nummer:03d}_temp{ext}"
                final_name = f"{seite_nummer:03d}.{self.ausgabe_format}"
                pfad_temp = os.path.join(self.speicher_ort, temp_name)
                pfad_final = os.path.join(self.speicher_ort, final_name)

                if os.path.exists(pfad_final):
                    self.signal_protokoll.emit(f"Page {seite_nummer}: Exists.")
                    self.abgeschlossen_counter += 1
                    self.signal_fortschritt.emit(int((self.abgeschlossen_counter/self.gesamt_dateien)*100))
                    continue

                self.signal_status.emit(f"Downloading Page {seite_nummer}...")
                
                dl_ok = False
                for _ in range(3):
                    if self.bild_herunterladen(bild_url, pfad_temp):
                        dl_ok = True
                        break
                    time.sleep(1)
                
                if not dl_ok:
                    self.signal_protokoll.emit(f"Download failed: Page {seite_nummer}")
                    self.alles_erfolgreich = False
                    self.abgeschlossen_counter += 1
                    continue

                future = pool.submit(self.task_konvertieren, pfad_temp, pfad_final, seite_nummer)
                future.add_done_callback(lambda f, s=seite_nummer: self.on_task_done_wrapper(f, s))
                time.sleep(0.1) 

            self.signal_status.emit("Finishing conversion tasks...")
            pool.shutdown(wait=True)
            
            if self.ist_aktiv:
                self.signal_status.emit("All done!")
                self.signal_protokoll.emit("All tasks finished.")
                
        except Exception as e:
            self.signal_fehler.emit(f"Error: {str(e)}")
            self.alles_erfolgreich = False
        finally:
            self.sitzung.close()
            self.signal_fertig.emit(self.alles_erfolgreich, self.speicher_ort)


class ComicWalkerThread(QThread):
    signal_protokoll = pyqtSignal(str)
    signal_fortschritt = pyqtSignal(int)
    signal_status = pyqtSignal(str)
    signal_fertig = pyqtSignal(bool, str)
    signal_fehler = pyqtSignal(str)

    def __init__(self, ziel_url, ffmpeg_pfad, ausgabe_format, episode_daten, werk_titel):
        super().__init__()
        self.ziel_url = ziel_url
        self.ffmpeg_pfad = ffmpeg_pfad
        self.ausgabe_format = ausgabe_format
        self.episode_daten = episode_daten
        self.werk_titel = werk_titel
        self.ist_aktiv = True
        self.sitzung = requests.Session()
        self.sitzung.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
        })
        self.alles_erfolgreich = True
        self.speicher_ort = ""
        self.max_arbeiter = min(os.cpu_count() or 4, 8)
        self.abgeschlossen_counter = 0
        self.gesamt_dateien = 0

    def stoppen(self):
        self.ist_aktiv = False

    def dateiname_bereinigen(self, name):
        return "".join(c for c in name if c.isalnum() or c in " ._-").strip()

    def task_konvertieren(self, eingabe_pfad, ausgabe_pfad, seite_nummer):
        if not self.ist_aktiv:
            return False

        start_info = None
        if os.name == 'nt':
            start_info = subprocess.STARTUPINFO()
            start_info.dwFlags |= subprocess.STARTF_USESHOWWINDOW

        if self.ausgabe_format == "png":
            befehl = [
                self.ffmpeg_pfad, '-y', '-v', 'error',
                '-i', eingabe_pfad,
                '-pred', 'mixed',
                '-compression_level', '8', 
                ausgabe_pfad
            ]
        else:
            befehl = [
                self.ffmpeg_pfad, '-y', '-v', 'error',
                '-i', eingabe_pfad,
                '-q:v', '1', 
                ausgabe_pfad
            ]
        
        try:
            subprocess.run(befehl, startupinfo=start_info, check=True, timeout=60)
            if os.path.exists(eingabe_pfad):
                os.remove(eingabe_pfad)
            return True
        except Exception:
            return False

    def seite_herunterladen_und_entschluesseln(self, seite, ausgabe_verzeichnis):
        drm_hash_hex = seite.get("drmHash")
        bild_url = seite.get("drmImageUrl")
        seiten_idx = seite.get("page")

        if not drm_hash_hex or not bild_url or not seiten_idx:
            raise ValueError("Missing essential page info for decryption")
        
        drm_hash = bytes.fromhex(drm_hash_hex)
        antwort = self.sitzung.get(bild_url, stream=True, timeout=30)
        antwort.raise_for_status()
        verschluesselte_daten = antwort.content
        entschluesselte_daten = bytes([b ^ drm_hash[i % len(drm_hash)] for i, b in enumerate(verschluesselte_daten)])

        datei_pfad = os.path.join(ausgabe_verzeichnis, f'{seiten_idx:03d}_temp.webp')
        with open(datei_pfad, "wb") as f:
            f.write(entschluesselte_daten)
        return datei_pfad, seiten_idx

    def on_task_done_wrapper(self, future, seite_nummer):
        try:
            if future.result():
                self.signal_protokoll.emit(f"Page {seite_nummer}: Ready.")
            else:
                self.signal_protokoll.emit(f"Page {seite_nummer}: Convert Error.")
                self.alles_erfolgreich = False
        except:
            self.alles_erfolgreich = False
        
        self.abgeschlossen_counter += 1
        if self.gesamt_dateien > 0:
            pct = int((self.abgeschlossen_counter / self.gesamt_dateien) * 100)
            self.signal_fortschritt.emit(pct)

    def run(self):
        pool = concurrent.futures.ThreadPoolExecutor(max_workers=self.max_arbeiter)
        
        try:
            self.signal_protokoll.emit(f"Multithreading enabled (Cores: {self.max_arbeiter})")
            self.signal_protokoll.emit(f"Source: ComicWalker")
            self.signal_protokoll.emit(f"Episode: {self.episode_daten.get('title', 'Unknown')}")
            
            episode_id = self.episode_daten.get('id')
            if not episode_id:
                self.signal_fehler.emit("Episode ID not found!")
                return

            self.signal_status.emit("Fetching page data...")
            
            try:
                api_url = f"https://comic-walker.com/api/contents/viewer?episodeId={episode_id}&imageSizeType=width%3A768"
                antwort = self.sitzung.get(api_url, timeout=15)
                antwort.raise_for_status()
                api_daten = antwort.json()
            except Exception as e:
                self.signal_fehler.emit(f"API Error: {str(e)}")
                return

            manuskripte = api_daten.get("manuscripts")
            if not manuskripte:
                self.signal_fehler.emit("No images available for this episode")
                return

            self.gesamt_dateien = len(manuskripte)
            
            werk_name = self.dateiname_bereinigen(self.werk_titel)
            episode_name = self.dateiname_bereinigen(self.episode_daten.get('title', 'Episode'))
            episode_nummer = self.episode_daten.get('number', 0)
            
            ordner_name = f"{werk_name} - EP{episode_nummer:03d} - {episode_name}"
            
            basis_verzeichnis = os.path.dirname(self.ffmpeg_pfad)
            self.speicher_ort = os.path.join(basis_verzeichnis, ordner_name)
            os.makedirs(self.speicher_ort, exist_ok=True)

            self.signal_protokoll.emit(f"Output: {self.speicher_ort}")
            self.signal_protokoll.emit(f"Total pages: {self.gesamt_dateien}")
            self.signal_protokoll.emit("-" * 40)

            for index, seite in enumerate(manuskripte):
                if not self.ist_aktiv:
                    break

                seite_nummer = seite.get("page", index + 1)
                final_name = f"{seite_nummer:03d}.{self.ausgabe_format}"
                pfad_final = os.path.join(self.speicher_ort, final_name)

                if os.path.exists(pfad_final):
                    self.signal_protokoll.emit(f"Page {seite_nummer}: Exists.")
                    self.abgeschlossen_counter += 1
                    self.signal_fortschritt.emit(int((self.abgeschlossen_counter/self.gesamt_dateien)*100))
                    continue

                self.signal_status.emit(f"Downloading Page {seite_nummer}...")
                
                try:
                    pfad_temp, seiten_idx = self.seite_herunterladen_und_entschluesseln(seite, self.speicher_ort)
                    future = pool.submit(self.task_konvertieren, pfad_temp, pfad_final, seiten_idx)
                    future.add_done_callback(lambda f, s=seiten_idx: self.on_task_done_wrapper(f, s))
                except Exception as e:
                    self.signal_protokoll.emit(f"Download failed: Page {seite_nummer} - {str(e)}")
                    self.alles_erfolgreich = False
                    self.abgeschlossen_counter += 1
                    continue

            self.signal_status.emit("Finishing conversion tasks...")
            pool.shutdown(wait=True)
            
            if self.ist_aktiv:
                self.signal_status.emit("All done!")
                self.signal_protokoll.emit("All tasks finished.")
                
        except Exception as e:
            self.signal_fehler.emit(f"Error: {str(e)}")
            self.alles_erfolgreich = False
        finally:
            self.sitzung.close()
            self.signal_fertig.emit(self.alles_erfolgreich, self.speicher_ort)


class ArbeitsThread(QThread):
    signal_protokoll = pyqtSignal(str)
    signal_fortschritt = pyqtSignal(int)
    signal_status = pyqtSignal(str)
    signal_fertig = pyqtSignal(bool, str)
    signal_fehler = pyqtSignal(str)

    def __init__(self, ziel_url, ffmpeg_pfad, ausgabe_format):
        super().__init__()
        self.ziel_url = ziel_url
        self.ffmpeg_pfad = ffmpeg_pfad
        self.ausgabe_format = ausgabe_format
        self.ist_aktiv = True
        self.sitzung = requests.Session()
        self.sitzung.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        })
        self.alles_erfolgreich = True
        self.speicher_ort = ""
        self.max_arbeiter = min(os.cpu_count() or 4, 8)
        self.abgeschlossen_counter = 0
        self.gesamt_dateien = 0

    def stoppen(self):
        self.ist_aktiv = False

    def dateiname_bereinigen(self, name):
        return "".join(c for c in name if c.isalnum() or c in " ._-").strip()

    def task_konvertieren(self, eingabe_pfad, ausgabe_pfad, seite_nummer):
        if not self.ist_aktiv:
            return False

        start_info = None
        if os.name == 'nt':
            start_info = subprocess.STARTUPINFO()
            start_info.dwFlags |= subprocess.STARTF_USESHOWWINDOW

        if self.ausgabe_format == "png":
            befehl = [
                self.ffmpeg_pfad, '-y', '-v', 'error',
                '-i', eingabe_pfad,
                '-pred', 'mixed',
                '-compression_level', '8', 
                ausgabe_pfad
            ]
        else:
            befehl = [
                self.ffmpeg_pfad, '-y', '-v', 'error',
                '-i', eingabe_pfad,
                '-q:v', '1', 
                ausgabe_pfad
            ]
        
        try:
            subprocess.run(befehl, startupinfo=start_info, check=True, timeout=60)
            if os.path.exists(eingabe_pfad):
                os.remove(eingabe_pfad)
            return True
        except Exception:
            return False

    def datei_herunterladen(self, url, pfad):
        try:
            with self.sitzung.get(url, stream=True, timeout=15) as antwort:
                antwort.raise_for_status()
                with open(pfad, 'wb') as f:
                    for stueck in antwort.iter_content(chunk_size=8192):
                        if not self.ist_aktiv:
                            return False
                        if stueck:
                            f.write(stueck)
            return True
        except:
            return False

    def on_task_done_wrapper(self, future, seite_nummer):
        try:
            if future.result():
                self.signal_protokoll.emit(f"Page {seite_nummer}: Ready.")
            else:
                self.signal_protokoll.emit(f"Page {seite_nummer}: Convert Error.")
                self.alles_erfolgreich = False
        except:
            self.alles_erfolgreich = False
        
        self.abgeschlossen_counter += 1
        if self.gesamt_dateien > 0:
            pct = int((self.abgeschlossen_counter / self.gesamt_dateien) * 100)
            self.signal_fortschritt.emit(pct)

    def run(self):
        pool = concurrent.futures.ThreadPoolExecutor(max_workers=self.max_arbeiter)
        
        try:
            self.signal_protokoll.emit(f"Multithreading enabled (Cores: {self.max_arbeiter})")
            self.signal_protokoll.emit(f"Source: Naxter")
            self.signal_protokoll.emit(f"Analyzing: {self.ziel_url}")
            
            try:
                antwort = self.sitzung.get(self.ziel_url, timeout=15)
                antwort.raise_for_status()
            except Exception as e:
                self.signal_fehler.emit(f"Connection Error: {str(e)}")
                return

            suppe = BeautifulSoup(antwort.text, 'html.parser')
            daten_skript = suppe.find("script", id="__NEXT_DATA__")
            
            if not daten_skript:
                self.signal_fehler.emit("Metadata not found!")
                return

            json_daten = json.loads(daten_skript.string)
            try:
                galerie = json_daten['props']['pageProps']['gallery']
                titel = self.dateiname_bereinigen(galerie.get('title', 'Unknown_Hentai'))
                dateien_liste = galerie['files']
                self.gesamt_dateien = len(dateien_liste)
            except:
                self.signal_fehler.emit("JSON Error.")
                return

            if self.gesamt_dateien == 0:
                self.signal_fehler.emit("No images found.")
                return

            basis_verzeichnis = os.path.dirname(self.ffmpeg_pfad)
            self.speicher_ort = os.path.join(basis_verzeichnis, titel)
            os.makedirs(self.speicher_ort, exist_ok=True)

            self.signal_protokoll.emit(f"Output: {self.speicher_ort}")
            self.signal_protokoll.emit("-" * 40)

            for index, datei in enumerate(dateien_liste):
                if not self.ist_aktiv:
                    break

                seite_nummer = index + 1
                bild_id = datei.get('id')
                bild_url = f"https://naxter.net/media/{bild_id}"                
                temp_name = f"{seite_nummer:03d}_temp.avif"
                final_name = f"{seite_nummer:03d}.{self.ausgabe_format}"
                pfad_temp = os.path.join(self.speicher_ort, temp_name)
                pfad_final = os.path.join(self.speicher_ort, final_name)

                if os.path.exists(pfad_final):
                    self.signal_protokoll.emit(f"Page {seite_nummer}: Exists.")
                    self.abgeschlossen_counter += 1
                    self.signal_fortschritt.emit(int((self.abgeschlossen_counter/self.gesamt_dateien)*100))
                    continue
                    
                self.signal_status.emit(f"Downloading Page {seite_nummer}...")
                dl_ok = False
                for _ in range(3):
                    if self.datei_herunterladen(bild_url, pfad_temp):
                        dl_ok = True
                        break
                    time.sleep(1)
                    
                if not dl_ok:
                    self.signal_protokoll.emit(f"Download failed: Page {seite_nummer}")
                    self.alles_erfolgreich = False
                    self.abgeschlossen_counter += 1
                    continue

                future = pool.submit(self.task_konvertieren, pfad_temp, pfad_final, seite_nummer)
                future.add_done_callback(lambda f, s=seite_nummer: self.on_task_done_wrapper(f, s))
                
            self.signal_status.emit("Finishing conversion tasks...")
            pool.shutdown(wait=True)
            
            if self.ist_aktiv:
                self.signal_status.emit("All done!")
                self.signal_protokoll.emit("All tasks finished.")
                
        except Exception as e:
            self.signal_fehler.emit(f"Error: {str(e)}")
            self.alles_erfolgreich = False
        finally:
            self.sitzung.close()
            self.signal_fertig.emit(self.alles_erfolgreich, self.speicher_ort)


class PdfThread(QThread):
    signal_protokoll = pyqtSignal(str)
    signal_fertig = pyqtSignal(bool, str)

    def __init__(self, ordner_pfad):
        super().__init__()
        self.ordner_pfad = ordner_pfad
        
    def run(self):
        try:
            pdf_pfad = self.ordner_pfad + ".pdf"
            self.signal_protokoll.emit(f"Creating: {os.path.basename(pdf_pfad)}")
            
            bild_dateien = []
            for datei in os.listdir(self.ordner_pfad):
                if datei.lower().endswith(('.png', '.jpg', '.jpeg')):
                    bild_dateien.append(os.path.join(self.ordner_pfad, datei))
            bild_dateien.sort(key=lambda x: os.path.basename(x))
            
            if not bild_dateien:
                self.signal_protokoll.emit("No images found in folder.")
                self.signal_fertig.emit(False, "")
                return
                
            self.signal_protokoll.emit(f"Processing {len(bild_dateien)} images...")
            
            bilder = []
            for pfad in bild_dateien:
                bild = Image.open(pfad)
                if bild.mode == 'RGBA':
                    hintergrund = Image.new('RGB', bild.size, (255, 255, 255))
                    hintergrund.paste(bild, mask=bild.split()[3])
                    bild = hintergrund
                elif bild.mode != 'RGB':
                    bild = bild.convert('RGB')
                bilder.append(bild)
            
            bilder[0].save(
                pdf_pfad,
                save_all=True,
                append_images=bilder[1:],
                resolution=100.0
            )
            
            for bild in bilder:
                bild.close()
                
            self.signal_protokoll.emit("PDF created successfully!")
            self.signal_fertig.emit(True, pdf_pfad)
            
        except Exception as e:
            self.signal_protokoll.emit(f"PDF error: {str(e)}")
            self.signal_fertig.emit(False, "")


class KomprimierThread(QThread):
    signal_protokoll = pyqtSignal(str)
    signal_fertig = pyqtSignal(bool, str)

    def __init__(self, ordner_pfad):
        super().__init__()
        self.ordner_pfad = ordner_pfad
        current_dir = os.path.dirname(os.path.abspath(__file__))
        self.seven_zip_exe = os.path.join(current_dir, "7zr.exe")

    def run(self):
        archiv_pfad = self.ordner_pfad + ".7z"
        try:
            if not os.path.exists(self.seven_zip_exe):
                self.signal_protokoll.emit("Error: '7zr.exe' not found!")
                self.signal_protokoll.emit("Please make sure 7zr.exe is in the same folder.")
                self.signal_fertig.emit(False, "")
                return

            self.signal_protokoll.emit(f"Compressing: {os.path.basename(archiv_pfad)}")
            start_info = None
            if os.name == 'nt':
                start_info = subprocess.STARTUPINFO()
                start_info.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            befehl = [
                self.seven_zip_exe, 'a', 
                '-t7z', 
                '-mx=5',       
                '-mmt=on',     
                '-y',          
                archiv_pfad, 
                self.ordner_pfad
            ]
            subprocess.run(befehl, startupinfo=start_info, check=True)
            
            if os.path.exists(archiv_pfad):
                self.signal_protokoll.emit("Compression complete!")
                self.signal_fertig.emit(True, archiv_pfad)
            else:
                self.signal_protokoll.emit("Output file not created.")
                self.signal_fertig.emit(False, "")

        except subprocess.CalledProcessError as e:
            self.signal_protokoll.emit(f"7-Zip Error: {str(e)}")
            self.signal_fertig.emit(False, "")
        except Exception as e:
            self.signal_protokoll.emit(f"Error: {str(e)}")
            self.signal_fertig.emit(False, "")


class HauptFenster(QMainWindow):
    def __init__(self):
        super().__init__()
        self.arbeiter = None
        self.komprimierer = None
        self.pdf_ersteller = None
        self.anim = None
        self.letzter_ordner = ""
        self.fenster_erstellen()

    def fenster_erstellen(self):
        self.setWindowTitle("Manga Downloader")
        self.setFixedSize(700, 560)
        self.setStyleSheet(ROSA_STIL)

        zentral = QWidget()
        self.setCentralWidget(zentral)
        haupt_layout = QVBoxLayout(zentral)
        haupt_layout.setSpacing(10)
        haupt_layout.setContentsMargins(28, 22, 28, 22)

        titel = QLabel("Manga Downloader")
        titel.setAlignment(Qt.AlignCenter)
        titel.setStyleSheet("""
            font-size: 26px; 
            font-weight: bold; 
            color: #f4a5c7; 
            padding: 5px;
            letter-spacing: 1px;
        """)
        haupt_layout.addWidget(titel)

        untertitel = QLabel("Supports: Naxter | ComicWalker | MangaDex")
        untertitel.setAlignment(Qt.AlignCenter)
        untertitel.setStyleSheet("font-size: 11px; color: #8a7a88; margin-bottom: 8px;")
        haupt_layout.addWidget(untertitel)

        eingabe_bereich = QFrame()
        eingabe_layout = QVBoxLayout(eingabe_bereich)
        eingabe_layout.setContentsMargins(0, 5, 0, 5)
        eingabe_layout.setSpacing(8)
        
        url_label = QLabel("Gallery / Episode URL")
        url_label.setStyleSheet("font-weight: bold; font-size: 12px; color: #d4749a;")
        
        self.url_eingabe = QLineEdit()
        self.url_eingabe.setPlaceholderText("Paste your link here...")
        self.url_eingabe.setClearButtonEnabled(True)
        self.url_eingabe.setMinimumHeight(42)
        self.url_eingabe.returnPressed.connect(self.download_starten)
        
        eingabe_layout.addWidget(url_label)
        eingabe_layout.addWidget(self.url_eingabe)
        haupt_layout.addWidget(eingabe_bereich)

        knopf_zeile1 = QHBoxLayout()
        knopf_zeile1.setSpacing(10)
        
        self.start_knopf = QPushButton("Start Download")
        self.start_knopf.setCursor(Qt.PointingHandCursor)
        self.start_knopf.setMinimumHeight(40)
        self.start_knopf.clicked.connect(self.download_starten)
        
        self.stop_knopf = QPushButton("Cancel")
        self.stop_knopf.setObjectName("stopBtn")
        self.stop_knopf.setCursor(Qt.PointingHandCursor)
        self.stop_knopf.setMinimumHeight(40)
        self.stop_knopf.clicked.connect(self.download_stoppen)
        self.stop_knopf.setEnabled(False)

        knopf_zeile1.addWidget(self.start_knopf, 2)
        knopf_zeile1.addWidget(self.stop_knopf, 1)
        haupt_layout.addLayout(knopf_zeile1)

        knopf_zeile2 = QHBoxLayout()
        knopf_zeile2.setSpacing(10)

        self.pdf_knopf = QPushButton("Create PDF")
        self.pdf_knopf.setObjectName("pdfBtn")
        self.pdf_knopf.setCursor(Qt.PointingHandCursor)
        self.pdf_knopf.setMinimumHeight(40)
        self.pdf_knopf.clicked.connect(self.pdf_erstellen)
        self.pdf_knopf.setEnabled(False)

        self.komprimier_knopf = QPushButton("Compress .7z")
        self.komprimier_knopf.setObjectName("compressBtn")
        self.komprimier_knopf.setCursor(Qt.PointingHandCursor)
        self.komprimier_knopf.setMinimumHeight(40)
        self.komprimier_knopf.clicked.connect(self.komprimierung_starten)
        self.komprimier_knopf.setEnabled(False)

        knopf_zeile2.addWidget(self.pdf_knopf, 1)
        knopf_zeile2.addWidget(self.komprimier_knopf, 1)
        haupt_layout.addLayout(knopf_zeile2)

        self.status_label = QLabel("Ready")
        self.status_label.setAlignment(Qt.AlignCenter)
        self.status_label.setStyleSheet("color: #9a8a98; font-size: 11px; padding: 3px;")
        haupt_layout.addWidget(self.status_label)

        self.fortschritt = QProgressBar()
        self.fortschritt.setTextVisible(True)
        self.fortschritt.setFormat("%p%")
        self.fortschritt.setFixedHeight(16)
        self.fortschritt.setValue(0)
        haupt_layout.addWidget(self.fortschritt)

        self.log_bereich = QTextEdit()
        self.log_bereich.setReadOnly(True)
        self.log_bereich.setPlaceholderText("Activity log will appear here...")
        haupt_layout.addWidget(self.log_bereich)

    def protokollieren(self, nachricht):
        self.log_bereich.append(nachricht)
        scrollbar = self.log_bereich.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())

    def fortschritt_aktualisieren(self, wert):
        if not self.anim:
            self.anim = QPropertyAnimation(self.fortschritt, b"value")
            self.anim.setEasingCurve(QEasingCurve.OutCubic)
            self.anim.setDuration(250)

        if self.anim.state() == QPropertyAnimation.Running:
            self.anim.stop()

        self.anim.setStartValue(self.fortschritt.value())
        self.anim.setEndValue(wert)
        self.anim.start()

    def quelle_erkennen(self, url):
        if 'naxter.net' in url:
            return 'naxter'
        elif 'comic-walker.com' in url:
            return 'comicwalker'
        elif 'mangadex.org' in url:
            return 'mangadex'
        return None

    def download_starten(self):
        url = self.url_eingabe.text().strip()
        if not url:
            self.protokollieren("Please enter a URL.")
            return
            
        if not url.startswith(('http://', 'https://')):
            self.protokollieren("Invalid URL format.")
            return

        quelle = self.quelle_erkennen(url)
        if quelle is None:
            self.protokollieren("Unsupported website. Supported: Naxter, ComicWalker, MangaDex")
            return

        aktuelles_verzeichnis = os.path.dirname(os.path.abspath(__file__))
        ffmpeg_pfad = os.path.join(aktuelles_verzeichnis, FFMPEG_DATEI)
        
        if not os.path.exists(ffmpeg_pfad):
            QMessageBox.warning(self, "Error", f"'{FFMPEG_DATEI}' not found!")
            return

        format_dialog = FormatAuswahl(self)
        if format_dialog.exec_() != QDialog.Accepted:
            return
        
        gewaehltes_format = format_dialog.gewaehltes_format()

        if self.arbeiter and self.arbeiter.isRunning():
            self.arbeiter.stoppen()
            self.arbeiter.wait(2000)

        if quelle == 'comicwalker':
            self.comicwalker_download_starten(url, ffmpeg_pfad, gewaehltes_format)
        elif quelle == 'mangadex':
            self.mangadex_download_starten(url, ffmpeg_pfad, gewaehltes_format)
        else:
            self.naxter_download_starten(url, ffmpeg_pfad, gewaehltes_format)

    def comicwalker_download_starten(self, url, ffmpeg_pfad, gewaehltes_format):
        self.status_label.setText("Parsing ComicWalker data...")
        self.log_bereich.clear()
        self.protokollieren("Connecting to ComicWalker...")
        QApplication.processEvents()
        
        try:
            parser = ComicWalkerParser(url)
            episoden_liste = parser.episodenliste_holen()
            aktuelle_episode = parser.aktuelle_episode_holen()
            werk_titel = parser.werk_titel_holen()
        except Exception as e:
            QMessageBox.warning(self, "Error", f"Failed to parse ComicWalker: {str(e)}")
            self.status_label.setText("Ready")
            return

        episoden_dialog = EpisodenAuswahl(episoden_liste, aktuelle_episode, self)
        if episoden_dialog.exec_() != QDialog.Accepted:
            self.status_label.setText("Ready")
            return
        
        gewaehlte_episode = episoden_dialog.hole_gewaehlte_episode()
        if not gewaehlte_episode:
            self.status_label.setText("Ready")
            return

        self.start_knopf.setEnabled(False)
        self.stop_knopf.setEnabled(True)
        self.pdf_knopf.setEnabled(False)
        self.komprimier_knopf.setEnabled(False)
        self.fortschritt.setValue(0)
        self.status_label.setText("Starting...")

        self.arbeiter = ComicWalkerThread(url, ffmpeg_pfad, gewaehltes_format, gewaehlte_episode, werk_titel)
        self.arbeiter.signal_protokoll.connect(self.protokollieren)
        self.arbeiter.signal_fortschritt.connect(self.fortschritt_aktualisieren)
        self.arbeiter.signal_status.connect(self.status_label.setText)
        self.arbeiter.signal_fehler.connect(lambda m: QMessageBox.warning(self, "Error", m))
        self.arbeiter.signal_fertig.connect(self.download_beendet)
        self.arbeiter.start()

    def mangadex_download_starten(self, url, ffmpeg_pfad, gewaehltes_format):
        self.status_label.setText("Parsing MangaDex data...")
        self.log_bereich.clear()
        self.protokollieren("Connecting to MangaDex...")
        QApplication.processEvents()

        try:
            parser = MangaDexParser(url)
            
            # If URL is a Title URL, show list
            if '/title/' in url:
                self.protokollieren("Fetching chapter list...")
                parser.metadaten_laden()
                kapitel_liste = parser.episodenliste_holen()
                werk_titel = parser.werk_titel_holen()

                if not kapitel_liste:
                    QMessageBox.warning(self, "Info", "No chapters found for this manga.")
                    self.status_label.setText("Ready")
                    return

                # Re-use EpisodenAuswahl for MangaDex
                episoden_dialog = EpisodenAuswahl(kapitel_liste, None, self)
                if episoden_dialog.exec_() != QDialog.Accepted:
                    self.status_label.setText("Ready")
                    return
                
                gewaehltes_kapitel = episoden_dialog.hole_gewaehlte_episode()
                
            # If URL is a Chapter URL directly
            elif '/chapter/' in url:
                match = re.search(r"chapter\/([a-f0-9\-]+)", url)
                if not match:
                     raise ValueError("Invalid Chapter URL")
                kap_id = match.group(1)
                
                # We need basic info
                resp = requests.get(f"https://api.mangadex.org/chapter/{kap_id}?includes[]=manga")
                resp.raise_for_status()
                data = resp.json()['data']
                attr = data['attributes']
                
                werk_titel = "Unknown Manga"
                for rel in data['relationships']:
                    if rel['type'] == 'manga':
                        manga_resp = requests.get(f"https://api.mangadex.org/manga/{rel['id']}")
                        m_data = manga_resp.json()['data']
                        t_dict = m_data['attributes']['title']
                        werk_titel = t_dict.get('en') or next(iter(t_dict.values()), 'Unknown')
                        break
                
                gewaehltes_kapitel = {
                    'id': kap_id,
                    'number': attr.get('chapter', '0'),
                    'title': attr.get('title', ''),
                }
            else:
                 raise ValueError("URL type not supported (only title or chapter)")

        except Exception as e:
            QMessageBox.warning(self, "Error", f"Failed to parse MangaDex: {str(e)}")
            self.status_label.setText("Ready")
            return

        self.start_knopf.setEnabled(False)
        self.stop_knopf.setEnabled(True)
        self.pdf_knopf.setEnabled(False)
        self.komprimier_knopf.setEnabled(False)
        self.fortschritt.setValue(0)
        self.status_label.setText("Starting...")

        self.arbeiter = MangaDexThread(ffmpeg_pfad, gewaehltes_format, gewaehltes_kapitel, werk_titel)
        self.arbeiter.signal_protokoll.connect(self.protokollieren)
        self.arbeiter.signal_fortschritt.connect(self.fortschritt_aktualisieren)
        self.arbeiter.signal_status.connect(self.status_label.setText)
        self.arbeiter.signal_fehler.connect(lambda m: QMessageBox.warning(self, "Error", m))
        self.arbeiter.signal_fertig.connect(self.download_beendet)
        self.arbeiter.start()

    def naxter_download_starten(self, url, ffmpeg_pfad, gewaehltes_format):
        self.start_knopf.setEnabled(False)
        self.stop_knopf.setEnabled(True)
        self.pdf_knopf.setEnabled(False)
        self.komprimier_knopf.setEnabled(False)
        self.fortschritt.setValue(0)
        self.log_bereich.clear()
        self.status_label.setText("Starting...")

        self.arbeiter = ArbeitsThread(url, ffmpeg_pfad, gewaehltes_format)
        self.arbeiter.signal_protokoll.connect(self.protokollieren)
        self.arbeiter.signal_fortschritt.connect(self.fortschritt_aktualisieren)
        self.arbeiter.signal_status.connect(self.status_label.setText)
        self.arbeiter.signal_fehler.connect(lambda m: QMessageBox.warning(self, "Error", m))
        self.arbeiter.signal_fertig.connect(self.download_beendet)
        self.arbeiter.start()

    def download_stoppen(self):
        if self.arbeiter and self.arbeiter.isRunning():
            self.arbeiter.stoppen()
            self.status_label.setText("Cancelling...")
            self.stop_knopf.setEnabled(False)

    def download_beendet(self, erfolg, ordner_pfad):
            self.start_knopf.setEnabled(True)
            self.stop_knopf.setEnabled(False)
          
            if ordner_pfad and os.path.exists(ordner_pfad):
                self.letzter_ordner = ordner_pfad
                self.pdf_knopf.setEnabled(True)
                self.komprimier_knopf.setEnabled(True)
                
                if erfolg:
                    self.status_label.setText("Done - Ready to export")
                else:
                    self.status_label.setText("Completed (with errors) - Ready")
            else:
                self.pdf_knopf.setEnabled(False)
                self.komprimier_knopf.setEnabled(False)
                self.letzter_ordner = ""
                if "Cancelling" not in self.status_label.text():
                    self.status_label.setText("Failed / No output")

    def pdf_erstellen(self):
        if not self.letzter_ordner or not os.path.exists(self.letzter_ordner):
            self.protokollieren("Folder not found.")
            return

        self.pdf_knopf.setEnabled(False)
        self.komprimier_knopf.setEnabled(False)
        self.start_knopf.setEnabled(False)
        self.status_label.setText("Creating PDF...")

        self.pdf_ersteller = PdfThread(self.letzter_ordner)
        self.pdf_ersteller.signal_protokoll.connect(self.protokollieren)
        self.pdf_ersteller.signal_fertig.connect(self.pdf_beendet)
        self.pdf_ersteller.start()

    def pdf_beendet(self, erfolg, pdf_pfad):
        self.start_knopf.setEnabled(True)
        self.pdf_knopf.setEnabled(True)
        self.komprimier_knopf.setEnabled(True)
        
        if erfolg:
            self.status_label.setText("PDF created!")
        else:
            self.status_label.setText("PDF creation failed")

    def komprimierung_starten(self):
        if not self.letzter_ordner or not os.path.exists(self.letzter_ordner):
            self.protokollieren("Folder not found.")
            return

        self.komprimier_knopf.setEnabled(False)
        self.pdf_knopf.setEnabled(False)
        self.start_knopf.setEnabled(False)
        self.status_label.setText("Compressing...")

        self.komprimierer = KomprimierThread(self.letzter_ordner)
        self.komprimierer.signal_protokoll.connect(self.protokollieren)
        self.komprimierer.signal_fertig.connect(self.komprimierung_beendet)
        self.komprimierer.start()

    def komprimierung_beendet(self, erfolg, archiv_pfad):
        self.start_knopf.setEnabled(True)
        self.pdf_knopf.setEnabled(True)
        
        if erfolg:
            self.status_label.setText("Compression complete")
            
            antwort = QMessageBox()
            antwort.setWindowTitle("Delete Original?")
            antwort.setText("Archive created successfully.\nDelete the original folder?")
            antwort.setStandardButtons(QMessageBox.Yes | QMessageBox.No)
            antwort.setDefaultButton(QMessageBox.No)
            antwort.setStyleSheet(ROSA_STIL)
            
            if antwort.exec_() == QMessageBox.Yes:
                try:
                    shutil.rmtree(self.letzter_ordner)
                    self.protokollieren("Original folder deleted.")
                    self.letzter_ordner = ""
                    self.pdf_knopf.setEnabled(False)
                    self.komprimier_knopf.setEnabled(False)
                except Exception as e:
                    self.protokollieren(f"Delete failed: {str(e)}")
                    self.komprimier_knopf.setEnabled(True)
            else:
                self.komprimier_knopf.setEnabled(True)
        else:
            self.komprimier_knopf.setEnabled(True)
            self.status_label.setText("Compression failed")

    def closeEvent(self, event):
        if self.arbeiter and self.arbeiter.isRunning():
            self.arbeiter.stoppen()
            self.arbeiter.wait(3000)
        if self.komprimierer and self.komprimierer.isRunning():
            self.komprimierer.wait(3000)
        if self.pdf_ersteller and self.pdf_ersteller.isRunning():
            self.pdf_ersteller.wait(3000)
        event.accept()


if __name__ == "__main__":
    app = QApplication(sys.argv)
    app.setStyle('Fusion')
    fenster = HauptFenster()
    fenster.show()
    sys.exit(app.exec_())