import sys
import os
from PyQt6.QtWidgets import QApplication
from UI import PrivacyYouTubeApp

# Silence Qt font warnings and Wayland noise
os.environ["QT_LOGGING_RULES"] = "qt.qpa.wayland.textinput.warning=false;qt.text.font.db.warning=false"

if __name__ == '__main__':
    app = QApplication(sys.argv)
    ex = PrivacyYouTubeApp()
    ex.show()
    sys.exit(app.exec())
