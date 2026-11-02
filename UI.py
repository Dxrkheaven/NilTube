import os
import subprocess
from PyQt6.QtCore import QStringListModel, Qt
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, 
    QLineEdit, QPushButton, QListView, QFrame, QLabel,
    QStackedWidget, QInputDialog, QMessageBox
)
from PyQt6.QtGui import QShortcut, QKeySequence, QKeyEvent

from database import LocalHistoryManager
from fetch import SearchThread

class SearchBar(QLineEdit):
    """Custom search input with Down/Left arrow focus handling."""
    def __init__(self, focus_target=None, parent=None, app_ref=None):
        super().__init__(parent)
        self.focus_target = focus_target
        self.app_ref = app_ref

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Down and self.focus_target:
            self.focus_target.setFocus()
            event.accept()
        elif event.key() == Qt.Key.Key_Left and self.cursorPosition() == 0 and self.app_ref:
            self.app_ref.focus_sidebar()
            event.accept()
        else:
            super().keyPressEvent(event)


class FocusListView(QListView):
    """Custom QListView that jumps to the sidebar when pressing Left arrow."""
    def __init__(self, parent=None, app_ref=None):
        super().__init__(parent)
        self.app_ref = app_ref

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Left and self.app_ref:
            self.app_ref.focus_sidebar()
            event.accept()
        else:
            super().keyPressEvent(event)


class HoverSidebar(QFrame):
    """LibreWolf-style expandable sidebar with arrow-key navigation support"""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedWidth(46)
        self.expanded_width = 160
        self.collapsed_width = 46
        self.setObjectName("HoverSidebar")
        self.buttons = []
        self.main_app = parent

    def expand(self):
        self.setFixedWidth(self.expanded_width)
        if self.buttons:
            for btn, icon, text in self.buttons:
                btn.setText(f"  {icon}  {text}")

    def collapse(self):
        self.setFixedWidth(self.collapsed_width)
        if self.buttons:
            for btn, icon, text in self.buttons:
                btn.setText(icon)

    def enterEvent(self, event):
        self.expand()
        super().enterEvent(event)

    def leaveEvent(self, event):
        if not self.hasFocus() and not any(btn.hasFocus() for btn, _, _ in self.buttons):
            self.collapse()
        super().leaveEvent(event)

    def set_buttons(self, buttons_list):
        self.buttons = buttons_list
        for btn, icon, text in self.buttons:
            btn.setText(icon)

    def keyPressEvent(self, event: QKeyEvent):
        current_focus = self.focusWidget()
        btn_widgets = [b[0] for b in self.buttons]
        
        if current_focus in btn_widgets:
            idx = btn_widgets.index(current_focus)
            if event.key() == Qt.Key.Key_Down:
                next_idx = (idx + 1) % len(btn_widgets)
                btn_widgets[next_idx].setFocus()
                event.accept()
                return
            elif event.key() == Qt.Key.Key_Up:
                prev_idx = (idx - 1) % len(btn_widgets)
                btn_widgets[prev_idx].setFocus()
                event.accept()
                return
            elif event.key() in (Qt.Key.Key_Right, Qt.Key.Key_Return, Qt.Key.Key_Enter):
                current_focus.click()
                if self.main_app:
                    self.main_app.return_focus_to_content()
                event.accept()
                return
            elif event.key() == Qt.Key.Key_Left:
                if self.main_app:
                    self.main_app.return_focus_to_content()
                event.accept()
                return
                
        super().keyPressEvent(event)


class PrivacyYouTubeApp(QWidget):
    def __init__(self):
        super().__init__()
        self.history_mgr = LocalHistoryManager()
        
        self.all_search_data = []
        # Load personalized recommendations based on watch history, fallback to trending
        queries = self.history_mgr.get_recommendation_queries()
        self.current_query = queries[0] if queries else "trending"
        self.search_count = 50 
        self.is_fetching = False
        self.called = False
        
        self.history_data = []
        self.playlist_data = []
        self.current_playlist_id = None
        
        self.initUI()
        self.apply_terminal_stylesheet()
        self.setup_shortcuts()

        # Initial fetch on launch (personalized recommendations feed)
        self.load_more_content()

    def initUI(self):
        self.setWindowTitle('NilTube // By Dxrkheaven')
        self.resize(1050, 700)

        main_root_layout = QHBoxLayout(self)
        main_root_layout.setContentsMargins(4, 4, 4, 4)
        main_root_layout.setSpacing(4)

        # --- Left Hover Sidebar ---
        self.sidebar = HoverSidebar(self)
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

        buttons_list = [
            (self.nav_search_btn, "🔍", "Search"),
            (self.nav_audio_btn, "🎵", "Audio Mode"),
            (self.nav_history_btn, "📜", "History"),
            (self.nav_playlists_btn, "📂", "Playlists")
        ]
        self.sidebar.set_buttons(buttons_list)
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
        self.search_input = SearchBar(focus_target=None, app_ref=self)
        self.search_input.setPlaceholderText("Search YouTube privately (leave blank for recommendations)...")
        self.search_input.returnPressed.connect(self.perform_search)
        
        self.search_btn = QPushButton("Search")
        self.search_btn.clicked.connect(self.perform_search)

        top_bar.addWidget(self.search_input)
        top_bar.addWidget(self.search_btn)
        search_layout.addLayout(top_bar)

        self.results_model = QStringListModel()
        self.results_view = FocusListView(app_ref=self)
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
        self.history_view = FocusListView(app_ref=self)
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
        self.playlists_view = FocusListView(app_ref=self)
        self.playlists_view.setModel(self.playlists_model)
        self.playlists_view.setFrameShape(QFrame.Shape.NoFrame)
        self.playlists_view.setEditTriggers(QListView.EditTrigger.NoEditTriggers)
        self.playlists_view.clicked.connect(self.select_playlist_index)
        self.playlists_view.keyPressEvent = self.playlist_key_press_event
        
        pl_left_layout.addWidget(self.playlists_view)
        pl_left_container.setFixedWidth(250)
        pl_layout.addWidget(pl_left_container)

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
        self.playlist_items_view = FocusListView(app_ref=self)
        self.playlist_items_view.setModel(self.playlist_items_model)
        self.playlist_items_view.setFrameShape(QFrame.Shape.NoFrame)
        self.playlist_items_view.setEditTriggers(QListView.EditTrigger.NoEditTriggers)
        self.playlist_items_view.doubleClicked.connect(self.play_from_playlist_index)
        self.playlist_items_view.keyPressEvent = self.playlist_item_key_press_event

        pl_right_layout.addWidget(self.playlist_items_view)
        pl_layout.addWidget(pl_right_container)

        self.stack.addWidget(page_search)   # Index 0
        self.stack.addWidget(page_history)  # Index 1
        self.stack.addWidget(page_playlists)# Index 2

        main_root_layout.addWidget(self.stack)
        self.setLayout(main_root_layout)

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
        self.return_shortcut = QShortcut(QKeySequence("Return"), self.results_view)
        self.return_shortcut.setContext(Qt.ShortcutContext.WidgetShortcut)
        self.return_shortcut.activated.connect(self.play_selected_item)

        self.shift_return_shortcut = QShortcut(QKeySequence("Shift+Return"), self.results_view)
        self.shift_return_shortcut.setContext(Qt.ShortcutContext.WidgetShortcut)
        self.shift_return_shortcut.activated.connect(lambda: self.play_selected_item(force_audio=True))

        self.history_shortcut = QShortcut(QKeySequence("Ctrl+H"), self)
        self.history_shortcut.activated.connect(lambda: self.switch_page(1 if self.stack.currentIndex() != 1 else 0))

        self.playlist_shortcut = QShortcut(QKeySequence("Ctrl+P"), self)
        self.playlist_shortcut.activated.connect(lambda: self.switch_page(2 if self.stack.currentIndex() != 2 else 0))

        self.add_pl_shortcut = QShortcut(QKeySequence("Ctrl+Shift+A"), self)
        self.add_pl_shortcut.activated.connect(self.prompt_add_to_playlist)

        self.kill_audio_shortcut = QShortcut(QKeySequence("Escape"), self)
        self.kill_audio_shortcut.activated.connect(lambda: subprocess.run(["pkill", "mpv"]))

        self.focus_search_shortcut = QShortcut(QKeySequence("Ctrl+L"), self)
        self.focus_search_shortcut.activated.connect(self.focus_search_bar)

        self.focus_sidebar_shortcut = QShortcut(QKeySequence("Ctrl+B"), self)
        self.focus_sidebar_shortcut.activated.connect(self.focus_sidebar)

    def focus_sidebar(self):
        self.sidebar.expand()
        self.nav_search_btn.setFocus()

    def return_focus_to_content(self):
        self.sidebar.collapse()
        current_idx = self.stack.currentIndex()
        if current_idx == 0:
            self.search_input.setFocus()
        elif current_idx == 1:
            self.history_view.setFocus()
        elif current_idx == 2:
            self.playlists_view.setFocus()

    def playlist_key_press_event(self, event: QKeyEvent):
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            index = self.playlists_view.currentIndex()
            if index.isValid():
                self.select_playlist_index(index)
                self.playlist_items_view.setFocus()
            event.accept()
        elif event.key() == Qt.Key.Key_Left:
            self.focus_sidebar()
            event.accept()
        else:
            FocusListView.keyPressEvent(self.playlists_view, event)

    def playlist_item_key_press_event(self, event: QKeyEvent):
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            index = self.playlist_items_view.currentIndex()
            if index.isValid():
                self.play_from_playlist_index(index)
            event.accept()
        elif event.key() == Qt.Key.Key_Left:
            self.playlists_view.setFocus()
            event.accept()
        else:
            FocusListView.keyPressEvent(self.playlist_items_view, event)

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
            #HoverSidebar QPushButton:focus {
                background-color: #AA1515;
                border: 2px solid #FF746C;
                color: #ffffff;
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
        
        # Check if we are browsing recommendations vs searching
        is_rec = not bool(self.search_input.text().strip())
        
        self.search_thread = SearchThread(self.current_query, count=self.search_count, is_recommendation=is_rec)
        self.search_thread.results_ready.connect(self.on_search_results)
        self.search_thread.error_occurred.connect(self.on_search_error)
        self.search_thread.start()
    
    def perform_search(self):
        query = self.search_input.text().strip()
        if query:
            self.current_query = query
        else:
            queries = self.history_mgr.get_recommendation_queries()
            self.current_query = queries[0] if queries else "trending"

        self.all_search_data.clear()
        self.search_count = 50
        self.is_fetching = False
        self.called = False
        self.results_model.setStringList(["Fetching recommendations..." if not query else "Searching..."])
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
        print("Local watch history cleared (persistent recommendation cache preserved).")

    def load_playlists_ui(self):
        playlists = self.history_mgr.get_playlists()
        self.playlists_meta = playlists 
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
