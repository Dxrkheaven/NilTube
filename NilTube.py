import sys
import os
import sqlite3
import yt_dlp
import subprocess
from PyQt6.QtCore import QStringListModel, QThread, pyqtSignal, Qt
from PyQt6.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout, 
    QLineEdit, QPushButton, QListView, QSplitter, QFrame, QLabel
)
from PyQt6.QtGui import QShortcut, QKeySequence

# Silence Qt font warnings and Wayland noise
os.environ["QT_LOGGING_RULES"] = "qt.qpa.wayland.textinput.warning=false;qt.text.font.db.warning=false"

DB_PATH = "local_history.db"

class SearchBar(QLineEdit):
    """Custom search input that lets you press Down arrow to jump focus into the results list."""
    def __init__(self, focus_target=None, parent=None):
        super().__init__(parent)
        self.focus_target = focus_target

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Down and self.focus_target:
            self.focus_target.setFocus()
            event.accept()
        else:
            super().keyPressEvent(event)


class LocalHistoryManager:
    def __init__(self, db_path=DB_PATH):
        self.db_path = db_path
        self._init_db()

    def _init_db(self):
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                video_id TEXT,
                title TEXT,
                author TEXT,
                watched_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        conn.commit()
        conn.close()

    def add_video(self, video_id, title, author, max_limit=100):
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute('INSERT INTO history (video_id, title, author) VALUES (?, ?, ?)', 
                       (video_id, title, author))
        cursor.execute('''
            DELETE FROM history WHERE id NOT IN (
                SELECT id FROM history ORDER BY watched_at DESC LIMIT ?
            )
        ''', (max_limit,))
        conn.commit()
        conn.close()

    def get_recent(self):
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute('SELECT video_id, title, author FROM history ORDER BY watched_at DESC LIMIT 50')
        rows = cursor.fetchall()
        conn.close()
        return rows

    def clear_history(self):
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute('DELETE FROM history')
        conn.commit()
        conn.close()


class SearchThread(QThread):
    results_ready = pyqtSignal(list)
    error_occurred = pyqtSignal(str)

    def __init__(self, query, count=50):
        super().__init__()
        self.query = query
        self.count = count

    def run(self):
        try:
            search_query = self.query if self.query else "trending"
            ydl_opts = {
                'extract_flat': True,
                'quiet': True,
                'no_warnings': True,
                'skip_download': True,
            }
            
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                result = ydl.extract_info(f"ytsearch{self.count}:{search_query}", download=False)
                entries = result.get('entries', []) if result else []
                
                items = []
                for entry in entries:
                    if not entry:
                        continue
                    title = entry.get('title', 'Unknown Title')
                    video_id = entry.get('id', '')
                    author = entry.get('uploader') or entry.get('channel') or 'Unknown Author'
                    if video_id:
                        items.append((video_id, title, author))
                        
                self.results_ready.emit(items)
        except Exception as e:
            self.error_occurred.emit(str(e))


class PrivacyYouTubeApp(QWidget):
    def __init__(self):
        super().__init__()
        self.history_mgr = LocalHistoryManager()
        
        self.all_search_data = []
        self.current_query = "trending"
        self.search_count = 50  
        self.is_fetching = False
        self.called = False
        
        self.history_data = []
        
        self.initUI()
        self.apply_terminal_stylesheet()
        self.setup_shortcuts()

        # Initial fetch on launch
        self.load_more_content()

    def initUI(self):
        self.setWindowTitle('VoidTube // By Dxrkheaven')
        self.resize(1000, 700)

        main_layout = QVBoxLayout()
        main_layout.setContentsMargins(6, 6, 6, 6)
        main_layout.setSpacing(4)
        
        # Central Splitter (Created early so search bar can reference results_view for focus transfer)
        self.splitter = QSplitter(Qt.Orientation.Horizontal)
        
        # Left Container (Results View)
        left_container = QWidget()
        left_layout = QVBoxLayout(left_container)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(0)
        
        self.results_model = QStringListModel()
        self.results_view = QListView()
        self.results_view.setModel(self.results_model)
        self.results_view.setFrameShape(QFrame.Shape.NoFrame)
        self.results_view.setEditTriggers(QListView.EditTrigger.NoEditTriggers)
        self.results_view.doubleClicked.connect(self.play_selected_item)
        
        self.results_view.verticalScrollBar().valueChanged.connect(self.handle_search_scroll)
        
        left_layout.addWidget(self.results_view)
        self.splitter.addWidget(left_container)

        # Top Bar
        top_bar = QHBoxLayout()
        top_bar.setSpacing(4)
        
        # Search input with custom Down-arrow focus escape to results_view
        self.search_input = SearchBar(focus_target=self.results_view)
        self.search_input.setPlaceholderText("Search YouTube privately...")
        self.search_input.returnPressed.connect(self.perform_search)
        
        self.search_btn = QPushButton("Search")
        self.search_btn.clicked.connect(self.perform_search)
        
        # Checkable Audio Mode Toggle Button
        self.audio_btn = QPushButton("Audio Mode")
        self.audio_btn.setCheckable(True)
        
        self.history_toggle_btn = QPushButton("History")
        self.history_toggle_btn.setCheckable(True)
        self.history_toggle_btn.clicked.connect(self.toggle_history_sidebar)
        
        top_bar.addWidget(self.search_input)
        top_bar.addWidget(self.search_btn)
        top_bar.addWidget(self.audio_btn)
        top_bar.addWidget(self.history_toggle_btn)
        
        # Assemble Main Layout
        root_layout = QVBoxLayout()
        root_layout.setContentsMargins(6, 6, 6, 6)
        root_layout.setSpacing(4)
        root_layout.addLayout(top_bar)

        # Right Container (History Sidebar)
        self.history_container = QWidget()
        history_layout = QVBoxLayout(self.history_container)
        history_layout.setContentsMargins(4, 0, 0, 0)
        history_layout.setSpacing(2)
        
        history_header_layout = QHBoxLayout()
        history_header_layout.setContentsMargins(0, 0, 0, 0)
        history_label = QLabel("<b>Watch History</b>")
        
        self.clear_history_btn = QPushButton("Clear")
        self.clear_history_btn.clicked.connect(self.clear_history_action)
        
        history_header_layout.addWidget(history_label)
        history_header_layout.addStretch()
        history_header_layout.addWidget(self.clear_history_btn)
        history_layout.addLayout(history_header_layout)
        
        self.history_model = QStringListModel()
        self.history_view = QListView()
        self.history_view.setModel(self.history_model)
        self.history_view.setFrameShape(QFrame.Shape.NoFrame)
        self.history_view.setEditTriggers(QListView.EditTrigger.NoEditTriggers)
        self.history_view.doubleClicked.connect(self.play_from_history_index)
        history_layout.addWidget(self.history_view)
        
        self.splitter.addWidget(self.history_container)
        self.history_container.hide()
        
        self.splitter.setSizes([700, 400])
        root_layout.addWidget(self.splitter)

        self.setLayout(root_layout)
        self.load_history_ui()

    def focus_search_bar(self):
        self.search_input.setFocus()
        self.search_input.selectAll()

    def setup_shortcuts(self):
        # Enter / Return triggers playback on focused list item
        self.return_shortcut = QShortcut(QKeySequence("Return"), self.results_view)
        self.return_shortcut.setContext(Qt.ShortcutContext.WidgetShortcut)
        self.return_shortcut.activated.connect(self.play_selected_item)

        # Shift + Return forces audio-only playback instantly
        self.shift_return_shortcut = QShortcut(QKeySequence("Shift+Return"), self.results_view)
        self.shift_return_shortcut.setContext(Qt.ShortcutContext.WidgetShortcut)
        self.shift_return_shortcut.activated.connect(lambda: self.play_selected_item(force_audio=True))

        # Ctrl + H toggles history sidebar
        self.history_shortcut = QShortcut(QKeySequence("Ctrl+H"), self)
        self.history_shortcut.activated.connect(self.toggle_history_sidebar)

        # Escape kill audio-only playback
        self.kill_audio_shortcut = QShortcut(QKeySequence("Escape"), self)
        self.kill_audio_shortcut.activated.connect(lambda: subprocess.run(["pkill", "mpv"]))

        # Ctrl + L focuses and selects the search bar from anywhere
        self.focus_search_shortcut = QShortcut(QKeySequence("Ctrl+L"), self)
        self.focus_search_shortcut.activated.connect(self.focus_search_bar)

    def apply_terminal_stylesheet(self):
        qss = """
            QWidget {
                background-color: #1a1a1a;
                color: #e0e0e0;
                font-family: monospace;
                font-size: 13px;
            }
            QLineEdit {
                background-color: #262626;
                border: 1px solid #3d3d3d;
                border-radius: 4px;
                padding: 4px 6px;
                color: #ffffff;
            }
            QLineEdit:focus {
                border: 1px solid #AA1515;
            }
            QPushButton {
                background-color: #333333;
                border: 1px solid #444444;
                border-radius: 4px;
                padding: 4px 10px;
                color: #ffffff;
                font-weight: bold;
            }
            QPushButton:hover {
                background-color: #AA1515;
                border: 1px solid #FF746C;
            }
            QPushButton:checked {
                background-color: #AA1515;
                border: 1px solid #FF746C;
                color: #ffffff;
            }
            QListView {
                background-color: #222222;
                border: none;
                border-radius: 0px;
                padding: 0px;
                margin: 0px;
            }
            QListView::item {
                height: 22px;
                padding: 0px 4px;
                border-bottom: 1px solid #2a2a2a;
            }
            QListView::item:hover {
                background-color: #333333;
                color: #FF746C;
            }
            QListView::item:selected {
                background-color: #AA1515;
                color: #ffffff;
            }
            QSplitter::handle {
                background-color: #333333;
                width: 2px;
            }
        """
        self.setStyleSheet(qss)

    def toggle_history_sidebar(self):
        if self.history_container.isVisible():
            self.history_container.hide()
            self.history_toggle_btn.setChecked(False)
        else:
            self.history_container.show()
            self.history_toggle_btn.setChecked(True)
            self.splitter.setSizes([650, 350])

    def load_more_content(self):
        if self.is_fetching:
            return
        self.is_fetching = True
        
        self.search_thread = SearchThread(self.current_query, count=self.search_count)
        self.search_thread.results_ready.connect(self.on_search_results)
        self.search_thread.error_occurred.connect(self.on_search_error)
        self.search_thread.start()

    def perform_search(self):
        query = self.search_input.text().strip()
        self.current_query = query if query else "trending"
        self.all_search_data.clear()
        self.search_count = 50
        self.is_fetching = False
        self.called = False
        self.results_model.setStringList(["Searching..."])
        self.search_btn.setEnabled(False)
        
        self.load_more_content()

    def on_search_results(self, items):
        self.search_btn.setEnabled(True)
        self.is_fetching = False
        self.called = False
        
        if not items:
            return

        self.all_search_data.clear()
        for video_id, title, author in items:
            self.all_search_data.append((f"https://www.youtube.com/watch?v={video_id}", video_id, title, author))
        
        scrollbar = self.results_view.verticalScrollBar()
        current_scroll = scrollbar.value()
        
        display_strings = [f"{title}  —  👤 {author}" for _, _, title, author in self.all_search_data]
        self.results_model.setStringList(display_strings)
        
        scrollbar.setValue(current_scroll)

    def handle_search_scroll(self, value):
        scrollbar = self.results_view.verticalScrollBar()
        max_val = scrollbar.maximum()
        
        if max_val > 0 and value >= max_val * 0.75:
            if not self.called and not self.is_fetching:
                self.called = True
                self.search_count += 30
                self.load_more_content()

    def on_search_error(self, err):
        self.search_btn.setEnabled(True)
        self.is_fetching = False
        self.called = False
        print(f"Search error: {err}")

    def load_history_ui(self):
        self.history_data.clear()
        rows = self.history_mgr.get_recent()
        display_strings = []
        for vid, title, author in rows:
            self.history_data.append((f"https://www.youtube.com/watch?v={vid}", vid, title, author))
            display_strings.append(f"{title}  —  👤 {author}")
        self.history_model.setStringList(display_strings)

    def clear_history_action(self):
        self.history_mgr.clear_history()
        self.load_history_ui()
        print("Local watch history cleared.")

    def play_selected_item(self, force_audio=False):
        index = self.results_view.currentIndex()
        row = index.row()
        if 0 <= row < len(self.all_search_data):
            url, video_id, title, author = self.all_search_data[row]
            # Audio mode is active if the toggle button is checked OR Shift+Return was pressed
            is_audio = force_audio or self.audio_btn.isChecked()
            self.trigger_playback(url, video_id, title, author, audio_only=is_audio)

    def play_from_history_index(self, index):
        row = index.row()
        if 0 <= row < len(self.history_data):
            url, video_id, title, author = self.history_data[row]
            is_audio = self.audio_btn.isChecked()
            self.trigger_playback(url, video_id, title, author, audio_only=is_audio)

    def trigger_playback(self, url, video_id, title, author, audio_only=False):
        self.history_mgr.add_video(video_id, title, author)
        self.load_history_ui()
        
        mode_str = "audio-only" if audio_only else "video"
        print(f"Launching mpv ({mode_str}) for: {title}")
        
        mpv_flags = ['mpv', '--hwdec=auto']
        if audio_only:
            mpv_flags.extend(['--ytdl-format=bestaudio/best', '--no-video'])
        else:
            mpv_flags.extend(['--ytdl-format=bestvideo[height<=1080]+bestaudio/best'])
            
        mpv_flags.extend([
            '--ytdl-raw-options=cookies-from-browser=firefox,cache-dir=' + os.path.expanduser('~/.cache/yt-dlp'),
            '--cache=yes',
            '--demuxer-max-bytes=150M',
            '--demuxer-readahead-secs=10',
            url
        ])
        subprocess.Popen(mpv_flags)

if __name__ == '__main__':
    app = QApplication(sys.argv)
    ex = PrivacyYouTubeApp()
    ex.show()
    sys.exit(app.exec())
