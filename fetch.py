import math
import yt_dlp
from PyQt6.QtCore import QThread, pyqtSignal
from database import LocalHistoryManager
import ranking


class SearchThread(QThread):
    results_ready = pyqtSignal(list)
    error_occurred = pyqtSignal(str)

    def __init__(self, query, count=50, is_recommendation=False):
        super().__init__()
        self.query = query
        self.count = count
        self.is_recommendation = is_recommendation
        self.history_mgr = LocalHistoryManager()

    def run(self):
        try:
            ydl_opts = {
                'extract_flat': True,
                'quiet': True,
                'no_warnings': True,
                'skip_download': True,
            }

            feed_mode = self.is_recommendation or not self.query
            rows = ranking.load_history_rows(self.history_mgr.db_path)
            profile = ranking.build_profile(rows)

            pool = {}
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                if feed_mode:
                    # Several candidate sources: up-next, channels, topics, exploration
                    plan = ranking.build_queries(profile)
                    per_query = max(15, math.ceil(self.count * 1.5 / len(plan)))
                    for source, q in plan:
                        try:
                            result = ydl.extract_info(f"ytsearch{per_query}:{q}", download=False)
                        except Exception:
                            continue
                        ranking.add_entries(pool, (result or {}).get('entries') or [], source)
                else:
                    result = ydl.extract_info(f"ytsearch{self.count}:{self.query}", download=False)
                    ranking.add_entries(pool, (result or {}).get('entries') or [], "search")

            ranked = ranking.rank(pool, profile, "feed" if feed_mode else "search", self.count)
            self.results_ready.emit([(c.video_id, c.title, c.author) for c in ranked])

        except Exception as e:
            self.error_occurred.emit(str(e))
