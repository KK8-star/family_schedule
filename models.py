from datetime import date
from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()


class Event(db.Model):
    __tablename__ = 'events'

    # ════════════════════════════════════════════════════
    # カラム定義
    # ════════════════════════════════════════════════════
    id            = db.Column(db.Integer,     primary_key=True)
    title         = db.Column(db.String(200), nullable=False)
    date          = db.Column(db.Date,        nullable=False)   # 開始日
    end_date      = db.Column(db.Date,        nullable=True)    # 終了日（複数日イベント用）
    start_time    = db.Column(db.String(10),  nullable=True)
    end_time      = db.Column(db.String(10),  nullable=True)
    family_member = db.Column(db.String(50),  nullable=True)
    memo          = db.Column(db.Text,        nullable=True)
    location      = db.Column(db.String(200), nullable=True)
    sort_order    = db.Column(db.Integer,     default=0, nullable=False)

    # ════════════════════════════════════════════════════
    # プロパティ
    # ════════════════════════════════════════════════════

    @property
    def is_multi_day(self) -> bool:
        """終了日が開始日より後であれば複数日イベントとみなす"""
        return self.end_date is not None and self.end_date > self.date

    @property
    def duration_days(self) -> int:
        """イベントの日数（単日なら 1）"""
        if self.is_multi_day:
            return (self.end_date - self.date).days + 1
        return 1

    @property
    def effective_end_date(self) -> date:
        """end_date が未設定の場合は date を返す（カレンダー計算用）"""
        return self.end_date if self.end_date is not None else self.date

    @property
    def display_date(self) -> str:
        """表示用の日付文字列"""
        if self.is_multi_day:
            return (
                f"{self.date.strftime('%Y/%m/%d')} 〜 "
                f"{self.end_date.strftime('%Y/%m/%d')}"
                f"（{self.duration_days}日間）"
            )
        return self.date.strftime('%Y/%m/%d')

    @property
    def display_time(self) -> str:
        """表示用の時刻文字列"""
        if self.start_time and self.end_time:
            return f"{self.start_time} 〜 {self.end_time}"
        return self.start_time or ''

    # ════════════════════════════════════════════════════
    # シリアライズ
    # ════════════════════════════════════════════════════

    def to_dict(self) -> dict:
        """JSON レスポンス・セッション保存用の辞書を返す"""
        return {
            'id':            self.id,
            'title':         self.title,
            'date':          self.date.strftime('%Y-%m-%d') if self.date else None,
            'end_date':      self.end_date.strftime('%Y-%m-%d') if self.end_date else None,
            'start_time':    self.start_time  or '',
            'end_time':      self.end_time    or '',
            'family_member': self.family_member or '',
            'memo':          self.memo          or '',
            'location':      self.location      or '',
            'sort_order':    self.sort_order     if self.sort_order is not None else 0,
            'is_multi_day':  self.is_multi_day,
            'duration_days': self.duration_days,
            'display_date':  self.display_date,
            'display_time':  self.display_time,
        }

    # ════════════════════════════════════════════════════
    # デバッグ用
    # ════════════════════════════════════════════════════

    def __repr__(self) -> str:
        end = f' 〜 {self.end_date}' if self.end_date else ''
        return f'<Event id={self.id} title="{self.title}" date={self.date}{end}>'
