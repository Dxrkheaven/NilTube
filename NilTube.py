import sys
import os
import sqlite3
import yt_dlp
import subprocess
from PyQt6.QtCore import QStringListModel, QThread, pyqtSignal, Qt
from PyQt6.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout, 
    QLineEdit, QPushButton, QListView, QSplitter, QFrame, QLabel,
    QStackedWidget, QInputDialog, QMessageBox
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
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS playlists (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT UNIQUE
            )
        ''')
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS playlist_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                playlist_id INTEGER,
                video_id TEXT,
                title TEXT,
                author TEXT,
                FOREIGN KEY(playlist_id) REFERENCES playlists(id) ON DELETE CASCADE
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

    # Playlist DB methods
    def get_playlists(self):
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute('SELECT id, name FROM playlists ORDER BY name ASC')
        rows = cursor.fetchall()
        conn.close()
        return rows

    def create_playlist(self, name):
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            cursor.execute('INSERT INTO playlists (name) VALUES (?)', (name,))
            conn.commit()
            conn.close()
            return True
        except sqlite3.IntegrityError:
            return False

    def delete_playlist(self, playlist_id):
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute('DELETE FROM playlist_items WHERE playlist_id = ?', (playlist_id,))
        cursor.execute('DELETE FROM playlists WHERE id = ?', (playlist_id,))
        conn.commit()
        conn.close()

    def get_playlist_items(self, playlist_id):
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute('SELECT video_id, title, author FROM playlist_items WHERE playlist_id = ?', (playlist_id,))
        rows = cursor.fetchall()
        conn.close()
        return rows

    def add_to_playlist(self, playlist_id, video_id, title, author):
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        # Avoid exact duplicate in same playlist
        cursor.execute('SELECT id FROM playlist_items WHERE playlist_id = ? AND video_id = ?', (playlist_id, video_id))
        if not cursor.fetchone():
            cursor.execute('INSERT INTO playlist_items (playlist_id, video_id, title, author) VALUES (?, ?, ?, ?)',
                           (playlist_id, video_id, title, author))
            conn.commit()
        conn.close()

    def remove_from_playlist(self, playlist_id, video_id):
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute('DELETE FROM playlist_items WHERE playlist_id = ? AND video_id = ?', (playlist_id, video_id))
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


class HoverSidebar(QFrame):
    """LibreWolf-style expandable sidebar on hover"""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedWidth(46)
        self.expanded_width = 160
        self.collapsed_width = 46
        self.setObjectName("HoverSidebar")

    def enterEvent(self, event):
        self.setFixedWidth(self.expanded_width)
        # Expand buttons to show text
        if hasattr(self, 'buttons'):
            for btn, icon, text in self.buttons:
                btn.setText(f"  {icon}  {text}")
        super().enterEvent(event)

    def leaveEvent(self, event):
        self.setFixedWidth(self.collapsed_width)
        # Collapse buttons to icons only
        if hasattr(self, 'buttons'):
            for btn, icon, text in self.buttons:
                btn.setText(icon)
        super().leaveEvent(event)

    def set_buttons(self, buttons_list):
        # buttons_list is a list of tuples: (QPushButton, icon_str, text_str)
        self.buttons = buttons_list
        for btn, icon, text in self.buttons:
            btn.setText(icon)


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
        self.playlist_data = []
        self.current_playlist_id = None
        
        self.initUI()
        self.apply_terminal_stylesheet()
        self.setup_shortcuts()

        # Initial fetch on launch
        self.load_more_content()

    def initUI(self):
        self.setWindowTitle('VoidTube // By Dxrkheaven')
        self.resize(1050, 700)

        main_root_layout = QHBoxLayout(self)
        main_root_layout.setContentsMargins(4, 4, 4, 4)
        main_root_layout.setSpacing(4)

        # --- Left Hover Sidebar ---
        self.sidebar = HoverSidebar()
        sidebar_layout = QVBoxLayout(self.sidebar)
        sidebar_layout.setContentsMargins(4, 4, 4, 4)
        sidebar_layout.setSpacing(6)

        self.nav_search_btn = QPushButton("🔍")
        self.nav_search_btn.setCheckable(True)
        self.nav_search_btn.setChecked(True)
        self.nav_search_btn.setToolTip("Search / Home")
        self.nav_search_btn.clicked.connect(lambda: self.switch_page(0))

        self.nav_audio_btn = QPushButton("🎵")
        self.nav_audio_btn.setCheckable(True)
        self.nav_audio_btn.setToolTip("Audio Mode Toggle")

        self.nav_history_btn = QPushButton("📜")
        self.nav_history_btn.setCheckable(True)
        self.nav_history_btn.setToolTip("Watch History")
        self.nav_history_btn.clicked.connect(lambda: self.switch_page(1))

        self.nav_playlists_btn = QPushButton("📂")
        self.nav_playlists_btn.setCheckable(True)
        self.nav_playlists_btn.setToolTip("Playlists")
        self.nav_playlists_btn.clicked.connect(lambda: self.switch_page(2))

        sidebar_layout.addWidget(self.nav_search_btn)
        sidebar_layout.addWidget(self.nav_audio_btn)
        sidebar_layout.addWidget(self.nav_history_btn)
        sidebar_layout.addWidget(self.nav_playlists_btn)
        sidebar_layout.addStretch()

        # Wire up hover sidebar button labels
        self.sidebar.set_buttons([
            (self.nav_search_btn, "🔍", "Search"),
            (self.nav_audio_btn, "🎵", "Audio Mode"),
            (self.nav_history_btn, "📜", "History"),
            (self.nav_playlists_btn, "📂", "Playlists")
        ])

        main_root_layout.addWidget(self.sidebar)

        # --- Main Content Area (Stacked Widget) ---
        self.stack = QStackedWidget()

        # Page 0: Search & Results View
        page_search = QWidget()
        search_layout = QVBoxLayout(page_search)
        search_layout.setContentsMargins(0, 0, 0, 0)
        search_layout.setSpacing(4)

        top_bar = QHBoxLayout()
        top_bar.setSpacing(4)
        self.search_input = SearchBar()
        self.search_input.setPlaceholderText("Search YouTube privately...")
        self.search_input.returnPressed.connect(self.perform_search)
        
        self.search_btn = QPushButton("Search")
        self.search_btn.clicked.connect(self.perform_search)

        top_bar.addWidget(self.search_input)
        top_bar.addWidget(self.search_btn)
        search_layout.addLayout(top_bar)

        self.results_model = QStringListModel()
        self.results_view = QListView()
        self.results_view.setModel(self.results_model)
        self.results_view.setFrameShape(QFrame.Shape.NoFrame)
        self.results_view.setEditTriggers(QListView.EditTrigger.NoEditTriggers)
        self.results_view.doubleClicked.connect(self.play_selected_item)
        self.search_input.focus_target = self.results_view
        self.results_view.verticalScrollBar().valueChanged.connect(self.handle_search_scroll)
        search_layout.addWidget(self.results_view)

        # Page 1: History View
        page_history = QWidget()
        hist_layout = QVBoxLayout(page_history)
        hist_layout.setContentsMargins(0, 0, 0, 0)
        hist_layout.setSpacing(4)

        hist_header = QHBoxLayout()
        hist_header.addWidget(QLabel("<b>Watch History</b>"))
        hist_header.addStretch()
        self.clear_history_btn = QPushButton("Clear History")
        self.clear_history_btn.clicked.connect(self.clear_history_action)
        hist_header.addWidget(self.clear_history_btn)
        hist_layout.addLayout(hist_header)

        self.history_model = QStringListModel()
        self.history_view = QListView()
        self.history_view.setModel(self.history_model)
        self.history_view.setFrameShape(QFrame.Shape.NoFrame)
        self.history_view.setEditTriggers(QListView.EditTrigger.NoEditTriggers)
        self.history_view.doubleClicked.connect(self.play_from_history_index)
        hist_layout.addWidget(self.history_view)

        # Page 2: Playlists View
        page_playlists = QWidget()
        pl_layout = QHBoxLayout(page_playlists)
        pl_layout.setContentsMargins(0, 0, 0, 0)
        pl_layout.setSpacing(6)

        # Left sub-pane: Playlists list
        pl_left_container = QWidget()
        pl_left_layout = QVBoxLayout(pl_left_container)
        pl_left_layout.setContentsMargins(0, 0, 0, 0)
        pl_left_layout.setSpacing(4)

        pl_header = QHBoxLayout()
        pl_header.addWidget(QLabel("<b>Playlists</b>"))
        pl_header.addStretch()
        self.new_pl_btn = QPushButton("+ New")
        self.new_pl_btn.clicked.connect(self.create_playlist_action)
        self.del_pl_btn = QPushButton("Delete")
        self.del_pl_btn.clicked.connect(self.delete_playlist_action)
        pl_header.addWidget(self.new_pl_btn)
        pl_header.addWidget(self.del_pl_btn)
        pl_left_layout.addLayout(pl_header)

        self.playlists_model = QStringListModel()
        self.playlists_view = QListView()
        self.playlists_view.setModel(self.playlists_model)
        self.playlists_view.setFrameShape(QFrame.Shape.NoFrame)
        self.playlists_view.setEditTriggers(QListView.EditTrigger.NoEditTriggers)
        self.playlists_view.clicked.connect(self.select_playlist_index)
        pl_left_layout.addWidget(self.playlists_view)
        
        pl_left_container.setFixedWidth(250)
        pl_layout.addWidget(pl_left_container)

        # Right sub-pane: Items inside selected playlist
        pl_right_container = QWidget()
        pl_right_layout = QVBoxLayout(pl_right_container)
        pl_right_layout.setContentsMargins(0, 0, 0, 0)
        pl_right_layout.setSpacing(4)

        pl_items_header = QHBoxLayout()
        self.playlist_title_label = QLabel("<b>Select a playlist</b>")
        pl_items_header.addWidget(self.playlist_title_label)
        pl_items_header.addStretch()
        self.remove_from_pl_btn = QPushButton("Remove Item")
        self.remove_from_pl_btn.clicked.connect(self.remove_playlist_item_action)
        pl_items_header.addWidget(self.remove_from_pl_btn)
        pl_right_layout.addLayout(pl_items_header)

        self.playlist_items_model = QStringListModel()
        self.playlist_items_view = QListView()
        self.playlist_items_view.setModel(self.playlist_items_model)
        self.playlist_items_view.setFrameShape(QFrame.Shape.NoFrame)
        self.playlist_items_view.setEditTriggers(QListView.EditTrigger.NoEditTriggers)
        self.playlist_items_view.doubleClicked.connect(self.play_from_playlist_index)
        pl_right_layout.addWidget(self.playlist_items_view)

        pl_layout.addWidget(pl_right_container)

        # Add pages to StackedWidget
        self.stack.addWidget(page_search)   # Index 0
        self.stack.addWidget(page_history)  # Index 1
        self.stack.addWidget(page_playlists)# Index 2

        main_root_layout.addWidget(self.stack)
        self.setLayout(main_root_layout)

        # Populate initial lists
        self.load_history_ui()
        self.load_playlists_ui()

    def switch_page(self, index):
        self.stack.setCurrentIndex(index)
        self.nav_search_btn.setChecked(index == 0)
        self.nav_history_btn.setChecked(index == 1)
        self.nav_playlists_btn.setChecked(index == 2)

    def focus_search_bar(self):
        self.switch_page(0)
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

        # Ctrl + H toggles history page
        self.history_shortcut = QShortcut(QKeySequence("Ctrl+H"), self)
        self.history_shortcut.activated.connect(lambda: self.switch_page(1 if self.stack.currentIndex() != 1 else 0))

        # Ctrl + P toggles playlists page
        self.playlist_shortcut = QShortcut(QKeySequence("Ctrl+P"), self)
        self.playlist_shortcut.activated.connect(lambda: self.switch_page(2 if self.stack.currentIndex() != 2 else 0))

        # Ctrl + Shift + A adds currently selected search item to a playlist
        self.add_pl_shortcut = QShortcut(QKeySequence("Ctrl+Shift+A"), self)
        self.add_pl_shortcut.activated.connect(self.prompt_add_to_playlist)

        # Escape kills audio-only playback
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
            #HoverSidebar {
                background-color: #202020;
                border-right: 1px solid #333333;
                border-radius: 0px;
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
                text-align: left;
            }
            #HoverSidebar QPushButton {
                text-align: left;
                border: none;
                background-color: transparent;
                padding: 6px;
                font-size: 14px;
            }
            #HoverSidebar QPushButton:hover {
                background-color: #333333;
                color: #FF746C;
            }
            #HoverSidebar QPushButton:checked {
                background-color: #AA1515;
                color: #ffffff;
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
                height: 24px;
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
        """
        self.setStyleSheet(qss)

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

    # Playlist UI actions
    def load_playlists_ui(self):
        playlists = self.history_mgr.get_playlists()
        self.playlists_meta = playlists  # list of (id, name)
        display_names = [name for _, name in playlists]
        self.playlists_model.setStringList(display_names)

    def create_playlist_action(self):
        name, ok = QInputDialog.getText(self, "New Playlist", "Enter playlist name:")
        if ok and name.strip():
            success = self.history_mgr.create_playlist(name.strip())
            if not success:
                QMessageBox.warning(self, "Error", "A playlist with this name already exists.")
            self.load_playlists_ui()

    def delete_playlist_action(self):
        index = self.playlists_view.currentIndex()
        row = index.row()
        if 0 <= row < len(self.playlists_meta):
            pl_id, pl_name = self.playlists_meta[row]
            confirm = QMessageBox.question(self, "Delete Playlist", f"Delete playlist '{pl_name}'?", 
                                           QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
            if confirm == QMessageBox.StandardButton.Yes:
                self.history_mgr.delete_playlist(pl_id)
                self.current_playlist_id = None
                self.playlist_title_label.setText("<b>Select a playlist</b>")
                self.playlist_items_model.setStringList([])
                self.load_playlists_ui()

    def select_playlist_index(self, index):
        row = index.row()
        if 0 <= row < len(self.playlists_meta):
            pl_id, pl_name = self.playlists_meta[row]
            self.current_playlist_id = pl_id
            self.playlist_title_label.setText(f"<b>Playlist: {pl_name}</b>")
            
            items = self.history_mgr.get_playlist_items(pl_id)
            self.playlist_items_data = []
            display_strings = []
            for vid, title, author in items:
                self.playlist_items_data.append((f"https://www.youtube.com/watch?v={vid}", vid, title, author))
                display_strings.append(f"{title}  —  👤 {author}")
            self.playlist_items_model.setStringList(display_strings)

    def prompt_add_to_playlist(self):
        index = self.results_view.currentIndex()
        row = index.row()
        if not (0 <= row < len(self.all_search_data)):
            QMessageBox.information(self, "Info", "Please select a search result first to add to a playlist.")
            return

        playlists = self.history_mgr.get_playlists()
        if not playlists:
            QMessageBox.information(self, "Info", "No playlists available. Create one in the Playlists tab first.")
            return

        pl_names = [name for _, name in playlists]
        item_name, ok = QInputDialog.getItem(self, "Add to Playlist", "Choose playlist:", pl_names, 0, False)
        if ok and item_name:
            selected_pl_id = next(pid for pid, name in playlists if name == item_name)
            url, video_id, title, author = self.all_search_data[row]
            self.history_mgr.add_to_playlist(selected_pl_id, video_id, title, author)
            print(f"Added '{title}' to playlist '{item_name}'")

    def remove_playlist_item_action(self):
        if self.current_playlist_id is None:
            return
        index = self.playlist_items_view.currentIndex()
        row = index.row()
        if 0 <= row < len(self.playlist_items_data):
            _, video_id, title, _ = self.playlist_items_data[row]
            self.history_mgr.remove_from_playlist(self.current_playlist_id, video_id)
            # Refresh items
            self.select_playlist_index(self.playlists_view.currentIndex())

    def play_selected_item(self, force_audio=False):
        index = self.results_view.currentIndex()
        row = index.row()
        if 0 <= row < len(self.all_search_data):
            url, video_id, title, author = self.all_search_data[row]
            is_audio = force_audio or self.nav_audio_btn.isChecked()
            self.trigger_playback(url, video_id, title, author, audio_only=is_audio)

    def play_from_history_index(self, index):
        row = index.row()
        if 0 <= row < len(self.history_data):
            url, video_id, title, author = self.history_data[row]
            is_audio = self.nav_audio_btn.isChecked()
            self.trigger_playback(url, video_id, title, author, audio_only=is_audio)

    def play_from_playlist_index(self, index):
        row = index.row()
        if hasattr(self, 'playlist_items_data') and 0 <= row < len(self.playlist_items_data):
            url, video_id, title, author = self.playlist_items_data[row]
            is_audio = self.nav_audio_btn.isChecked()
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
