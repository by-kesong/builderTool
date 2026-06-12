"""入口：python main.py"""
import sys

from PyQt5.QtWidgets import QApplication

from app.main_window import MainWindow
from app.theme import apply_theme


def main():
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    
    # 实例化并获取配置主题
    win = MainWindow()
    theme = win.cfg.get("theme", "dark")
    apply_theme(app, theme)
    win.current_theme = theme
    
    # 密码锁验证
    from app.lock import verify_password
    if not verify_password():
        sys.exit(0)
        
    win.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
