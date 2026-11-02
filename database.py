import sqlite3
from collections import Counter
import re
import random
from datetime import datetime, timezone

DB_PATH = "local_history.db"

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
            CREATE TABLE IF NOT EXISTS persistent_history (
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

        cursor.execute('INSERT INTO persistent_history (video_id, title, author) VALUES (?, ?, ?)', 
                       (video_id, title, author))
        cursor.execute('''
            DELETE FROM persistent_history WHERE id NOT IN (
                SELECT id FROM persistent_history ORDER BY watched_at DESC LIMIT ?
            )
        ''', (500,))

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

    def get_user_preferences(self):
        """Extracts time-decayed author weights, keyword frequencies, and watched IDs."""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute('SELECT video_id, title, author, watched_at FROM persistent_history ORDER BY watched_at DESC LIMIT 200')
        rows = cursor.fetchall()
        conn.close()

        if not rows:
            return {}, Counter(), set()

        watched_ids = {row[0] for row in rows}
        author_weights = {}
        words = []
        
        now = datetime.now(timezone.utc)
        stop_words = {"ft", "feat", "official", "video", "lyrics", "hd", "live", "audio", "music", "the", "a", "an", "and", "or", "to", "of", "in"}

        for row in rows:
            video_id, title, author, watched_at_str = row[0], row[1], row[2], row[3]
            
            # Parse timestamp for exponential time decay (half-life of ~4 days)
            try:
                # Handle standard SQLite timestamp format
                watched_dt = datetime.strptime(watched_at_str.split('.')[0], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
                age_days = max(0.0, (now - watched_dt).total_seconds() / 86400.0)
            except Exception:
                age_days = 1.0

            # Decay factor: drops weight smoothly over time
            time_weight = 0.5 ** (age_days / 4.0)

            if author and author != 'Unknown Author':
                author_weights[author] = author_weights.get(author, 0.0) + time_weight

            if title:
                cleaned = re.sub(r'[^\w\s]', '', title.lower())
                for word in cleaned.split():
                    if len(word) > 3 and word not in stop_words:
                        words.append((word, time_weight))

        # Build weighted keyword frequency Counter
        word_counts = Counter()
        for word, weight in words:
            word_counts[word] += weight

        return author_weights, word_counts, watched_ids

    def get_recommendation_queries(self):
        author_weights, word_counts, _ = self.get_user_preferences()
        if not author_weights and not word_counts:
            return ["trending"]

        queries = []
        # Sort authors by their decayed score
        sorted_authors = sorted(author_weights.items(), key=lambda x: x[1], reverse=True)
        for author, _ in sorted_authors[:3]:
            queries.append(author)

        top_words = [word for word, _ in word_counts.most_common(6)]
        if len(top_words) >= 2:
            queries.append(f"{top_words[0]} {top_words[1]}")
        if top_words:
            queries.append(top_words[0])

        queries.append("trending")
        return queries

    # Playlist DB methods (unchanged)
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
