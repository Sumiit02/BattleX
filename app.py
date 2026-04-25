from flask import Flask, render_template, request, redirect, url_for, flash, session, jsonify, Response, send_from_directory
import sqlite3
from werkzeug.security import generate_password_hash, check_password_hash
import os
try:
    import psycopg2
except ImportError:
    psycopg2 = None
import requests
from bs4 import BeautifulSoup
import csv
import io
try:
    from requests_oauthlib import OAuth2Session
except ImportError:
    OAuth2Session = None
import secrets
import json
from datetime import datetime
from zoneinfo import ZoneInfo
import re
from werkzeug.utils import secure_filename
from urllib.parse import urlparse, urlencode

# Environment helpers
def _is_truthy(value):
    return str(value or '').strip().lower() in ('1', 'true', 'yes', 'on')

IS_PRODUCTION = _is_truthy(os.getenv('RENDER')) or os.getenv('FLASK_ENV', '').strip().lower() == 'production'

# Allow OAuth over HTTP for local development only.
if not IS_PRODUCTION:
    os.environ['OAUTHLIB_INSECURE_TRANSPORT'] = '1'

app = Flask(__name__)
app.secret_key = os.getenv('SECRET_KEY', 'dev-secret-change-me')
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'  # allow cookie on OAuth redirect
app.config['SESSION_COOKIE_SECURE'] = _is_truthy(os.getenv('SESSION_COOKIE_SECURE', '1' if IS_PRODUCTION else '0'))
DATABASE_URL = (os.getenv('DATABASE_URL') or '').strip()
if DATABASE_URL.startswith('postgres://'):
    DATABASE_URL = DATABASE_URL.replace('postgres://', 'postgresql://', 1)
USE_POSTGRES = DATABASE_URL.startswith('postgresql://')
DB_NAME = os.getenv('DB_NAME') or os.getenv('DATABASE_PATH') or 'gamezone.db'
ALLOWED_IMAGE_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif', 'webp', 'avif'}
DEFAULT_UPLOAD_FOLDER = os.path.join(app.static_folder or 'static', 'uploads')
STATIC_UPLOAD_FOLDER = os.getenv('UPLOAD_DIR') or DEFAULT_UPLOAD_FOLDER
try:
    os.makedirs(STATIC_UPLOAD_FOLDER, exist_ok=True)
except PermissionError:
    # On platforms without writable mounted disks, fall back to temp storage.
    STATIC_UPLOAD_FOLDER = os.path.join('/tmp', 'battlex_uploads')
    os.makedirs(STATIC_UPLOAD_FOLDER, exist_ok=True)
    print(f"UPLOAD_DIR not writable, using fallback: {STATIC_UPLOAD_FOLDER}")
APP_TIMEZONE = os.getenv('APP_TIMEZONE', 'Asia/Kolkata')
try:
    LOCAL_TIMEZONE = ZoneInfo(APP_TIMEZONE)
except Exception:
    LOCAL_TIMEZONE = None

_EVENT_ROLLOVER_LAST_RUN_DATE = None

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ADMIN_SETTINGS_FILE = os.path.join(BASE_DIR, 'admin_settings.json')

# Bootstrap admin (can be overridden by env vars on Render)
BOOTSTRAP_ADMIN_USERNAME = (os.getenv('ADMIN_USERNAME') or 'admin1').strip()
BOOTSTRAP_ADMIN_PASSWORD = os.getenv('ADMIN_PASSWORD') or 'V9!kQ2@xL7#pM4s'
BOOTSTRAP_ADMIN_CODE = os.getenv('ADMIN_CODE') or '74829'
BOOTSTRAP_ADMIN_EMAIL = (os.getenv('ADMIN_EMAIL') or 'admin1@example.com').strip()
BOOTSTRAP_ADMIN_PHONE = (os.getenv('ADMIN_PHONE') or '').strip()

_ORIGINAL_SQLITE_CONNECT = sqlite3.connect


def _translate_sql_placeholders(query):
    # Keep existing sqlite-style qmark queries working in Postgres.
    return query.replace('?', '%s')


class _CompatCursor:
    def __init__(self, raw_cursor, use_postgres=False):
        self._raw = raw_cursor
        self._use_postgres = use_postgres

    def execute(self, query, params=None):
        if self._use_postgres:
            query = _translate_sql_placeholders(query)
        if params is None:
            return self._raw.execute(query)
        return self._raw.execute(query, params)

    def executemany(self, query, seq_of_params):
        if self._use_postgres:
            query = _translate_sql_placeholders(query)
        return self._raw.executemany(query, seq_of_params)

    def __getattr__(self, name):
        return getattr(self._raw, name)


class _CompatConnection:
    def __init__(self, raw_conn, use_postgres=False):
        self._raw = raw_conn
        self._use_postgres = use_postgres

    def cursor(self):
        return _CompatCursor(self._raw.cursor(), use_postgres=self._use_postgres)

    def __getattr__(self, name):
        return getattr(self._raw, name)


def _db_connect(database_name=DB_NAME, *args, **kwargs):
    if USE_POSTGRES:
        if psycopg2 is None:
            raise RuntimeError('psycopg2 is required when DATABASE_URL is set for PostgreSQL')
        sslmode = os.getenv('PGSSLMODE') or ('require' if IS_PRODUCTION else 'prefer')
        raw = psycopg2.connect(DATABASE_URL, sslmode=sslmode)
        raw.autocommit = False
        return _CompatConnection(raw, use_postgres=True)
    return _ORIGINAL_SQLITE_CONNECT(database_name, *args, **kwargs)


# Route all existing sqlite3.connect(...) calls through the compatibility connector.
sqlite3.connect = _db_connect

def load_admin_settings():
    try:
        if os.path.exists(ADMIN_SETTINGS_FILE):
            with open(ADMIN_SETTINGS_FILE, 'r', encoding='utf-8') as fh:
                data = json.load(fh)
                if isinstance(data, dict):
                    return data
    except Exception as ex:
        print('Unable to load admin settings:', ex)
    return {}

def save_admin_settings(payload):
    global ADMIN_SETTINGS
    ADMIN_SETTINGS = payload or {}
    try:
        with open(ADMIN_SETTINGS_FILE, 'w', encoding='utf-8') as fh:
            json.dump(ADMIN_SETTINGS, fh)
    except Exception as ex:
        print('Unable to save admin settings:', ex)

ADMIN_SETTINGS = load_admin_settings()

MATCH_STATUSES = ('upcoming', 'ongoing', 'completed')
MATCH_STATUS_ORDER = {'ongoing': 0, 'upcoming': 1, 'completed': 2}
MATCH_GAME_TYPES = ('BR', 'CS', 'Custom')
WALLET_MIN_DEPOSIT_RUPEES = 15
WALLET_MAX_DEPOSIT_RUPEES = 1000
WALLET_MIN_WITHDRAW_RUPEES = 50
WALLET_MAX_WITHDRAW_RUPEES = 5000
GAME_TYPE_LABELS = {
    'BR': 'Battle Royale',
    'CS': 'Clash Squad',
    'Custom': 'Custom Room'
}

def _allowed_image(filename):
	return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_IMAGE_EXTENSIONS


def _safe_int(value, default=0):
    """Return int(value) or fallback to default when conversion fails."""
    try:
        return int(value)
    except Exception:
        return default

def _save_event_image(file_storage):
	filename = secure_filename(file_storage.filename or '')
	if not filename or not _allowed_image(filename):
		return None
	unique_name = f"{int(datetime.utcnow().timestamp())}_{filename}"
	target_path = os.path.join(STATIC_UPLOAD_FOLDER, unique_name)
	file_storage.save(target_path)
	return f"uploads/{unique_name}"

def _resolve_event_image(path):
    if not path:
        return url_for('static', filename='images/freefire.webp')
    normalized = str(path).strip()
    if normalized.startswith(('http://', 'https://')):
        return normalized
    if normalized.startswith('uploads/'):
        return url_for('uploaded_file', filename=normalized[len('uploads/'):])
    if normalized.startswith('/'):
        return normalized
    if normalized.startswith('static/'):
        normalized = normalized[len('static/'):]
    return url_for('static', filename=normalized)


@app.route('/uploads/<path:filename>')
def uploaded_file(filename):
    return send_from_directory(STATIC_UPLOAD_FOLDER, filename)


def _default_event_image(mode):
    """Map BR modes to their default card images; fall back to freefire."""
    base = 'images/freefire.webp'
    if not mode:
        return base
    text = str(mode).lower()
    is_br = 'br' in text or 'battle royale' in text
    if is_br and 'solo' in text:
        return 'images/solobr.webp'
    if is_br and 'duo' in text:
        return 'images/duobr.webp'
    if is_br and ('squad' in text or 'team' in text):
        return 'images/sqbr.webp'
    return base

def _append_cache_bust(url, ver):
	if not ver:
		return url
	# sanitize version value (timestamp/iso) into a short token
	token = re.sub(r'[^0-9A-Za-z]', '', str(ver))[:16]
	sep = '&' if '?' in url else '?'
	return f"{url}{sep}v={token}"


def _current_local_time():
    if LOCAL_TIMEZONE is not None:
        return datetime.now(LOCAL_TIMEZONE).replace(tzinfo=None)
    return datetime.now()


def _move_time_value_to_date(value, target_date):
    if not value or not target_date:
        return value
    dt = _parse_datetime_value(value)
    if dt:
        return datetime.combine(target_date, dt.time()).isoformat()
    time_component = _parse_time_component(value)
    if time_component:
        return datetime.combine(target_date, time_component).isoformat()
    return value


def _auto_roll_event_dates_daily():
    """Move past event dates to today once per day to keep recurring tournaments fresh."""
    global _EVENT_ROLLOVER_LAST_RUN_DATE
    today = _current_local_time().date()
    if _EVENT_ROLLOVER_LAST_RUN_DATE == today:
        return

    conn = None
    try:
        conn = sqlite3.connect(DB_NAME)
        cur = conn.cursor()
        cur.execute('SELECT id, date, start_time, end_time, field_updates FROM events')
        rows = cur.fetchall()
        if not rows:
            _EVENT_ROLLOVER_LAST_RUN_DATE = today
            conn.close()
            return

        changed = False
        now_iso = datetime.utcnow().isoformat()
        for row in rows:
            event_id = row[0]
            event_date = _parse_date_value(row[1])
            if not event_date or event_date >= today:
                continue

            new_date = today.isoformat()
            new_start = _move_time_value_to_date(row[2], today)
            new_end = _move_time_value_to_date(row[3], today)

            try:
                field_updates = json.loads(row[4]) if row[4] else {}
            except Exception:
                field_updates = {}
            field_updates['date'] = now_iso
            if new_start != row[2]:
                field_updates['start_time'] = now_iso
            if new_end != row[3]:
                field_updates['end_time'] = now_iso

            cur.execute(
                'UPDATE events SET date = ?, start_time = ?, end_time = ?, field_updates = ? WHERE id = ?',
                (new_date, new_start, new_end, json.dumps(field_updates), event_id)
            )
            changed = True

        if changed:
            conn.commit()
        _EVENT_ROLLOVER_LAST_RUN_DATE = today
    except Exception:
        # Keep reads resilient even if this background consistency update fails.
        pass
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass

def get_events(include_closed=False):
    # Load events from database if available; otherwise fall back to the in-memory defaults.
    _auto_roll_event_dates_daily()
    events = []
    try:
        conn = sqlite3.connect(DB_NAME)
        cur = conn.cursor()
        where_clause = '' if include_closed else ' WHERE is_open = 1'
        cur.execute(f"SELECT id, title, date, prize, slots_left, max_slots, region, platform, mode, image, description, entry_fee, prize_pool, start_time, end_time, is_open, field_updates, rules FROM events{where_clause} ORDER BY id")
        rows = cur.fetchall()
        for r in rows:
            max_slots_val = _safe_int(r[5], 0)
            slots_left_val = _safe_int(r[4], max_slots_val)
            events.append({
                'id': r[0],
                'title': r[1],
                'date': r[2],
                'prize': r[3],
                'slots_left': slots_left_val,
                'max_slots': max_slots_val,
                'region': r[6],
                'platform': r[7],
                'mode': r[8],
				# resolve and cache-bust image so players see updates immediately
				'image': _append_cache_bust(
					_resolve_event_image(r[9]),
					(json.loads(r[16]).get('image') if r[16] else '')
				),
                'description': r[10],
                'entry_fee': r[11] if len(r) > 11 else 0,
                'prize_pool': r[12] if len(r) > 12 else r[3],
                'start_time': r[13] if len(r) > 13 else None,
                'end_time': r[14] if len(r) > 14 else None,
                'is_open': bool(r[15]) if r[15] is not None else True,
                'rules': r[17] if len(r) > 17 else None
            })
        conn.close()
        # If we found events in DB return them
        if events:
            return events
    except Exception:
        # on any DB error, fall back to defaults below
        pass

    # Fallback: build a small default set (used only when DB missing or empty)
    modes = [
        {'id': 1, 'mode_key': 'BR Solo', 'title': 'BR Solo', 'max_slots': 128},
        {'id': 2, 'mode_key': 'BR Duo', 'title': 'BR Duo', 'max_slots': 64},
        {'id': 3, 'mode_key': 'BR Squad', 'title': 'BR Squad', 'max_slots': 32},
        {'id': 4, 'mode_key': 'CS 1v1', 'title': 'Clash Squad 1v1', 'max_slots': 64},
        {'id': 5, 'mode_key': 'CS 2v2', 'title': 'Clash Squad 2v2', 'max_slots': 32},
        {'id': 6, 'mode_key': 'CS 3v3', 'title': 'Clash Squad 3v3', 'max_slots': 24},
        {'id': 7, 'mode_key': 'CS 4v4', 'title': 'Clash Squad 4v4', 'max_slots': 16},
    ]
    events = []
    for m in modes:
        max_slots = m.get('max_slots', 32)
        slots_left = max_slots
        events.append({
            'id': m['id'],
            'title': m['title'],
            'date': 'Upcoming',
            'prize': '₹' + str(1000 * m['id']),
            'slots_left': slots_left,
            'max_slots': max_slots,
            'region': 'India',
            'platform': 'Mobile',
            'mode': m['mode_key'],
            'image': _resolve_event_image('images/freefire.webp'),
            'description': 'Auto-created placeholder event',
            'entry_fee': 0,
            'prize_pool': '₹' + str(1000 * m['id']),
            'start_time': None,
            'end_time': None,
            'is_open': True
        })
    return events


def _normalize_match_status(value, default='upcoming'):
    if not value:
        return default
    val = str(value).strip().lower()
    for status in MATCH_STATUSES:
        if val == status:
            return status
    return default


def _normalize_match_type(value):
    if not value:
        return 'BR'
    val = str(value).strip().upper()
    if 'CUSTOM' in val:
        return 'BR'
    if val.startswith('BR') or 'BATTLE' in val:
        return 'BR'
    if val.startswith('CS') or 'CLASH' in val:
        return 'CS'
    if val in ('BR', 'CS'):
        return val
    return 'BR'


def _parse_datetime_value(value):
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    text = str(value).strip()
    if not text:
        return None
    for fmt in (
        '%Y-%m-%dT%H:%M:%S', '%Y-%m-%d %H:%M:%S', '%Y-%m-%dT%H:%M', '%Y-%m-%d %H:%M',
        '%d-%m-%Y %H:%M', '%d/%m/%Y %H:%M'
    ):
        try:
            return datetime.strptime(text, fmt)
        except Exception:
            continue
    try:
        dt = datetime.fromisoformat(text)
        if dt.tzinfo and LOCAL_TIMEZONE is not None:
            dt = dt.astimezone(LOCAL_TIMEZONE)
        if dt.tzinfo:
            dt = dt.replace(tzinfo=None)
        return dt
    except Exception:
        return None


def _parse_date_value(value):
    if not value:
        return None
    if isinstance(value, datetime):
        return value.date()
    text = str(value).strip()
    if not text:
        return None
    for fmt in ('%Y-%m-%d', '%d-%m-%Y', '%d/%m/%Y'):
        try:
            return datetime.strptime(text, fmt).date()
        except Exception:
            continue
    return None


def _parse_time_component(value):
    if not value:
        return None
    if isinstance(value, datetime):
        return value.time()
    text = str(value).strip()
    if not text:
        return None
    text_upper = text.upper()
    for fmt in ('%I:%M %p', '%I %p', '%H:%M:%S', '%H:%M'):
        try:
            return datetime.strptime(text_upper, fmt).time()
        except Exception:
            continue
    return None


def _combine_time_with_period(time_value, period_value):
    """Merge a 24h time picker value with an optional AM/PM selector for parsing."""
    if time_value is None:
        return None
    text = str(time_value).strip()
    if not text:
        return None
    period = (period_value or '').strip().upper()
    if period not in ('AM', 'PM'):
        return text
    upper = text.upper()
    if upper.endswith(' AM') or upper.endswith(' PM'):
        return text
    parts = text.split(':')
    if parts and parts[0].isdigit():
        hour = int(parts[0])
        remainder_parts = parts[1:]
        if hour == 0:
            display_hour = 12
        elif hour > 12:
            display_hour = hour - 12
        else:
            display_hour = hour
        if display_hour <= 0:
            display_hour = 12
        hh = str(display_hour).zfill(2)
        remainder = ':' + ':'.join(remainder_parts) if remainder_parts else ''
        return f"{hh}{remainder} {period}"
    return f"{text} {period}"


def _normalize_time_input(time_value, date_value=None):
    if time_value is None:
        return None
    if isinstance(time_value, datetime):
        return time_value.isoformat()
    text = str(time_value).strip()
    if not text:
        return None
    dt = _parse_datetime_value(text)
    if dt:
        return dt.isoformat()
    date_obj = _parse_date_value(date_value)
    time_component = _parse_time_component(text)
    if time_component:
        base_date = date_obj or _current_local_time().date()
        combined = datetime.combine(base_date, time_component)
        return combined.isoformat()
    return text


def _format_schedule_label(start_dt, date_obj, fallback):
    if start_dt:
        return start_dt.strftime('%d %b • %I:%M %p').replace(' 0', ' ')
    if date_obj:
        return date_obj.strftime('%d %b')
    return fallback or 'TBD'


def _format_admin_timestamp(value):
    if not value:
        return ''
    if isinstance(value, datetime):
        return value.strftime('%d %b %Y • %I:%M %p').lstrip('0')
    text = str(value).strip()
    if not text:
        return ''
    iso_candidate = text.replace('Z', '+00:00') if 'Z' in text and '+' not in text else text
    try:
        parsed = datetime.fromisoformat(iso_candidate)
        return parsed.strftime('%d %b %Y • %I:%M %p').lstrip('0')
    except Exception:
        return text


def _classify_event_status(event_date, start_dt, end_dt, now=None):
    now = now or _current_local_time()
    if start_dt and end_dt:
        if start_dt <= now <= end_dt:
            return 'ongoing'
        if now < start_dt:
            return 'upcoming'
        return 'completed'
    if start_dt:
        return 'ongoing' if now >= start_dt else 'upcoming'
    if event_date:
        if event_date == now.date():
            return 'ongoing'
        if event_date > now.date():
            return 'upcoming'
        return 'completed'
    return 'upcoming'


def _event_to_match(event_dict, now=None):
    start_dt = _parse_datetime_value(event_dict.get('start_time'))
    end_dt = _parse_datetime_value(event_dict.get('end_time'))
    event_date = _parse_date_value(event_dict.get('date'))
    if not start_dt and event_dict.get('start_time'):
        time_component = _parse_time_component(event_dict.get('start_time'))
        if time_component and event_date:
            start_dt = datetime.combine(event_date, time_component)
    if not end_dt and event_dict.get('end_time'):
        time_component = _parse_time_component(event_dict.get('end_time'))
        if time_component and event_date:
            end_dt = datetime.combine(event_date, time_component)
    if not event_date and start_dt:
        event_date = start_dt.date()
    elif not event_date and end_dt:
        event_date = end_dt.date()
    status = _classify_event_status(event_date, start_dt, end_dt, now=now)
    mode = event_dict.get('mode') or ''
    game_type = _normalize_match_type(mode)
    schedule_label = _format_schedule_label(start_dt, event_date, event_dict.get('date'))
    entry_fee = event_dict.get('entry_fee')
    if isinstance(entry_fee, (int, float)):
        entry_fee_label = f"₹{entry_fee}"
    else:
        entry_fee_label = entry_fee or '—'
    return {
        'id': event_dict.get('id'),
        'title': event_dict.get('title') or 'Untitled Event',
        'mode': mode,
        'format': mode or 'Format',
        'game_type': game_type,
        'status': status,
        'scheduled_for': schedule_label,
        'prize_pool': event_dict.get('prize_pool') or event_dict.get('prize') or '—',
        'entry_fee': entry_fee_label,
        'lobby_info': event_dict.get('region'),
        'custom_room_id': event_dict.get('platform'),
        'stream_link': event_dict.get('stream_link'),
        'notes': event_dict.get('description'),
        '_start_dt': start_dt,
        '_end_dt': end_dt,
        '_event_date': event_date,
    }


def _collate_matches_by_game(events=None):
    events = events or get_events()
    now = _current_local_time()
    collections = {gt: {'ongoing': [], 'upcoming': [], 'completed': []} for gt in MATCH_GAME_TYPES}
    for event in events:
        match = _event_to_match(event, now=now)
        status = match.get('status')
        if status not in ('ongoing', 'upcoming', 'completed'):
            continue
        game_type = match.get('game_type') or 'Custom'
        if game_type not in collections:
            collections[game_type] = {'ongoing': [], 'upcoming': [], 'completed': []}
        collections[game_type][status].append(match)
    for buckets in collections.values():
        for key, items in buckets.items():
            reverse = (key == 'completed')
            items.sort(key=lambda m: (m.get('_start_dt') or m.get('_end_dt') or datetime.max), reverse=reverse)
    return collections


def _match_counts_summary(collections):
    counts = {}
    for key, statuses in collections.items():
        counts[key.lower()] = {
            'ongoing': len(statuses.get('ongoing', [])),
            'upcoming': len(statuses.get('upcoming', [])),
            'completed': len(statuses.get('completed', []))
        }
    totals = {
        'ongoing': sum(counts.get(gt, {}).get('ongoing', 0) for gt in ('br', 'cs')),
        'upcoming': sum(counts.get(gt, {}).get('upcoming', 0) for gt in ('br', 'cs')),
        'completed': sum(counts.get(gt, {}).get('completed', 0) for gt in ('br', 'cs'))
    }
    counts['totals'] = totals
    counts.setdefault('br', {'ongoing': 0, 'upcoming': 0, 'completed': 0})
    counts.setdefault('cs', {'ongoing': 0, 'upcoming': 0, 'completed': 0})
    counts.setdefault('custom', {'ongoing': 0, 'upcoming': 0, 'completed': 0})
    return counts


def _format_currency_value(amount):
    if amount in (None, ''):
        return '—'
    try:
        value = int(amount)
    except Exception:
        try:
            numeric = float(amount)
        except Exception:
            return str(amount)
        formatted = f"₹{numeric:,.2f}".replace(',','')
        return formatted.rstrip('0').rstrip('.') if '.' in formatted else formatted
    rupees = value / 100
    if rupees.is_integer():
        return f"₹{int(rupees)}"
    formatted = f"₹{rupees:.2f}"
    return formatted.rstrip('0').rstrip('.') if '.' in formatted else formatted


def _wallet_amount_from_request(value):
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        amount = int(float(text))
    except Exception:
        return None
    return amount


def _wallet_rupees_to_paise(rupees):
    return int(rupees) * 100


def _wallet_load_recent_transactions(cur, username, limit=12):
    try:
        cur.execute(
            'SELECT id, txn_type, amount, status, note, created_at FROM wallet_transactions WHERE username = ? ORDER BY id DESC LIMIT ?',
            (username, limit)
        )
        rows = cur.fetchall()
    except Exception:
        return []
    items = []
    for row in rows:
        items.append({
            'id': row[0],
            'txn_type': row[1] or '-',
            'amount_label': _format_currency_value(row[2]),
            'status': row[3] or 'completed',
            'note': row[4] or '',
            'created_at': row[5] or '-'
        })
    return items


def _wallet_user_balance_paise(cur, username):
    try:
        cur.execute('SELECT COALESCE(wallet_balance, 0) FROM users WHERE username = ?', (username,))
        row = cur.fetchone()
        return int(row[0] or 0) if row else 0
    except Exception:
        return 0


def _wallet_user_balance_paise_by_name(username):
    conn = None
    try:
        conn = sqlite3.connect(DB_NAME)
        cur = conn.cursor()
        return _wallet_user_balance_paise(cur, username)
    except Exception:
        return 0
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def _is_valid_ifsc(value):
    return bool(re.match(r'^[A-Za-z]{4}0[A-Za-z0-9]{6}$', str(value or '').strip()))


def _is_valid_bank_account(value):
    return bool(re.match(r'^[0-9]{9,18}$', str(value or '').strip()))


def _is_valid_mobile(value):
    return bool(re.match(r'^[0-9]{10}$', str(value or '').strip()))


def _is_valid_upi(value):
    return bool(re.match(r'^[A-Za-z0-9._-]{2,}@[A-Za-z0-9]{2,}$', str(value or '').strip()))

# ---------- CASHFREE CONFIGURATION ----------
CASHFREE_APP_ID = os.getenv('CASHFREE_APP_ID', '').strip()
CASHFREE_SECRET_KEY = os.getenv('CASHFREE_SECRET_KEY', '').strip()
CASHFREE_ENV = (os.getenv('CASHFREE_ENV', 'sandbox') or 'sandbox').strip().lower()
CASHFREE_API_VERSION = os.getenv('CASHFREE_API_VERSION', '2023-08-01').strip()
CASHFREE_API_BASE = 'https://api.cashfree.com/pg' if CASHFREE_ENV == 'production' else 'https://sandbox.cashfree.com/pg'


def is_cashfree_ready():
    return bool(CASHFREE_APP_ID and CASHFREE_SECRET_KEY)


def _cashfree_headers():
    return {
        'x-client-id': CASHFREE_APP_ID,
        'x-client-secret': CASHFREE_SECRET_KEY,
        'x-api-version': CASHFREE_API_VERSION,
        'Content-Type': 'application/json'
    }

google_creds_file = os.getenv('GOOGLE_OAUTH_CREDENTIALS_FILE')
# ---------- GOOGLE OAUTH CONFIG ----------
def _load_static_google_credentials():
    static_root = app.static_folder or 'static'
    if not static_root:
        return {}
    try:
        for name in os.listdir(static_root):
            if not name.startswith('client_secret') or not name.endswith('.json'):
                continue
            candidate = os.path.join(static_root, name)
            with open(candidate, 'r', encoding='utf-8') as fh:
                payload = json.load(fh) or {}
            if 'web' in payload:
                payload = payload['web']
            elif 'installed' in payload:
                payload = payload['installed']
            client_id = payload.get('client_id')
            client_secret = payload.get('client_secret')
            redirect_uris = payload.get('redirect_uris') or payload.get('redirectUris') or []
            if client_id and client_secret:
                return {
                    'client_id': client_id,
                    'client_secret': client_secret,
                    'redirect_uris': redirect_uris
                }
    except Exception as ex:
        print('Failed to load static Google OAuth credentials:', ex)
    return {}

def compute_google_credentials(settings=None):
    client_id = os.getenv('GOOGLE_CLIENT_ID') or 'your_google_client_id'
    client_secret = os.getenv('GOOGLE_CLIENT_SECRET') or 'your_google_client_secret'
    redirect_uri = os.getenv('GOOGLE_REDIRECT_URI') or None
    creds_file = os.getenv('GOOGLE_OAUTH_CREDENTIALS_FILE')
    if creds_file and os.path.exists(creds_file):
        try:
            with open(creds_file, 'r', encoding='utf-8') as f:
                creds_payload = json.load(f) or {}
                client_id = creds_payload.get('client_id', client_id)
                client_secret = creds_payload.get('client_secret', client_secret)
                redirect_uri = creds_payload.get('redirect_uri') or redirect_uri
        except Exception as ex:
            print('Failed to load Google OAuth credentials file:', ex)
    static_creds = _load_static_google_credentials()
    if static_creds:
        client_id = static_creds.get('client_id', client_id)
        client_secret = static_creds.get('client_secret', client_secret)
        redirect_list = static_creds.get('redirect_uris') or []
        if redirect_list and not redirect_uri:
            redirect_uri = redirect_list[0]
    active_settings = settings or ADMIN_SETTINGS or {}
    client_id = active_settings.get('google_client_id') or client_id
    client_secret = active_settings.get('google_client_secret') or client_secret
    redirect_uri = active_settings.get('google_redirect_uri') or redirect_uri
    try:
        if not redirect_uri:
            redirect_uri = url_for('google_callback', _external=True)
    except RuntimeError:
        # No app context yet; postpone until runtime
        redirect_uri = redirect_uri or None
    return client_id, client_secret, redirect_uri

GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, GOOGLE_REDIRECT_URI = compute_google_credentials()
GOOGLE_AUTHORIZATION_BASE_URL = 'https://accounts.google.com/o/oauth2/v2/auth'
GOOGLE_TOKEN_URL = 'https://oauth2.googleapis.com/token'
GOOGLE_USERINFO_URL = 'https://www.googleapis.com/oauth2/v2/userinfo'
GOOGLE_SCOPE = ['openid', 'https://www.googleapis.com/auth/userinfo.email', 'https://www.googleapis.com/auth/userinfo.profile']

def is_google_login_ready():
    if OAuth2Session is None:
        return False
    if not GOOGLE_CLIENT_ID or not GOOGLE_CLIENT_SECRET:
        return False
    placeholders = {'your_google_client_id', 'your_google_client_secret'}
    return GOOGLE_CLIENT_ID not in placeholders and GOOGLE_CLIENT_SECRET not in placeholders


def refresh_google_config():
    """Reload Google OAuth credentials from env/settings so changes take effect without restart."""
    global GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, GOOGLE_REDIRECT_URI
    GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, GOOGLE_REDIRECT_URI = compute_google_credentials()
    try:
        redir = GOOGLE_REDIRECT_URI
        print('[GOOGLE_CONFIG]', 'client_id=', GOOGLE_CLIENT_ID, 'redirect_uri=', redir)
    except Exception:
        pass
    return GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, GOOGLE_REDIRECT_URI

@app.context_processor
def inject_google_login_flag():
    return {'google_login_enabled': is_google_login_ready()}

@app.template_filter('friendly_date')
def friendly_date(value):
    if not value:
        return ''
    if isinstance(value, datetime):
        dt = value
    else:
        raw = str(value).strip()
        if not raw:
            return ''
        for parser in (
            lambda v: datetime.fromisoformat(v),
            lambda v: datetime.strptime(v, '%Y-%m-%d'),
            lambda v: datetime.strptime(v, '%Y-%m-%d %H:%M:%S'),
            lambda v: datetime.strptime(v, '%Y-%m-%dT%H:%M:%S'),
            lambda v: datetime.strptime(v, '%d-%m-%Y'),
            lambda v: datetime.strptime(v, '%d/%m/%Y'),
        ):
            try:
                dt = parser(raw)
                return dt.strftime('%d %b')
            except Exception:
                continue
    return ''


@app.template_filter('friendly_time')
def friendly_time(value):
    if not value:
        return ''
    if isinstance(value, datetime):
        dt = value
    else:
        text = str(value).strip()
        if not text:
            return ''
        dt = None
        for fmt in (
            '%Y-%m-%dT%H:%M:%S', '%Y-%m-%d %H:%M:%S', '%Y-%m-%dT%H:%M', '%Y-%m-%d %H:%M',
            '%d-%m-%Y %H:%M', '%d/%m/%Y %H:%M'
        ):
            try:
                dt = datetime.strptime(text, fmt)
                break
            except Exception:
                continue
        if dt is None:
            # Strip date portion if present and parse only time
            tail = text.split('T', 1)[1] if 'T' in text else text
            for fmt in ('%H:%M:%S', '%H:%M'):
                try:
                    dt = datetime.strptime(tail, fmt)
                    break
                except Exception:
                    continue
        if dt is None:
            try:
                dt = datetime.fromisoformat(text)
            except Exception:
                return text
    return dt.strftime('%I:%M %p').lstrip('0').replace(' 0', ' ')

@app.template_filter('dateonly')
def dateonly(value):
    """Return YYYY-MM-DD from a string/datetime; empty string if none."""
    if not value:
        return ''
    try:
        if isinstance(value, datetime):
            return value.strftime('%Y-%m-%d')
        s = str(value)
        if 'T' in s:
            return s.split('T', 1)[0]
        return s[:10]
    except Exception:
        return ''


@app.template_filter('timeonly')
def timeonly(value):
    """Return hh:mm AM/PM for datetime/iso strings; empty string if none."""
    if not value:
        return ''
    try:
        dt = _parse_datetime_value(value)
        if dt:
            return dt.strftime('%I:%M %p').lstrip('0')
        s = str(value).strip()
        if not s:
            return ''
        # Attempt to parse raw time strings
        for fmt in ('%H:%M:%S', '%H:%M', '%I:%M %p', '%I %p'):
            try:
                dt = datetime.strptime(s.upper(), fmt)
                return dt.strftime('%I:%M %p').lstrip('0')
            except Exception:
                continue
    except Exception:
        return ''
    return ''


@app.template_filter('timevalue')
def timevalue(value):
    """Return HH:MM (24h) string for inputs."""
    if not value:
        return ''
    dt = _parse_datetime_value(value)
    if dt:
        return dt.strftime('%H:%M')
    time_component = _parse_time_component(value)
    if time_component:
        return time_component.strftime('%H:%M')
    text = str(value).strip()
    if not text:
        return ''
    # Fallback: assume already HH:MM
    parts = text.split(':')
    if len(parts) >= 2 and parts[0].isdigit() and parts[1][:2].isdigit():
        return f"{parts[0].zfill(2)}:{parts[1][:2]}"
    return ''


@app.template_filter('timeperiod')
def timeperiod(value):
    """Return AM/PM based on stored time."""
    if not value:
        return ''
    dt = _parse_datetime_value(value)
    if dt:
        return dt.strftime('%p')
    time_component = _parse_time_component(value)
    if time_component:
        return datetime.combine(datetime.utcnow().date(), time_component).strftime('%p')
    text = str(value).strip().upper()
    if text.endswith('AM'):
        return 'AM'
    if text.endswith('PM'):
        return 'PM'
    return ''

def _get_google_oauth_session(state=None, token=None):
    if OAuth2Session is None:
        raise RuntimeError('requests-oauthlib is not installed')
    # Prefer explicitly configured redirect URI if present; otherwise build from current host.
    redirect_uri = GOOGLE_REDIRECT_URI or None
    try:
        if not redirect_uri:
            redirect_uri = request.host_url.rstrip('/') + url_for('google_callback_alias')
        # normalize localhost/127.0.0.1 mismatch for local dev to match console config
        if redirect_uri.startswith('http://127.0.0.1') and 'localhost' in (request.host or ''):
            redirect_uri = redirect_uri.replace('127.0.0.1', 'localhost')
        if redirect_uri.startswith('http://localhost') and '127.0.0.1' in (request.host or ''):
            redirect_uri = redirect_uri.replace('localhost', '127.0.0.1')
    except RuntimeError:
        redirect_uri = redirect_uri or None
    if not redirect_uri:
        redirect_uri = request.host_url.rstrip('/') + url_for('google_callback_alias')
    return OAuth2Session(
        GOOGLE_CLIENT_ID,
        scope=GOOGLE_SCOPE,
        state=state,
        redirect_uri=redirect_uri,
        token=token
    )

# ---------- DATABASE SETUP ----------
def init_db():
    # Ensure DB exists and all necessary tables are present. Use IF NOT EXISTS so this is safe
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()

    if USE_POSTGRES:
        cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
            username TEXT NOT NULL UNIQUE,
            email TEXT NOT NULL UNIQUE,
            password TEXT NOT NULL,
            role TEXT NOT NULL,
            game_id TEXT,
            phone TEXT,
            admin_code TEXT
        )
        """)
        cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP")
        cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS wallet_balance INTEGER DEFAULT 0")

        cur.execute("""
        CREATE TABLE IF NOT EXISTS registrations (
            id INTEGER GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
            username TEXT NOT NULL,
            email TEXT NOT NULL,
            game_id TEXT NOT NULL,
            phone TEXT NOT NULL,
            payout_upi TEXT,
            mode TEXT NOT NULL,
            team_size INTEGER DEFAULT 1,
            team_name TEXT,
            payment_id TEXT UNIQUE,
            order_id TEXT UNIQUE,
            amount INTEGER NOT NULL,
            status TEXT DEFAULT 'pending',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (username) REFERENCES users (username)
        )
        """)

        cur.execute("ALTER TABLE registrations ADD COLUMN IF NOT EXISTS refund_requested INTEGER DEFAULT 0")
        cur.execute("ALTER TABLE registrations ADD COLUMN IF NOT EXISTS refunded INTEGER DEFAULT 0")
        cur.execute("ALTER TABLE registrations ADD COLUMN IF NOT EXISTS manual_upi TEXT")
        cur.execute("ALTER TABLE registrations ADD COLUMN IF NOT EXISTS manual_payer_name TEXT")
        cur.execute("ALTER TABLE registrations ADD COLUMN IF NOT EXISTS manual_txn_id TEXT")
        cur.execute("ALTER TABLE registrations ADD COLUMN IF NOT EXISTS manual_paid_at TEXT")
        cur.execute("ALTER TABLE registrations ADD COLUMN IF NOT EXISTS paid_at TEXT")
        cur.execute("ALTER TABLE registrations ADD COLUMN IF NOT EXISTS payout_upi TEXT")
        cur.execute("ALTER TABLE registrations ADD COLUMN IF NOT EXISTS event_id INTEGER")

        cur.execute("""
        CREATE TABLE IF NOT EXISTS wallet_transactions (
            id INTEGER GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
            username TEXT NOT NULL,
            txn_type TEXT NOT NULL,
            amount INTEGER NOT NULL,
            status TEXT DEFAULT 'completed',
            note TEXT,
            withdrawal_request_id INTEGER,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (username) REFERENCES users (username)
        )
        """)
        cur.execute("ALTER TABLE wallet_transactions ADD COLUMN IF NOT EXISTS withdrawal_request_id INTEGER")

        cur.execute("""
        CREATE TABLE IF NOT EXISTS wallet_deposit_orders (
            id INTEGER GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
            username TEXT NOT NULL,
            order_id TEXT NOT NULL UNIQUE,
            amount INTEGER NOT NULL,
            status TEXT DEFAULT 'created',
            payment_id TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (username) REFERENCES users (username)
        )
        """)

        cur.execute("""
        CREATE TABLE IF NOT EXISTS wallet_withdrawal_requests (
            id INTEGER GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
            username TEXT NOT NULL,
            amount INTEGER NOT NULL,
            method TEXT NOT NULL,
            holder_name TEXT,
            bank_account_number TEXT,
            ifsc_code TEXT,
            upi_id TEXT,
            mobile_number TEXT,
            status TEXT DEFAULT 'pending',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (username) REFERENCES users (username)
        )
        """)

        cur.execute("""
        CREATE TABLE IF NOT EXISTS team_members (
            id INTEGER GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
            registration_id INTEGER,
            player_number INTEGER,
            game_id TEXT NOT NULL,
            character_name TEXT,
            FOREIGN KEY (registration_id) REFERENCES registrations (id)
        )
        """)

        cur.execute("""
        CREATE TABLE IF NOT EXISTS notifications (
            id INTEGER GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
            type TEXT,
            message TEXT,
            metadata TEXT,
            is_read INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """)

        cur.execute("""
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
            slug TEXT UNIQUE,
            title TEXT NOT NULL,
            mode TEXT,
            date TEXT,
            prize TEXT,
            max_slots INTEGER DEFAULT 0,
            slots_left INTEGER DEFAULT 0,
            region TEXT,
            platform TEXT,
            image TEXT,
            description TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """)
        cur.execute("ALTER TABLE events ADD COLUMN IF NOT EXISTS entry_fee INTEGER DEFAULT 0")
        cur.execute("ALTER TABLE events ADD COLUMN IF NOT EXISTS prize_pool TEXT")
        cur.execute("ALTER TABLE events ADD COLUMN IF NOT EXISTS is_open INTEGER DEFAULT 1")
        cur.execute("ALTER TABLE events ADD COLUMN IF NOT EXISTS start_time TEXT")
        cur.execute("ALTER TABLE events ADD COLUMN IF NOT EXISTS end_time TEXT")
        cur.execute("ALTER TABLE events ADD COLUMN IF NOT EXISTS field_updates TEXT")
        cur.execute("ALTER TABLE events ADD COLUMN IF NOT EXISTS rules TEXT")

        try:
            cur.execute("SELECT COUNT(*) FROM events")
            cnt = cur.fetchone()[0]
            if cnt == 0:
                default_modes = [
                    ('br-solo', 'BR Solo', 'BR Solo', 'Upcoming', '₹1000', 128, 128, 'India', 'Mobile', 'images/freefire.webp', 'Battle Royale Solo'),
                    ('br-duo', 'BR Duo', 'BR Duo', 'Upcoming', '₹2000', 64, 64, 'India', 'Mobile', 'images/freefire.webp', 'Battle Royale Duo'),
                    ('br-squad', 'BR Squad', 'BR Squad', 'Upcoming', '₹3000', 32, 32, 'India', 'Mobile', 'images/freefire.webp', 'Battle Royale Squad'),
                    ('cs-1v1', 'CS 1v1', 'Clash Squad 1v1', 'Upcoming', '₹4000', 64, 64, 'India', 'Mobile', 'images/freefire.webp', 'Clash 1v1'),
                    ('cs-2v2', 'CS 2v2', 'Clash Squad 2v2', 'Upcoming', '₹5000', 32, 32, 'India', 'Mobile', 'images/freefire.webp', 'Clash 2v2'),
                    ('cs-3v3', 'CS 3v3', 'Clash Squad 3v3', 'Upcoming', '₹6000', 24, 24, 'India', 'Mobile', 'images/freefire.webp', 'Clash 3v3'),
                    ('cs-4v4', 'CS 4v4', 'Clash Squad 4v4', 'Upcoming', '₹7000', 16, 16, 'India', 'Mobile', 'images/freefire.webp', 'Clash 4v4'),
                ]
                for dm in default_modes:
                    cur.execute("""
                        INSERT INTO events (slug, title, mode, date, prize, max_slots, slots_left, region, platform, image, description)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, dm)
        except Exception:
            pass

        conn.commit()
        conn.close()
        return

    # Users table
    cur.execute("""
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT NOT NULL UNIQUE,
        email TEXT NOT NULL UNIQUE,
        password TEXT NOT NULL,
        role TEXT NOT NULL,
        game_id TEXT,
        phone TEXT,
        admin_code TEXT
    )
    """)
    # Ensure users have a created_at column for signup timestamps
    try:
        cur.execute("ALTER TABLE users ADD COLUMN created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP")
    except Exception:
        # column may already exist or sqlite limitation; ignore
        pass
    try:
        cur.execute("ALTER TABLE users ADD COLUMN wallet_balance INTEGER DEFAULT 0")
    except Exception:
        pass

    # Registrations table for payment tracking
    cur.execute("""
    CREATE TABLE IF NOT EXISTS registrations (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT NOT NULL,
        email TEXT NOT NULL,
        game_id TEXT NOT NULL,
        phone TEXT NOT NULL,
        payout_upi TEXT,
        mode TEXT NOT NULL,
        team_size INTEGER DEFAULT 1,
        team_name TEXT,
        payment_id TEXT UNIQUE,
        order_id TEXT UNIQUE,
        amount INTEGER NOT NULL,
        status TEXT DEFAULT 'pending',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (username) REFERENCES users (username)
    )
    """)
    # Add refund columns to registrations if missing
    try:
        cur.execute("ALTER TABLE registrations ADD COLUMN refund_requested INTEGER DEFAULT 0")
    except Exception:
        pass
    try:
        cur.execute("ALTER TABLE registrations ADD COLUMN refunded INTEGER DEFAULT 0")
    except Exception:
        pass
    # Add manual payment tracking columns
    try:
        cur.execute("ALTER TABLE registrations ADD COLUMN manual_upi TEXT")
    except Exception:
        pass
    try:
        cur.execute("ALTER TABLE registrations ADD COLUMN manual_payer_name TEXT")
    except Exception:
        pass
    try:
        cur.execute("ALTER TABLE registrations ADD COLUMN manual_txn_id TEXT")
    except Exception:
        pass
    try:
        cur.execute("ALTER TABLE registrations ADD COLUMN manual_paid_at TEXT")
    except Exception:
        pass
    try:
        cur.execute("ALTER TABLE registrations ADD COLUMN paid_at TEXT")
    except Exception:
        pass
    try:
        cur.execute("ALTER TABLE registrations ADD COLUMN payout_upi TEXT")
    except Exception:
        pass

    # Team members table for Clash Squad
    cur.execute("""
    CREATE TABLE IF NOT EXISTS team_members (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        registration_id INTEGER,
        player_number INTEGER,
        game_id TEXT NOT NULL,
        character_name TEXT,
        FOREIGN KEY (registration_id) REFERENCES registrations (id)
    )
    """)

    # Notifications table for admin alerts
    cur.execute("""
    CREATE TABLE IF NOT EXISTS notifications (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        type TEXT,
        message TEXT,
        metadata TEXT,
        is_read INTEGER DEFAULT 0,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS wallet_transactions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT NOT NULL,
        txn_type TEXT NOT NULL,
        amount INTEGER NOT NULL,
        status TEXT DEFAULT 'completed',
        note TEXT,
        withdrawal_request_id INTEGER,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (username) REFERENCES users (username)
    )
    """)
    try:
        cur.execute("ALTER TABLE wallet_transactions ADD COLUMN withdrawal_request_id INTEGER")
    except Exception:
        pass

    cur.execute("""
    CREATE TABLE IF NOT EXISTS wallet_deposit_orders (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT NOT NULL,
        order_id TEXT NOT NULL UNIQUE,
        amount INTEGER NOT NULL,
        status TEXT DEFAULT 'created',
        payment_id TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (username) REFERENCES users (username)
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS wallet_withdrawal_requests (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT NOT NULL,
        amount INTEGER NOT NULL,
        method TEXT NOT NULL,
        holder_name TEXT,
        bank_account_number TEXT,
        ifsc_code TEXT,
        upi_id TEXT,
        mobile_number TEXT,
        status TEXT DEFAULT 'pending',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (username) REFERENCES users (username)
    )
    """)

    # Events table to persist tournaments/modes
    cur.execute("""
    CREATE TABLE IF NOT EXISTS events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        slug TEXT UNIQUE,
        title TEXT NOT NULL,
        mode TEXT,
        date TEXT,
        prize TEXT,
        max_slots INTEGER DEFAULT 0,
        slots_left INTEGER DEFAULT 0,
        region TEXT,
        platform TEXT,
        image TEXT,
        description TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """)
    # Add new columns if they don't exist (best-effort)
    for col_sql in [
        ("entry_fee", "INTEGER DEFAULT 0"),
        ("prize_pool", "TEXT"),
        ("is_open", "INTEGER DEFAULT 1"),
        ("start_time", "TEXT"),
        ("end_time", "TEXT"),
        ("field_updates", "TEXT"),
        ("rules", "TEXT")
    ]:
        col, typ = col_sql
        try:
            cur.execute(f"ALTER TABLE events ADD COLUMN {col} {typ}")
        except Exception:
            pass

    # Backfill: if registrations table missing event_id column, try to add it (safe on most SQLite versions)
    try:
        cur.execute("ALTER TABLE registrations ADD COLUMN event_id INTEGER")
    except Exception:
        # column probably already exists; ignore
        pass

    # Seed default events if table empty
    try:
        cur.execute("SELECT COUNT(*) FROM events")
        cnt = cur.fetchone()[0]
        if cnt == 0:
            default_modes = [
                ('br-solo','BR Solo','BR Solo','Upcoming','₹1000',128,128,'India','Mobile','images/freefire.webp','Battle Royale Solo'),
                ('br-duo','BR Duo','BR Duo','Upcoming','₹2000',64,64,'India','Mobile','images/freefire.webp','Battle Royale Duo'),
                ('br-squad','BR Squad','BR Squad','Upcoming','₹3000',32,32,'India','Mobile','images/freefire.webp','Battle Royale Squad'),
                ('cs-1v1','CS 1v1','Clash Squad 1v1','Upcoming','₹4000',64,64,'India','Mobile','images/freefire.webp','Clash 1v1'),
                ('cs-2v2','CS 2v2','Clash Squad 2v2','Upcoming','₹5000',32,32,'India','Mobile','images/freefire.webp','Clash 2v2'),
                ('cs-3v3','CS 3v3','Clash Squad 3v3','Upcoming','₹6000',24,24,'India','Mobile','images/freefire.webp','Clash 3v3'),
                ('cs-4v4','CS 4v4','Clash Squad 4v4','Upcoming','₹7000',16,16,'India','Mobile','images/freefire.webp','Clash 4v4'),
            ]
            for dm in default_modes:
                cur.execute("""
                    INSERT INTO events (slug, title, mode, date, prize, max_slots, slots_left, region, platform, image, description)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, dm)
    except Exception:
        # ignore seeding errors
        pass

    conn.commit()
    conn.close()


def ensure_bootstrap_admin():
    """Ensure one bootstrap admin exists for first login in fresh deployments."""
    if not BOOTSTRAP_ADMIN_USERNAME or not BOOTSTRAP_ADMIN_PASSWORD or not BOOTSTRAP_ADMIN_CODE:
        return
    conn = None
    try:
        conn = sqlite3.connect(DB_NAME)
        cur = conn.cursor()
        cur.execute('SELECT id, role FROM users WHERE username = ?', (BOOTSTRAP_ADMIN_USERNAME,))
        row = cur.fetchone()
        password_hash = generate_password_hash(BOOTSTRAP_ADMIN_PASSWORD)

        if not row:
            cur.execute(
                """
                INSERT INTO users (username, email, password, role, game_id, phone, admin_code)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    BOOTSTRAP_ADMIN_USERNAME,
                    BOOTSTRAP_ADMIN_EMAIL,
                    password_hash,
                    'admin',
                    None,
                    BOOTSTRAP_ADMIN_PHONE,
                    BOOTSTRAP_ADMIN_CODE,
                )
            )
        else:
            user_id, role = row[0], (row[1] or '')
            cur.execute(
                """
                UPDATE users
                SET email = COALESCE(NULLIF(email, ''), ?),
                    password = ?,
                    role = 'admin',
                    admin_code = ?,
                    phone = COALESCE(NULLIF(phone, ''), ?)
                WHERE id = ?
                """,
                (
                    BOOTSTRAP_ADMIN_EMAIL,
                    password_hash,
                    BOOTSTRAP_ADMIN_CODE,
                    BOOTSTRAP_ADMIN_PHONE,
                    user_id,
                )
            )

        conn.commit()
    except Exception as ex:
        print('Failed to ensure bootstrap admin:', ex)
    finally:
        if conn:
            conn.close()

# ---------- ROUTES ----------
@app.route('/')
def home():
    # If admin, go directly to dashboard
    if session.get('role') == 'admin':
        return redirect(url_for('admin_dashboard'))
    # Regular users or guests see homepage with Free Fire upcoming spotlight
    events = get_events()
    now = _current_local_time()
    register_base = url_for('register_freefire')
    upcoming_events = []
    for event in events:
        match = _event_to_match(event, now=now)
        if match.get('status') != 'upcoming':
            continue
        event_id = event.get('id')
        event_mode = match.get('mode') or event.get('mode') or 'Free Fire'
        prize_value = event.get('prize_pool') or event.get('prize')
        if isinstance(prize_value, (int, float)):
            prize_label = f"₹{int(prize_value):,}" if float(prize_value).is_integer() else f"₹{prize_value}"
        else:
            prize_label = str(prize_value) if prize_value else 'Prize TBA'
        entry_fee_value = event.get('entry_fee')
        if isinstance(entry_fee_value, (int, float)):
            entry_fee_label = f"₹{int(entry_fee_value)}" if float(entry_fee_value).is_integer() else f"₹{entry_fee_value}"
        else:
            entry_fee_label = str(entry_fee_value) if entry_fee_value else 'Free'
        max_slots = event.get('max_slots') or 0
        slots_left = event.get('slots_left')
        if max_slots and slots_left is not None:
            slots_summary = f"{slots_left} slots left"
            capacity_label = f"{max_slots} slots"
        elif max_slots:
            slots_summary = f"{max_slots} slots"
            capacity_label = slots_summary
        else:
            slots_summary = 'Slots TBA'
            capacity_label = 'Slots TBA'
        theme_label = event.get('description') or f"{event_mode} showdown"
        schedule_label = match.get('scheduled_for') or event.get('date') or 'Coming soon'
        query_params = {}
        if event_id:
            query_params['event_id'] = event_id
        if event_mode:
            query_params['mode'] = event_mode
        register_url = register_base
        if query_params:
            register_url = f"{register_base}?{urlencode(query_params)}"
        sort_key = match.get('_start_dt')
        event_date = match.get('_event_date')
        if not sort_key and event_date:
            if isinstance(event_date, datetime):
                sort_key = event_date
            else:
                sort_key = datetime.combine(event_date, datetime.min.time())
        sort_key = sort_key or datetime.max
        upcoming_events.append({
            'id': event_id,
            'title': event.get('title') or 'Free Fire Event',
            'mode': event_mode,
            'schedule_label': schedule_label,
            'prize_label': prize_label,
            'slots_summary': slots_summary,
            'capacity_label': capacity_label,
            'theme_label': theme_label,
            'region': event.get('region') or 'Online',
            'entry_fee_label': entry_fee_label,
            'max_slots': max_slots,
            'slots_left': slots_left,
            'status': match.get('status'),
            'register_url': register_url,
            'sort_key': sort_key
        })
    upcoming_events.sort(key=lambda e: e.get('sort_key') or datetime.max)
    for item in upcoming_events:
        item.pop('sort_key', None)
    spotlight_events = upcoming_events[:4]
    return render_template('index.html', upcoming_events=spotlight_events)


@app.route('/terms-and-conditions')
def terms_and_conditions():
    return render_template('terms_and_conditions.html')


@app.route('/privacy-policy')
def privacy_policy():
    return render_template('privacy_policy.html')


@app.route('/refund-and-cancellation-policy')
def refund_and_cancellation_policy():
    return render_template('refund_cancellation.html')


@app.route('/contact-us')
def contact_us():
    return render_template('contact_us.html')


@app.route('/about-us')
def about_us():
    return render_template('about_us.html')


@app.route('/faq')
def faq_page():
    return render_template('faq.html')


@app.route('/login', methods=['GET', 'POST'])
def login():
    # default to home (index) after login for players
    next_page = request.args.get('next', url_for('home'))
    next_page = request.args.get('next', url_for('home'))
    welcome_user = session.pop('welcome_user', None)
    if request.method == 'POST':
        is_json_request = request.is_json
        payload = request.get_json() if is_json_request else None
        username_or_email = request.form.get('username')
        password = request.form.get('password')
        admin_code_submitted = request.form.get('admin_code')
        # Allow login by username OR email for player role
        conn = sqlite3.connect(DB_NAME)
        cur = conn.cursor()
        # Allow lookup for any role; we'll inspect role after verifying password
        cur.execute("SELECT * FROM users WHERE (username = ? OR email = ?)", (username_or_email, username_or_email))
        user = cur.fetchone()
        conn.close()

        if user and check_password_hash(user[3], password):
            # user[1] is username, user[2] is email, user[4] is role
            role = user[4]
            # If admin, require admin_code validation (preserve existing security)
            if role == 'admin':
                stored_admin_code = user[7] if len(user) > 7 else None
                if not admin_code_submitted or stored_admin_code is None or admin_code_submitted != stored_admin_code:
                    flash('Invalid admin code or missing admin code for admin login.', 'error')
                    return redirect(url_for('login'))
            session['user'] = user[1]
            session['role'] = role
            # also set display name and optional avatar initial
            session['display_name'] = user[1]
            flash(f"Welcome back, {user[1]}!", "success")
            # Prefer a safe internal `next` param if provided, otherwise go to the appropriate dashboard
            next_param = request.form.get('next') or request.args.get('next')
            # If the user is an admin, always send them to admin dashboard regardless of `next`
            if session.get('role') == 'admin':
                return redirect(url_for('admin_dashboard'))
            # For players, allow a safe `next` path if provided, otherwise go to the home page
            if next_param and isinstance(next_param, str) and next_param.startswith('/'):
                return redirect(next_param)
            return redirect(url_for('home'))
        else:
            flash("Invalid player credentials!", "error")

    return render_template('login.html', next=next_page, welcome_user=welcome_user)

# ---------- ADMIN PORTAL ----------
@app.route('/admin_login', methods=['GET', 'POST'])
def admin_login():
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        admin_code = request.form.get('admin_code')
        # Only allow admin role
        conn = sqlite3.connect(DB_NAME)
        cur = conn.cursor()
        cur.execute("SELECT * FROM users WHERE username = ? AND role = 'admin'", (username,))
        user = cur.fetchone()
        conn.close()

        if user and check_password_hash(user[3], password) and (user[7] == admin_code):
            session['user'] = user[1]
            session['role'] = user[4]
            flash(f"Welcome Admin {user[1]}!", "success")
            return redirect(url_for('admin_dashboard'))
        else:
            flash("Invalid admin credentials or code!", "error")
    return render_template('admin_login.html')


@app.route('/signup', methods=['GET', 'POST'])
def signup():
    if request.method == 'POST':
        username = request.form.get('username')
        email = request.form.get('email')
        password = request.form.get('password')
        password2 = request.form.get('password2')
        # Default role to 'player' if form does not include it
        role = request.form.get('role') or 'player'
        game_id = request.form.get('game_id')
        phone = request.form.get('phone')
        admin_code = request.form.get('admin_code')

        if password != password2:
            flash("Passwords do not match!", "error")
            return redirect(url_for('signup'))

        hashed = generate_password_hash(password)

        try:
            # Basic input validation
            if not username or not email or not password:
                flash('Please provide username, email and password.', 'error')
                return redirect(url_for('signup'))

            # Validate email format
            def is_valid_email(e):
                # Simple RFC-like validation (not exhaustive but practical)
                email_re = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")
                return bool(email_re.match(e or ''))

            # Validate phone (Indian 10-digit) or allow blank
            def is_valid_phone(p):
                if not p:
                    return True
                phone_re = re.compile(r"^[0-9]{10}$")
                return bool(phone_re.match(p))

            if not is_valid_email(email):
                flash('Please provide a valid email address.', 'error')
                return redirect(url_for('signup'))

            if not is_valid_phone(phone):
                flash('Please provide a valid 10-digit phone number (numbers only).', 'error')
                return redirect(url_for('signup'))

            conn = sqlite3.connect(DB_NAME)
            cur = conn.cursor()
            # Check uniqueness first to provide a friendly error
            cur.execute('SELECT id FROM users WHERE username = ? OR email = ?', (username, email))
            existing = cur.fetchone()
            if existing:
                conn.close()
                flash('Username or email already exists. Try logging in or choose a different username/email.', 'error')
                return redirect(url_for('signup'))

            cur.execute("""
                INSERT INTO users (username, email, password, role, game_id, phone, admin_code)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (username, email, hashed, role, game_id, phone, admin_code))
            conn.commit()
            # Create an admin notification so admins see new signups
            try:
                metadata = json.dumps({'username': username, 'email': email, 'role': role})
                msg = f"New user signup: {username}"
                cur.execute("INSERT INTO notifications (type, message, metadata) VALUES (?, ?, ?)", ('signup', msg, metadata))
                conn.commit()
            except Exception as e:
                print('Failed to create signup notification:', e)
            finally:
                conn.close()

            # For admin accounts, keep auto-login and send to admin dashboard
            if role == 'admin':
                session['user'] = username
                session['role'] = role
                flash("Account created successfully!", "success")
                return redirect(url_for('admin_dashboard'))

            # For player accounts, do not auto-login — send them to login page and show a welcome message there
            session['welcome_user'] = username
            flash("Account created successfully! Please log in to continue.", "success")
            return redirect(url_for('login'))
        except Exception as e:
            # Catch-all: log error and show friendly message
            print('Signup error:', e)
            flash('Failed to create account. Please try again later.', 'error')
            return redirect(url_for('signup'))
    return render_template('signup.html')


@app.route('/signup_ajax', methods=['POST'])
def signup_ajax():
    """AJAX endpoint for signup. Returns JSON with success and message or redirect URL."""
    data = {}
    # support JSON or form data
    if request.is_json:
        data = request.get_json()
    else:
        # pull from form
        data = request.form.to_dict()

    username = data.get('username')
    email = data.get('email')
    password = data.get('password')
    password2 = data.get('password2')
    role = data.get('role') or 'player'
    game_id = data.get('game_id')
    phone = data.get('phone')
    admin_code = data.get('admin_code')

    # basic checks
    if not username or not email or not password:
        return jsonify({'success': False, 'error': 'Please provide username, email and password.'}), 400
    if password != password2:
        return jsonify({'success': False, 'error': 'Passwords do not match.'}), 400

    # reuse same validators as main signup
    def is_valid_email(e):
        email_re = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")
        return bool(email_re.match(e or ''))
    def is_valid_phone(p):
        if not p:
            return True
        phone_re = re.compile(r"^[0-9]{10}$")
        return bool(phone_re.match(p))

    if not is_valid_email(email):
        return jsonify({'success': False, 'error': 'Please provide a valid email address.'}), 400
    if not is_valid_phone(phone):
        return jsonify({'success': False, 'error': 'Please provide a valid 10-digit phone number (numbers only).'}), 400

    hashed = generate_password_hash(password)
    try:
        conn = sqlite3.connect(DB_NAME)
        cur = conn.cursor()
        cur.execute('SELECT id FROM users WHERE username = ? OR email = ?', (username, email))
        existing = cur.fetchone()
        if existing:
            conn.close()
            return jsonify({'success': False, 'error': 'Username or email already exists.'}), 400

        cur.execute('''
            INSERT INTO users (username, email, password, role, game_id, phone, admin_code)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        ''', (username, email, hashed, role, game_id, phone, admin_code))
        conn.commit()
        # notification
        try:
            metadata = json.dumps({'username': username, 'email': email, 'role': role})
            msg = f"New user signup: {username}"
            cur.execute("INSERT INTO notifications (type, message, metadata) VALUES (?, ?, ?)", ('signup', msg, metadata))
            conn.commit()
        except Exception:
            pass
        conn.close()

        # decide redirect target
        if role == 'admin':
            redirect_to = url_for('admin_dashboard')
        else:
            redirect_to = url_for('home')

        return jsonify({'success': True, 'redirect': redirect_to, 'message': 'Account created successfully!'}), 201
    except Exception as e:
        print('signup_ajax error:', e)
        return jsonify({'success': False, 'error': 'Failed to create account. Please try again later.'}), 500

# ---------- PLAYER ACCOUNT PAGE ----------
@app.route('/account')
def player_account():
    if 'user' not in session or session.get('role') != 'player':
        flash("Please log in as a player.", "error")
        return redirect(url_for('login'))
    username = session.get('user')
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute("SELECT username, email, game_id, phone, COALESCE(wallet_balance, 0) FROM users WHERE username = ?", (username,))
    user = cur.fetchone()
    wallet_balance_paise = int(user[4] or 0) if user and len(user) > 4 else _wallet_user_balance_paise(cur, username)
    wallet_transactions = _wallet_load_recent_transactions(cur, username)
    conn.close()
    return render_template(
        'player_account.html',
        user=user,
        wallet_balance_paise=wallet_balance_paise,
        wallet_balance_label=_format_currency_value(wallet_balance_paise),
        wallet_transactions=wallet_transactions,
        wallet_min_deposit=WALLET_MIN_DEPOSIT_RUPEES,
        wallet_max_deposit=WALLET_MAX_DEPOSIT_RUPEES,
        wallet_min_withdraw=WALLET_MIN_WITHDRAW_RUPEES,
        wallet_max_withdraw=WALLET_MAX_WITHDRAW_RUPEES
    )


@app.route('/wallet')
def wallet_page():
    if 'user' not in session or session.get('role') != 'player':
        flash("Please log in as a player.", "error")
        return redirect(url_for('login'))

    username = session.get('user')
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute("SELECT username, email, game_id, phone, COALESCE(wallet_balance, 0) FROM users WHERE username = ?", (username,))
    user = cur.fetchone()
    wallet_balance_paise = int(user[4] or 0) if user and len(user) > 4 else _wallet_user_balance_paise(cur, username)
    wallet_transactions = _wallet_load_recent_transactions(cur, username)

    try:
        cur.execute(
            'SELECT id, amount, method, holder_name, bank_account_number, ifsc_code, upi_id, mobile_number, status, created_at FROM wallet_withdrawal_requests WHERE username = ? ORDER BY id DESC LIMIT 12',
            (username,)
        )
        w_rows = cur.fetchall()
    except Exception:
        w_rows = []

    withdrawals = []
    for row in w_rows:
        withdrawals.append({
            'id': row[0],
            'amount_label': _format_currency_value(row[1]),
            'method': row[2] or '-',
            'holder_name': row[3] or '-',
            'bank_account_number': row[4] or '',
            'ifsc_code': row[5] or '',
            'upi_id': row[6] or '',
            'mobile_number': row[7] or '',
            'status': row[8] or 'pending',
            'created_at': row[9] or '-'
        })

    conn.close()
    return render_template(
        'wallet.html',
        user=user,
        wallet_balance_paise=wallet_balance_paise,
        wallet_balance_label=_format_currency_value(wallet_balance_paise),
        wallet_transactions=wallet_transactions,
        withdrawals=withdrawals,
        wallet_min_deposit=WALLET_MIN_DEPOSIT_RUPEES,
        wallet_max_deposit=WALLET_MAX_DEPOSIT_RUPEES,
        wallet_min_withdraw=WALLET_MIN_WITHDRAW_RUPEES,
        wallet_max_withdraw=WALLET_MAX_WITHDRAW_RUPEES,
        cashfree_env=CASHFREE_ENV
    )


@app.route('/wallet/deposit', methods=['POST'])
def wallet_deposit():
    if 'user' not in session or session.get('role') != 'player':
        return jsonify({'success': False, 'error': 'Please log in as a player.'}), 403

    if not is_cashfree_ready():
        return jsonify({'success': False, 'error': 'Cashfree is not configured.'}), 503

    payload = request.get_json(silent=True) or request.form
    amount_rupees = _wallet_amount_from_request(payload.get('amount'))
    if amount_rupees is None:
        return jsonify({'success': False, 'error': 'Enter a valid deposit amount.'}), 400
    if amount_rupees < WALLET_MIN_DEPOSIT_RUPEES or amount_rupees > WALLET_MAX_DEPOSIT_RUPEES:
        return jsonify({'success': False, 'error': f'Deposit must be between ₹{WALLET_MIN_DEPOSIT_RUPEES} and ₹{WALLET_MAX_DEPOSIT_RUPEES}.'}), 400

    username = session.get('user')
    amount_paise = _wallet_rupees_to_paise(amount_rupees)

    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute('SELECT email, phone FROM users WHERE username = ?', (username,))
    user_row = cur.fetchone() or ('', '')
    conn.close()

    customer_email = str(user_row[0] or '').strip() or 'player@battlex.local'
    customer_phone = str(user_row[1] or '').strip()
    if not _is_valid_mobile(customer_phone):
        customer_phone = '9999999999'

    cf_order_id = f"WLT{int(datetime.utcnow().timestamp())}{secrets.randbelow(1000):03d}"
    callback_url = url_for('wallet_cashfree_callback', _external=True)
    return_url = f"{callback_url}?order_id={{order_id}}"

    order_payload = {
        'order_id': cf_order_id,
        'order_amount': float(amount_rupees),
        'order_currency': 'INR',
        'customer_details': {
            'customer_id': f"wallet_{username}",
            'customer_name': username,
            'customer_email': customer_email,
            'customer_phone': customer_phone
        },
        'order_meta': {
            'return_url': return_url
        },
        'order_note': 'BATTLE-X wallet top-up'
    }

    try:
        resp = requests.post(
            f"{CASHFREE_API_BASE}/orders",
            headers=_cashfree_headers(),
            json=order_payload,
            timeout=25
        )
    except Exception as ex:
        return jsonify({'success': False, 'error': f'Cashfree order creation failed: {ex}'}), 502

    try:
        order_data = resp.json()
    except Exception:
        order_data = {}

    if resp.status_code >= 400:
        return jsonify({'success': False, 'error': order_data.get('message') or 'Unable to create wallet deposit order'}), 400

    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    try:
        cur.execute(
            'INSERT INTO wallet_deposit_orders (username, order_id, amount, status) VALUES (?, ?, ?, ?)',
            (username, cf_order_id, amount_paise, 'created')
        )
        conn.commit()
    except Exception:
        conn.rollback()
        conn.close()
        return jsonify({'success': False, 'error': 'Could not initialize wallet deposit order.'}), 500
    conn.close()

    return jsonify({
        'success': True,
        'gateway': 'cashfree',
        'environment': CASHFREE_ENV,
        'order_id': cf_order_id,
        'amount': amount_paise,
        'currency': 'INR',
        'payment_session_id': order_data.get('payment_session_id'),
        'payment_link': order_data.get('payment_link') or (order_data.get('order_meta') or {}).get('payment_link'),
        'return_url': return_url
    })


@app.route('/wallet/cashfree/callback')
def wallet_cashfree_callback():
    if 'user' not in session or session.get('role') != 'player':
        flash('Please log in as player to verify wallet deposit.', 'error')
        return redirect(url_for('login'))

    order_id = (request.args.get('order_id') or request.args.get('cf_order_id') or '').strip()
    if not order_id:
        flash('Missing Cashfree order id for wallet deposit.', 'error')
        return redirect(url_for('wallet_page'))

    try:
        resp = requests.get(
            f"{CASHFREE_API_BASE}/orders/{order_id}",
            headers=_cashfree_headers(),
            timeout=25
        )
        payload = resp.json() if resp.content else {}
    except Exception as ex:
        flash(f'Unable to verify wallet payment: {ex}', 'error')
        return redirect(url_for('wallet_page'))

    if resp.status_code >= 400:
        flash(payload.get('message') or 'Wallet payment verification failed.', 'error')
        return redirect(url_for('wallet_page'))

    order_status = str(payload.get('order_status') or '').upper()
    if order_status != 'PAID':
        flash(f'Wallet deposit not completed yet (status: {order_status or "UNKNOWN"}).', 'error')
        return redirect(url_for('wallet_page'))

    payment_id = f"CASHFREE-{order_id}"
    username = session.get('user')
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    try:
        cur.execute('SELECT id, username, amount, status FROM wallet_deposit_orders WHERE order_id = ?', (order_id,))
        row = cur.fetchone()
        if not row:
            conn.close()
            flash('Wallet deposit order not found.', 'error')
            return redirect(url_for('wallet_page'))

        deposit_id, owner, amount_paise, status = row[0], row[1], int(row[2] or 0), str(row[3] or '')
        if owner != username:
            conn.close()
            flash('This wallet deposit belongs to another account.', 'error')
            return redirect(url_for('wallet_page'))

        if status == 'completed':
            conn.close()
            flash('Wallet deposit already credited.', 'info')
            return redirect(url_for('wallet_page'))

        cur.execute('UPDATE users SET wallet_balance = COALESCE(wallet_balance, 0) + ? WHERE username = ?', (amount_paise, username))
        cur.execute(
            'INSERT INTO wallet_transactions (username, txn_type, amount, status, note) VALUES (?, ?, ?, ?, ?)',
            (username, 'deposit', amount_paise, 'completed', f'Cashfree top-up {order_id}')
        )
        cur.execute(
            'UPDATE wallet_deposit_orders SET status = ?, payment_id = ?, updated_at = ? WHERE id = ?',
            ('completed', payment_id, datetime.utcnow().isoformat(), deposit_id)
        )
        conn.commit()
        flash(f'Wallet credited successfully: {_format_currency_value(amount_paise)}', 'success')
    except Exception:
        conn.rollback()
        flash('Could not credit wallet for this payment.', 'error')
    finally:
        conn.close()
    return redirect(url_for('wallet_page'))


@app.route('/wallet/withdraw', methods=['POST'])
def wallet_withdraw():
    if 'user' not in session or session.get('role') != 'player':
        flash("Please log in as a player.", "error")
        return redirect(url_for('login'))

    amount_rupees = _wallet_amount_from_request(request.form.get('amount'))
    if amount_rupees is None:
        flash('Enter a valid withdrawal amount.', 'error')
        return redirect(url_for('wallet_page'))
    if amount_rupees < WALLET_MIN_WITHDRAW_RUPEES or amount_rupees > WALLET_MAX_WITHDRAW_RUPEES:
        flash(f'Withdrawal must be between ₹{WALLET_MIN_WITHDRAW_RUPEES} and ₹{WALLET_MAX_WITHDRAW_RUPEES}.', 'error')
        return redirect(url_for('wallet_page'))

    method = (request.form.get('method') or '').strip().lower()
    holder_name = (request.form.get('holder_name') or '').strip()

    if method not in ('bank', 'upi'):
        flash('Select a valid withdrawal method.', 'error')
        return redirect(url_for('wallet_page'))
    if not holder_name:
        flash('Holder name is required for withdrawal.', 'error')
        return redirect(url_for('wallet_page'))

    bank_account_number = ''
    ifsc_code = ''
    upi_id = ''
    mobile_number = ''

    if method == 'bank':
        bank_account_number = (request.form.get('bank_account_number') or '').strip()
        ifsc_code = (request.form.get('ifsc_code') or '').strip().upper()
        if not _is_valid_bank_account(bank_account_number):
            flash('Enter a valid bank account number (9 to 18 digits).', 'error')
            return redirect(url_for('wallet_page'))
        if not _is_valid_ifsc(ifsc_code):
            flash('Enter a valid IFSC code.', 'error')
            return redirect(url_for('wallet_page'))
    else:
        upi_id = (request.form.get('upi_id') or '').strip().lower()
        mobile_number = (request.form.get('mobile_number') or '').strip()
        if not _is_valid_mobile(mobile_number):
            flash('Enter a valid 10-digit mobile number.', 'error')
            return redirect(url_for('wallet_page'))
        if not _is_valid_upi(upi_id):
            flash('Enter a valid UPI ID.', 'error')
            return redirect(url_for('wallet_page'))

    username = session.get('user')
    amount_paise = _wallet_rupees_to_paise(amount_rupees)
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    try:
        cur.execute(
            'UPDATE users SET wallet_balance = COALESCE(wallet_balance, 0) - ? WHERE username = ? AND COALESCE(wallet_balance, 0) >= ?',
            (amount_paise, username, amount_paise)
        )
        if cur.rowcount == 0:
            conn.rollback()
            flash('Insufficient wallet balance.', 'error')
            return redirect(url_for('wallet_page'))

        cur.execute(
            '''INSERT INTO wallet_withdrawal_requests
               (username, amount, method, holder_name, bank_account_number, ifsc_code, upi_id, mobile_number, status)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)''',
            (username, amount_paise, method, holder_name, bank_account_number, ifsc_code, upi_id, mobile_number, 'pending')
        )
        withdrawal_request_id = cur.lastrowid
        cur.execute(
            'INSERT INTO wallet_transactions (username, txn_type, amount, status, note, withdrawal_request_id) VALUES (?, ?, ?, ?, ?, ?)',
            (username, 'withdrawal', amount_paise, 'pending', f'Withdrawal request #{withdrawal_request_id} via {method.upper()}', withdrawal_request_id)
        )
        conn.commit()
        flash(f'Withdrawal request submitted: ₹{amount_rupees}.', 'success')
    except Exception:
        conn.rollback()
        flash('Could not submit withdrawal request. Please try again.', 'error')
    finally:
        conn.close()
    return redirect(url_for('wallet_page'))


@app.route('/settings', methods=['GET', 'POST'])
def player_settings():
    if 'user' not in session or session.get('role') != 'player':
        flash("Please log in as a player.", "error")
        return redirect(url_for('login'))

    username = session.get('user')
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()

    if request.method == 'POST':
        email = (request.form.get('email') or '').strip()
        game_id = (request.form.get('game_id') or '').strip()
        phone = (request.form.get('phone') or '').strip()

        if email:
            email_re = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")
            if not email_re.match(email):
                conn.close()
                flash('Please provide a valid email address.', 'error')
                return redirect(url_for('player_settings'))

        if phone:
            phone_re = re.compile(r"^[0-9]{10}$")
            if not phone_re.match(phone):
                conn.close()
                flash('Please provide a valid 10-digit phone number.', 'error')
                return redirect(url_for('player_settings'))

        try:
            cur.execute(
                "UPDATE users SET email = ?, game_id = ?, phone = ? WHERE username = ?",
                (email, game_id, phone, username)
            )
            conn.commit()
            flash('Settings updated successfully.', 'success')
        except sqlite3.IntegrityError:
            flash('That email is already in use by another account.', 'error')
        except Exception:
            flash('Unable to update settings right now. Please try again.', 'error')

    cur.execute("SELECT username, email, game_id, phone FROM users WHERE username = ?", (username,))
    user = cur.fetchone()
    conn.close()
    return render_template('settings.html', user=user)


@app.route('/history')
def player_history():
    if 'user' not in session or session.get('role') != 'player':
        flash("Please log in as a player.", "error")
        return redirect(url_for('login'))

    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute(
        """
        SELECT r.id, r.mode, r.team_size, r.team_name, r.amount, r.status, r.created_at,
               r.payment_id, r.order_id, e.title
        FROM registrations r
        LEFT JOIN events e ON e.id = r.event_id
        WHERE r.username = ?
        ORDER BY r.id DESC
        """,
        (session.get('user'),)
    )
    rows = cur.fetchall()
    conn.close()

    history_rows = []
    for r in rows:
        amount_raw = r[4]
        amount_label = 'N/A'
        try:
            amount_num = int(float(amount_raw))
            if amount_num >= 1000:
                amount_label = f"₹{amount_num / 100:.2f}"
            else:
                amount_label = f"₹{amount_num}"
        except Exception:
            if amount_raw is not None:
                amount_label = str(amount_raw)

        history_rows.append({
            'id': r[0],
            'mode': r[1] or 'N/A',
            'team_size': r[2] or 1,
            'team_name': r[3] or '-',
            'amount_label': amount_label,
            'status': r[5] or 'pending',
            'created_at': r[6] or '-',
            'payment_id': r[7] or '-',
            'order_id': r[8] or '-',
            'event_title': r[9] or 'Free Fire Event',
            'payment_page_url': url_for('payment_page', reg_id=r[0]),
            'can_pay_now': (str(r[5] or 'pending').lower() in {'pending', 'manual', 'processing', 'failed', 'cancelled'})
        })

    return render_template('history.html', history_rows=history_rows)

@app.route('/players')
def players():
    if 'user' not in session or session.get('role') != 'player':
        flash("Players only: Please log in as a player.", "error")
        return redirect(url_for('login'))

    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute("SELECT username, role, game_id, phone FROM users")
    all_players = cur.fetchall()
    conn.close()

    return render_template('players.html', players=all_players, user=session.get('user'), role=session.get('role'))

@app.route('/freefire')
def freefire():
    if 'user' not in session or session.get('role') != 'player':
        flash("Players only: Please log in as a player.", "error")
        return redirect(url_for('login'))
    events = get_events()
    return render_template('freefire.html', events=events, session_user=session.get('user'))

# ---------- CASHFREE PAYMENT ROUTES ----------

def _finalize_registration_payment(order_id, payment_id, reg_id=None):
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()

    reg_row = None
    if reg_id:
        cur.execute('SELECT id, event_id, status FROM registrations WHERE id = ?', (int(reg_id),))
        reg_row = cur.fetchone()
    if not reg_row and order_id:
        cur.execute('SELECT id, event_id, status FROM registrations WHERE order_id = ?', (order_id,))
        reg_row = cur.fetchone()

    if not reg_row:
        conn.close()
        return False, {'success': False, 'error': 'Registration not found for payment'}, 404

    reg_pk, reg_event_id, current_status = reg_row[0], reg_row[1], str(reg_row[2] or '').lower()

    # Only decrement slots once, when transitioning into completed.
    if current_status != 'completed' and reg_event_id:
        try:
            cur.execute('BEGIN IMMEDIATE')
            cur.execute('SELECT slots_left, is_open FROM events WHERE id = ?', (reg_event_id,))
            er = cur.fetchone()
            if not er:
                conn.rollback()
                conn.close()
                return False, {'success': False, 'error': 'Event missing while finalizing payment'}, 400

            slots_left, is_open = er[0], er[1]
            if is_open is not None and not bool(is_open):
                conn.rollback()
                conn.close()
                return False, {'success': False, 'error': 'Event closed during payment'}, 400
            if slots_left is not None and slots_left <= 0:
                conn.rollback()
                conn.close()
                return False, {'success': False, 'error': 'Event is full now'}, 400

            cur.execute('UPDATE events SET slots_left = slots_left - 1 WHERE id = ?', (reg_event_id,))
            cur.execute('SELECT slots_left FROM events WHERE id = ?', (reg_event_id,))
            if (cur.fetchone() or [0])[0] <= 0:
                cur.execute('UPDATE events SET is_open = 0 WHERE id = ?', (reg_event_id,))
        except Exception:
            try:
                conn.rollback()
            except Exception:
                pass

    # Some existing DBs may not have paid_at; fallback gracefully.
    try:
        cur.execute(
            'UPDATE registrations SET payment_id = ?, status = "completed", order_id = ?, paid_at = ? WHERE id = ?',
            (payment_id, order_id, datetime.utcnow().isoformat(), reg_pk)
        )
    except Exception:
        cur.execute(
            'UPDATE registrations SET payment_id = ?, status = "completed", order_id = ? WHERE id = ?',
            (payment_id, order_id, reg_pk)
        )
    conn.commit()
    conn.close()

    return True, {
        'success': True,
        'message': 'Payment verified and registration completed successfully!',
        'payment_id': payment_id
    }, 200


def _verify_cashfree_and_finalize(order_id, reg_id=None):
    if not is_cashfree_ready():
        return False, {'success': False, 'error': 'Cashfree is not configured.'}, 503
    if not order_id:
        return False, {'success': False, 'error': 'Missing Cashfree order id.'}, 400

    try:
        resp = requests.get(
            f"{CASHFREE_API_BASE}/orders/{order_id}",
            headers=_cashfree_headers(),
            timeout=25
        )
    except Exception as ex:
        return False, {'success': False, 'error': f'Cashfree status check failed: {ex}'}, 502

    try:
        payload = resp.json()
    except Exception:
        payload = {}

    if resp.status_code >= 400:
        return False, {'success': False, 'error': payload.get('message') or 'Unable to verify payment status'}, 400

    order_status = str(payload.get('order_status') or '').upper()
    if order_status != 'PAID':
        return False, {'success': False, 'error': f'Payment not completed yet (status: {order_status or "UNKNOWN"})'}, 400

    payment_id = f"CASHFREE-{order_id}"
    return _finalize_registration_payment(order_id=order_id, payment_id=payment_id, reg_id=reg_id)

@app.route('/create_payment_order', methods=['POST'])
def create_payment_order():
    """Create Cashfree order/session for payment."""
    try:
        if not is_cashfree_ready():
            return jsonify({'success': False, 'error': 'Cashfree is not configured. Set CASHFREE_APP_ID and CASHFREE_SECRET_KEY.'}), 503

        data = request.json or {}
        reg_id = data.get('registration_id')
        event_id = data.get('event_id')
        amount = 10000  # default ₹100 in paise
        customer_email = (data.get('email') or '').strip()
        customer_phone = ''
        customer_name = (session.get('user') or 'Player').strip()

        # Helper to parse stored date strings for open/close checks
        def parse_dt(val):
            if not val:
                return None
            try:
                return datetime.fromisoformat(val)
            except Exception:
                for fmt in ('%Y-%m-%d %H:%M:%S', '%Y-%m-%d'):
                    try:
                        return datetime.strptime(val, fmt)
                    except Exception:
                        continue
            return None

        # If a registration id was provided, pull its event and amount
        reg_event_id = None
        if reg_id:
            try:
                conn = sqlite3.connect(DB_NAME)
                cur = conn.cursor()
                cur.execute('SELECT event_id, amount, email, phone, username FROM registrations WHERE id = ?', (int(reg_id),))
                rrow = cur.fetchone()
                conn.close()
                if rrow:
                    reg_event_id = rrow[0]
                    # amount in paise stored on registration; fall back to default if missing/zero
                    try:
                        if rrow[1] and int(rrow[1]) > 0:
                            amount = int(rrow[1])
                    except Exception:
                        pass
                    customer_email = customer_email or (rrow[2] or '')
                    customer_phone = (rrow[3] or '').strip()
                    customer_name = (rrow[4] or customer_name).strip()
            except Exception as e:
                return jsonify({'success': False, 'error': f'Could not load registration: {e}'}), 400

        # If event id not explicitly provided, reuse from registration
        if not event_id and reg_event_id:
            event_id = reg_event_id

        # If an event_id is provided, validate and pick its entry_fee
        # If reg_id exists, allow a bit more leniency (honor existing pending slot) while still blocking full events.
        if event_id:
            try:
                conn = sqlite3.connect(DB_NAME)
                cur = conn.cursor()
                cur.execute('SELECT slots_left, is_open, entry_fee, start_time, end_time FROM events WHERE id = ?', (int(event_id),))
                erow = cur.fetchone()
                if not erow:
                    conn.close()
                    return jsonify({'success': False, 'error': 'Event not found'})

                slots_left, is_open, entry_fee, start_time, end_time = erow[0], erow[1], erow[2], erow[3], erow[4]
                now = datetime.utcnow()
                st = parse_dt(start_time)
                et = parse_dt(end_time)

                # Only enforce open window strictly for new checkouts (no reg_id)
                if not reg_id:
                    if st and now < st:
                        conn.close()
                        return jsonify({'success': False, 'error': f'Registration not open yet (opens {st})'})
                    if et and now >= et:
                        try:
                            cur.execute('UPDATE events SET is_open = 0 WHERE id = ?', (int(event_id),))
                            conn.commit()
                        except Exception:
                            pass
                        conn.close()
                        return jsonify({'success': False, 'error': 'Event is closed'})
                    if is_open is not None and not bool(is_open):
                        conn.close()
                        return jsonify({'success': False, 'error': 'Event is closed for registrations'})

                # Always block if slots are exhausted
                if slots_left is not None and slots_left <= 0:
                    try:
                        cur.execute('UPDATE events SET is_open = 0 WHERE id = ?', (int(event_id),))
                        conn.commit()
                    except Exception:
                        pass
                    conn.close()
                    return jsonify({'success': False, 'error': 'Event is full'})

                # amount in rupees -> paise
                try:
                    if entry_fee is not None:
                        amount = max(int(entry_fee), 0) * 100
                except Exception:
                    pass
                conn.close()
            except Exception as e:
                return jsonify({'success': False, 'error': str(e)})

        # Cashfree requires amount in INR rupees.
        if amount is None or amount <= 0:
            amount = 100
        order_amount = round(float(amount) / 100.0, 2)
        if order_amount <= 0:
            order_amount = 1.0

        if not customer_email:
            customer_email = 'player@battlex.local'
        if not re.match(r'^[0-9]{10}$', str(customer_phone or '')):
            customer_phone = '9999999999'

        if reg_id:
            cf_order_id = f"BXR{int(reg_id)}{int(datetime.utcnow().timestamp())}"
        else:
            cf_order_id = f"BX{int(datetime.utcnow().timestamp())}{secrets.randbelow(1000):03d}"

        callback_url = url_for('cashfree_payment_callback', _external=True)
        registration_part = str(reg_id) if reg_id else ''
        return_url = f"{callback_url}?registration_id={registration_part}&order_id={{order_id}}"

        order_payload = {
            'order_id': cf_order_id,
            'order_amount': order_amount,
            'order_currency': 'INR',
            'customer_details': {
                'customer_id': f"player_{(session.get('user') or 'guest')}_{registration_part or 'new'}",
                'customer_name': customer_name or 'Player',
                'customer_email': customer_email,
                'customer_phone': customer_phone
            },
            'order_meta': {
                'return_url': return_url
            },
            'order_note': f"BATTLE-X {data.get('mode') or 'registration'}"
        }

        resp = requests.post(
            f"{CASHFREE_API_BASE}/orders",
            headers=_cashfree_headers(),
            json=order_payload,
            timeout=25
        )
        try:
            order = resp.json()
        except Exception:
            order = {}
        if resp.status_code >= 400:
            return jsonify({'success': False, 'error': order.get('message') or 'Unable to create Cashfree order'}), 400

        # If a registration_id was provided, attach the order and persist the amount used
        if reg_id:
            try:
                conn = sqlite3.connect(DB_NAME)
                cur = conn.cursor()
                cur.execute('UPDATE registrations SET order_id = ?, amount = ? WHERE id = ?', (cf_order_id, amount, int(reg_id)))
                conn.commit()
                conn.close()
            except Exception:
                pass

        return jsonify({
            'success': True,
            'gateway': 'cashfree',
            'environment': CASHFREE_ENV,
            'order_id': cf_order_id,
            'amount': amount,
            'currency': 'INR',
            'payment_session_id': order.get('payment_session_id'),
            'payment_link': order.get('payment_link') or (order.get('order_meta') or {}).get('payment_link'),
            'return_url': return_url
        })

    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/verify_payment', methods=['POST'])
def verify_payment():
    """Verify Cashfree payment status and finalize registration."""
    try:
        data = request.json or {}
        reg_id = data.get('registration_id')
        order_id = data.get('cashfree_order_id') or data.get('order_id')
        ok, payload, code = _verify_cashfree_and_finalize(order_id=order_id, reg_id=reg_id)
        return jsonify(payload), code
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/payment/cashfree/callback')
def cashfree_payment_callback():
    """Cashfree return URL callback; verifies status and completes registration."""
    reg_id = request.args.get('registration_id')
    order_id = request.args.get('order_id') or request.args.get('cf_order_id')
    ok, payload, _ = _verify_cashfree_and_finalize(order_id=order_id, reg_id=reg_id)
    if ok:
        return redirect(url_for('payment_success', payment_id=payload.get('payment_id')))

    if reg_id:
        flash(payload.get('error') or 'Payment is not completed yet. Please try again.', 'error')
        return redirect(url_for('payment_page', reg_id=int(reg_id)))
    flash(payload.get('error') or 'Payment verification failed. Please try again.', 'error')
    return redirect(url_for('player_history'))

def save_registration(data, payment_id, order_id):
    """Save registration data to database"""
    try:
        data = data or {}
        conn = sqlite3.connect(DB_NAME)
        cur = conn.cursor()

        mode = data.get('mode')
        amount = 10000  # ₹100 in paise
        contact_phone = (data.get('team_phone') or data.get('phone') or '').strip()
        payout_upi = (data.get('payout_upi') or '').strip().lower()

        # If an event_id was provided, ensure event is open, slots are available and decrement atomically.
        event_id = data.get('event_id')
        if event_id:
            try:
                event_id = int(event_id)
                # Start a transaction and obtain a write lock to avoid race conditions
                # BEGIN IMMEDIATE requests a reserved write lock in SQLite
                cur.execute('BEGIN IMMEDIATE')
                cur.execute('SELECT slots_left, max_slots, is_open, start_time, end_time FROM events WHERE id = ?', (event_id,))
                row = cur.fetchone()
                # SQLite doesn't support FOR UPDATE; fallback handle: re-query and enforce
                if not row:
                    # event not found
                    conn.rollback()
                    conn.close()
                    print('Event not found during registration')
                    return False
                slots_left = row[0]
                is_open = row[2] if len(row) > 2 else 1
                start_time = row[3] if len(row) > 3 else None
                end_time = row[4] if len(row) > 4 else None
                # check timing
                now = datetime.utcnow()
                def parse_dt(val):
                    if not val:
                        return None
                    try:
                        return datetime.fromisoformat(val)
                    except Exception:
                        for fmt in ('%Y-%m-%d %H:%M:%S', '%Y-%m-%d'):
                            try:
                                return datetime.strptime(val, fmt)
                            except Exception:
                                continue
                    return None
                st = parse_dt(start_time)
                et = parse_dt(end_time)
                if st and now < st:
                    conn.rollback()
                    conn.close()
                    print('Event not yet open')
                    return False
                if et and now >= et:
                    try:
                        cur.execute('UPDATE events SET is_open = 0 WHERE id = ?', (event_id,))
                        conn.commit()
                    except Exception:
                        pass
                    conn.rollback()
                    conn.close()
                    print('Event closed by time')
                    return False
                if not is_open:
                    conn.rollback()
                    conn.close()
                    print('Event is closed for registrations')
                    return False
                if slots_left <= 0:
                    # mark closed defensively
                    try:
                        cur.execute('UPDATE events SET is_open = 0 WHERE id = ?', (event_id,))
                        conn.commit()
                    except Exception:
                        pass
                    conn.rollback()
                    conn.close()
                    print('No slots left for event')
                    return False
                # decrement slots
                cur.execute('UPDATE events SET slots_left = slots_left - 1 WHERE id = ?', (event_id,))
                # if this update made slots_left 0, also set is_open to 0
                cur.execute('SELECT slots_left FROM events WHERE id = ?', (event_id,))
                new_slots = cur.fetchone()[0]
                if new_slots <= 0:
                    try:
                        cur.execute('UPDATE events SET is_open = 0 WHERE id = ?', (event_id,))
                    except Exception:
                        pass
            except Exception:
                # In case of any error, rollback and continue
                try:
                    conn.rollback()
                except Exception:
                    pass

        if mode == 'BR':
            # Battle Royale registration; support duo/squad grouping by team_size
            br_team_size = int(data.get('team_size', data.get('br_team_size', 1)))
            cur.execute("""
                INSERT INTO registrations 
                (username, email, game_id, phone, payout_upi, mode, team_size, payment_id, order_id, amount, status, event_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'completed', ?)
            """, (
                session.get('user'),
                data.get('email'),
                data.get('game_id'),
                contact_phone,
                payout_upi,
                mode,
                br_team_size,
                payment_id,
                order_id,
                amount,
                event_id
            ))
            registration_id = cur.lastrowid
            # Save additional BR players if provided (br_player_2_id, br_player_2_name, etc.)
            for i in range(2, br_team_size+1):
                player_id = data.get(f'br_player_{i}_id')
                character_name = data.get(f'br_player_{i}_name','')
                if player_id:
                    cur.execute("""
                        INSERT INTO team_members (registration_id, player_number, game_id, character_name)
                        VALUES (?, ?, ?, ?)
                    """, (registration_id, i, player_id, character_name))
        elif mode == 'CS':
            # Clash Squad registration
            team_size = int(data.get('team_size', 1))
            cur.execute("""
                INSERT INTO registrations 
                (username, email, game_id, phone, payout_upi, mode, team_size, team_name, payment_id, order_id, amount, status, event_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'completed', ?)
            """, (
                session.get('user'),
                data.get('team_email'),
                data.get('player_1_id'),  # Use first player as main game_id
                contact_phone,
                payout_upi,
                mode,
                team_size,
                data.get('team_name', ''),
                payment_id,
                order_id,
                amount,
                event_id
            ))
            registration_id = cur.lastrowid
            # Save team members
            for i in range(1, team_size + 1):
                player_id = data.get(f'player_{i}_id')
                character_name = data.get(f'player_{i}_name', '')
                cur.execute("""
                    INSERT INTO team_members (registration_id, player_number, game_id, character_name)
                    VALUES (?, ?, ?, ?)
                """, (registration_id, i, player_id, character_name))

        conn.commit()
        # create an admin notification about the new registration
        try:
            reg_id = cur.lastrowid
            username = session.get('user')
            mode = data.get('mode')
            msg = f"New registration: {mode} by {username} (reg id {reg_id})"
            metadata = json.dumps({'registration_id': reg_id, 'username': username, 'mode': mode})
            cur.execute("INSERT INTO notifications (type, message, metadata) VALUES (?, ?, ?)", ('registration', msg, metadata))
            conn.commit()
        except Exception as e:
            print(f"Failed to create notification: {e}")
        finally:
            conn.close()
        return True
        
    except Exception as e:
        print(f"Error saving registration: {e}")
        return False

@app.route('/register_freefire', methods=['GET', 'POST'])
def register_freefire():
    if 'user' not in session or session.get('role') != 'player':
        flash("Players only: Please log in as a player.", "error")
        return redirect(url_for('login'))

    selected_mode = request.args.get('mode', None)
    event_id = request.args.get('event_id')

    events = get_events()
    event = None
    if event_id:
        try:
            eid = int(event_id)
            for ev in events:
                if ev['id'] == eid:
                    event = ev
                    break
        except Exception:
            event = None

    if request.method == 'POST':
        flash("Please complete the payment to finish registration.", "info")
        return redirect(url_for('register_freefire'))

    wallet_balance_paise = _wallet_user_balance_paise_by_name(session.get('user'))

    return render_template(
        'register_freefire.html',
        selected_mode=selected_mode,
        event=event,
        cashfree_env=CASHFREE_ENV,
        session_user=session.get('user', ''),
        wallet_balance_paise=wallet_balance_paise,
        wallet_balance_label=_format_currency_value(wallet_balance_paise)
    )


@app.route('/proceed_registration', methods=['POST'])
def proceed_registration():
    """Receive registration form data, save it as a pending registration in DB and return a registration id plus amount for checkout."""
    if 'user' not in session or session.get('role') != 'player':
        return jsonify({'success': False, 'error': 'Authentication required'}), 403

    data = request.json or {}
    # Strong server-side validation to prevent incomplete registrations
    def is_email(v):
        if not v: return False
        return bool(re.match(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$", v))

    # Use stored profile phone if the streamlined form doesn't send one
    primary_phone = (data.get('phone') or '').strip()
    if not primary_phone:
        lookup_conn = None
        try:
            lookup_conn = sqlite3.connect(DB_NAME)
            lookup_cur = lookup_conn.cursor()
            lookup_cur.execute('SELECT phone FROM users WHERE username = ?', (session.get('user'),))
            row = lookup_cur.fetchone()
            if row and row[0]:
                primary_phone = str(row[0]).strip()
        except Exception:
            primary_phone = ''
        finally:
            if lookup_conn:
                lookup_conn.close()

    mode = (data.get('mode') or '').upper()
    if not mode or mode not in ('BR', 'CS'):
        return jsonify({'success': False, 'error': 'Invalid or missing mode'}), 400

    if not data.get('email') or not is_email(data.get('email')):
        return jsonify({'success': False, 'error': 'Invalid or missing email'}), 400

    # Mode-specific validation
    if mode == 'BR':
        if not data.get('game_id') or not str(data.get('game_id')).strip():
            return jsonify({'success': False, 'error': 'Missing game_id for BR mode'}), 400
        if not data.get('character_name') or not str(data.get('character_name')).strip():
            return jsonify({'success': False, 'error': 'Missing character_name for BR mode'}), 400
        # validate br team members if any
        try:
            br_team_size = int(data.get('team_size') or 1)
        except Exception:
            br_team_size = 1
        for i in range(2, br_team_size+1):
            if not data.get(f'br_player_{i}_id') or not data.get(f'br_player_{i}_name'):
                return jsonify({'success': False, 'error': f'Missing BR player {i} details'}), 400
    else:
        # CS mode
        if not data.get('team_name') or not str(data.get('team_name')).strip():
            return jsonify({'success': False, 'error': 'Missing team_name for CS mode'}), 400
        if not data.get('team_email') or not is_email(data.get('team_email')):
            return jsonify({'success': False, 'error': 'Missing or invalid team_email for CS mode'}), 400
        try:
            team_size = int(data.get('team_size') or 1)
        except Exception:
            team_size = 1
        for i in range(1, team_size+1):
            if not data.get(f'player_{i}_id') or not data.get(f'player_{i}_name'):
                return jsonify({'success': False, 'error': f'Missing team player {i} details'}), 400

    data['phone'] = primary_phone
    if not data.get('team_phone'):
        data['team_phone'] = primary_phone
    data['payout_upi'] = (data.get('payout_upi') or '').strip().lower()

    # Pull the correct entry fee from event (in paise)
    amount_paise = 10000
    try:
        if data.get('event_id'):
            conn_fee = sqlite3.connect(DB_NAME)
            cur_fee = conn_fee.cursor()
            cur_fee.execute('SELECT entry_fee FROM events WHERE id = ?', (int(data.get('event_id')),))
            row_fee = cur_fee.fetchone()
            if row_fee and row_fee[0] is not None:
                try:
                    amount_paise = max(int(row_fee[0]), 0) * 100
                except Exception:
                    amount_paise = 10000
            conn_fee.close()
    except Exception:
        amount_paise = 10000

    if amount_paise is None or amount_paise <= 0:
        amount_paise = 100

    try:
        conn = sqlite3.connect(DB_NAME)
        cur = conn.cursor()
        # save a pending registration (status pending). amount stored in paise based on event entry fee
        cur.execute("""
            INSERT INTO registrations (username, email, game_id, phone, payout_upi, mode, team_size, team_name, amount, status, event_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)
        """, (
            session.get('user'),
            data.get('email'),
            data.get('game_id') or '',
            primary_phone,
            data.get('payout_upi') or '',
            data.get('mode'),
            int(data.get('team_size') or 1),
            data.get('team_name') or '',
            amount_paise,
            int(data.get('event_id')) if data.get('event_id') else None
        ))
        reg_id = cur.lastrowid
        conn.commit()
        conn.close()

        # Return registration id so frontend can call create_payment_order with registration_id
        # create a notification for admin so they can see pending registration
        try:
            conn = sqlite3.connect(DB_NAME)
            cur = conn.cursor()
            msg = f"Pending registration saved: reg id {reg_id} by {session.get('user')}"
            metadata = json.dumps({'registration_id': reg_id, 'username': session.get('user'), 'mode': data.get('mode')})
            cur.execute("INSERT INTO notifications (type, message, metadata) VALUES (?, ?, ?)", ('registration_pending', msg, metadata))
            conn.commit()
            conn.close()
        except Exception:
            pass

        return jsonify({'success': True, 'registration_id': reg_id, 'amount': amount_paise, 'currency': 'INR'})
    except Exception as e:
        print('proceed_registration error', e)
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/wallet/pay_registration', methods=['POST'])
def wallet_pay_registration():
    if 'user' not in session or session.get('role') != 'player':
        return jsonify({'success': False, 'error': 'Authentication required'}), 403

    data = request.get_json(silent=True) or {}
    reg_id = data.get('registration_id')
    try:
        reg_id = int(reg_id)
    except Exception:
        return jsonify({'success': False, 'error': 'Invalid registration id'}), 400

    username = session.get('user')
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    try:
        cur.execute('SELECT id, username, amount, status FROM registrations WHERE id = ?', (reg_id,))
        row = cur.fetchone()
        if not row:
            conn.close()
            return jsonify({'success': False, 'error': 'Registration not found'}), 404
        if row[1] != username:
            conn.close()
            return jsonify({'success': False, 'error': 'Not authorized for this registration'}), 403

        amount_paise = int(row[2] or 0)
        if amount_paise <= 0:
            amount_paise = 100

        status = str(row[3] or '').lower()
        if status == 'completed':
            conn.close()
            return jsonify({'success': True, 'message': 'Registration already paid', 'redirect_url': url_for('payment_success', payment_id=f'WALLET-REG-{reg_id}')})

        cur.execute(
            'UPDATE users SET wallet_balance = COALESCE(wallet_balance, 0) - ? WHERE username = ? AND COALESCE(wallet_balance, 0) >= ?',
            (amount_paise, username, amount_paise)
        )
        if cur.rowcount == 0:
            conn.rollback()
            conn.close()
            return jsonify({'success': False, 'error': 'Insufficient wallet balance for this match fee'}), 400

        cur.execute(
            'INSERT INTO wallet_transactions (username, txn_type, amount, status, note) VALUES (?, ?, ?, ?, ?)',
            (username, 'match_fee', amount_paise, 'completed', f'Match fee paid for registration #{reg_id}')
        )
        conn.commit()
    except Exception:
        conn.rollback()
        conn.close()
        return jsonify({'success': False, 'error': 'Failed to debit wallet for match fee'}), 500
    finally:
        try:
            conn.close()
        except Exception:
            pass

    order_id = f"WALLET-REG-{reg_id}-{int(datetime.utcnow().timestamp())}"
    payment_id = f"WALLET-{reg_id}-{int(datetime.utcnow().timestamp())}"
    ok, payload, code = _finalize_registration_payment(order_id=order_id, payment_id=payment_id, reg_id=reg_id)
    if not ok:
        # Refund wallet if registration finalization fails.
        refund_conn = sqlite3.connect(DB_NAME)
        refund_cur = refund_conn.cursor()
        try:
            refund_cur.execute('UPDATE users SET wallet_balance = COALESCE(wallet_balance, 0) + ? WHERE username = ?', (amount_paise, username))
            refund_cur.execute(
                'INSERT INTO wallet_transactions (username, txn_type, amount, status, note) VALUES (?, ?, ?, ?, ?)',
                (username, 'refund', amount_paise, 'completed', f'Auto-refund: wallet match fee payment failed for registration #{reg_id}')
            )
            refund_conn.commit()
        except Exception:
            refund_conn.rollback()
        finally:
            refund_conn.close()
        return jsonify(payload), code

    payload['redirect_url'] = url_for('payment_success', payment_id=payload.get('payment_id'))
    return jsonify(payload), 200


@app.route('/payment_page/<int:reg_id>')
def payment_page(reg_id):
    """Payment UI for Cashfree checkout only."""
    if 'user' not in session or session.get('role') != 'player':
        flash('Please login as player to continue payment', 'error')
        return redirect(url_for('login'))

    # Load the pending registration
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute('SELECT id, username, email, game_id, phone, payout_upi, mode, amount, status, event_id FROM registrations WHERE id = ?', (reg_id,))
    row = cur.fetchone()
    conn.close()
    if not row:
        flash('Registration not found', 'error')
        return redirect(url_for('freefire'))
    if row[1] != session.get('user'):
        flash('You are not authorized to access this payment page.', 'error')
        return redirect(url_for('player_history'))

    registration = {
        'id': row[0],
        'username': row[1],
        'email': row[2],
        'game_id': row[3],
        'phone': row[4],
        'payout_upi': row[5],
        'mode': row[6],
        'amount': row[7] or 0,
        'status': row[8],
        'event_id': row[9]
    }

    # If amount is 0, try to compute from event
    if (not registration['amount'] or registration['amount'] == 0) and registration.get('event_id'):
        try:
            conn = sqlite3.connect(DB_NAME)
            cur = conn.cursor()
            cur.execute('SELECT entry_fee FROM events WHERE id = ?', (registration['event_id'],))
            r = cur.fetchone()
            if r and r[0]:
                registration['amount'] = int(r[0]) * 100
            conn.close()
        except Exception:
            pass

    # Render a minimal payment page where admin can integrate QR; we'll include a 'Proceed to Pay' button
    return render_template('payment_page.html', registration=registration, cashfree_env=CASHFREE_ENV)


@app.route('/payment_success')
def payment_success():
    if 'user' not in session or session.get('role') != 'player':
        flash('Please login as player to continue.', 'error')
        return redirect(url_for('login'))
    payment_id = request.args.get('payment_id', '').strip()
    return render_template('payment_success.html', payment_id=payment_id)


@app.route('/verify_manual_payment', methods=['POST'])
def verify_manual_payment():
    return jsonify({'success': False, 'error': 'Manual verification is disabled. Please pay through Cashfree checkout only.'}), 410

@app.route('/bgmi')
def bgmi():
    if 'user' not in session or session.get('role') != 'player':
        flash("Players only: Please log in as a player.", "error")
        return redirect(url_for('login'))
    return render_template('bgmi.html')

@app.route('/valorant')
def valorant():
    if 'user' not in session or session.get('role') != 'player':
        flash("Players only: Please log in as a player.", "error")
        return redirect(url_for('login'))
    return render_template('valorant.html')

@app.route('/smashkart')
def smashkart():
    if 'user' not in session or session.get('role') != 'player':
        flash("Players only: Please log in as a player.", "error")
        return redirect(url_for('login'))
    return render_template('smashkart.html')

@app.route('/logout', methods=['GET', 'POST'])
def logout():
    """Log the user out and redirect home. Accepts GET (fallback) and POST (preferred)."""
    session.clear()
    flash("You have been logged out.", "info")
    return redirect(url_for('home'))

# ---------- ADMIN DASHBOARD ----------
@app.route('/admin_dashboard')
def admin_dashboard():
    if 'user' not in session or session.get('role') != 'admin':
        flash("Admins only: Please log in as an admin.", "error")
        return redirect(url_for('admin_login'))
    # Show all users and registrations
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute("SELECT id, username, email, role, game_id, phone FROM users")
    users = cur.fetchall()
    cur.execute("SELECT id, username, email, game_id, phone, mode, team_size, team_name, payment_id, order_id, amount, status, created_at FROM registrations")
    registrations = cur.fetchall()
    cur.execute("SELECT id, type, message, metadata, is_read, created_at FROM notifications ORDER BY created_at DESC LIMIT 50")
    notifications = cur.fetchall()
    # Today's signups
    try:
        cur.execute("SELECT id, username, email, created_at FROM users WHERE date(created_at) = date('now') ORDER BY created_at DESC")
        signups_today = cur.fetchall()
    except Exception:
        signups_today = []

    conn.close()

    # Compute total revenue safely (registrations may be empty)
    total_revenue = 0
    try:
        for r in registrations:
            # amount is expected at index 10, ensure it's an int
            amt = r[10] if len(r) > 10 and r[10] is not None else 0
            try:
                total_revenue += int(amt)
            except Exception:
                # skip invalid amounts
                continue
    except Exception:
        total_revenue = 0

    # Pass total_revenue to template to avoid Jinja map/attribute operations
    event_collections = _collate_matches_by_game()
    match_counts = _match_counts_summary(event_collections)
    dashboard_match_counts = {
        'ongoing': match_counts['totals']['ongoing'],
        'upcoming': match_counts['totals']['upcoming']
    }

    return render_template('admin_dashboard.html', users=users, registrations=registrations, total_revenue=total_revenue, notifications=notifications, signups_today=signups_today, match_counts=dashboard_match_counts)


@app.route('/admin/events')
def admin_events():
    if 'user' not in session or session.get('role') != 'admin':
        flash('Admins only', 'error')
        return redirect(url_for('admin_login'))
    _auto_roll_event_dates_daily()
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute('SELECT id, slug, title, mode, date, prize, max_slots, slots_left, is_open, entry_fee, prize_pool, image, field_updates, region, platform, start_time, end_time, description, rules FROM events ORDER BY id')
    rows = cur.fetchall()
    events = []
    for r in rows:
        # versioned preview for admin cards too
        try:
            fupd = json.loads(r[12]) if r[12] else {}
        except Exception:
            fupd = {}
        img_url = _append_cache_bust(_resolve_event_image(r[11]), fupd.get('image'))
        events.append({
            'id': r[0],
            'slug': r[1],
            'title': r[2],
            'mode': r[3],
            'date': r[4],
            'prize': r[5],
            'max_slots': r[6],
            'slots_left': r[7],
            'is_open': bool(r[8]) if r[8] is not None else True,
            'entry_fee': r[9],
            'prize_pool': r[10],
            'image': r[11],
            'image_url': img_url,
            'region': r[13],
            'platform': r[14],
            'start_time': r[15],
            'end_time': r[16],
            'description': r[17],
            'rules': r[18] if len(r) > 18 else None
        })
    conn.close()
    return render_template('admin_events.html', events=events)


@app.route('/admin/events_json')
def admin_events_json():
    """Return raw events rows as JSON for admin debugging."""
    if 'user' not in session or session.get('role') != 'admin':
        return jsonify({'success': False, 'error': 'Admins only'}), 403
    _auto_roll_event_dates_daily()
    try:
        conn = sqlite3.connect(DB_NAME)
        cur = conn.cursor()
        cur.execute('SELECT id, slug, title, mode, date, prize, max_slots, slots_left, is_open, entry_fee, prize_pool, description, image, region, platform, start_time, end_time, field_updates, rules FROM events ORDER BY id')
        rows = cur.fetchall()
        conn.close()
        events = []
        for r in rows:
            events.append({
                'id': r[0], 'slug': r[1], 'title': r[2], 'mode': r[3], 'date': r[4], 'prize': r[5],
                'max_slots': r[6], 'slots_left': r[7], 'is_open': bool(r[8]) if r[8] is not None else True,
                'entry_fee': r[9], 'prize_pool': r[10], 'description': r[11], 'image': r[12], 'region': r[13], 'platform': r[14], 'start_time': r[15], 'end_time': r[16], 'field_updates': json.loads(r[17]) if r[17] else {}, 'rules': r[18] if len(r) > 18 else None
            })
        return jsonify({'success': True, 'events': events})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})


@app.route('/admin/events/new', methods=['GET', 'POST'])
def admin_event_new():
    if 'user' not in session or session.get('role') != 'admin':
        flash('Admins only', 'error')
        return redirect(url_for('admin_login'))
    if request.method == 'POST':
        uploaded = request.files.get('card_image')
        new_image_path = _save_event_image(uploaded) if uploaded and uploaded.filename else None
        title = request.form.get('title')
        slug = request.form.get('slug') or (title.lower().replace(' ', '-') if title else '')
        mode = request.form.get('mode')
        date = request.form.get('date')
        prize = request.form.get('prize')
        max_slots = int(request.form.get('max_slots') or 0)
        slots_left = int(request.form.get('slots_left') or max_slots)
        entry_fee = int(request.form.get('entry_fee') or 0)
        region = request.form.get('region') or 'India'
        platform = request.form.get('platform') or 'Mobile'
        prize_pool = request.form.get('prize_pool')
        description = request.form.get('description')
        start_raw = request.form.get('start_time')
        start_period = request.form.get('start_period') or ''
        end_raw = request.form.get('end_time')
        end_period = request.form.get('end_period') or ''
        start_input = _combine_time_with_period(start_raw, start_period)
        end_input = _combine_time_with_period(end_raw, end_period)
        start_time = _normalize_time_input(start_input, date)
        end_time = _normalize_time_input(end_input, date)
        fallback_image = request.form.get('existing_image') or _default_event_image(mode)
        image_path = new_image_path or fallback_image
        image = image_path or _default_event_image(mode)
        # validation
        if not title:
            flash('Title is required', 'error')
            # re-render with current form values
            event = {
                'slug': slug,
                'title': title,
                'mode': mode,
                'date': date,
                'prize': prize,
                'max_slots': max_slots,
                'slots_left': slots_left,
                'entry_fee': entry_fee,
                'prize_pool': prize_pool,
                'description': description,
                'image': image,
                'is_open': True,
                'region': region,
                'platform': platform,
                'start_time': start_time,
                'end_time': end_time
            }
            return render_template('admin_event_form.html', event=event)

        conn = sqlite3.connect(DB_NAME)
        cur = conn.cursor()
        rules = request.form.get('rules')
        cur.execute('INSERT INTO events (slug, title, mode, date, prize, max_slots, slots_left, entry_fee, prize_pool, description, image, region, platform, start_time, end_time, rules) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)', (slug, title, mode, date, prize, max_slots, slots_left, entry_fee, prize_pool, description, image, region, platform, start_time, end_time, rules))
        conn.commit()
        conn.close()
        flash('Event created', 'success')
        return redirect(url_for('admin_events'))
    return render_template('admin_event_form.html', event={})


@app.route('/admin/events/<int:eid>/edit', methods=['GET', 'POST'])
def admin_event_edit(eid):
    if 'user' not in session or session.get('role') != 'admin':
        flash('Admins only', 'error')
        return redirect(url_for('admin_login'))
    _auto_roll_event_dates_daily()
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    if request.method == 'POST':
        # Support JSON payloads for AJAX saves
        is_json_request = request.is_json
        payload = request.get_json() if is_json_request else None
        wants_json_response = (
            is_json_request
            or request.headers.get('X-Requested-With', '').lower() == 'fetch'
            or 'application/json' in (request.headers.get('Accept') or '')
        )
        # Fetch current event to use as fallback for missing fields (prevents NOT NULL constraint failures)
        cur.execute('SELECT slug, title, mode, date, prize, max_slots, slots_left, is_open, entry_fee, prize_pool, description, image, region, platform, start_time, end_time, field_updates, rules FROM events WHERE id = ?', (eid,))
        existing = cur.fetchone() or [None] * 19
        exlen = len(existing)
        def ex(idx, default=None):
            return existing[idx] if idx < exlen else default
        try:
            if is_json_request and payload:
                title = payload.get('title') or ex(1)
                slug = payload.get('slug') or ex(0)
                mode = payload.get('mode') or ex(2)
                date = payload.get('date') or ex(3)
                prize = payload.get('prize') or ex(4)
                try:
                    max_slots = int(payload.get('max_slots') if payload.get('max_slots') is not None else (ex(5) or 0))
                except Exception:
                    max_slots = ex(5) or 0
                try:
                    slots_left = int(payload.get('slots_left') if payload.get('slots_left') is not None else (ex(6) or max_slots))
                except Exception:
                    slots_left = ex(6) or max_slots
                try:
                    entry_fee = int(payload.get('entry_fee') if payload.get('entry_fee') is not None else (ex(8) or 0))
                except Exception:
                    entry_fee = ex(8) or 0
                prize_pool = payload.get('prize_pool') or ex(9)
                description = payload.get('description') or ex(10)
                image = payload.get('image') or ex(11) or _default_event_image(mode)
                rules = payload.get('rules') or ex(18)
                if 'is_open' in payload:
                    is_open = 1 if str(payload.get('is_open')).lower() in ('1', 'true', 'yes', 'on') else 0
                else:
                    is_open = 1 if ex(7) else 0
                region = payload.get('region') or ex(12) or 'India'
                platform = payload.get('platform') or ex(13) or 'Mobile'
                start_time = ex(14)
                if 'start_time' in payload:
                    start_time = _normalize_time_input(payload.get('start_time'), date)
                end_time = ex(15)
                if 'end_time' in payload:
                    end_time = _normalize_time_input(payload.get('end_time'), date)
            else:
                title = request.form.get('title') or ex(1)
                slug = request.form.get('slug') or ex(0)
                mode = request.form.get('mode') or ex(2)
                date = request.form.get('date') or ex(3)
                prize = request.form.get('prize') or ex(4)
                try:
                    max_slots = int(request.form.get('max_slots') or ex(5) or 0)
                except Exception:
                    max_slots = ex(5) or 0
                try:
                    slots_left = int(request.form.get('slots_left') or ex(6) or max_slots)
                except Exception:
                    slots_left = ex(6) or max_slots
                try:
                    entry_fee = int(request.form.get('entry_fee') or ex(8) or 0)
                except Exception:
                    entry_fee = ex(8) or 0
                prize_pool = request.form.get('prize_pool') or ex(9)
                description = request.form.get('description') or ex(10)
                form_image = request.form.get('image')
                base_image = form_image or request.form.get('existing_image') or ex(11) or _default_event_image(mode)
                uploaded = request.files.get('card_image')
                uploaded_path = _save_event_image(uploaded) if uploaded and uploaded.filename else None
                image = uploaded_path or base_image
                is_open = 1 if request.form.get('is_open') else 0
                region = request.form.get('region') or ex(12) or 'India'
                platform = request.form.get('platform') or ex(13) or 'Mobile'
                start_raw = request.form.get('start_time')
                start_period = request.form.get('start_period') or ''
                end_raw = request.form.get('end_time')
                end_period = request.form.get('end_period') or ''
                start_input = _combine_time_with_period(start_raw, start_period)
                end_input = _combine_time_with_period(end_raw, end_period)
                start_time = _normalize_time_input(start_input, date) or ex(14)
                end_time = _normalize_time_input(end_input, date) or ex(15)
                rules = request.form.get('rules') or ex(18)
            # load existing field_updates JSON
            try:
                existing_field_updates = json.loads(ex(16, '') or '{}')
            except Exception:
                existing_field_updates = {}

            # determine changes and update timestamps
            now_iso = datetime.utcnow().isoformat()
            field_updates = existing_field_updates.copy()
            def maybe_update(field, newval, oldval):
                if (oldval is None and newval is not None) or (oldval is not None and str(newval) != str(oldval)):
                    field_updates[field] = now_iso

            maybe_update('title', title, ex(1))
            maybe_update('slug', slug, ex(0))
            maybe_update('mode', mode, ex(2))
            maybe_update('date', date, ex(3))
            maybe_update('prize', prize, ex(4))
            maybe_update('max_slots', max_slots, ex(5))
            maybe_update('slots_left', slots_left, ex(6))
            maybe_update('entry_fee', entry_fee, ex(8))
            maybe_update('prize_pool', prize_pool, ex(9))
            maybe_update('description', description, ex(10))
            maybe_update('image', image, ex(11))
            maybe_update('is_open', is_open, ex(7))
            maybe_update('region', region, ex(12))
            maybe_update('platform', platform, ex(13))
            maybe_update('start_time', start_time, ex(14))
            maybe_update('end_time', end_time, ex(15))
            maybe_update('rules', rules, ex(18))

            cur.execute('''UPDATE events SET slug=?, title=?, mode=?, date=?, prize=?, max_slots=?, slots_left=?, entry_fee=?, prize_pool=?, description=?, image=?, is_open=?, region=?, platform=?, start_time=?, end_time=?, rules=?, field_updates=? WHERE id=?''', (slug, title, mode, date, prize, max_slots, slots_left, entry_fee, prize_pool, description, image, is_open, region, platform, start_time, end_time, rules, json.dumps(field_updates), eid))
            conn.commit()
        except Exception as ex:
            conn.close()
            if wants_json_response:
                return jsonify({'success': False, 'error': str(ex)}), 500
            flash('Failed to update event', 'error')
            return redirect(url_for('admin_events'))

        # After saving, if the event is now upcoming, purge prior registrations for a clean slate
        updated_event = {
            'id': eid,
            'title': title,
            'mode': mode,
            'date': date,
            'prize': prize,
            'prize_pool': prize_pool,
            'entry_fee': entry_fee,
            'start_time': start_time,
            'end_time': end_time,
            'max_slots': max_slots,
            'slots_left': slots_left,
            'region': region,
            'platform': platform,
            'is_open': bool(is_open)
        }
        status_now = _event_to_match(updated_event).get('status') if updated_event else None
        if status_now == 'upcoming':
            _purge_event_registrations(eid)

        conn.close()
        if wants_json_response:
            return jsonify({'success': True, 'message': 'Event updated', 'field_updates': field_updates})
        flash('Event updated', 'success')
        # After saving, redirect back to the same edit page so admins see the updated values immediately
        return redirect(url_for('admin_event_edit', eid=eid))
    cur.execute('SELECT id, slug, title, mode, date, prize, max_slots, slots_left, is_open, entry_fee, prize_pool, description, image, region, platform, start_time, end_time, field_updates, rules FROM events WHERE id = ?', (eid,))
    row = cur.fetchone()
    conn.close()
    if not row:
        flash('Event not found', 'error')
        return redirect(url_for('admin_events'))
    # convert row to dict for template convenience
    try:
        field_updates = json.loads(row[17]) if row and len(row) > 17 and row[17] else {}
    except Exception:
        field_updates = {}
    def r(idx, default=None):
        return row[idx] if idx < len(row) else default

    event = {
        'id': r(0), 'slug': r(1), 'title': r(2), 'mode': r(3), 'date': r(4), 'prize': r(5),
        'max_slots': r(6), 'slots_left': r(7), 'is_open': bool(r(8)), 'entry_fee': r(9), 'prize_pool': r(10),
        'description': r(11), 'image': r(12), 'region': r(13), 'platform': r(14), 'start_time': r(15), 'end_time': r(16),
        'field_updates': field_updates,
        'rules': r(18)
    }
    return render_template('event.html', event=event)


@app.route('/admin/events/<int:eid>/delete', methods=['POST'])
def admin_event_delete(eid):
    if 'user' not in session or session.get('role') != 'admin':
        return jsonify({'success': False, 'error': 'Admins only'}), 403
    try:
        conn = sqlite3.connect(DB_NAME)
        cur = conn.cursor()
        cur.execute('DELETE FROM events WHERE id = ?', (eid,))
        conn.commit()
        conn.close()
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})


@app.route('/admin/mark_notification_read/<int:nid>', methods=['POST'])
def admin_mark_notification(nid):
    if 'user' not in session or session.get('role') != 'admin':
        return jsonify({'success': False, 'error': 'Admins only'}), 403
    try:
        conn = sqlite3.connect(DB_NAME)
        cur = conn.cursor()
        cur.execute('UPDATE notifications SET is_read = 1 WHERE id = ?', (nid,))
        conn.commit()
        conn.close()
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})


@app.route('/admin/delete_user/<int:uid>', methods=['POST'])
def admin_delete_user(uid):
    if 'user' not in session or session.get('role') != 'admin':
        return jsonify({'success': False, 'error': 'Admins only'}), 403
    try:
        conn = sqlite3.connect(DB_NAME)
        cur = conn.cursor()
        # Get username
        cur.execute('SELECT username FROM users WHERE id = ?', (uid,))
        row = cur.fetchone()
        if not row:
            conn.close()
            return jsonify({'success': False, 'error': 'User not found'})
        username = row[0]

        # Find registrations by this username to reclaim slots
        cur.execute('SELECT id, team_size, event_id FROM registrations WHERE username = ?', (username,))
        regs = cur.fetchall()
        for reg in regs:
            reg_id, team_size, event_id = reg
            if event_id:
                try:
                    cur.execute('UPDATE events SET slots_left = slots_left + ? WHERE id = ?', (team_size or 1, event_id))
                except Exception:
                    pass
            # delete team members
            cur.execute('DELETE FROM team_members WHERE registration_id = ?', (reg_id,))
        # delete registrations
        cur.execute('DELETE FROM registrations WHERE username = ?', (username,))
        # delete user
        cur.execute('DELETE FROM users WHERE id = ?', (uid,))
        conn.commit()
        conn.close()
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})


@app.route('/admin/delete_registration/<int:rid>', methods=['POST'])
def admin_delete_registration(rid):
    if 'user' not in session or session.get('role') != 'admin':
        return jsonify({'success': False, 'error': 'Admins only'}), 403
    try:
        conn = sqlite3.connect(DB_NAME)
        cur = conn.cursor()
        cur.execute('SELECT team_size, event_id FROM registrations WHERE id = ?', (rid,))
        row = cur.fetchone()
        if not row:
            conn.close()
            return jsonify({'success': False, 'error': 'Registration not found'})
        team_size, event_id = row
        if event_id:
            try:
                cur.execute('UPDATE events SET slots_left = slots_left + ? WHERE id = ?', (team_size or 1, event_id))
            except Exception:
                pass
        # delete team members and registration
        cur.execute('DELETE FROM team_members WHERE registration_id = ?', (rid,))
        cur.execute('DELETE FROM registrations WHERE id = ?', (rid,))
        conn.commit()
        conn.close()
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})


@app.route('/admin/payments')
def admin_payments():
    if 'user' not in session or session.get('role') != 'admin':
        flash('Admins only', 'error')
        return redirect(url_for('admin_login'))
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute('SELECT id, username, email, game_id, phone, mode, team_size, payment_id, order_id, amount, status, refund_requested, refunded, event_id, created_at FROM registrations ORDER BY created_at DESC')
    rows = cur.fetchall()
    regs = []
    for r in rows:
        regs.append({
            'id': r[0], 'username': r[1], 'email': r[2], 'game_id': r[3], 'phone': r[4], 'mode': r[5], 'team_size': r[6],
            'payment_id': r[7], 'order_id': r[8], 'amount': r[9], 'status': r[10], 'refund_requested': bool(r[11]), 'refunded': bool(r[12]), 'event_id': r[13], 'created_at': r[14]
        })
    conn.close()
    return render_template('admin_payments.html', regs=regs)


@app.route('/admin/wallet')
def admin_wallet():
    if 'user' not in session or session.get('role') != 'admin':
        flash('Admins only', 'error')
        return redirect(url_for('admin_login'))

    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute(
        '''SELECT id, username, amount, method, holder_name, bank_account_number, ifsc_code, upi_id, mobile_number, status, created_at
           FROM wallet_withdrawal_requests
           ORDER BY CASE WHEN status = 'pending' THEN 0 ELSE 1 END, id DESC'''
    )
    rows = cur.fetchall()
    conn.close()

    requests_list = []
    for r in rows:
        requests_list.append({
            'id': r[0],
            'username': r[1],
            'amount': r[2],
            'amount_label': _format_currency_value(r[2]),
            'method': r[3] or '-',
            'holder_name': r[4] or '-',
            'bank_account_number': r[5] or '',
            'ifsc_code': r[6] or '',
            'upi_id': r[7] or '',
            'mobile_number': r[8] or '',
            'status': r[9] or 'pending',
            'created_at': r[10] or '-'
        })
    return render_template('admin_wallet.html', requests_list=requests_list)


@app.route('/admin/wallet/withdrawals/<int:request_id>/decision', methods=['POST'])
def admin_wallet_withdrawal_decision(request_id):
    if 'user' not in session or session.get('role') != 'admin':
        flash('Admins only', 'error')
        return redirect(url_for('admin_login'))

    action = (request.form.get('action') or '').strip().lower()
    if action not in ('approve', 'reject'):
        flash('Invalid action for withdrawal request.', 'error')
        return redirect(url_for('admin_wallet'))

    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    try:
        cur.execute(
            'SELECT id, username, amount, method, status FROM wallet_withdrawal_requests WHERE id = ?',
            (request_id,)
        )
        row = cur.fetchone()
        if not row:
            conn.close()
            flash('Withdrawal request not found.', 'error')
            return redirect(url_for('admin_wallet'))

        _, username, amount_paise, method, status = row
        if str(status or '').lower() != 'pending':
            conn.close()
            flash('Withdrawal request already processed.', 'info')
            return redirect(url_for('admin_wallet'))

        if action == 'approve':
            cur.execute('UPDATE wallet_withdrawal_requests SET status = ? WHERE id = ?', ('approved', request_id))
            cur.execute(
                'UPDATE wallet_transactions SET status = ?, note = ? WHERE withdrawal_request_id = ? AND txn_type = ? AND status = ?',
                ('completed', f'Withdrawal request #{request_id} approved', request_id, 'withdrawal', 'pending')
            )
            cur.execute(
                'INSERT INTO notifications (type, message, metadata) VALUES (?, ?, ?)',
                ('wallet_withdraw_approved', f'Wallet withdrawal approved for {username}', json.dumps({'withdrawal_request_id': request_id, 'username': username, 'method': method}))
            )
            conn.commit()
            flash('Withdrawal request approved.', 'success')
        else:
            cur.execute('UPDATE wallet_withdrawal_requests SET status = ? WHERE id = ?', ('rejected', request_id))
            cur.execute('UPDATE users SET wallet_balance = COALESCE(wallet_balance, 0) + ? WHERE username = ?', (amount_paise, username))
            cur.execute(
                'UPDATE wallet_transactions SET status = ?, note = ? WHERE withdrawal_request_id = ? AND txn_type = ? AND status = ?',
                ('rejected', f'Withdrawal request #{request_id} rejected', request_id, 'withdrawal', 'pending')
            )
            cur.execute(
                'INSERT INTO wallet_transactions (username, txn_type, amount, status, note, withdrawal_request_id) VALUES (?, ?, ?, ?, ?, ?)',
                (username, 'refund', amount_paise, 'completed', f'Auto-refund for rejected withdrawal #{request_id}', request_id)
            )
            cur.execute(
                'INSERT INTO notifications (type, message, metadata) VALUES (?, ?, ?)',
                ('wallet_withdraw_rejected', f'Wallet withdrawal rejected/refunded for {username}', json.dumps({'withdrawal_request_id': request_id, 'username': username, 'method': method}))
            )
            conn.commit()
            flash('Withdrawal request rejected and wallet refunded.', 'success')
    except Exception:
        conn.rollback()
        flash('Failed to process withdrawal request.', 'error')
    finally:
        conn.close()

    return redirect(url_for('admin_wallet'))


@app.route('/admin/mark_refund_request/<int:rid>', methods=['POST'])
def admin_mark_refund_request(rid):
    if 'user' not in session or session.get('role') != 'admin':
        return jsonify({'success': False, 'error': 'Admins only'}), 403
    try:
        conn = sqlite3.connect(DB_NAME)
        cur = conn.cursor()
        cur.execute('UPDATE registrations SET refund_requested = 1 WHERE id = ?', (rid,))
        # notify admins
        try:
            cur.execute('SELECT username, mode FROM registrations WHERE id = ?', (rid,))
            r = cur.fetchone()
            if r:
                msg = f"Refund requested for registration {rid} by {r[0]} ({r[1]})"
                cur.execute('INSERT INTO notifications (type, message, metadata) VALUES (?, ?, ?)', ('refund_request', msg, json.dumps({'registration_id': rid})))
        except Exception:
            pass
        conn.commit()
        conn.close()
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})


@app.route('/admin/process_refund/<int:rid>', methods=['POST'])
def admin_process_refund(rid):
    if 'user' not in session or session.get('role') != 'admin':
        return jsonify({'success': False, 'error': 'Admins only'}), 403
    try:
        conn = sqlite3.connect(DB_NAME)
        cur = conn.cursor()
        # mark refunded and clear refund_requested; optionally reclaim slots
        cur.execute('SELECT team_size, event_id FROM registrations WHERE id = ?', (rid,))
        row = cur.fetchone()
        if not row:
            conn.close()
            return jsonify({'success': False, 'error': 'Registration not found'})
        team_size, event_id = row
        try:
            cur.execute('UPDATE registrations SET refunded = 1, refund_requested = 0, status = ? WHERE id = ?', ('refunded', rid))
            if event_id:
                cur.execute('UPDATE events SET slots_left = slots_left + ? WHERE id = ?', (team_size or 1, event_id))
            # notification
            cur.execute('INSERT INTO notifications (type, message, metadata) VALUES (?, ?, ?)', ('refund_processed', f'Refund processed for registration {rid}', json.dumps({'registration_id': rid})))
        except Exception as e:
            print('Error processing refund inner:', e)
        conn.commit()
        conn.close()
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})


@app.route('/admin/settings', methods=['GET', 'POST'])
def admin_settings():
    if 'user' not in session or session.get('role') != 'admin':
        flash('Admins only', 'error')
        return redirect(url_for('admin_login'))
    defaults = {
        'site_name': 'BATTLE-X',
        'default_entry_fee': 0,
        'google_client_id': '',
        'google_client_secret': '',
        'google_redirect_uri': ''
    }
    global GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, GOOGLE_REDIRECT_URI
    if request.method == 'POST':
        site_name = request.form.get('site_name') or defaults['site_name']
        default_entry_fee = int(request.form.get('default_entry_fee') or 0)
        google_client_id = (request.form.get('google_client_id') or '').strip()
        google_client_secret = (request.form.get('google_client_secret') or '').strip()
        google_redirect_uri = (request.form.get('google_redirect_uri') or '').strip()
        new_settings = {
            'site_name': site_name,
            'default_entry_fee': default_entry_fee,
            'google_client_id': google_client_id,
            'google_client_secret': google_client_secret,
            'google_redirect_uri': google_redirect_uri
        }
        save_admin_settings(new_settings)
        GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, GOOGLE_REDIRECT_URI = compute_google_credentials(new_settings)
        flash('Settings saved', 'success')
        return redirect(url_for('admin_settings'))

    current_settings = defaults.copy()
    current_settings.update(ADMIN_SETTINGS or {})
    effective_google_id, effective_google_secret, effective_redirect = compute_google_credentials(current_settings)
    return render_template(
        'admin_settings.html',
        settings=current_settings,
        effective_google_id=effective_google_id,
        effective_google_secret=effective_google_secret,
        effective_google_redirect=effective_redirect
    )


@app.route('/admin/players')
def admin_players():
    if 'user' not in session or session.get('role') != 'admin':
        flash('Admins only', 'error')
        return redirect(url_for('admin_login'))
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute('SELECT id, username, email, role, game_id, phone FROM users ORDER BY id')
    rows = cur.fetchall()
    users = [{'id': r[0], 'username': r[1], 'email': r[2], 'role': r[3], 'game_id': r[4], 'phone': r[5]} for r in rows]
    conn.close()
    return render_template('admin_players.html', users=users)


@app.route('/admin/matches')
def admin_matches():
    if 'user' not in session or session.get('role') != 'admin':
        flash('Admins only', 'error')
        return redirect(url_for('admin_login'))
    events = get_events(include_closed=True)
    collections = _collate_matches_by_game(events)
    match_counts = _match_counts_summary(collections)
    def ensure_bucket(bucket):
        if not bucket:
            return {'ongoing': [], 'upcoming': [], 'completed': []}
        for status in ('ongoing', 'upcoming', 'completed'):
            bucket.setdefault(status, [])
        return bucket

    br_matches = ensure_bucket(collections.get('BR'))
    cs_matches = ensure_bucket(collections.get('CS'))
    custom_matches = ensure_bucket(collections.get('Custom'))
    return render_template(
        'admin_matches.html',
        br_matches=br_matches,
        cs_matches=cs_matches,
        custom_matches=custom_matches,
        match_counts=match_counts,
        game_labels=GAME_TYPE_LABELS
    )


def _fetch_event_registration_payload(event_id):
    conn = None
    try:
        conn = sqlite3.connect(DB_NAME)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute("""
            SELECT id, title, mode, date, prize, prize_pool, entry_fee, start_time, end_time,
                   max_slots, slots_left, region, platform, image, description
            FROM events
            WHERE id = ?
        """, (event_id,))
        event_row = cur.fetchone()
        if not event_row:
            raise LookupError('Event not found')
        event_dict = dict(event_row)
        match_view = _event_to_match(event_dict)
        cur.execute("""
                 SELECT id, username, email, phone, payout_upi, mode, team_size, team_name, payment_id,
                     order_id, amount, status, created_at, game_id
            FROM registrations
            WHERE event_id = ?
            ORDER BY created_at DESC
        """, (event_id,))
        registration_rows = cur.fetchall()
        if (not registration_rows) and event_dict.get('mode'):
            cur.execute("""
                  SELECT id, username, email, phone, payout_upi, mode, team_size, team_name, payment_id,
                      order_id, amount, status, created_at, game_id
                FROM registrations
                WHERE (event_id IS NULL OR event_id = 0) AND mode = ?
                ORDER BY created_at DESC
            """, (event_dict.get('mode'),))
            registration_rows = cur.fetchall()
        reg_ids = [row['id'] for row in registration_rows]
        members_by_reg = {rid: [] for rid in reg_ids}
        if reg_ids:
            placeholders = ','.join('?' for _ in reg_ids)
            cur.execute(f"""
                SELECT registration_id, player_number, game_id, character_name
                FROM team_members
                WHERE registration_id IN ({placeholders})
                ORDER BY registration_id, player_number
            """, reg_ids)
            for member in cur.fetchall():
                members_by_reg[member['registration_id']].append({
                    'slot': member['player_number'],
                    'game_id': member['game_id'],
                    'name': member['character_name']
                })
    except LookupError:
        if conn:
            conn.close()
        raise
    except Exception:
        if conn:
            conn.close()
        raise
    if conn:
        conn.close()

    registrations_payload = []
    total_players = 0
    for row in registration_rows:
        team_size = row['team_size'] or 1
        total_players += team_size
        players = members_by_reg.get(row['id'], [])
        if not any(p.get('slot') == 1 for p in players):
            players.insert(0, {
                'slot': 1,
                'game_id': row['game_id'],
                'name': row['team_name'] or row['username']
            })
        raw_amount = row['amount'] if row['amount'] is not None else 0
        try:
            raw_amount = int(raw_amount)
        except Exception:
            raw_amount = 0
        registrations_payload.append({
            'id': row['id'],
            'username': row['username'],
            'email': row['email'],
            'phone': row['phone'],
            'payout_upi': row['payout_upi'] or '',
            'mode': row['mode'],
            'team_size': team_size,
            'team_name': row['team_name'] or '',
            'amount': _format_currency_value(raw_amount),
            'amount_value': raw_amount,
            'status': row['status'] or 'pending',
            'created_at': row['created_at'],
            'created_display': _format_admin_timestamp(row['created_at']),
            'game_id': row['game_id'],
            'payment_id': row['payment_id'],
            'order_id': row['order_id'],
            'players': players
        })

    payload = {
        'event': {
            'id': event_dict.get('id'),
            'title': match_view.get('title'),
            'mode': match_view.get('mode'),
            'status': match_view.get('status'),
            'scheduled_for': match_view.get('scheduled_for'),
            'prize_pool': match_view.get('prize_pool'),
            'entry_fee': match_view.get('entry_fee'),
            'region': match_view.get('lobby_info'),
            'platform': match_view.get('custom_room_id'),
            'slots_left': event_dict.get('slots_left'),
            'max_slots': event_dict.get('max_slots'),
            'registrations': len(registrations_payload),
            'players_registered': total_players
        },
        'registrations': registrations_payload
    }
    return payload


def _purge_event_registrations(event_id):
    """Delete registrations (and their team members) for an event, used when resetting to upcoming."""
    conn = None
    try:
        conn = sqlite3.connect(DB_NAME)
        cur = conn.cursor()
        cur.execute('SELECT id FROM registrations WHERE event_id = ?', (event_id,))
        reg_ids = [row[0] for row in cur.fetchall()]
        if reg_ids:
            placeholders = ','.join('?' for _ in reg_ids)
            cur.execute(f"DELETE FROM team_members WHERE registration_id IN ({placeholders})", reg_ids)
        cur.execute('DELETE FROM registrations WHERE event_id = ?', (event_id,))
        conn.commit()
    except Exception as exc:
        print('Error purging registrations for event', event_id, ':', exc)
    finally:
        if conn:
            conn.close()


@app.route('/admin/events/<int:event_id>/registrations')
def admin_event_registrations(event_id):
    if 'user' not in session or session.get('role') != 'admin':
        return jsonify({'success': False, 'error': 'Admins only'}), 403
    try:
        payload = _fetch_event_registration_payload(event_id)
    except LookupError as exc:
        return jsonify({'success': False, 'error': str(exc)}), 404
    except Exception as exc:
        return jsonify({'success': False, 'error': str(exc)}), 500
    response_payload = {'success': True}
    response_payload.update(payload)
    return jsonify(response_payload)


@app.route('/admin/events/<int:event_id>/players.csv')
def admin_event_players_csv(event_id):
    if 'user' not in session or session.get('role') != 'admin':
        return jsonify({'success': False, 'error': 'Admins only'}), 403
    try:
        payload = _fetch_event_registration_payload(event_id)
    except LookupError as exc:
        return jsonify({'success': False, 'error': str(exc)}), 404
    except Exception as exc:
        return jsonify({'success': False, 'error': str(exc)}), 500

    registrations = payload.get('registrations', [])
    event_status = (payload.get('event', {}).get('status') or '').lower()
    if event_status == 'upcoming':
        _purge_event_registrations(event_id)
        gmail_rows = []
    else:
        gmail_rows = [
            (reg.get('email') or '').strip()
            for reg in reversed(registrations)
            if (reg.get('email') or '').strip() and 'gmail' in reg.get('email', '').lower()
        ]

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(['Email'])
    for email in gmail_rows:
        writer.writerow([email])

    csv_output = buffer.getvalue()
    buffer.close()

    event_title = payload.get('event', {}).get('title') or f'event-{event_id}'
    safe_title = re.sub(r'[^0-9A-Za-z_-]+', '-', event_title).strip('-') or f'event-{event_id}'
    filename = f"{safe_title.lower()}-gmails.csv"

    response = Response(csv_output, mimetype='text/csv')
    response.headers['Content-Disposition'] = f'attachment; filename="{filename}"'
    return response


@app.route('/admin/matches/<int:event_id>')
def admin_match_detail(event_id):
    if 'user' not in session or session.get('role') != 'admin':
        flash('Admins only', 'error')
        return redirect(url_for('admin_login'))
    try:
        payload = _fetch_event_registration_payload(event_id)
    except LookupError:
        flash('Event not found', 'error')
        return redirect(url_for('admin_matches'))
    except Exception as exc:
        flash('Failed to load event data: ' + str(exc), 'error')
        return redirect(url_for('admin_matches'))

    registrations = payload.get('registrations', [])
    status_counts = {}
    for reg in registrations:
        status_key = (reg.get('status') or 'pending').lower()
        status_counts[status_key] = status_counts.get(status_key, 0) + 1
    status_priority = ['completed', 'pending', 'processing', 'manual', 'failed', 'refunded', 'cancelled']
    ordered_status_counts = []
    for key in status_priority:
        if key in status_counts:
            ordered_status_counts.append((key, status_counts[key]))
    for key, count in status_counts.items():
        if key not in status_priority:
            ordered_status_counts.append((key, count))

    total_amount_value = sum(reg.get('amount_value') or 0 for reg in registrations)
    total_amount_label = _format_currency_value(total_amount_value)

    return render_template(
        'admin_match_detail.html',
        event=payload.get('event', {}),
        registrations=registrations,
        status_counts=ordered_status_counts,
        total_amount=total_amount_label
    )


@app.route('/admin/matches/<int:event_id>/registrations/<int:registration_id>/credit_winnings', methods=['POST'])
def admin_credit_winnings(event_id, registration_id):
    if 'user' not in session or session.get('role') != 'admin':
        flash('Admins only', 'error')
        return redirect(url_for('admin_login'))

    amount_rupees = _wallet_amount_from_request(request.form.get('amount'))
    if amount_rupees is None or amount_rupees <= 0:
        flash('Enter a valid winnings amount in rupees.', 'error')
        return redirect(url_for('admin_match_detail', event_id=event_id))

    amount_paise = _wallet_rupees_to_paise(amount_rupees)
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    try:
        cur.execute('SELECT username FROM registrations WHERE id = ? AND event_id = ?', (registration_id, event_id))
        row = cur.fetchone()
        if not row:
            conn.close()
            flash('Registration not found for this event.', 'error')
            return redirect(url_for('admin_match_detail', event_id=event_id))

        username = row[0]
        cur.execute('UPDATE users SET wallet_balance = COALESCE(wallet_balance, 0) + ? WHERE username = ?', (amount_paise, username))
        cur.execute(
            'INSERT INTO wallet_transactions (username, txn_type, amount, status, note) VALUES (?, ?, ?, ?, ?)',
            (username, 'winnings', amount_paise, 'completed', f'Winnings credited by admin for event #{event_id}, registration #{registration_id}')
        )
        cur.execute(
            'INSERT INTO notifications (type, message, metadata) VALUES (?, ?, ?)',
            ('wallet_winnings_credit', f'Winnings credited to {username}', json.dumps({'event_id': event_id, 'registration_id': registration_id, 'amount_paise': amount_paise}))
        )
        conn.commit()
        flash(f'Winnings credited to {username}: ₹{amount_rupees}', 'success')
    except Exception:
        conn.rollback()
        flash('Failed to credit winnings to wallet.', 'error')
    finally:
        conn.close()

    return redirect(url_for('admin_match_detail', event_id=event_id))


@app.route('/admin/events_status')
def admin_events_status():
    """Return simple JSON with events and slots for admin live view."""
    if 'user' not in session or session.get('role') != 'admin':
        return jsonify({'success': False, 'error': 'Admins only'}), 403
    try:
        conn = sqlite3.connect(DB_NAME)
        cur = conn.cursor()
        cur.execute('SELECT id, title, mode, slots_left, max_slots, is_open FROM events ORDER BY id')
        rows = cur.fetchall()
        conn.close()
        events = []
        for r in rows:
            events.append({
                'id': r[0], 'title': r[1], 'mode': r[2], 'slots_left': r[3], 'max_slots': r[4], 'is_open': bool(r[5]) if r[5] is not None else True
            })
        return jsonify({'success': True, 'events': events})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})





@app.route('/admin/events/<int:eid>/update', methods=['PATCH'])
def admin_event_update(eid):
    if 'user' not in session or session.get('role') != 'admin':
        return jsonify({'success': False, 'error': 'Admins only'}), 403
    payload = request.get_json(force=True, silent=True) or {}
    editable_fields = {
        'title': str,
        'mode': str,
        'date': str,
        'prize': str,
        'prize_pool': str,
        'entry_fee': int,
        'description': str,
        'image': str,
        'region': str,
        'platform': str,
        'start_time': str,
        'end_time': str,
        'max_slots': int,
        'slots_left': int,
        'is_open': lambda v: 1 if str(v).lower() in ('1', 'true', 'yes', 'on') else 0
    }
    updates = {}
    for field, caster in editable_fields.items():
        if field in payload:
            try:
                updates[field] = caster(payload[field])
            except Exception:
                return jsonify({'success': False, 'error': f'Invalid value for {field}'}), 400
    if not updates:
        return jsonify({'success': False, 'error': 'No updatable fields supplied'}), 400
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute('SELECT id FROM events WHERE id = ?', (eid,))
    if not cur.fetchone():
        conn.close()
        return jsonify({'success': False, 'error': 'Event not found'}), 404
    set_clause = ', '.join(f"{field} = ?" for field in updates)
    params = list(updates.values()) + [eid]
    cur.execute(f'UPDATE events SET {set_clause} WHERE id = ?', params)
    conn.commit()
    conn.close()
    return jsonify({'success': True, 'updated_fields': list(updates.keys())})


# --- Google OAuth login ---
@app.route('/login/google')
def google_login():
    refresh_google_config()
    if not is_google_login_ready():
        flash('Google login is not configured. Set GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET.', 'error')
        return redirect(url_for('login'))
    # ensure session cookie persists across the redirect
    session.permanent = True
    oauth = _get_google_oauth_session()
    authorization_url, state = oauth.authorization_url(
        GOOGLE_AUTHORIZATION_BASE_URL,
        access_type='offline',
        prompt='select_account'
    )
    try:
        print('[GOOGLE_LOGIN] host=', request.host_url, 'redirect_uri=', oauth.redirect_uri, 'state=', state, 'client_id=', GOOGLE_CLIENT_ID, 'auth_url=', authorization_url)
    except Exception:
        pass
    session['google_oauth_state'] = state
    next_target = request.args.get('next')
    if next_target and next_target.startswith('/'):
        session['google_next'] = next_target
    return redirect(authorization_url)

@app.route('/auth/google/callback')
def google_callback():
    refresh_google_config()
    if not is_google_login_ready():
        flash('Google login is not configured. Set GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET.', 'error')
        return redirect(url_for('login'))
    state = session.get('google_oauth_state')
    incoming_state = request.args.get('state')
    if not state and incoming_state:
        # If host mismatch prevented the session cookie from being sent, recover using the incoming state once.
        session['google_oauth_state'] = incoming_state
        session.permanent = True
        state = incoming_state
    try:
        print('[GOOGLE_CALLBACK] host=', request.host_url, 'incoming_state=', incoming_state, 'session_state=', state, 'full_url=', request.url, 'client_id=', GOOGLE_CLIENT_ID)
    except Exception:
        pass
    if not state:
        flash('Google login session expired. Please try again.', 'error')
        return redirect(url_for('login'))
    try:
        oauth = _get_google_oauth_session(state=state)
        token = oauth.fetch_token(
            GOOGLE_TOKEN_URL,
            client_secret=GOOGLE_CLIENT_SECRET,
            authorization_response=request.url
        )
        oauth = _get_google_oauth_session(token=token)
        profile = oauth.get(GOOGLE_USERINFO_URL).json()
        try:
            print('[GOOGLE_CALLBACK] token received; token_keys=', list(token.keys()), 'profile=', profile)
        except Exception:
            pass
    except Exception as ex:
        print('Google OAuth error:', ex)
        flash('Failed to authenticate with Google.', 'error')
        return redirect(url_for('login'))
    email = profile.get('email')
    display_name = profile.get('name') or profile.get('given_name') or 'Player'
    if not email:
        flash('Google account did not provide an email address.', 'error')
        return redirect(url_for('login'))
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute("SELECT username, role FROM users WHERE email = ?", (email,))
    row = cur.fetchone()
    if row:
        username, role = row
    else:
        base = email.split('@')[0].replace(' ', '').lower()
        candidate = base
        cur.execute("SELECT 1 FROM users WHERE username = ?", (candidate,))
        counter = 1
        while cur.fetchone():
            counter += 1
            candidate = f"{base}{counter}"
            cur.execute("SELECT 1 FROM users WHERE username = ?", (candidate,))
        username, role = candidate, 'player'
        hashed = generate_password_hash(secrets.token_urlsafe(16))
        try:
            cur.execute("""
                INSERT INTO users (username, email, password, role, game_id, phone, admin_code)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (username, email, hashed, role, '', '', None))
            conn.commit()
        except Exception as ex:
            conn.rollback()
            conn.close()
            print('Google account creation failed:', ex)
            flash('Could not create an account from Google login.', 'error')
            return redirect(url_for('login'))
        conn.close()
        # Persist the login so the session survives the OAuth redirect roundtrip.
        session.permanent = True
    session['user'] = username
    session['role'] = role
    session['display_name'] = display_name
    try:
            print('[GOOGLE_CALLBACK] session set ->', {
                'user': session.get('user'),
                'role': session.get('role'),
                'display_name': session.get('display_name'),
                'permanent': session.permanent
            })
    except Exception:
            pass
    flash(f"Welcome, {display_name}!", 'success')
    next_target = session.pop('google_next', None)
    if next_target and next_target.startswith('/'):
        return redirect(next_target)
    if role == 'admin':
        return redirect(url_for('admin_dashboard'))
    return redirect(url_for('home'))

@app.route('/auth/callback')
def google_callback_alias():
    """Support older Google OAuth redirect URIs."""
    return google_callback()

# Initialize the database for both local runs and WSGI servers (e.g., gunicorn).
try:
    init_db()
    ensure_bootstrap_admin()
except Exception as db_init_error:
    print('Database initialization failed:', db_init_error)

if __name__ == "__main__":
    app.run(
        host=os.getenv('HOST', '127.0.0.1'),
        port=int(os.getenv('PORT', '5000')),
        debug=_is_truthy(os.getenv('FLASK_DEBUG', '0'))
    )