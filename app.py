# -*- coding: utf-8 -*-
"""
社用車管理アプリ（Streamlit製）＝オンライン版
  ・社内/社外の両方からアクセスできるようにオンライン公開向けに調整した版です。
  ・現行（社内PC版）とは別ファイルで、こちらが「オンライン版」です。

オンライン版での主な違い：
  1. 日本時間(JST)で「今日」を判定（サーバーがUTCでも日付がずれない）
  2. パスワードでログイン（社外公開のため）
  3. データベースの保存場所を環境変数で変更可能（永続ディスク等に対応）
  4. 掃除当番の横に「入替」列を追加（当番交代をその日だけ直接入力できる）
"""

import base64
import calendar
import datetime
import json
import os
import sqlite3
from pathlib import Path

import jpholiday
import pandas as pd
import streamlit as st

# ============================================================
# 基本設定
# ============================================================
st.set_page_config(page_title="社用車・当番管理", page_icon="🚗", layout="wide")

# 日本時間（JST = UTC+9）
JST = datetime.timezone(datetime.timedelta(hours=9))


def jst_today() -> datetime.date:
    """日本時間での今日の日付（サーバーがUTCでも正しく判定）"""
    return datetime.datetime.now(JST).date()


# データベースの保存場所。
# 環境変数 SHAYOSHA_DB があればそれを使う（クラウドの永続ディスク等を指定可能）。
# 無ければこのファイルと同じフォルダの shayosha.db を使う。
DB_PATH = Path(os.environ.get("SHAYOSHA_DB", Path(__file__).parent / "shayosha.db"))

WEEKDAY_JP = ["月", "火", "水", "木", "金", "土", "日"]


# ============================================================
# データ保存先：ローカルSQLite または Turso（クラウドDB）
#   ・Turso（クラウドDB）の接続情報があればそちらに保存（オンライン公開向け）。
#   ・無ければローカルの shayosha.db を使う（社内/ローカル利用向け）。
#   ・Turso へは公式ライブラリではなく HTTP API で接続するため、
#     追加のネイティブライブラリ不要でどの環境でも動く。
# ============================================================
def _turso_config():
    """Turso接続情報 (url, token) を返す。未設定なら (None, None)。
    Streamlit secrets か環境変数（TURSO_DATABASE_URL / TURSO_AUTH_TOKEN）から取得。"""
    url = token = None
    try:
        if "turso_database_url" in st.secrets:
            url = str(st.secrets["turso_database_url"])
        if "turso_auth_token" in st.secrets:
            token = str(st.secrets["turso_auth_token"])
    except Exception:
        pass
    url = url or os.environ.get("TURSO_DATABASE_URL")
    token = token or os.environ.get("TURSO_AUTH_TOKEN")
    return (url, token) if (url and token) else (None, None)


def _turso_endpoint(url: str) -> str:
    """libsql:// 形式のURLを https://.../v2/pipeline のHTTPエンドポイントに変換"""
    u = url.strip()
    if u.startswith("libsql://"):
        u = "https://" + u[len("libsql://"):]
    elif u.startswith("wss://"):
        u = "https://" + u[len("wss://"):]
    elif u.startswith("ws://"):
        u = "http://" + u[len("ws://"):]
    return u.rstrip("/") + "/v2/pipeline"


class _TursoRow:
    """sqlite3.Row 互換の行（名前アクセス r["x"]・位置アクセス r[0]・dict(r) 対応）"""
    __slots__ = ("_cols", "_vals")

    def __init__(self, cols, vals):
        self._cols, self._vals = cols, vals

    def __getitem__(self, key):
        if isinstance(key, int):
            return self._vals[key]
        return self._vals[self._cols.index(key)]

    def keys(self):
        return list(self._cols)

    def __iter__(self):
        return iter(self._vals)

    def __len__(self):
        return len(self._vals)


class _TursoCursor:
    """execute結果。fetchone/fetchall と反復に対応。"""
    def __init__(self, cols, rows):
        self._rows = [_TursoRow(cols, v) for v in rows]
        self._i = 0
        self.description = [(c,) for c in cols] if cols else None

    def fetchone(self):
        if self._i < len(self._rows):
            r = self._rows[self._i]
            self._i += 1
            return r
        return None

    def fetchall(self):
        out = self._rows[self._i:]
        self._i = len(self._rows)
        return out

    def __iter__(self):
        return iter(self.fetchall())


class TursoConn:
    """Turso(libSQL)へHTTPで接続する簡易クライアント（自動コミット）。
    sqlite3.Connection と同じ感覚で execute / executescript / with が使える。"""

    def __init__(self, url, token):
        import requests  # 遅延import（ローカルSQLite利用時は読み込まない）
        self._requests = requests
        self._endpoint = _turso_endpoint(url)
        self._headers = {"Authorization": f"Bearer {token}",
                         "Content-Type": "application/json"}

    @staticmethod
    def _arg(v):
        """Python値 → Hrana引数"""
        if v is None:
            return {"type": "null"}
        if isinstance(v, bool):
            return {"type": "integer", "value": str(int(v))}
        if isinstance(v, int):
            return {"type": "integer", "value": str(v)}
        if isinstance(v, float):
            return {"type": "float", "value": v}
        if isinstance(v, bytes):
            return {"type": "blob", "base64": base64.b64encode(v).decode()}
        return {"type": "text", "value": str(v)}

    @staticmethod
    def _val(cell):
        """Hrana値 → Python値"""
        t = cell.get("type")
        if t == "null":
            return None
        raw = cell.get("value")
        if t == "integer":
            return int(raw)
        if t == "float":
            return float(raw)
        if t == "blob":
            return base64.b64decode(cell.get("base64", ""))
        return raw  # text

    def _pipeline(self, requests_list):
        resp = self._requests.post(
            self._endpoint, headers=self._headers,
            data=json.dumps({"requests": requests_list}), timeout=30)
        if resp.status_code >= 400:
            # サーバーの具体的な理由を読めるようにする
            raise RuntimeError(
                f"Turso HTTP {resp.status_code}: {resp.text[:400]}")
        data = resp.json()
        for res in data.get("results", []):
            if res.get("type") == "error":
                err = res.get("error", {})
                raise RuntimeError("Turso error: " + str(err.get("message", err)))
        return data

    def execute(self, sql, params=()):
        stmt = {"sql": sql}
        if params:
            stmt["args"] = [self._arg(p) for p in params]
        data = self._pipeline([{"type": "execute", "stmt": stmt},
                               {"type": "close"}])
        result = data["results"][0]["response"]["result"]
        cols = [c.get("name") for c in result.get("cols", [])]
        rows = [[self._val(cell) for cell in row] for row in result.get("rows", [])]
        return _TursoCursor(cols, rows)

    def executescript(self, script):
        # Tursoは1回のpipelineに複数SQLをまとめると400を返すことがあるため、
        # 1文ずつ確実に送る。
        stmts = [s.strip() for s in script.split(";") if s.strip()]
        for s in stmts:
            self._pipeline([{"type": "execute", "stmt": {"sql": s}},
                            {"type": "close"}])
        return _TursoCursor([], [])

    def commit(self):
        pass  # 1文ごとに自動コミット

    def rollback(self):
        pass

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


# ============================================================
# データベース接続
# ============================================================
def get_conn():
    """保存先への接続を返す。Turso設定があればTurso、無ければローカルSQLite。"""
    url, token = _turso_config()
    if url and token:
        return TursoConn(url, token)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def using_turso() -> bool:
    return _turso_config() != (None, None)


def init_db():
    """初回起動時にテーブルを作成し、初期データを入れる"""
    with get_conn() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS vehicles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                plate TEXT DEFAULT '',
                active INTEGER DEFAULT 1
            );
            CREATE TABLE IF NOT EXISTS reservations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                date TEXT NOT NULL,
                vehicle_id INTEGER NOT NULL,
                start_time TEXT NOT NULL,
                end_time TEXT NOT NULL,
                user_name TEXT NOT NULL,
                purpose TEXT DEFAULT '',
                transferable INTEGER DEFAULT 0,
                created_at TEXT DEFAULT (datetime('now', 'localtime'))
            );
            CREATE TABLE IF NOT EXISTS usage_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                date TEXT NOT NULL,
                vehicle_id INTEGER NOT NULL,
                driver TEXT NOT NULL,
                odo_start REAL,
                odo_end REAL,
                fuel_cost INTEGER DEFAULT 0,
                notes TEXT DEFAULT '',
                created_at TEXT DEFAULT (datetime('now', 'localtime'))
            );
            CREATE TABLE IF NOT EXISTS members (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                sort_order INTEGER DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT
            );
            CREATE TABLE IF NOT EXISTS day_overrides (
                date TEXT PRIMARY KEY,
                duty TEXT,
                event TEXT,
                note TEXT,
                duty_swap TEXT
            );
            """
        )
        # 既存DBに不足列があれば追加する（マイグレーション）
        cols = [r[1] for r in conn.execute("PRAGMA table_info(day_overrides)").fetchall()]
        if "note" not in cols:
            conn.execute("ALTER TABLE day_overrides ADD COLUMN note TEXT")
        if "duty_swap" not in cols:
            conn.execute("ALTER TABLE day_overrides ADD COLUMN duty_swap TEXT")
        # 予約テーブルに「譲れる」フラグ列を追加（既存DB対応）
        rcols = [r[1] for r in conn.execute("PRAGMA table_info(reservations)").fetchall()]
        if "transferable" not in rcols:
            conn.execute("ALTER TABLE reservations ADD COLUMN transferable INTEGER DEFAULT 0")
        # 設定の初期値（既にあれば上書きしない）
        defaults = {
            "weekly_event_name": "朝礼",           # 名前は設定画面で変更可能
            "rotation_base_date": "2026-01-05",    # 掃除当番ローテーションの起算日
            "weekly_event_rotation": "1",          # 朝礼を週替わり当番制にするか（0/1）
        }
        for k, v in defaults.items():
            conn.execute("INSERT OR IGNORE INTO settings(key, value) VALUES(?, ?)", (k, v))


def get_setting(key, default=""):
    with get_conn() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(key, value):
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO settings(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, str(value)),
        )


def get_members():
    """掃除当番メンバーを並び順で取得"""
    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM members ORDER BY sort_order, id").fetchall()
    return [dict(r) for r in rows]


def get_vehicles(active_only=True):
    sql = "SELECT * FROM vehicles"
    if active_only:
        sql += " WHERE active=1"
    with get_conn() as conn:
        rows = conn.execute(sql + " ORDER BY id").fetchall()
    return [dict(r) for r in rows]


# ============================================================
# 営業日・当番・月曜行事のロジック
# ============================================================
def _nth_weekday(d: datetime.date) -> int:
    """その日が月内で第何回目の同じ曜日か（第1なら1）"""
    return (d.day - 1) // 7 + 1


def _saturday_of_week_containing(target: datetime.date) -> datetime.date:
    """target を含む週（月曜始まり）の土曜日を返す"""
    return target + datetime.timedelta(days=(5 - target.weekday()))


def is_newyear_closed(d: datetime.date) -> bool:
    """1月1日〜5日は事務所休み"""
    return d.month == 1 and d.day <= 5


def is_working_saturday(d: datetime.date) -> bool:
    """
    土曜日が会社の出勤日かどうか（会社カレンダー規則）。d は土曜日である前提。
      1月・2月・5月 … 全ての土曜が出勤
      3月         … 20日を含む週の土曜のみ休み（他は出勤）
      4月         … 第2・第3土曜が休み（他は出勤）
      6〜10月      … 第1〜第3土曜が休み（第4・第5のみ出勤）
      11月・12月    … 第2・第3土曜が休み（他は出勤）
    """
    n = _nth_weekday(d)  # 第何土曜か
    m = d.month
    if m in (1, 2, 5):
        return True
    if m == 3:
        return d != _saturday_of_week_containing(datetime.date(d.year, 3, 20))
    if m == 4:
        return n not in (2, 3)
    if m in (6, 7, 8, 9, 10):
        return n not in (1, 2, 3)
    if m in (11, 12):
        return n not in (2, 3)
    return False


def is_workday(d: datetime.date) -> bool:
    """会社の出勤日かどうか（日曜・祝日・年始休み・休みの土曜を除く）"""
    if is_newyear_closed(d):
        return False
    if jpholiday.is_holiday(d):
        return False
    wd = d.weekday()
    if wd == 6:          # 日曜は休み
        return False
    if wd == 5:          # 土曜は会社規則で判定
        return is_working_saturday(d)
    return True          # 平日は出勤


def is_business_day(d: datetime.date) -> bool:
    """互換用の別名（掃除当番・朝礼の振替は会社の出勤日を基準にする）"""
    return is_workday(d)


def _esc(s) -> str:
    """HTML表示用に最小限のエスケープを行う（備考などの自由入力向け）"""
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def day_status_label(d: datetime.date) -> str:
    """その日の区分を文字で返す（出勤／休（理由））"""
    if is_newyear_closed(d):
        return "休（事務所休み）"
    hn = jpholiday.is_holiday_name(d)
    if hn:
        return f"休（{hn}）"
    if not is_workday(d):
        return "休"
    return "出勤"


# 掃除当番のローテーションから除外するメンバー（朝礼の輪番には影響しない）
DUTY_EXCLUDED_NAMES = {"門脇", "平岡"}


def build_duty_map(start: datetime.date, end: datetime.date):
    """
    期間内の各営業日に掃除当番を割り当てる。
    起算日から営業日を1日ずつ数え、メンバーを名簿順に回す（ローテーション）。

    掃除当番を「名簿のメンバー名」に直接編集した日は“アンカー”として扱い、
    その日のローテーション位置をそのメンバーに合わせ直す。
    これにより、その日以降の当番も編集後の人から名簿順で続く。

    なお DUTY_EXCLUDED_NAMES の人は掃除当番の輪番から外す（朝礼には影響しない）。
    戻り値: {date: メンバー名}
    """
    members = get_members()
    if not members:
        return {}
    # 掃除当番のローテーション対象（除外メンバーを抜く）
    duty_members = [m for m in members if m["name"] not in DUTY_EXCLUDED_NAMES]
    if not duty_members:           # 全員除外になってしまう場合の保険
        duty_members = members
    members = duty_members
    name_to_idx = {m["name"]: i for i, m in enumerate(members)}
    try:
        base = datetime.date.fromisoformat(get_setting("rotation_base_date", "2026-01-05"))
    except ValueError:
        base = datetime.date(2026, 1, 5)

    duty = {}
    # 起算日より前の表示には割り当てない
    cur = base
    idx = 0
    if cur > end:
        return {}
    # 起算日〜期間末までの当番上書き（アンカー判定に使う）
    overrides = get_day_overrides(base, end)
    while cur <= end:
        if is_business_day(cur):
            ov = overrides.get(cur, {}).get("duty")
            # 名簿に載っているメンバーへの変更は、その位置に順番を合わせ直す
            if ov is not None and ov in name_to_idx:
                idx = name_to_idx[ov]
            if cur >= start:
                duty[cur] = members[idx % len(members)]["name"]
            idx += 1
        cur += datetime.timedelta(days=1)
    return duty


def build_weekly_events(start: datetime.date, end: datetime.date):
    """
    毎週月曜日の朝礼を割り当てる。月曜が祝日の場合は翌出勤日にズレる。
    朝礼当番は名簿の番号順に1週間ごとに回る（週替わりローテーション）。

    朝礼当番を「名簿のメンバー名」に直接編集した週は“アンカー”として扱い、
    その週以降の当番も編集後の人から名簿順で続く。
    戻り値: {実施日: 担当者名（当番制でない場合は行事名ラベル）}
    """
    event_name = get_setting("weekly_event_name", "朝礼")
    use_rotation = get_setting("weekly_event_rotation", "1") == "1"
    members = get_members()
    name_to_idx = {m["name"]: i for i, m in enumerate(members)}
    try:
        base = datetime.date.fromisoformat(get_setting("rotation_base_date", "2026-01-05"))
    except ValueError:
        base = datetime.date(2026, 1, 5)

    # 起算日の週の月曜から、週単位でスキャンする（順番を通しで数えるため起算日から）
    base_monday = base - datetime.timedelta(days=base.weekday())
    overrides = get_day_overrides(base_monday, end) if (use_rotation and members) else {}

    events = {}
    cur = base_monday
    widx = 0  # 週ごとのローテーション位置
    while cur <= end:
        actual = cur
        # 祝日・休みなら翌出勤日へ順送り
        while not is_business_day(actual):
            actual += datetime.timedelta(days=1)
        if use_rotation and members:
            # 名簿メンバーへの変更があれば、その位置に週の順番を合わせ直す
            ov = overrides.get(actual, {}).get("event")
            if ov is not None and ov in name_to_idx:
                widx = name_to_idx[ov]
            person = members[widx % len(members)]["name"]
            widx += 1
            if start <= actual <= end:
                events[actual] = person
        else:
            if start <= actual <= end:
                label = event_name
                if actual != cur:
                    label += "（月曜振替）"
                events[actual] = label
        cur += datetime.timedelta(days=7)
    return events


def get_reservations_between(start: datetime.date, end: datetime.date):
    """期間内の予約を {date: [予約,...]} で返す"""
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT r.*, v.name AS vehicle_name FROM reservations r
               JOIN vehicles v ON v.id = r.vehicle_id
               WHERE r.date BETWEEN ? AND ? ORDER BY r.start_time""",
            (start.isoformat(), end.isoformat()),
        ).fetchall()
    result = {}
    for r in rows:
        d = datetime.date.fromisoformat(r["date"])
        result.setdefault(d, []).append(dict(r))
    return result


def get_day_overrides(start: datetime.date, end: datetime.date):
    """期間内の手入力上書き（掃除当番・朝礼・備考・入替）を
    {date: {'duty':.., 'event':.., 'note':.., 'duty_swap':..}} で返す。
    値が None のフィールドは『上書きなし（自動値を使う）』を意味する。"""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT date, duty, event, note, duty_swap FROM day_overrides "
            "WHERE date BETWEEN ? AND ?",
            (start.isoformat(), end.isoformat()),
        ).fetchall()
    return {datetime.date.fromisoformat(r["date"]):
            {"duty": r["duty"], "event": r["event"], "note": r["note"],
             "duty_swap": r["duty_swap"]}
            for r in rows}


def save_day_override(d: datetime.date, duty, event, note, duty_swap):
    """1日分の上書きを保存する。各フィールドが None なら自動値に戻す。
    すべて None なら行を削除する。"""
    with get_conn() as conn:
        if duty is None and event is None and note is None and duty_swap is None:
            conn.execute("DELETE FROM day_overrides WHERE date=?", (d.isoformat(),))
        else:
            conn.execute(
                "INSERT INTO day_overrides(date, duty, event, note, duty_swap) "
                "VALUES(?,?,?,?,?) "
                "ON CONFLICT(date) DO UPDATE SET duty=excluded.duty, "
                "event=excluded.event, note=excluded.note, duty_swap=excluded.duty_swap",
                (d.isoformat(), duty, event, note, duty_swap),
            )


# ============================================================
# 画面：カレンダー（月初〜月末の縦表示・クリックで直接編集）
# ============================================================
def page_calendar():
    st.subheader("📅 月間カレンダー（縦表示）")

    today = jst_today()
    col1, col2 = st.columns(2)
    year = col1.selectbox("年", range(today.year - 1, today.year + 3), index=1)
    month = col2.selectbox("月", range(1, 13), index=today.month - 1)

    first = datetime.date(year, month, 1)
    last = datetime.date(year, month, calendar.monthrange(year, month)[1])

    mobile = st.toggle(
        "📱 スマホ表示（1日ずつ大きく見やすく）", key="mobile_view",
        help="スマホなど画面が狭いときに、横スクロール不要の『1日ずつカード表示』に切り替えます。")

    duty = build_duty_map(first, last)            # 自動ローテーションの当番
    events = build_weekly_events(first, last)      # 自動の朝礼
    reservations = get_reservations_between(first, last)
    overrides = get_day_overrides(first, last)     # 手入力の上書き

    def eff_duty(d):
        """その日の掃除当番（上書きがあれば優先、なければ自動値）"""
        ov = overrides.get(d, {})
        return ov["duty"] if ov.get("duty") is not None else (duty.get(d) or "")

    def eff_event(d):
        """その日の行事（上書きがあれば優先、なければ自動値）"""
        ov = overrides.get(d, {})
        return ov["event"] if ov.get("event") is not None else (events.get(d) or "")

    def eff_note(d):
        """その日の備考（手入力のみ。自動値は無い）"""
        return overrides.get(d, {}).get("note") or ""

    def eff_swap(d):
        """その日の掃除当番の入替（交代）。入力があればその人が実際の当番。"""
        return overrides.get(d, {}).get("duty_swap") or ""

    def actual_duty(d):
        """実際に掃除をする人（入替があれば入替後、なければ予定の当番）"""
        return eff_swap(d) or eff_duty(d)

    # カレンダーの備考セルをクリックしたとき、その日の備考入力欄を開く
    en_str = st.session_state.get("editnote_date")
    if en_str:
        try:
            en_date = datetime.date.fromisoformat(en_str)
        except ValueError:
            en_date = None
        if en_date and first <= en_date <= last:
            with st.container(border=True):
                st.markdown(
                    f"**📝 {en_date.month}/{en_date.day}"
                    f"（{WEEKDAY_JP[en_date.weekday()]}）の備考を入力**")
                new_note = st.text_input("備考", value=eff_note(en_date),
                                         key=f"quicknote_input_{en_str}")
                qc1, qc2, _ = st.columns([1, 1, 4])
                if qc1.button("💾 保存", type="primary", key="quicknote_save"):
                    cu = overrides.get(en_date, {})
                    save_day_override(en_date, cu.get("duty"), cu.get("event"),
                                      (new_note.strip() or None), cu.get("duty_swap"))
                    del st.session_state["editnote_date"]
                    st.rerun()
                if qc2.button("閉じる", key="quicknote_close"):
                    del st.session_state["editnote_date"]
                    st.rerun()
        else:
            # 表示中の月以外なら開きっぱなしにしない
            del st.session_state["editnote_date"]

    # カレンダーの入替セルをクリックしたとき、その日の入替入力欄を開く
    es_str = st.session_state.get("editswap_date")
    if es_str:
        try:
            es_date = datetime.date.fromisoformat(es_str)
        except ValueError:
            es_date = None
        if es_date and first <= es_date <= last:
            with st.container(border=True):
                st.markdown(
                    f"**🔁 {es_date.month}/{es_date.day}"
                    f"（{WEEKDAY_JP[es_date.weekday()]}）の掃除当番を交代（入替）**")
                st.caption(f"予定の当番：{eff_duty(es_date) or '（なし）'}　→　交代後の人を選んでください。"
                           "（その日だけの差し替え。ローテーション順は変わりません）")
                cur_swap = eff_swap(es_date)
                member_names = [m["name"] for m in get_members()]
                opts = ["（入替なし）"] + member_names
                if cur_swap and cur_swap not in member_names:
                    opts.insert(1, cur_swap)
                sw_idx = opts.index(cur_swap) if cur_swap in opts else 0
                new_swap = st.selectbox("交代後の人", opts, index=sw_idx,
                                        key=f"quickswap_input_{es_str}")
                sc1, sc2, _ = st.columns([1, 1, 4])
                if sc1.button("💾 保存", type="primary", key="quickswap_save"):
                    cu = overrides.get(es_date, {})
                    val = None if new_swap == "（入替なし）" else new_swap
                    save_day_override(es_date, cu.get("duty"), cu.get("event"),
                                      cu.get("note"), val)
                    del st.session_state["editswap_date"]
                    st.rerun()
                if sc2.button("閉じる", key="quickswap_close"):
                    del st.session_state["editswap_date"]
                    st.rerun()
        else:
            del st.session_state["editswap_date"]

    if not get_members():
        st.warning("掃除当番メンバーが未登録です。「⚙️ 設定」画面で登録してください。")

    # ------------------------------------------------------------
    # スマホ向け表示：1日ずつのカード（横スクロール不要）
    # ------------------------------------------------------------
    if mobile:
        cards = """
        <style>
          .daycard {border:1px solid #ccc; border-radius:10px; margin:10px 0;
                    padding:10px 12px; background:#fff;}
          .daycard.today {background:#fffbe0; border-color:#e0c000; border-width:2px;}
          .daycard.hol {background:#fdeeee;}
          .daycard.sat {background:#eef5ff;}
          .dc-date {font-size:21px; font-weight:800;}
          .dc-date .badge {font-size:13px; border-radius:4px; padding:1px 6px;
                           color:#fff; margin-left:6px;}
          .dc-date .badge.work {background:#2e7d32;} .dc-date .badge.off {background:#c62828;}
          .dc-row {font-size:17px; margin-top:5px; line-height:1.5;}
          .dc-label {display:inline-block; min-width:5.2em; color:#555; font-weight:bold;}
          .dc-row a {color:#06c; text-decoration:none;}
          .dc-duty {background:#e8f5e9; border-radius:4px; padding:1px 6px; font-weight:bold;}
          .dc-swap {background:#ffe0b2; border-radius:4px; padding:1px 6px; font-weight:bold;
                    color:#e65100;}
          .dc-event {background:#fff3e0; border-radius:4px; padding:1px 6px; font-weight:bold;}
        </style>
        """
        d = first
        while d <= last:
            wd = d.weekday()
            workday = is_workday(d)
            newyear = is_newyear_closed(d)
            hname = jpholiday.is_holiday_name(d)
            cls = []
            if not workday:
                cls.append("hol")
            elif wd == 5:
                cls.append("sat")
            if d == today:
                cls.append("today")
            iso = d.isoformat()

            if newyear:
                badge = '<span class="badge off">事務所休み</span>'
            elif hname:
                badge = f'<span class="badge off">{hname}</span>'
            elif workday:
                badge = '<span class="badge work">出勤</span>'
            else:
                badge = '<span class="badge off">休</span>'
            head = f'<div class="dc-date">{d.month}/{d.day}（{WEEKDAY_JP[wd]}）{badge}</div>'

            # 予約
            day_resv = reservations.get(d, [])
            if day_resv:
                items = []
                for r in day_resv:
                    t = f'{r["start_time"]}-{r["end_time"]} {_esc(r["user_name"])}'
                    if r["purpose"]:
                        t += f'（{_esc(r["purpose"])}）'
                    if r.get("transferable"):
                        t = (f'<span style="color:#d00; font-weight:bold;">🔁{t}'
                             f'（譲れます）</span>')
                    items.append(t)
                resv_html = ("<br>".join(items)
                             + f' <a href="?nav=resv&date={iso}" target="_self">［予約変更］</a>')
            else:
                resv_html = f'<a href="?nav=resv&date={iso}" target="_self">＋ 予約する</a>'

            ed, ee, en, es = eff_duty(d), eff_event(d), eff_note(d), eff_swap(d)
            duty_html = f'<span class="dc-duty">{_esc(ed)}</span>' if ed else "—"
            if es:
                duty_html += f' → <span class="dc-swap">🔁{_esc(es)}</span>'
            duty_html += f' <a href="?nav=swap&date={iso}" target="_self">［入替］</a>'
            event_html = f'<span class="dc-event">{_esc(ee)}</span>' if ee else "—"
            note_html = (f'{_esc(en)} ' if en else "")
            note_html += f'<a href="?nav=note&date={iso}" target="_self">［{"編集" if en else "＋ 入力"}］</a>'

            cards += (
                f'<div class="daycard {" ".join(cls)}">{head}'
                f'<div class="dc-row"><span class="dc-label">🚗 予約</span>{resv_html}</div>'
                f'<div class="dc-row"><span class="dc-label">🧹 当番</span>{duty_html}</div>'
                f'<div class="dc-row"><span class="dc-label">📌 朝礼</span>{event_html}</div>'
                f'<div class="dc-row"><span class="dc-label">📝 備考</span>{note_html}</div>'
                f'</div>')
            d += datetime.timedelta(days=1)
        st.markdown(cards, unsafe_allow_html=True)
        st.caption("🚗予約・🔁入替・📝備考の各リンクをタップすると、その場で入力・変更できます。"
                   "通常の一覧表に戻すには、上の「📱 スマホ表示」をオフにしてください。")
        return

    def status_text(d):
        """その日の区分（出勤／休）を絵文字付きで表す"""
        lbl = day_status_label(d)
        return ("🟢 " if lbl == "出勤" else "🔴 ") + lbl

    # ------------------------------------------------------------
    # 車両予約のガントチャート表示（9:00〜17:30）＋当番の一覧（読み取り専用）
    # ------------------------------------------------------------
    day_start_min, day_end_min = 9 * 60, 17 * 60 + 30
    total_min = day_end_min - day_start_min

    def to_min(s):
        h, m = s.split(":")
        return int(h) * 60 + int(m)

    # 車両ごとに色分けする
    palette = ["#64b5f6", "#81c784", "#ffb74d", "#ba68c8", "#4db6ac", "#f06292"]
    all_vehicles = get_vehicles(active_only=False)
    vehicle_color = {v["id"]: palette[i % len(palette)] for i, v in enumerate(all_vehicles)}

    # 予約列の上に表示する時間目盛り（30分刻み。正時は時刻、30分は小さく表示）
    scale = '<div class="tl-scale">'
    t = day_start_min
    while t <= day_end_min:
        left = (t - day_start_min) / total_min * 100
        hh, mm = divmod(t, 60)
        if mm == 0:
            scale += f'<span style="left:{left:.2f}%;">{hh}時</span>'
        else:
            scale += f'<span class="half" style="left:{left:.2f}%;">30</span>'
        t += 30
    scale += "</div>"

    html = f"""
    <style>
      table.vcal {{width:100%; border-collapse:collapse;}}
      table.vcal th {{border:1px solid #ccc; padding:5px; background:#f0f2f6; font-size:15px;}}
      table.vcal td {{border:1px solid #ccc; padding:7px 6px; font-size:15px; vertical-align:top;}}
      table.vcal td.dcell {{white-space:nowrap; line-height:1.7;}}
      tr.sat td {{background:#eef5ff;}}
      tr.hol td {{background:#fdeeee;}}
      tr.today td {{background:#fffbe0;}}
      .red {{color:#d00; font-weight:bold;}}
      .blue {{color:#06c; font-weight:bold;}}
      .todaydate {{font-size:19px; font-weight:900; color:#000;}}
      a.resvlink {{display:block; min-height:22px; text-decoration:none; color:inherit; cursor:pointer;}}
      a.resvlink:hover {{outline:2px solid #4caf50;}}
      a.notelink {{display:block; min-height:22px; text-decoration:none; color:inherit; cursor:pointer;}}
      a.notelink:hover {{background:#fff8e1;}}
      .noteempty {{color:#bbb; font-size:13px;}}
      .duty {{background:#e8f5e9; border-radius:4px; padding:1px 6px;
              font-size:20px; font-weight:bold;}}
      .event {{background:#fff3e0; border-radius:4px; padding:1px 6px;
               font-size:20px; font-weight:bold;}}
      .badge {{font-size:12px; border-radius:3px; padding:0 4px; color:#fff; margin-left:3px;}}
      .badge.work {{background:#2e7d32;}}
      .badge.off {{background:#c62828;}}
      .tl-scale {{position:relative; height:16px; font-size:12px; font-weight:normal; margin-top:3px;}}
      .tl-scale span {{position:absolute; transform:translateX(-50%);}}
      .tl-scale span.half {{font-size:9px; color:#999;}}
      .tl {{position:relative;
            background:repeating-linear-gradient(to right,
                       #bbb 0 1px, transparent 1px 5.8824%);}}
      .tl-bar {{position:absolute; height:26px; border-radius:3px; color:#fff;
               font-size:18px; line-height:26px; padding:0 4px; overflow:hidden;
               white-space:nowrap;}}
    </style>
    <table class="vcal">
      <tr><th style="width:132px;">日付</th>
          <th>🚗 車両予約（9:00〜17:30）{scale}</th>
          <th style="width:92px;">🧹 掃除当番</th>
          <th style="width:92px;">🔁 入替</th>
          <th style="width:110px;">📌 朝礼当番</th>
          <th style="width:150px;">📝 備考</th></tr>
    """
    d = first
    while d <= last:
        wd = d.weekday()
        holiday_name = jpholiday.is_holiday_name(d)
        workday = is_workday(d)
        newyear = is_newyear_closed(d)

        row_cls = []
        if not workday:
            row_cls.append("hol")
        elif wd == 5:
            row_cls.append("sat")
        if d == today:
            row_cls.append("today")

        date_cls = "blue" if wd == 5 else ("red" if not workday else "")
        if d == today:
            date_cls = (date_cls + " todaydate").strip()
        date_label = f'<span class="{date_cls}">{d.month}/{d.day}（{WEEKDAY_JP[wd]}）</span>'
        if newyear:
            date_label += '<span class="badge off">事務所休み</span>'
        elif holiday_name:
            date_label += f'<br><span class="red" style="font-size:11px;">{holiday_name}</span>'
        elif wd == 5:
            date_label += ('<span class="badge work">出勤</span>' if workday
                           else '<span class="badge off">休</span>')
        elif wd == 6:
            date_label += '<span class="badge off">休</span>'

        # 予約をタイムラインのバーとして描画（時間帯の位置に合わせる）
        day_resv = reservations.get(d, [])
        bars = ""
        for j, r in enumerate(day_resv):
            s = max(to_min(r["start_time"]), day_start_min)
            e = min(to_min(r["end_time"]), day_end_min)
            if e <= s:
                continue
            left = (s - day_start_min) / total_min * 100
            width = (e - s) / total_min * 100
            yieldable = bool(r.get("transferable"))
            # 「譲れる」予約は赤色で目立たせる
            color = "#e53935" if yieldable else vehicle_color.get(r["vehicle_id"], "#64b5f6")
            label_txt = ("🔁" if yieldable else "") + f'{r["start_time"]}-{r["end_time"]} {r["user_name"]}'
            if r["purpose"]:
                label_txt += f'（{r["purpose"]}）'
            title_txt = (f'{r["vehicle_name"]} {r["start_time"]}-{r["end_time"]} '
                         f'{r["user_name"]} {r["purpose"]}'
                         + ("（他の人に譲れます）" if yieldable else ""))
            bars += (f'<div class="tl-bar" style="left:{left:.2f}%; width:{width:.2f}%; '
                     f'top:{j * 28 + 2}px; background:{color};" '
                     f'title="{title_txt}">'
                     f'{label_txt}</div>')
        height = max(len(day_resv) * 28 + 4, 30)
        iso = d.isoformat()
        # 時間帯セル＝クリックで車両予約画面へ移動するリンク
        resv_cell = (f'<a class="resvlink" href="?nav=resv&date={iso}" target="_self" '
                     f'title="クリックで予約画面へ">'
                     f'<div class="tl" style="height:{height}px;">{bars}</div></a>')

        ed, ee, en, es = eff_duty(d), eff_event(d), eff_note(d), eff_swap(d)
        duty_cell = f'<span class="duty">{ed}</span>' if ed else ""
        # 入替セル＝クリックでその日の入替入力欄を開くリンク
        swap_disp = (f'<span class="duty" style="background:#ffe0b2;">{_esc(es)}</span>'
                     if es else '<span class="noteempty">＋ 入力</span>')
        swap_cell = (f'<a class="notelink" href="?nav=swap&date={iso}" target="_self" '
                     f'title="クリックで入替を入力">{swap_disp}</a>')
        event_cell = f'<span class="event">{ee}</span>' if ee else ""
        # 備考セル＝クリックでその日の備考入力欄を開くリンク
        note_inner = _esc(en) if en else '<span class="noteempty">＋ 入力</span>'
        note_cell = (f'<a class="notelink" href="?nav=note&date={iso}" target="_self" '
                     f'title="クリックで備考を入力">{note_inner}</a>')
        html += (f'<tr class="{" ".join(row_cls)}"><td class="dcell">{date_label}</td>'
                 f'<td>{resv_cell}</td><td>{duty_cell}</td><td>{swap_cell}</td>'
                 f'<td>{event_cell}</td><td>{note_cell}</td></tr>')
        d += datetime.timedelta(days=1)
    html += "</table>"
    st.markdown(html, unsafe_allow_html=True)

    if all_vehicles:
        legend = "　".join(
            f'<span style="background:{vehicle_color[v["id"]]}; color:#fff; '
            f'border-radius:3px; padding:1px 6px; font-size:12px;">{v["name"]}</span>'
            for v in all_vehicles)
        st.markdown(legend, unsafe_allow_html=True)
    st.caption("🟢出勤＝会社の出勤日　🔴休＝休み（日曜・祝日・年始・休みの土曜）　"
               "🚗＝車両予約（バーにマウスを乗せると詳細表示／🔁赤色＝他の人に譲れる予約）。"
               "👉 **時間帯の列をクリック**すると、その日の車両予約画面へ移動します。"
               "👉 **入替の列をクリック**すると、その日の掃除当番の交代を入力できます。"
               "👉 **備考の列をクリック**すると、その日の備考をその場で入力できます。")

    # ------------------------------------------------------------
    # 掃除当番・朝礼当番の直接編集
    # ------------------------------------------------------------
    st.divider()
    st.markdown("#### ✏️ 掃除当番・入替・朝礼当番・備考を直接編集")
    st.caption("各マスをダブルクリックすると書き換えられます。"
               "「掃除当番」を名簿のメンバー名に変えると、その日（その週）以降も名簿順で続きます。"
               "「🔁入替」は当番を交代したい日に交代後の人を入力します（その日だけ差し替え／"
               "ローテーション自体は変わりません）。"
               "空欄にすると自動の割り当てに戻ります（備考・入替は空欄で消去）。"
               "編集後は「💾 保存」を押してください。")

    rows_data = []
    d = first
    while d <= last:
        rows_data.append({
            "date": d.isoformat(),
            "日付": f"{d.month}/{d.day}（{WEEKDAY_JP[d.weekday()]}）",
            "区分": status_text(d),
            "🧹 掃除当番": eff_duty(d),
            "🔁 入替": eff_swap(d),
            "📌 朝礼当番": eff_event(d),
            "📝 備考": eff_note(d),
        })
        d += datetime.timedelta(days=1)
    cal_df = pd.DataFrame(rows_data)

    # 入替の選択肢＝名簿（番号順）。名簿外の名前も手入力できるよう自由入力可にする
    swap_help = "当番を交代する日に、交代後の人を入力（名簿の人を選ぶと確実です）"
    edited_cal = st.data_editor(
        cal_df, key="cal_editor", num_rows="fixed", hide_index=True,
        width="stretch", height=min(36 * (len(cal_df) + 1) + 3, 1200),
        column_config={
            "date": None,
            "日付": st.column_config.TextColumn("日付", disabled=True),
            "区分": st.column_config.TextColumn("区分", disabled=True),
            "🧹 掃除当番": st.column_config.TextColumn("🧹 掃除当番"),
            "🔁 入替": st.column_config.TextColumn("🔁 入替", help=swap_help),
            "📌 朝礼当番": st.column_config.TextColumn("📌 朝礼当番"),
            "📝 備考": st.column_config.TextColumn("📝 備考", width="medium"),
        },
    )

    if st.button("💾 保存", type="primary", key="cal_save"):
        try:
            stored = get_day_overrides(first, last)  # 現在の上書き（生の値）
            for i in range(len(edited_cal)):
                row, orig = edited_cal.iloc[i], cal_df.iloc[i]
                d = datetime.date.fromisoformat(row["date"])
                new_d = _cell_str(row["🧹 掃除当番"])
                new_s = _cell_str(row["🔁 入替"])
                new_e = _cell_str(row["📌 朝礼当番"])
                new_n = _cell_str(row["📝 備考"])
                duty_changed = new_d != _cell_str(orig["🧹 掃除当番"])
                swap_changed = new_s != _cell_str(orig["🔁 入替"])
                event_changed = new_e != _cell_str(orig["📌 朝礼当番"])
                note_changed = new_n != _cell_str(orig["📝 備考"])
                if not (duty_changed or swap_changed or event_changed or note_changed):
                    continue
                cur = stored.get(d, {"duty": None, "event": None,
                                     "note": None, "duty_swap": None})
                duty_ov, event_ov = cur["duty"], cur["event"]
                note_ov, swap_ov = cur["note"], cur["duty_swap"]
                if duty_changed:
                    duty_ov = None if new_d == "" else new_d
                if swap_changed:
                    swap_ov = None if new_s == "" else new_s
                if event_changed:
                    event_ov = None if new_e == "" else new_e
                if note_changed:
                    note_ov = None if new_n == "" else new_n
                save_day_override(d, duty_ov, event_ov, note_ov, swap_ov)
            st.success("カレンダーの変更を保存しました。")
            st.rerun()
        except Exception as e:
            st.error(f"保存できませんでした: {e}")


# ============================================================
# 画面：車両予約（タイムスケジュール方式 9:00〜17:30）
# ============================================================
# 予約可能な時刻（9:00〜17:30 の30分刻み）
TIME_SLOTS = []
_t = datetime.datetime(2000, 1, 1, 9, 0)
while _t <= datetime.datetime(2000, 1, 1, 17, 30):
    TIME_SLOTS.append(_t.strftime("%H:%M"))
    _t += datetime.timedelta(minutes=30)


def render_schedule_grid(date: datetime.date, vehicles: list,
                         preview_vehicle_id=None, preview_range=None, exclude_id=None):
    """
    選択日の車両別タイムスケジュール表（30分刻み）をHTMLで描画する。
    preview_range=(開始, 終了) を渡すと、その車両の選択中の時間帯を色で表示する
    （空きなら緑、既存予約と重なるなら赤で警告）。
    exclude_id を渡すと、その予約は「予約済み」表示から除外する（修正中の予約用）。
    """
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT r.*, v.name AS vehicle_name FROM reservations r
               JOIN vehicles v ON v.id=r.vehicle_id
               WHERE r.date=? ORDER BY r.start_time""",
            (date.isoformat(),),
        ).fetchall()
    resv_by_vehicle = {}
    for r in rows:
        if exclude_id is not None and r["id"] == exclude_id:
            continue
        resv_by_vehicle.setdefault(r["vehicle_id"], []).append(dict(r))

    pv_start, pv_end = (preview_range or (None, None))

    slot_starts = TIME_SLOTS[:-1]  # 各セルの開始時刻（9:00〜17:00）
    html = """
    <style>
      table.sched {border-collapse:collapse; width:100%; table-layout:fixed;}
      table.sched th {border:1px solid #bbb; background:#f0f2f6; font-size:13px; padding:2px 0;}
      table.sched td {border:1px solid #bbb; height:32px; padding:0; text-align:center;
                      font-size:13px; overflow:hidden; white-space:nowrap;}
      table.sched td.veh {background:#f0f2f6; font-weight:bold; font-size:14px; padding:0 4px;
                          text-align:left; white-space:normal;}
      td.booked {background:#64b5f6; color:#fff;}
      td.yield {background:#e53935; color:#fff;}
      td.select {background:#a5d6a7;}
      td.conflict {background:#ef9a9a;}
      td.free {background:#fff;}
    </style>
    <table class="sched"><tr><th style="width:120px;">車両</th>"""
    for s in slot_starts:
        # 正時のみラベル表示（30分セルは空欄でスッキリさせる）
        label = s if s.endswith(":00") else ""
        html += f"<th>{label}</th>"
    html += "</tr>"

    for v in vehicles:
        html += f'<tr><td class="veh">{v["name"]}</td>'
        for i, s in enumerate(slot_starts):
            slot_end = TIME_SLOTS[i + 1]
            hit = None
            for r in resv_by_vehicle.get(v["id"], []):
                # このセル(30分)と予約時間帯が重なるか
                if r["start_time"] < slot_end and r["end_time"] > s:
                    hit = r
                    break
            # 選択中の時間帯か（プレビュー対象の車両のみ）
            selected = (preview_range and v["id"] == preview_vehicle_id
                        and pv_start <= s and slot_end <= pv_end)
            if hit and selected:
                html += (f'<td class="conflict" title="重複：{hit["start_time"]}-'
                         f'{hit["end_time"]} {hit["user_name"]}">×</td>')
            elif hit:
                text = hit["user_name"] if hit["start_time"] == s or i == 0 else ""
                cls = "yield" if hit.get("transferable") else "booked"
                ymark = "（譲れます）" if hit.get("transferable") else ""
                html += (f'<td class="{cls}" '
                         f'title="{hit["start_time"]}-{hit["end_time"]} '
                         f'{hit["user_name"]} {hit["purpose"]}{ymark}">{text}</td>')
            elif selected:
                html += '<td class="select"></td>'
            else:
                html += '<td class="free"></td>'
        html += "</tr>"
    html += "</table>"
    st.markdown(html, unsafe_allow_html=True)
    if rows:
        parts = []
        for r in rows:
            txt = (f'🚗 {_esc(r["vehicle_name"])} {r["start_time"]}-{r["end_time"]} '
                   f'{_esc(r["user_name"])} {_esc(r["purpose"])}')
            if r["transferable"]:
                parts.append(f'<span style="color:#d00; font-weight:bold;">{txt}'
                             f'（🔁譲れます）</span>')
            else:
                parts.append(txt)
        st.markdown('<div style="font-size:13px; color:#666;">'
                    + "　".join(parts) + "</div>", unsafe_allow_html=True)


def page_reservation():
    st.subheader("🚗 社用車の予約（タイムスケジュール 9:00〜17:30）")

    vehicles = get_vehicles()
    if not vehicles:
        st.warning("車両が未登録です。「🚙 車両管理」画面で先に登録してください。")
        return

    st.markdown("#### 予約の追加・修正：時間帯はスライダーで調整")
    c1, c2 = st.columns(2)
    sel_date = c1.date_input("利用日", jst_today(), key="sched_date")
    vehicle = c2.selectbox("車両", vehicles, format_func=lambda v: f"{v['name']} {v['plate']}")
    st.caption(f"{sel_date}（{WEEKDAY_JP[sel_date.weekday()]}）　区分：{day_status_label(sel_date)}")

    # その日・その車両の既存予約（修正対象の選択に使う）
    with get_conn() as conn:
        day_rows = conn.execute(
            """SELECT id, start_time, end_time, user_name, purpose, transferable
               FROM reservations
               WHERE date=? AND vehicle_id=? ORDER BY start_time""",
            (sel_date.isoformat(), vehicle["id"]),
        ).fetchall()

    # 操作対象：新規 or 既存予約（既存を選ぶとスライダーがその時間に入り、バーで訂正できる）
    options = [("new", "＋ 新規予約")] + [
        (r["id"], f'{r["start_time"]}-{r["end_time"]}　{r["user_name"]}') for r in day_rows]
    target = st.selectbox("予約操作対象", options, format_func=lambda o: o[1], key="resv_target")
    target_id = target[0]
    editing = target_id != "new"

    if editing:
        cur = next(r for r in day_rows if r["id"] == target_id)
        def_range = (cur["start_time"], cur["end_time"])
        def_name, def_purpose = cur["user_name"], cur["purpose"]
        def_yield = bool(cur["transferable"])
    else:
        def_range, def_name, def_purpose = ("09:00", "12:00"), "", ""
        def_yield = False

    # 対象を切り替えると初期値が入るよう、ウィジェットのkeyに対象IDを含める
    start_s, end_s = st.select_slider(
        "予約する時間帯（両端をドラッグ）", options=TIME_SLOTS,
        value=def_range, key=f"resv_range_{vehicle['id']}_{target_id}")
    c3, c4 = st.columns(2)
    # 利用者名は掃除当番の名簿（番号順）から選ぶ。名簿外の既存名も選べるよう先頭に残す
    user_opts = [m["name"] for m in get_members()]
    if def_name and def_name not in user_opts:
        user_opts = [def_name] + user_opts
    if user_opts:
        user_idx = user_opts.index(def_name) if def_name in user_opts else None
        user_name = c3.selectbox("利用者名 *", user_opts, index=user_idx,
                                 placeholder="名簿から選択", key=f"resv_user_{target_id}")
    else:
        user_name = c3.text_input("利用者名 *", value=def_name, key=f"resv_user_{target_id}")
    user_name = (user_name or "").strip()
    purpose = c4.text_input("行き先・目的", value=def_purpose, key=f"resv_purpose_{target_id}")
    allow_yield = st.checkbox(
        "🔁 この時間帯は他の人に譲れます（カレンダーに赤字で表示）",
        value=def_yield, key=f"resv_yield_{target_id}")

    # 選択中の時間帯をタイムライン上にリアルタイム表示（修正中の予約は除外して判定）
    render_schedule_grid(sel_date, vehicles, preview_vehicle_id=vehicle["id"],
                         preview_range=(start_s, end_s),
                         exclude_id=(target_id if editing else None))
    st.caption("青＝予約済み　🔴赤＝他の人に譲れる予約　🟩緑＝選択中の時間帯　🟥赤×＝重複（取れません）")

    if start_s >= end_s:
        st.warning("終了は開始より後になるよう、スライダーの右端を動かしてください。")
    else:
        if editing:
            cc1, cc2 = st.columns(2)
            do_save = cc1.button(f"この内容に更新（{start_s}〜{end_s}）", type="primary", key="resv_update")
            do_delete = cc2.button("🗑 この予約を取消", key="resv_delete")
        else:
            do_save = st.button(f"この時間で予約する（{start_s}〜{end_s}）", type="primary", key="resv_book")
            do_delete = False

        if do_delete:
            with get_conn() as conn:
                conn.execute("DELETE FROM reservations WHERE id=?", (target_id,))
            st.success("予約を取消しました。")
            st.rerun()
        elif do_save:
            if not user_name:
                st.error("利用者名を選択してください。")
            else:
                # 重複チェックと保存（st.rerun() は with の外で。中だとロールバックされる）
                exclude = target_id if editing else None
                with get_conn() as conn:
                    dup = conn.execute(
                        """SELECT start_time, end_time, user_name FROM reservations
                           WHERE vehicle_id=? AND date=? AND id IS NOT ?
                                 AND NOT (end_time<=? OR start_time>=?)""",
                        (vehicle["id"], sel_date.isoformat(), exclude, start_s, end_s),
                    ).fetchone()
                    if not dup:
                        if editing:
                            conn.execute(
                                """UPDATE reservations SET start_time=?, end_time=?,
                                   user_name=?, purpose=?, transferable=? WHERE id=?""",
                                (start_s, end_s, user_name, purpose.strip(),
                                 int(allow_yield), target_id))
                        else:
                            conn.execute(
                                """INSERT INTO reservations(date, vehicle_id, start_time,
                                   end_time, user_name, purpose, transferable)
                                   VALUES(?,?,?,?,?,?,?)""",
                                (sel_date.isoformat(), vehicle["id"], start_s, end_s,
                                 user_name, purpose.strip(), int(allow_yield)))
                if dup:
                    st.error(f"この時間帯は既に予約があります"
                             f"（{dup['start_time']}-{dup['end_time']} {dup['user_name']}）。"
                             "スライダーで別の時間帯を選んでください。")
                else:
                    st.success(("更新しました：" if editing else "予約しました：")
                               + f"{sel_date} {start_s}-{end_s} {vehicle['name']}（{user_name}）")
                    st.rerun()

    # 今後の予約一覧（クリックで直接編集）
    st.markdown("#### 今後の予約一覧（クリックで直接編集）")
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT r.id, r.date, r.vehicle_id, r.start_time, r.end_time,
                      r.user_name, r.purpose, r.transferable FROM reservations r
               WHERE r.date >= ? ORDER BY r.date, r.start_time""",
            (jst_today().isoformat(),),
        ).fetchall()

    if not rows:
        st.info("今後の予約はありません。上のフォームか、この表の最終行から追加できます。")

    vmap_id2name = {v["id"]: v["name"] for v in vehicles}
    vmap_name2id = {v["name"]: v["id"] for v in vehicles}
    vehicle_names = [v["name"] for v in vehicles]

    list_cols = ["id", "利用日", "区分", "車両", "開始", "終了", "利用者名", "行き先", "🔁譲れる"]
    rdf_rows = []
    for r in rows:
        rd = datetime.date.fromisoformat(r["date"])
        rdf_rows.append({
            "id": r["id"],
            "利用日": rd,
            "区分": f"{WEEKDAY_JP[rd.weekday()]}・{day_status_label(rd)}",
            "車両": vmap_id2name.get(r["vehicle_id"], ""),
            "開始": r["start_time"],
            "終了": r["end_time"],
            "利用者名": r["user_name"],
            "行き先": r["purpose"],
            "🔁譲れる": bool(r["transferable"]),
        })
    rdf = pd.DataFrame(rdf_rows) if rdf_rows else pd.DataFrame(columns=list_cols)

    # 利用者名の選択肢＝掃除当番の名簿（番号順）＋既存予約の名簿外の名前
    user_options = [m["name"] for m in get_members()]
    for r in rows:
        if r["user_name"] and r["user_name"] not in user_options:
            user_options.append(r["user_name"])

    st.caption("マスをクリックして直接編集できます。最終行で追加、行頭チェックで削除。"
               "編集後は「💾 保存」を押してください。（区分は利用日から自動表示）")
    edited = st.data_editor(
        rdf, key="resv_editor", num_rows="dynamic", hide_index=True,
        width="stretch",
        column_config={
            "id": None,
            "利用日": st.column_config.DateColumn("利用日", format="YYYY-MM-DD", required=True),
            "区分": st.column_config.TextColumn("区分", disabled=True),
            "車両": st.column_config.SelectboxColumn("車両", options=vehicle_names, required=True),
            "開始": st.column_config.SelectboxColumn("開始", options=TIME_SLOTS[:-1], required=True),
            "終了": st.column_config.SelectboxColumn("終了", options=TIME_SLOTS[1:], required=True),
            "利用者名": st.column_config.SelectboxColumn("利用者名", options=user_options, required=True),
            "行き先": st.column_config.TextColumn("行き先"),
            "🔁譲れる": st.column_config.CheckboxColumn(
                "🔁譲れる", help="チェックすると、その予約はカレンダーに赤字で表示されます"),
        },
    )

    if st.button("💾 保存", type="primary", key="resv_save"):
        try:
            orig_ids = {int(i) for i in rdf["id"].dropna().tolist()}
            seen = set()
            records = []  # 重複チェック用 (vehicle_id, date, start, end)
            for _, row in edited.iterrows():
                user = _cell_str(row["利用者名"])
                vname = _cell_str(row["車両"])
                if not user or not vname or pd.isna(row["利用日"]):
                    continue  # 必須項目が空の行は無視
                vid = vmap_name2id.get(vname)
                if vid is None:
                    raise ValueError(f"車両「{vname}」が見つかりません。")
                start_s, end_s = _cell_str(row["開始"]), _cell_str(row["終了"])
                if start_s >= end_s:
                    raise ValueError(f"{user}さんの予約：終了時刻は開始時刻より後にしてください。")
                d = pd.to_datetime(row["利用日"]).date().isoformat()
                rid = int(row["id"]) if pd.notna(row["id"]) else None
                yv = row.get("🔁譲れる")
                yld = 1 if (pd.notna(yv) and bool(yv)) else 0
                records.append((rid, d, vid, start_s, end_s, user,
                                _cell_str(row["行き先"]), yld))

            # 同じ車両・同じ日の時間帯重複チェック
            for i in range(len(records)):
                for j in range(i + 1, len(records)):
                    a, b = records[i], records[j]
                    if a[2] == b[2] and a[1] == b[1] and not (a[4] <= b[3] or a[3] >= b[4]):
                        raise ValueError(
                            f"予約が重複しています：{a[1]} {vmap_id2name.get(a[2])} "
                            f"{a[3]}-{a[4]} と {b[3]}-{b[4]}")

            with get_conn() as conn:
                for rid, d, vid, start_s, end_s, user, purpose, yld in records:
                    if rid is not None:
                        seen.add(rid)
                        conn.execute(
                            """UPDATE reservations SET date=?, vehicle_id=?, start_time=?,
                               end_time=?, user_name=?, purpose=?, transferable=? WHERE id=?""",
                            (d, vid, start_s, end_s, user, purpose, yld, rid))
                    else:
                        conn.execute(
                            """INSERT INTO reservations(date, vehicle_id, start_time,
                               end_time, user_name, purpose, transferable)
                               VALUES(?,?,?,?,?,?,?)""",
                            (d, vid, start_s, end_s, user, purpose, yld))
                for did in orig_ids - seen:
                    conn.execute("DELETE FROM reservations WHERE id=?", (did,))
            st.success("保存しました。")
            st.rerun()
        except Exception as e:
            st.error(f"保存できませんでした: {e}")


# ============================================================
# 画面：車両管理（クリックで直接編集）
# ============================================================
def _cell_str(v):
    """セルの値を安全に文字列化（空欄・NaN対応）"""
    return str(v).strip() if pd.notna(v) else ""


def page_vehicles():
    st.subheader("🚙 車両管理")
    st.caption("表のマスをクリックすると直接編集できます。"
               "最終行で行を追加、行頭のチェックで削除できます。編集後は「💾 保存」を押してください。")

    rows = get_vehicles(active_only=False)
    df = pd.DataFrame(rows, columns=["id", "name", "plate", "active"])
    if df.empty:
        df = pd.DataFrame(columns=["id", "name", "plate", "active"])
    df["active"] = df["active"].astype(bool)
    df = df.rename(columns={"name": "車両名", "plate": "ナンバー", "active": "使用中"})

    edited = st.data_editor(
        df, key="veh_editor", num_rows="dynamic", hide_index=True,
        width="stretch",
        column_config={
            "id": None,
            "車両名": st.column_config.TextColumn("車両名", required=True),
            "ナンバー": st.column_config.TextColumn("ナンバー", help="例：品川500 あ 12-34"),
            "使用中": st.column_config.CheckboxColumn("使用中", default=True),
        },
    )

    if st.button("💾 保存", type="primary", key="veh_save"):
        try:
            orig_ids = {int(i) for i in df["id"].dropna().tolist()}
            seen = set()
            with get_conn() as conn:
                for _, row in edited.iterrows():
                    name = _cell_str(row["車両名"])
                    if not name:
                        continue  # 車両名が空の行は無視
                    plate = _cell_str(row["ナンバー"])
                    active = 1 if bool(row["使用中"]) else 0
                    rid = row["id"]
                    if pd.notna(rid):
                        rid = int(rid)
                        seen.add(rid)
                        conn.execute("UPDATE vehicles SET name=?, plate=?, active=? WHERE id=?",
                                     (name, plate, active, rid))
                    else:
                        conn.execute("INSERT INTO vehicles(name, plate, active) VALUES(?,?,?)",
                                     (name, plate, active))
                # 編集後に消えた行は削除（関連する予約も一緒に削除）
                for did in orig_ids - seen:
                    conn.execute("DELETE FROM reservations WHERE vehicle_id=?", (did,))
                    conn.execute("DELETE FROM vehicles WHERE id=?", (did,))
            st.success("保存しました。")
            st.rerun()
        except Exception as e:
            st.error(f"保存できませんでした: {e}")


# ============================================================
# 画面：設定
# ============================================================
def page_settings():
    st.subheader("⚙️ 設定")

    # --- 掃除当番メンバー（クリックで直接編集）---
    st.markdown("#### 🧹 朝の掃除当番メンバー")
    st.caption("名前のマスをクリックして直接編集できます。"
               "「順番」の数字が小さい人から当番が回ります。最終行で追加、行頭チェックで削除。"
               "編集後は「💾 保存」を押してください。")

    members = get_members()
    mdf = pd.DataFrame(members, columns=["id", "name", "sort_order"])
    if mdf.empty:
        mdf = pd.DataFrame(columns=["id", "name", "sort_order"])
    mdf["sort_order"] = mdf["sort_order"] + 1  # 1始まりで表示
    mdf = mdf.rename(columns={"name": "名前", "sort_order": "順番"})

    edited_m = st.data_editor(
        mdf, key="mem_editor", num_rows="dynamic", hide_index=True,
        width="stretch",
        column_config={
            "id": None,
            "順番": st.column_config.NumberColumn("順番", min_value=1, step=1,
                                                help="小さい順に当番が回ります"),
            "名前": st.column_config.TextColumn("名前", required=True),
        },
    )

    if st.button("💾 保存", type="primary", key="mem_save"):
        try:
            orig_ids = {int(i) for i in mdf["id"].dropna().tolist()}
            valid = []
            for _, row in edited_m.iterrows():
                name = _cell_str(row["名前"])
                if not name:
                    continue
                order = float(row["順番"]) if pd.notna(row["順番"]) else 9999.0
                rid = int(row["id"]) if pd.notna(row["id"]) else None
                valid.append((order, name, rid))
            valid.sort(key=lambda x: x[0])  # 順番で並べ替え
            seen = set()
            with get_conn() as conn:
                for i, (_, name, rid) in enumerate(valid):
                    if rid is not None:
                        seen.add(rid)
                        conn.execute("UPDATE members SET name=?, sort_order=? WHERE id=?",
                                     (name, i, rid))
                    else:
                        conn.execute("INSERT INTO members(name, sort_order) VALUES(?,?)",
                                     (name, i))
                for did in orig_ids - seen:
                    conn.execute("DELETE FROM members WHERE id=?", (did,))
            st.success("保存しました。")
            st.rerun()
        except Exception as e:
            st.error(f"保存できませんでした: {e}")

    st.divider()

    # --- ローテーション起算日 ---
    st.markdown("#### 📆 ローテーション起算日")
    st.caption("この日（の営業日）から1番目のメンバーが当番になります。")
    try:
        base = datetime.date.fromisoformat(get_setting("rotation_base_date", "2026-01-05"))
    except ValueError:
        base = datetime.date(2026, 1, 5)
    new_base = st.date_input("起算日", base)
    if new_base != base:
        set_setting("rotation_base_date", new_base.isoformat())
        st.success("起算日を更新しました。")

    st.divider()

    # --- 月曜行事 ---
    st.markdown("#### 📌 毎週月曜の行事")
    st.caption("月曜が祝日の場合は自動的に翌営業日に振り替えて表示されます。")
    event_name = st.text_input("行事の名前（例：朝礼、ゴミ出し、車両点検）",
                               get_setting("weekly_event_name", "朝礼"))
    if event_name != get_setting("weekly_event_name"):
        set_setting("weekly_event_name", event_name)
        st.success("行事名を更新しました。")

    rotation_on = st.checkbox("月曜行事も当番制にする（掃除当番メンバーで週替わりローテーション）",
                              value=get_setting("weekly_event_rotation", "0") == "1")
    if str(int(rotation_on)) != get_setting("weekly_event_rotation", "0"):
        set_setting("weekly_event_rotation", str(int(rotation_on)))
        st.rerun()


# ============================================================
# ログイン（社外公開時のパスワード保護）
# ============================================================
def _get_app_password():
    """パスワードを取得。Streamlit secrets か環境変数 APP_PASSWORD。未設定なら None。"""
    try:
        if "app_password" in st.secrets:
            return str(st.secrets["app_password"])
    except Exception:
        pass
    return os.environ.get("APP_PASSWORD")


def require_login():
    """パスワードが設定されていればログインを要求する。
    未設定ならそのまま通す（社内・ローカル利用向け）。"""
    pw = _get_app_password()
    if not pw:
        return  # パスワード未設定 → 認証なしで利用可（オンライン公開時は必ず設定）
    if st.session_state.get("_authed"):
        return
    st.title("🔒 社用車・当番管理システム")
    st.caption("社外からのアクセスにはパスワードが必要です。")
    entered = st.text_input("パスワード", type="password", key="_login_pw")
    if st.button("ログイン", type="primary"):
        if entered == pw:
            st.session_state["_authed"] = True
            st.rerun()
        else:
            st.error("パスワードが違います。")
    st.stop()


# ============================================================
# メイン
# ============================================================
def main():
    # 初期化（テーブル作成・マイグレーション）はセッション中に1回だけ
    # （Turso利用時の通信回数を減らすため）
    if not st.session_state.get("_db_inited"):
        try:
            init_db()
            st.session_state["_db_inited"] = True
        except Exception as e:
            st.error("データ保存先への接続に失敗しました。"
                     "Turso をお使いの場合は接続情報（URL・トークン）をご確認ください。")
            st.exception(e)
            st.stop()
    # パスワード認証は廃止（URLを知っていれば誰でも利用可）。
    # 再度パスワードで保護したくなったら、次行のコメントを外してください。
    # require_login()

    # 全体のフォントを大きくする（既定より約1.25倍）
    st.markdown(
        """
        <style>
          html, body, [class*="css"], .stApp, [data-testid="stMarkdownContainer"] {
              font-size: 19px;
          }
          [data-testid="stDataFrame"] div, [data-testid="stDataEditor"] div { font-size: 16px; }
          .stButton button { font-size: 18px; }
          h1 {font-size: 2.1rem;} h2 {font-size: 1.7rem;} h3 {font-size: 1.4rem;}
        </style>
        """,
        unsafe_allow_html=True,
    )

    # カレンダーのリンク（時間帯・備考）からの画面移動を処理する
    qp = st.query_params
    nav = qp.get("nav")
    if nav == "resv":                       # 時間帯クリック → 車両予約画面へ
        st.session_state["menu"] = "🚗 車両予約"
        ds = qp.get("date")
        if ds:
            try:
                st.session_state["sched_date"] = datetime.date.fromisoformat(ds)
            except ValueError:
                pass
        qp.clear()
    elif nav == "note":                     # 備考クリック → カレンダーで備考入力を開く
        st.session_state["menu"] = "📅 カレンダー"
        ds = qp.get("date")
        if ds:
            st.session_state["editnote_date"] = ds
        qp.clear()
    elif nav == "swap":                     # 入替クリック → カレンダーで入替入力を開く
        st.session_state["menu"] = "📅 カレンダー"
        ds = qp.get("date")
        if ds:
            st.session_state["editswap_date"] = ds
        qp.clear()

    st.title("🚗 社用車・当番管理システム")

    with st.sidebar:
        st.header("メニュー")
        page = st.radio(
            "画面を選択",
            ["📅 カレンダー", "🚗 車両予約", "🚙 車両管理", "⚙️ 設定"],
            label_visibility="collapsed",
            key="menu",
        )
        st.divider()
        today = jst_today()
        st.caption(f"今日：{today}（{WEEKDAY_JP[today.weekday()]}）")
        h = jpholiday.is_holiday_name(today)
        if h:
            st.caption(f"本日は祝日（{h}）です")
        # データ保存先の表示（Turso接続の確認用）
        st.caption("💾 保存先：☁️ Turso（クラウド）" if using_turso()
                   else "💾 保存先：このサーバー内（ローカル）")

    try:
        if page == "📅 カレンダー":
            page_calendar()
        elif page == "🚗 車両予約":
            page_reservation()
        elif page == "🚙 車両管理":
            page_vehicles()
        elif page == "⚙️ 設定":
            page_settings()
    except Exception as e:
        st.error(f"エラーが発生しました: {e}")


if __name__ == "__main__":
    main()
