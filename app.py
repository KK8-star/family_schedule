import os
import re
import json
import base64
from datetime import date, datetime, timedelta
from io import BytesIO

from flask import (Flask, render_template, request, redirect,
                   url_for, jsonify, session, flash)
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import extract
from models import db, Event

# ════════════════════════════════════════════════════════════════
# アプリ初期化
# ════════════════════════════════════════════════════════════════
app = Flask(__name__)
app.secret_key = 'family_schedule_secret_key_2024'
basedir = os.path.abspath(os.path.dirname(__file__))
database_url = os.environ.get('DATABASE_URL', 'sqlite:///' + os.path.join(basedir, 'family_schedule.db'))
if database_url.startswith('postgres://'):
    database_url = database_url.replace('postgres://', 'postgresql://', 1)
app.config['SQLALCHEMY_DATABASE_URI'] = database_url
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
db.init_app(app)

# ════════════════════════════════════════════════════════════════
# 定数
# ════════════════════════════════════════════════════════════════
MEMBERS = ['りゅうや', 'りょうや', 'しゅんや', 'さつき', 'あつこ', 'けいや', 'その他']

MEMBER_COLORS = {
    'りゅうや': '#e74c3c',
    'りょうや': '#2c3e8c',
    'しゅんや': '#f39c12',
    'さつき':   '#e91e8c',
    'あつこ':   '#27ae60',
    'けいや':   '#8e44ad',
    'その他':   '#7f8c8d',
}

MAX_BAR_ROWS = 4   # 1セルに表示するリボン最大行数

# ════════════════════════════════════════════════════════════════
# テンプレートグローバル変数
# ════════════════════════════════════════════════════════════════
@app.context_processor
def inject_globals():
    """全テンプレートから参照できるグローバル変数を注入する"""
    return dict(
        MAX_BAR_ROWS=MAX_BAR_ROWS,
        MEMBERS=MEMBERS,
        MEMBER_COLORS=MEMBER_COLORS,
    )


# ════════════════════════════════════════════════════════════════
# DB マイグレーション（起動時に不足カラムを追加）
# ════════════════════════════════════════════════════════════════
def migrate_db():
    with app.app_context():
        db.create_all()
        import sqlite3
        conn = sqlite3.connect(os.path.join(basedir, 'family_schedule.db'))
        cur  = conn.cursor()
        cur.execute("PRAGMA table_info(events)")
        cols = [row[1] for row in cur.fetchall()]
        additions = {
            'end_date':   'ALTER TABLE events ADD COLUMN end_date DATE',
            'memo':       'ALTER TABLE events ADD COLUMN memo TEXT',
            'location':   'ALTER TABLE events ADD COLUMN location VARCHAR(200)',
            'sort_order': 'ALTER TABLE events ADD COLUMN sort_order INTEGER DEFAULT 0',
        }
        for col, sql in additions.items():
            if col not in cols:
                cur.execute(sql)
                print(f'[migrate] Added column: {col}')
        conn.commit()
        conn.close()


# ════════════════════════════════════════════════════════════════
# カレンダー構築ヘルパー
# ════════════════════════════════════════════════════════════════

def _build_event_spans(events, week_start, week_end):
    """
    1週分のイベントスパン情報を構築する。

    Returns: list of dict
        {
          'event':      Eventオブジェクト,
          'row':        行インデックス(0始まり) または None(溢れ),
          'col_start':  週内開始列(0=日曜),
          'col_end':    週内終了列(0=日曜),  # inclusive
          'is_start':   イベント開始日が週内にあるか,
          'is_end':     イベント終了日が週内にあるか,
        }
    """
    # 週内に表示すべきイベントを絞り込む
    visible = []
    for ev in events:
        ev_start = ev.date
        ev_end   = ev.end_date if ev.end_date else ev.date
        if ev_end >= week_start and ev_start <= week_end:
            visible.append(ev)

    # sort_order → date でソート
    visible.sort(key=lambda e: (e.sort_order, e.date))

    # 行アサイン: occupied[row] = 次に使える列インデックス
    occupied = {}   # row -> next_free_col

    spans = []
    for ev in visible:
        ev_start = ev.date
        ev_end   = ev.end_date if ev.end_date else ev.date

        # 週内クリップ
        clip_start = max(ev_start, week_start)
        clip_end   = min(ev_end,   week_end)

        col_start = (clip_start - week_start).days   # 0〜6
        col_end   = (clip_end   - week_start).days   # 0〜6

        is_start = (ev_start >= week_start)
        is_end   = (ev_end   <= week_end)

        # 空いている最小行を探す
        row = 0
        while True:
            next_free = occupied.get(row, 0)
            if next_free <= col_start:
                break
            row += 1
            if row >= MAX_BAR_ROWS:
                row = None
                break

        if row is None:
            spans.append({
                'event':     ev,
                'row':       None,
                'col_start': col_start,
                'col_end':   col_end,
                'is_start':  is_start,
                'is_end':    is_end,
                'date':      clip_start.strftime('%Y-%m-%d'),
            })
        else:
            occupied[row] = col_end + 1
            spans.append({
                'event':     ev,
                'row':       row,
                'col_start': col_start,
                'col_end':   col_end,
                'is_start':  is_start,
                'is_end':    is_end,
                'date':      clip_start.strftime('%Y-%m-%d'),
            })

    return spans


def build_calendar_weeks(year, month, events, member_filter=None):
    """
    月カレンダーの週リストを構築して返す。

    各週は list[7] のセル辞書:
      {
        'date':         date,
        'in_month':     bool,
        'is_today':     bool,
        'bars':         [ {row, col_start, col_end, is_start, is_end, event} ... ],
        'overflow':     int,
      }
    """
    today = date.today()

    # フィルタ適用
    if member_filter and member_filter != '全員':
        filtered_events = [e for e in events if e.family_member == member_filter]
    else:
        filtered_events = list(events)

    # カレンダーの開始日（その月の1日を含む週の日曜日）
    first_day    = date(year, month, 1)
    start_offset = (first_day.weekday() + 1) % 7   # 月=0..日=6 → 日曜=0になるよう変換
    cal_start    = first_day - timedelta(days=start_offset)

    # カレンダーの終了日（その月の末日を含む週の土曜日）
    if month == 12:
        last_day = date(year + 1, 1, 1) - timedelta(days=1)
    else:
        last_day = date(year, month + 1, 1) - timedelta(days=1)
    end_offset = (6 - (last_day.weekday() + 1) % 7) % 7
    cal_end = last_day + timedelta(days=end_offset)

    weeks   = []
    current = cal_start

    while current <= cal_end:
        week_start = current
        week_end   = current + timedelta(days=6)

        # この週のスパンを計算
        spans = _build_event_spans(filtered_events, week_start, week_end)

        # セルごとのデータを初期化
        cells = []
        for d in range(7):
            cell_date = week_start + timedelta(days=d)
            cells.append({
                'date':     cell_date,
                'in_month': cell_date.month == month,
                'is_today': cell_date == today,
                'bars':     [],
                'overflow': 0,
            })

        # スパンをセルに割り当て
        overflow_per_col = {d: 0 for d in range(7)}

        for sp in spans:
            if sp['row'] is None:
                overflow_per_col[sp['col_start']] += 1
            else:
                cells[sp['col_start']]['bars'].append(sp)

        for d in range(7):
            cells[d]['overflow'] = overflow_per_col[d]

        weeks.append(cells)
        current += timedelta(days=7)

    return weeks


# ════════════════════════════════════════════════════════════════
# ルート
# ════════════════════════════════════════════════════════════════

@app.route('/')
def index():
    today = date.today()
    year  = int(request.args.get('year',  today.year))
    month = int(request.args.get('month', today.month))
    member_filter = request.args.get('member', '全員')

    # 前月・次月
    if month == 1:
        prev_y, prev_m = year - 1, 12
    else:
        prev_y, prev_m = year, month - 1
    if month == 12:
        next_y, next_m = year + 1, 1
    else:
        next_y, next_m = year, month + 1

    # 表示月 ±1ヶ月のイベントを取得（週またぎ対応）
    range_start = date(prev_y, prev_m, 1)
    range_end   = date(next_y, next_m, 1) + timedelta(days=37)

    events = Event.query.filter(
        Event.date <= range_end,
        db.or_(
            Event.end_date >= range_start,
            db.and_(Event.end_date == None, Event.date >= range_start)
        )
    ).order_by(Event.sort_order, Event.date).all()

    weeks  = build_calendar_weeks(year, month, events, member_filter)
    colors = session.get('member_colors', MEMBER_COLORS)

    return render_template('index.html',
        year=year,
        month=month,
        weeks=weeks,
        members=MEMBERS,
        member_filter=member_filter,
        member_colors=colors,
        prev_year=prev_y,
        prev_month=prev_m,
        next_year=next_y,
        next_month=next_m,
        today=today,
        MAX_BAR_ROWS=MAX_BAR_ROWS,
    )


# ────────────────────────────────────────
# イベント CRUD
# ────────────────────────────────────────

@app.route('/event/new', methods=['GET', 'POST'])
def new_event():
    if request.method == 'POST':
        ev = _event_from_form(request.form)
        db.session.add(ev)
        db.session.commit()
        flash('イベントを追加しました', 'success')
        return redirect(url_for('index',
            year=ev.date.year, month=ev.date.month))
    today  = date.today()
    colors = session.get('member_colors', MEMBER_COLORS)
    # ── ここを修正: event_form.html → add_event.html ──
    return render_template('add_event.html',
        event=None,
        members=MEMBERS,
        member_colors=colors,
        default_date=today.strftime('%Y-%m-%d'))


@app.route('/event/<int:event_id>/edit', methods=['GET', 'POST'])
def edit_event(event_id):
    ev = Event.query.get_or_404(event_id)
    if request.method == 'POST':
        _update_event_from_form(ev, request.form)
        db.session.commit()
        flash('イベントを更新しました', 'success')
        return redirect(url_for('index',
            year=ev.date.year, month=ev.date.month))
    colors = session.get('member_colors', MEMBER_COLORS)
    # ── ここを修正: event_form.html → edit_event.html ──
    return render_template('edit_event.html',
        event=ev,
        members=MEMBERS,
        member_colors=colors,
        default_date=ev.date.strftime('%Y-%m-%d'))


@app.route('/event/<int:event_id>/delete', methods=['POST'])
def delete_event(event_id):
    ev = Event.query.get_or_404(event_id)
    year, month = ev.date.year, ev.date.month
    db.session.delete(ev)
    db.session.commit()
    flash('イベントを削除しました', 'info')
    return redirect(url_for('index', year=year, month=month))


@app.route('/event/<int:event_id>/copy', methods=['POST'])
def copy_event(event_id):
    src = Event.query.get_or_404(event_id)
    from datetime import datetime, timedelta
    import json

    # new_date を JSON または Form から取得
    new_date_str = None
    if request.is_json:
        data = request.get_json(silent=True) or {}
        new_date_str = data.get('new_date')
    if not new_date_str:
        new_date_str = request.form.get('new_date')

    if new_date_str:
        try:
            new_start = datetime.strptime(new_date_str, '%Y-%m-%d').date()
            # 複数日イベントの場合、期間を保持して end_date をシフト
            if src.end_date and src.end_date > src.date:
                delta = src.end_date - src.date
                new_end = new_start + delta
            else:
                new_end = new_start
        except ValueError:
            new_start = src.date
            new_end = src.end_date
    else:
        new_start = src.date
        new_end = src.end_date

    new_ev = Event(
        title         = src.title,
        date          = new_start,
        end_date      = new_end,
        start_time    = src.start_time,
        end_time      = src.end_time,
        family_member = src.family_member,
        memo          = src.memo,
        location      = src.location,
        sort_order    = src.sort_order,
    )
    db.session.add(new_ev)
    db.session.commit()
    flash(f'「{src.title}」をコピーしました', 'success')
    return redirect(url_for('index',
        year=new_start.year, month=new_start.month))


@app.route('/event/<int:event_id>/move', methods=['POST'])
def move_event(event_id):
    ev = Event.query.get_or_404(event_id)
    if request.is_json:
        data = request.get_json()
        new_date_str = data.get('date') or data.get('new_date')
    else:
        new_date_str = request.form.get('new_date')
    if new_date_str:
        old_date = ev.date
        new_date = datetime.strptime(new_date_str, '%Y-%m-%d').date()
        delta    = new_date - old_date
        ev.date  = new_date
        if ev.end_date:
            ev.end_date = ev.end_date + delta
        db.session.commit()
        if request.is_json:
            return jsonify({'status': 'ok', 'event': ev.to_dict()})
        from flask import redirect, url_for
        return redirect(url_for('index', year=ev.date.year, month=ev.date.month))
    if request.is_json:
        return jsonify({'status': 'error', 'message': 'date required'}), 400
    from flask import redirect, url_for
    return redirect(url_for('index'))


@app.route('/event/<int:event_id>/json')
def event_json(event_id):
    ev = Event.query.get_or_404(event_id)
    return jsonify(ev.to_dict())


@app.route('/api/reorder', methods=['POST'])
def reorder_events():
    data  = request.get_json()
    order = data.get('order', [])   # [{id: X, sort_order: Y}, ...]
    for item in order:
        ev = Event.query.get(item['id'])
        if ev:
            ev.sort_order = item['sort_order']
    db.session.commit()
    return jsonify({'status': 'ok'})


# ────────────────────────────────────────
# カラー設定 API
# ────────────────────────────────────────

@app.route('/api/colors', methods=['GET'])
def get_colors():
    colors = session.get('member_colors', MEMBER_COLORS)
    return jsonify(colors)


@app.route('/api/colors', methods=['POST'])
def save_colors():
    data   = request.get_json()
    colors = session.get('member_colors', dict(MEMBER_COLORS))
    for member, color in data.items():
        if member in MEMBER_COLORS:
            colors[member] = color
    session['member_colors'] = colors
    session.modified = True
    return jsonify({'status': 'ok', 'colors': colors})


@app.route('/colors')
def color_settings():
    colors = session.get('member_colors', MEMBER_COLORS)
    return render_template('color_settings.html', members=MEMBERS, colors=colors)


# ────────────────────────────────────────
# AI 画像インポート
# ────────────────────────────────────────

@app.route('/import/image', methods=['GET', 'POST'])
def import_image():
    if request.method == 'GET':
        return render_template('import_image.html', members=MEMBERS)

    file = request.files.get('image')
    if not file:
        return jsonify({'status': 'error', 'message': '画像がありません'}), 400

    try:
        result = analyze_schedule_image(file)
        return jsonify({'status': 'ok', 'events': result})
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500


@app.route('/import/confirm', methods=['POST'])
def import_confirm():
    data   = request.get_json()
    events = data.get('events', [])
    saved  = []
    for item in events:
        try:
            ev_date = datetime.strptime(item['date'], '%Y-%m-%d').date()
        except Exception:
            ev_date = date.today()
        end_date = None
        if item.get('end_date'):
            try:
                end_date = datetime.strptime(item['end_date'], '%Y-%m-%d').date()
            except Exception:
                pass
        ev = Event(
            title         = item.get('title', '(無題)'),
            date          = ev_date,
            end_date      = end_date,
            start_time    = item.get('start_time', ''),
            end_time      = item.get('end_time', ''),
            family_member = item.get('family_member', 'その他'),
            memo          = item.get('memo', ''),
            location      = item.get('location', ''),
            sort_order    = 0,
        )
        db.session.add(ev)
        saved.append(ev)
    db.session.commit()
    return jsonify({'status': 'ok', 'count': len(saved)})


# ════════════════════════════════════════════════════════════════
# フォームヘルパー
# ════════════════════════════════════════════════════════════════

def _event_from_form(form) -> Event:
    return Event(
        title         = form.get('title', '').strip(),
        date          = _parse_date(form.get('date')),
        end_date      = _parse_date(form.get('end_date')) or None,
        start_time    = form.get('start_time', '').strip() or None,
        end_time      = form.get('end_time',   '').strip() or None,
        family_member = form.get('family_member', 'その他'),
        memo          = form.get('memo', '').strip() or None,
        location      = form.get('location', '').strip() or None,
        sort_order    = int(form.get('sort_order', 0)),
    )


def _update_event_from_form(ev: Event, form):
    ev.title         = form.get('title', '').strip()
    ev.date          = _parse_date(form.get('date'))
    ev.end_date      = _parse_date(form.get('end_date')) or None
    ev.start_time    = form.get('start_time', '').strip() or None
    ev.end_time      = form.get('end_time',   '').strip() or None
    ev.family_member = form.get('family_member', 'その他')
    ev.memo          = form.get('memo', '').strip() or None
    ev.location      = form.get('location', '').strip() or None
    ev.sort_order    = int(form.get('sort_order', 0))


def _parse_date(value: str):
    if not value:
        return None
    try:
        return datetime.strptime(value.strip(), '%Y-%m-%d').date()
    except ValueError:
        return None


# ════════════════════════════════════════════════════════════════
# AI 画像解析
# ════════════════════════════════════════════════════════════════

def analyze_schedule_image(file) -> list:
    try:
        from google import genai
        from google.genai import types
        from PIL import Image
    except ImportError as e:
        raise RuntimeError(f'必要なライブラリがありません: {e}')

    # 画像リサイズ
    img      = Image.open(file.stream)
    max_size = 2048
    if max(img.size) > max_size:
        ratio = max_size / max(img.size)
        img   = img.resize(
            (int(img.width * ratio), int(img.height * ratio)),
            Image.LANCZOS
        )
    buf = BytesIO()
    img.save(buf, format='JPEG', quality=85)
    img_bytes = buf.getvalue()

    client = genai.Client(api_key=os.environ.get('GEMINI_API_KEY', ''))

    members_str = '、'.join(MEMBERS)
    today_str   = date.today().strftime('%Y-%m-%d')
    bt          = '`' * 3

    prompt = f"""
この画像から家族のスケジュール情報を抽出してください。

家族メンバー: {members_str}
今日の日付: {today_str}

以下のJSON形式で返してください:
{bt}json
[
  {{
    "title": "イベント名",
    "date": "YYYY-MM-DD",
    "end_date": "YYYY-MM-DD または null",
    "start_time": "HH:MM または空文字",
    "end_time": "HH:MM または空文字",
    "family_member": "メンバー名",
    "memo": "メモ または空文字",
    "location": "場所 または空文字"
  }}
]
{bt}

注意事項:
- date は必ず YYYY-MM-DD 形式
- family_member は上記メンバーから選択（不明なら「その他」）
- 複数日イベントは end_date を設定
- JSONのみ返してください
"""

    response = client.models.generate_content(
        model='gemini-2.0-flash',
        contents=[
            types.Part.from_bytes(data=img_bytes, mime_type='image/jpeg'),
            prompt,
        ]
    )

    text = response.text.strip()

    # JSON 抽出
    json_match = re.search(r'```(?:json)?\s*([\s\S]*?)```', text)
    if json_match:
        json_str = json_match.group(1).strip()
    else:
        json_str = text

    try:
        result = json.loads(json_str)
    except json.JSONDecodeError:
        arr_match = re.search(r'\[[\s\S]*\]', json_str)
        if arr_match:
            result = json.loads(arr_match.group())
        else:
            raise ValueError(f'JSONの解析に失敗しました: {json_str[:200]}')

    # デフォルト値補完
    today_str = date.today().strftime('%Y-%m-%d')
    cleaned   = []
    for item in result:
        if not isinstance(item, dict):
            continue
        cleaned.append({
            'title':         item.get('title', '(無題)'),
            'date':          item.get('date', today_str),
            'end_date':      item.get('end_date') or None,
            'start_time':    item.get('start_time', ''),
            'end_time':      item.get('end_time', ''),
            'family_member': item.get('family_member', 'その他'),
            'memo':          item.get('memo', ''),
            'location':      item.get('location', ''),
        })
    return cleaned


# ════════════════════════════════════════════════════════════════
# エントリポイント
# ════════════════════════════════════════════════════════════════

if __name__ == '__main__':
    migrate_db()
    app.run(debug=True, host='0.0.0.0', port=5000)
