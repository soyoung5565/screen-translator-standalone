"""屏幕翻译器 UI 组件库 —— 分段控件 + 状态指示灯。

设计稿来源：OpenDesign `screen-translator-ui` 项目（2026-10-29）。
这两个组件是现有 app.py 里没有的、且 Qt Style Sheets 单独表达不了的部分：

- `SegmentedControl` 替代贴字模式的下拉框。Qt 的 QComboBox 在窄面板里
  既看不出是下拉框（要运行时画三角 PNG），又让用户猜不出两个选项的区别。
  分段控件把二选一摊平成两个并排按钮，语义直白。
- `StatusDot` 承担「运行态必须一眼可见」这个硬约束。文字状态（就绪 / 翻译中 /
  错误）藏在 10px 灰字里等于没有；状态灯用 8px 实心圆 + 呼吸动画表达
  「现在活着还是死了」，颜色只承担语义不承担全部信息。

两者都只用 Qt Style Sheets + paintEvent，不引入任何新依赖。
"""
from PyQt5.QtCore import (QPropertyAnimation, QEasingCurve, QPoint,
                          QPropertyAnimation, Qt, QTimer)
from PyQt5.QtGui import QColor, QFont, QPainter, QPen
from PyQt5.QtWidgets import QButtonGroup, QHBoxLayout, QPushButton, QWidget


class SegmentedControl(QWidget):
    """二选一（或多选一）的分段按钮组。

    视觉：一个凹槽容器 + N 个等宽按钮，选中项用主色填充 + 容器背景。
    高度固定 30px，与面板其它控件对齐。
    """

    def __init__(self, items, parent=None):
        super().__init__(parent)
        self.setObjectName("segControl")
        # items: [(显示文本, 数据值), ...]
        self._group = QButtonGroup(self)
        self._group.setExclusive(True)
        self._buttons = []
        lay = QHBoxLayout(self)
        lay.setContentsMargins(3, 3, 3, 3)
        lay.setSpacing(2)
        for i, (text, data) in enumerate(items):
            b = QPushButton(text)
            b.setCheckable(True)
            b.setCursor(Qt.PointingHandCursor)
            b.setProperty("segIndex", i)
            b.setProperty("segData", data)
            self._group.addButton(b, i)
            lay.addWidget(b, 1)
            self._buttons.append(b)
        self._group.idClicked.connect(self._on_clicked)
        if self._buttons:
            self._buttons[0].setChecked(True)

    def _on_clicked(self, idx):
        pass

    def currentData(self):
        b = self._group.checkedButton()
        return b.property("segData") if b is not None else None

    def setCurrentData(self, data):
        for b in self._buttons:
            if b.property("segData") == data:
                b.setChecked(True)
                return

    def currentText(self):
        b = self._group.checkedButton()
        return b.text() if b is not None else ""

    def buttons(self):
        return list(self._buttons)

    def setStyleSheet(self, css):
        super().setStyleSheet(css)
        for b in self._buttons:
            b.setStyleSheet(css)


class StatusDot(QWidget):
    """8px 状态灯。

    kind: 'idle' | 'live' | 'error'
    live 状态有呼吸动画（opacity 100% → 35% 循环），用来表达「进程在动」。
    动画走 QTimer + 手动重绘，不引入 QPropertyAnimation 的额外依赖面。
    """

    COLORS = {
        "idle":   ("#6e7681", 0),
        "live":   ("#2ea043", 1),
        "region": ("#58a6ff", 0),
        "error":  ("#f85149", 0),
    }

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(10, 10)
        self._kind = "idle"
        self._phase = 1.0
        self._timer = QTimer(self)
        self._timer.setInterval(60)
        self._timer.timeout.connect(self._tick)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)

    def set_kind(self, kind):
        if kind == self._kind:
            return
        self._kind = kind
        if kind == "live":
            self._phase = 1.0
            self._timer.start()
        else:
            self._timer.stop()
            self._phase = 1.0
        self.update()

    def kind(self):
        return self._kind

    def _tick(self):
        # 0.55s 一个来回
        self._phase = 1.0 if self._phase <= 0.35 else 0.35
        self.update()

    def paintEvent(self, ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        color = QColor(self.COLORS.get(self._kind, ("#6e7681", 0))[0])
        if self._kind == "live":
            color.setAlphaF(self._phase)
        p.setBrush(color)
        p.setPen(Qt.NoPen)
        r = self.rect()
        p.drawEllipse(r.center(), 4, 4)
        p.end()
