"""软件安全解锁模块：支持启动时密码验证与密码修改。"""
import os
import json
from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton, QMessageBox
)

LOCK_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".lock_config.json")

def get_stored_password() -> str:
    """获取当前存储的密码，默认 123456"""
    if os.path.exists(LOCK_FILE):
        try:
            with open(LOCK_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                return data.get("password", "123456")
        except Exception:
            pass
    return "123456"

def save_password(new_pwd: str):
    """保存新密码"""
    try:
        with open(LOCK_FILE, "w", encoding="utf-8") as f:
            json.dump({"password": new_pwd}, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"保存密码出错: {e}")

class UnlockDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("安全验证 - builderTool")
        self.setWindowFlags(self.windowFlags() & ~Qt.WindowContextHelpButtonHint)
        self.resize(320, 160)
        self.setFixedSize(320, 160)
        
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(12)
        
        # 提示语
        self.label = QLabel("🔑 请输入解锁密码以继续使用软件：")
        self.label.setStyleSheet("font-weight: bold; font-size: 12px;")
        layout.addWidget(self.label)
        
        # 输入框
        self.pwd_input = QLineEdit()
        self.pwd_input.setEchoMode(QLineEdit.Password)
        self.pwd_input.setPlaceholderText("请输入密码")
        self.pwd_input.returnPressed.connect(self._verify)
        layout.addWidget(self.pwd_input)
        
        # 按钮
        btn_layout = QHBoxLayout()
        btn_layout.setSpacing(10)
        
        self.ok_btn = QPushButton("解锁")
        self.ok_btn.setStyleSheet("background-color: #2980b9; color: white; font-weight: bold; padding: 4px 15px;")
        self.ok_btn.clicked.connect(self._verify)
        
        self.cancel_btn = QPushButton("取消")
        self.cancel_btn.clicked.connect(self.reject)
        
        btn_layout.addStretch(1)
        btn_layout.addWidget(self.ok_btn)
        btn_layout.addWidget(self.cancel_btn)
        
        layout.addLayout(btn_layout)
        
    def _verify(self):
        entered = self.pwd_input.text()
        correct = get_stored_password()
        if entered == correct:
            self.accept()
        else:
            QMessageBox.critical(self, "解锁失败", "输入的密码不正确，请重试！")
            self.pwd_input.clear()
            self.pwd_input.setFocus()

def verify_password() -> bool:
    """启动时验证密码"""
    # 第一次运行若文件不存在，创建默认文件
    if not os.path.exists(LOCK_FILE):
        save_password("123456")
        
    dlg = UnlockDialog()
    return dlg.exec_() == QDialog.Accepted

def change_unlock_password(parent):
    """修改解锁密码"""
    # 1. 验证旧密码
    from PyQt5.QtWidgets import QInputDialog
    old_pwd, ok1 = QInputDialog.getText(
        parent, "修改密码", "请输入当前密码：", QLineEdit.Password
    )
    if not ok1:
        return
        
    if old_pwd != get_stored_password():
        QMessageBox.critical(parent, "错误", "原密码验证失败！")
        return
        
    # 2. 输入新密码
    new_pwd, ok2 = QInputDialog.getText(
        parent, "修改密码", "请输入新密码：", QLineEdit.Password
    )
    if not ok2:
        return
        
    new_pwd = new_pwd.strip()
    if not new_pwd:
        QMessageBox.warning(parent, "警告", "密码不能为空！")
        return
        
    # 3. 确认新密码
    confirm_pwd, ok3 = QInputDialog.getText(
        parent, "修改密码", "请再次输入新密码以确认：", QLineEdit.Password
    )
    if not ok3:
        return
        
    if new_pwd != confirm_pwd:
        QMessageBox.critical(parent, "错误", "两次输入的新密码不一致！")
        return
        
    # 4. 保存
    save_password(new_pwd)
    QMessageBox.information(parent, "成功", "解锁密码修改成功！下次启动软件生效。")
