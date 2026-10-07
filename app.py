import os
import re
from datetime import date, datetime
from pathlib import Path
import calendar
import hashlib
import json
import uuid
import pandas as pd
import plotly.express as px
import streamlit as st
from streamlit_autorefresh import st_autorefresh

# ─── Streamlit < 1.27 compatibility shim ───────────────────────────────────
if not hasattr(st, "rerun"):
    st.rerun = st.experimental_rerun

# ==========================================
# 1. DATABASE CONNECTION  (PostgreSQL via Neon.tech)
# ==========================================
# On Streamlit Cloud: set DATABASE_URL in Advanced Settings → Secrets
# Locally:           set env var DATABASE_URL, or we fall back to the
#                    connection string hard-coded below (development only).
try:
    import psycopg2
    import psycopg2.extras
    _PG_AVAILABLE = True
except ImportError:
    _PG_AVAILABLE = False

# Resolve the DATABASE_URL (Streamlit secrets → env var → dev fallback)
def _get_database_url() -> str:
    # 1. Streamlit Secrets (Streamlit Cloud deployment)
    try:
        return st.secrets["DATABASE_URL"]
    except Exception:
        pass
    # 2. Environment variable (local dev with export DATABASE_URL=...)
    url = os.environ.get("DATABASE_URL", "")
    if url:
        return url
    # 3. Hard-coded dev fallback (replace if needed)
    return ""

DATABASE_URL = _get_database_url()

def _pg():
    """Return a fresh psycopg2 connection."""
    conn = psycopg2.connect(DATABASE_URL)
    return conn

def _pg_execute(sql: str, params=(), fetch: bool = False):
    """Run a single SQL statement; return rows if fetch=True, lastval otherwise."""
    conn = _pg()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute(sql, params)
    result = None
    if fetch:
        result = cur.fetchall()
    else:
        try:
            cur.execute("SELECT lastval()")
            result = cur.fetchone()["lastval"]
        except Exception:
            result = None
    conn.commit()
    conn.close()
    return result

def _pg_query(sql: str, params=()) -> pd.DataFrame:
    """Return a query as a pandas DataFrame."""
    conn = _pg()
    df = pd.read_sql_query(sql, conn, params=params)
    conn.close()
    return df

# ==========================================
# 2. SCHEMA INIT (create tables once on first boot)
# ==========================================
def init_schema():
    conn = _pg()
    cur = conn.cursor()
    cur.execute("""
    CREATE TABLE IF NOT EXISTS auth_users (
        id SERIAL PRIMARY KEY,
        username TEXT UNIQUE NOT NULL,
        password_hash TEXT NOT NULL,
        salt TEXT NOT NULL,
        full_name TEXT NOT NULL,
        recovery_pin TEXT NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    );

    CREATE TABLE IF NOT EXISTS transactions (
        id SERIAL PRIMARY KEY,
        owner TEXT NOT NULL,
        txn_date DATE NOT NULL,
        kind TEXT NOT NULL CHECK(kind IN ('income','expense')),
        category TEXT NOT NULL,
        expense_type TEXT,
        amount NUMERIC(14,2) NOT NULL CHECK(amount >= 0),
        payment_method TEXT,
        merchant TEXT,
        note TEXT,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    );

    CREATE INDEX IF NOT EXISTS idx_txn_owner ON transactions(owner);
    CREATE INDEX IF NOT EXISTS idx_txn_date  ON transactions(txn_date);
    CREATE INDEX IF NOT EXISTS idx_txn_kind  ON transactions(kind);

    CREATE TABLE IF NOT EXISTS budgets (
        id SERIAL PRIMARY KEY,
        owner TEXT NOT NULL,
        month TEXT NOT NULL,
        category TEXT NOT NULL,
        percent NUMERIC(6,2) NOT NULL DEFAULT 0,
        amount  NUMERIC(14,2) DEFAULT 0,
        UNIQUE(owner, month, category)
    );

    CREATE TABLE IF NOT EXISTS accounts (
        id SERIAL PRIMARY KEY,
        owner TEXT NOT NULL,
        name TEXT NOT NULL,
        opening_balance NUMERIC(14,2) NOT NULL DEFAULT 0,
        current_balance NUMERIC(14,2) NOT NULL DEFAULT 0,
        UNIQUE(owner, name)
    );
    """)
    conn.commit()

    # Seed admin account if the auth_users table is empty
    cur.execute("SELECT COUNT(*) FROM auth_users")
    if cur.fetchone()[0] == 0:
        salt = uuid.uuid4().hex
        p_hash = _hash_pw("admin123", salt)
        cur.execute("""
            INSERT INTO auth_users (username, password_hash, salt, full_name, recovery_pin)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT DO NOTHING
        """, ("admin", p_hash, salt, "Administrator", "1234"))
        conn.commit()

    conn.close()

# ==========================================
# 3. CONSTANTS
# ==========================================
CATEGORIES = [
    "Housing", "Food", "Transport", "Utilities", "Health", "Family",
    "Shopping", "Entertainment", "Travel", "Education", "Subscriptions",
    "Debt", "Savings", "Other"
]
INCOME_CATEGORIES = ["Salary", "Bonus", "Business", "Investment", "Other"]
PAYMENTS = ["Cash", "ABA", "ACLEDA", "Wing", "Bakong", "Credit Card", "Bank Transfer", "Other"]
EXPENSE_TYPES = ["Need", "Want", "Bill", "Debt", "Saving", "Investment"]

# ==========================================
# 4. PAGE CONFIG
# ==========================================
st.set_page_config(
    page_title="My Finance Pro",
    page_icon="💰",
    layout="wide",
    initial_sidebar_state="expanded"
)

# ==========================================
# 5. AUTHENTICATION HELPERS
# ==========================================
def _hash_pw(password: str, salt: str) -> str:
    return hashlib.sha256((password + salt).encode("utf-8")).hexdigest()

def authenticate_user(username: str, password: str):
    conn = _pg()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute(
        "SELECT id, username, password_hash, salt, full_name FROM auth_users WHERE LOWER(username)=LOWER(%s)",
        (username.strip(),)
    )
    row = cur.fetchone()
    conn.close()
    if row and _hash_pw(password, row["salt"]) == row["password_hash"]:
        return {"id": row["id"], "username": row["username"], "full_name": row["full_name"]}
    return None

def register_user(username: str, password: str, full_name: str, recovery_pin: str):
    salt = uuid.uuid4().hex
    p_hash = _hash_pw(password, salt)
    try:
        _pg_execute(
            "INSERT INTO auth_users (username, password_hash, salt, full_name, recovery_pin) VALUES (%s,%s,%s,%s,%s)",
            (username.strip().lower(), p_hash, salt, full_name.strip(), recovery_pin.strip())
        )
        return True, "Account created successfully! You can now log in."
    except Exception as e:
        if "unique" in str(e).lower():
            return False, "Username already exists. Please choose another username."
        return False, f"Registration error: {e}"

def reset_user_password(username: str, recovery_pin: str, new_password: str):
    conn = _pg()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT id, recovery_pin FROM auth_users WHERE LOWER(username)=LOWER(%s)", (username.strip(),))
    row = cur.fetchone()
    conn.close()
    if not row:
        return False, "Username not found."
    if row["recovery_pin"].strip() != recovery_pin.strip():
        return False, "Incorrect recovery PIN."
    new_salt = uuid.uuid4().hex
    new_hash = _hash_pw(new_password, new_salt)
    _pg_execute(
        "UPDATE auth_users SET password_hash=%s, salt=%s WHERE id=%s",
        (new_hash, new_salt, row["id"])
    )
    return True, "Password reset successfully! Please log in with your new password."

# ─── Session persistence (cookie-like JSON file on Streamlit Cloud /tmp) ──
_SESSION_FILE = Path("/tmp/finance_session.json")

def save_persistent_session(user_info: dict):
    try:
        _SESSION_FILE.write_text(json.dumps(user_info))
    except Exception:
        pass

def load_persistent_session():
    try:
        if _SESSION_FILE.exists():
            data = json.loads(_SESSION_FILE.read_text())
            if "username" in data:
                return data
    except Exception:
        pass
    return None

def clear_persistent_session():
    try:
        _SESSION_FILE.unlink(missing_ok=True)
    except Exception:
        pass

# ==========================================
# 6. BOOT (schema + session)
# ==========================================
if not DATABASE_URL:
    st.error(
        "⚠️ **DATABASE_URL not configured.**\n\n"
        "On Streamlit Cloud: add `DATABASE_URL` to **Advanced Settings → Secrets**.\n\n"
        "Locally: `export DATABASE_URL='postgresql://...'`"
    )
    st.stop()

try:
    init_schema()
except Exception as _e:
    st.error(f"Database connection failed: {_e}")
    st.stop()

if "logged_in_user" not in st.session_state:
    st.session_state.logged_in_user = load_persistent_session()

# ==========================================
# 7. CSS STYLING
# ==========================================
def css():
    st.markdown("""
    <style>
    @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap');
    html, body, [class*="css"] { font-family: Inter, sans-serif; }
    .block-container { padding-top: 1.2rem; max-width: 1500px; }
    [data-testid="stSidebar"] { border-right: 1px solid rgba(128,128,128,.18); }

    /* Sidebar Modern Redesign */
    .sidebar-brand-box {
        display: flex; align-items: center; gap: 12px;
        padding: 12px 14px;
        background: linear-gradient(135deg, rgba(30,41,59,.05), rgba(15,23,42,.08));
        border: 1px solid rgba(128,128,128,.16); border-radius: 14px; margin-bottom: 14px;
    }
    .brand-icon-box {
        font-size: 22px; width: 38px; height: 38px;
        display: flex; align-items: center; justify-content: center;
        background: linear-gradient(135deg,#2563eb,#1d4ed8);
        border-radius: 10px; box-shadow: 0 4px 10px rgba(37,99,235,.25);
    }
    .brand-name-title  { font-size:16px; font-weight:800; color:#0f172a; line-height:1.2; }
    .brand-tagline-text{ font-size:11px; font-weight:600; color:#64748b; margin-top:2px; }
    .user-profile-widget{
        display:flex; align-items:center; gap:10px;
        padding:6px 10px;
        background:rgba(128,128,128,.05); border:1px solid rgba(128,128,128,.14);
        border-radius:12px; min-height:44px;
    }
    .user-avatar-circle{
        width:32px; height:32px; border-radius:50%;
        background:linear-gradient(135deg,#059669,#10b981);
        color:white; font-size:12px; font-weight:800;
        display:flex; align-items:center; justify-content:center;
        box-shadow:0 2px 6px rgba(16,185,129,.25); flex-shrink:0;
    }
    .user-fullname-text{font-size:13px;font-weight:700;color:#1e293b;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;line-height:1.2;}
    .user-handle-badge {font-size:11px;font-weight:600;color:#64748b;margin-top:1px;}
    .sidebar-section-heading{font-size:11px;font-weight:700;color:#94a3b8;letter-spacing:.08em;text-transform:uppercase;margin:16px 4px 8px;}
    [data-testid="stSidebar"] .stButton > button {
        display:flex !important; align-items:center !important;
        justify-content:flex-start !important; text-align:left !important;
        padding:10px 14px !important; border-radius:10px !important;
        font-size:13.5px !important; font-weight:600 !important;
        margin-bottom:4px !important; transition:all .15s ease-in-out !important;
    }
    [data-testid="stSidebar"] .stButton > button[kind="primary"] {
        background:linear-gradient(135deg,#2563eb 0%,#1d4ed8 100%) !important;
        color:#fff !important; border:1px solid #1d4ed8 !important;
        box-shadow:0 4px 12px rgba(37,99,235,.25) !important; font-weight:700 !important;
    }
    [data-testid="stSidebar"] .stButton > button[kind="secondary"] {
        background:rgba(255,255,255,.6) !important; color:#334155 !important;
        border:1px solid rgba(203,213,225,.8) !important;
    }
    [data-testid="stSidebar"] .stButton > button[kind="secondary"]:hover {
        background:#f1f5f9 !important; color:#0f172a !important;
        border-color:#94a3b8 !important; transform:translateX(2px) !important;
    }
    [data-testid="stSidebar"] [data-testid="column"]:last-child .stButton > button {
        justify-content:center !important; text-align:center !important;
        padding:6px 10px !important; font-size:12px !important;
        border-radius:10px !important; height:44px !important;
        color:#ef4444 !important;
        border:1px solid rgba(239,68,68,.25) !important;
        background:rgba(239,68,68,.05) !important;
    }
    [data-testid="stSidebar"] [data-testid="column"]:last-child .stButton > button:hover {
        background:#ef4444 !important; color:#fff !important;
        border-color:#ef4444 !important; transform:none !important;
    }
    .rate-pill{
        display:inline-block; background:#e0f2fe; color:#0369a1;
        font-weight:700; font-size:12px; padding:5px 10px;
        border-radius:8px; margin-top:6px; border:1px solid #bae6fd;
        width:100%; text-align:center;
    }
    .sidebar-footer-text{
        text-align:center; font-size:11px; color:#94a3b8;
        margin-top:24px; padding-top:14px;
        border-top:1px dashed rgba(128,128,128,.2);
    }
    .hero{
        padding:24px 26px; border-radius:22px;
        background:linear-gradient(135deg,#111827,#1f2937);
        color:white; margin-bottom:18px;
        box-shadow:0 12px 35px rgba(0,0,0,.12);
    }
    .hero h1{margin:0; font-size:32px; font-weight:800;}
    .hero p{margin:6px 0 0; opacity:.75;}
    .card{
        padding:18px; border-radius:18px;
        border:1px solid rgba(128,128,128,.16);
        background:rgba(128,128,128,.045);
        min-height:115px;
    }
    .label{font-size:12px;opacity:.68;font-weight:600;text-transform:uppercase;letter-spacing:.05em;}
    .value{
        font-size:22px !important; font-weight:800; margin-top:7px;
        white-space:normal !important; word-break:break-word !important; line-height:1.2 !important;
    }
    div[data-testid="metric-container"] div[data-testid="stMetricValue"]{
        font-size:1.45rem !important; white-space:normal !important;
        word-break:break-word !important; line-height:1.2 !important;
    }
    .positive{color:#16a34a;} .negative{color:#dc2626;}
    .muted{opacity:.65;font-size:13px;}
    .section-title{font-size:21px;font-weight:800;margin:20px 0 10px;}
    .budget-item{
        background:white; border:1px solid #e2e8f0; border-radius:14px;
        padding:14px 18px; margin-bottom:12px; box-shadow:0 2px 4px rgba(0,0,0,.02);
    }
    .khr-tag{background:#e0f2fe;color:#0369a1;padding:2px 8px;border-radius:6px;font-size:12px;font-weight:700;}
    div.stButton > button{border-radius:12px;font-weight:600;}
    @media(max-width:700px){
      .block-container{padding:.7rem .7rem 4rem;}
      .hero{padding:18px;border-radius:17px;}
      .hero h1{font-size:25px;}
      .value{font-size:20px;}
      .card{min-height:95px;padding:14px;}
    }
    </style>
    """, unsafe_allow_html=True)

css()

# ==========================================
# 8. LOGIN / REGISTRATION SCREEN
# ==========================================
if st.session_state.logged_in_user is None:
    st.markdown("""
    <div style='text-align:center;margin-top:35px;margin-bottom:25px;'>
        <h1 style='font-size:34px;font-weight:800;color:#111827;'>💰 My Finance Pro</h1>
        <p style='color:#6b7280;font-size:15px;'>Private multi-user personal finance · USD &amp; KHR dual currency · Powered by Neon PostgreSQL</p>
    </div>
    """, unsafe_allow_html=True)

    col_l1, col_l2, col_l3 = st.columns([1, 2, 1])
    with col_l2:
        tab_login, tab_reg, tab_reset = st.tabs(["🔐 Log In", "📝 Create Account", "🔑 Reset Password"])

        with tab_login:
            st.markdown("### Sign In")
            with st.form("login_form"):
                u_in = st.text_input("Username", value="admin", placeholder="e.g. admin")
                p_in = st.text_input("Password", value="admin123", type="password")
                remember_me = st.checkbox("Remember me on this computer", value=True)
                if st.form_submit_button("🚀 Log In", use_container_width=True, type="primary"):
                    user = authenticate_user(u_in, p_in)
                    if user:
                        st.session_state.logged_in_user = user
                        if remember_me:
                            save_persistent_session(user)
                        st.success(f"Welcome back, {user['full_name']}!")
                        st.rerun()
                    else:
                        st.error("Invalid username or password.")
            st.caption("Default admin credentials: Username: `admin` | Password: `admin123`")

        with tab_reg:
            st.markdown("### Create New Account")
            st.caption("Each account's data is isolated by your username.")
            with st.form("reg_form"):
                r_name = st.text_input("Full Name", placeholder="e.g. Sokha Doe")
                r_user = st.text_input("Username", placeholder="e.g. sokha")
                r_pass = st.text_input("Password", type="password")
                r_pin  = st.text_input("Recovery PIN (4-6 digits)", placeholder="e.g. 1234")
                if st.form_submit_button("✨ Register Account", use_container_width=True, type="primary"):
                    if not r_name or not r_user or not r_pass or not r_pin:
                        st.error("All fields are required.")
                    else:
                        ok, msg = register_user(r_user, r_pass, r_name, r_pin)
                        if ok: st.success(msg)
                        else: st.error(msg)

        with tab_reset:
            st.markdown("### Reset Forgotten Password")
            with st.form("reset_form"):
                rs_user  = st.text_input("Username")
                rs_pin   = st.text_input("Recovery PIN", type="password")
                rs_new_p = st.text_input("New Password", type="password")
                if st.form_submit_button("🔄 Update Password", use_container_width=True, type="primary"):
                    if not rs_user or not rs_pin or not rs_new_p:
                        st.error("All fields are required.")
                    else:
                        ok, msg = reset_user_password(rs_user, rs_pin, rs_new_p)
                        if ok: st.success(msg)
                        else: st.error(msg)
    st.stop()

# ==========================================
# 9. PER-USER DATA HELPERS (PostgreSQL)
# ==========================================
current_user = st.session_state.logged_in_user
OWNER = current_user["username"]

def _seed_accounts():
    """Seed default accounts for a brand-new user."""
    conn = _pg()
    cur  = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM accounts WHERE owner=%s", (OWNER,))
    if cur.fetchone()[0] == 0:
        defaults = [
            (OWNER, "Cash",        0.0,  180.0),
            (OWNER, "ABA",         0.0, 1230.0),
            (OWNER, "ACLEDA",      0.0,  600.0),
            (OWNER, "Bakong",      0.0,  250.0),
            (OWNER, "Credit Card", 0.0, -500.0),
        ]
        cur.executemany(
            "INSERT INTO accounts (owner,name,opening_balance,current_balance) VALUES (%s,%s,%s,%s) ON CONFLICT DO NOTHING",
            defaults
        )
        conn.commit()
    conn.close()

_seed_accounts()

def execute(sql: str, params=()):
    return _pg_execute(sql, params, fetch=False)

def query(sql: str, params=()) -> pd.DataFrame:
    return _pg_query(sql, params)

def load_txns(start=None, end=None) -> pd.DataFrame:
    sql    = "SELECT * FROM transactions WHERE owner=%s"
    params = [OWNER]
    if start:
        sql += " AND txn_date >= %s"; params.append(str(start))
    if end:
        sql += " AND txn_date <= %s"; params.append(str(end))
    sql += " ORDER BY txn_date DESC, id DESC"
    return query(sql, params)

# ==========================================
# 10. CURRENCY HELPERS
# ==========================================
def money(x, mode=None, rate=None):
    if mode is None: mode = st.session_state.get("currency_mode", "Dual (USD / KHR)")
    if rate is None: rate = st.session_state.get("exchange_rate", 4100.0)
    try:    val = float(x)
    except: val = 0.0
    sign    = "-" if val < 0 else ""
    abs_usd = abs(val)
    abs_khr = round(abs_usd * rate)
    if mode == "USD ($)":  return f"{sign}${abs_usd:,.2f}"
    if mode == "KHR (៛)": return f"{sign}{abs_khr:,.0f} ៛"
    return f"{sign}${abs_usd:,.2f} ({sign}{abs_khr:,.0f} ៛)"

def format_usd(x):
    try:    v = float(x)
    except: v = 0.0
    return f"{'-' if v<0 else ''}${abs(v):,.2f}"

def format_khr(x, rate=None):
    if rate is None: rate = st.session_state.get("exchange_rate", 4100.0)
    try:    v = float(x)
    except: v = 0.0
    return f"{'-' if v<0 else ''}{round(abs(v)*rate):,.0f} ៛"

# ==========================================
# 11. DATE / PERIOD HELPERS
# ==========================================
def month_key(d): return d.strftime("%Y-%m")

def get_month_range(year, month):
    n = calendar.monthrange(year, month)[1]
    return date(year, month, 1), date(year, month, n)

def current_month_range():
    t = date.today()
    return t.replace(day=1), date(t.year, t.month, calendar.monthrange(t.year, t.month)[1])

def monthly_metrics(df):
    if df.empty: return 0.0, 0.0
    return float(df.loc[df.kind=="income","amount"].sum()), float(df.loc[df.kind=="expense","amount"].sum())

# ==========================================
# 12. LIVE STATE
# ==========================================
st_autorefresh(interval=10000, key="live_refresh")

if "page" not in st.session_state:
    st.session_state.page = "Dashboard"
if "show_quick_expense" not in st.session_state:
    st.session_state.show_quick_expense = False

today  = date.today()
mstart, mend = current_month_range()
month  = month_key(today)

# ==========================================
# 13. SIDEBAR
# ==========================================
with st.sidebar:
    st.markdown("""
    <div class="sidebar-brand-box">
        <div class="brand-icon-box">💰</div>
        <div>
            <div class="brand-name-title">My Finance Pro</div>
            <div class="brand-tagline-text">Private Wealth Suite</div>
        </div>
    </div>
    """, unsafe_allow_html=True)

    col_u1, col_u2 = st.columns([2.7, 1.3])
    with col_u1:
        initials = "".join([p[0].upper() for p in current_user.get("full_name","U").split()][:2]) or "U"
        st.markdown(f"""
        <div class="user-profile-widget">
            <div class="user-avatar-circle">{initials}</div>
            <div class="user-info-meta">
                <div class="user-fullname-text">{current_user['full_name']}</div>
                <div class="user-handle-badge">@{current_user['username']}</div>
            </div>
        </div>
        """, unsafe_allow_html=True)
    with col_u2:
        if st.button("Logout", key="logout_btn", use_container_width=True):
            clear_persistent_session()
            st.session_state.logged_in_user = None
            st.rerun()

    st.markdown('<div class="sidebar-section-heading">MAIN NAVIGATION</div>', unsafe_allow_html=True)
    nav_items = [
        ("📊 Dashboard",    "Dashboard"),
        ("📝 Daily Entry",  "Daily Entry"),
        ("📋 Transactions", "Transactions"),
        ("📈 Analytics",    "Analytics"),
        ("🎯 Budget",       "Budget"),
        ("⚙️ Settings",     "Settings"),
    ]
    for label, p_name in nav_items:
        is_active = (st.session_state.page == p_name)
        if st.button(label, key=f"nav_{p_name}", use_container_width=True,
                     type="primary" if is_active else "secondary"):
            st.session_state.page = p_name
            st.rerun()

    st.markdown('<div class="sidebar-section-heading" style="margin-top:20px;">🇰🇭 CURRENCY PREFERENCES</div>',
                unsafe_allow_html=True)
    currency_mode = st.radio(
        "Display Currency",
        ["Dual (USD / KHR)", "USD ($)", "KHR (៛)"],
        index=0, key="currency_mode"
    )
    exchange_rate = st.number_input(
        "Exchange Rate (1 USD = KHR)",
        min_value=1000.0, max_value=10000.0, value=4100.0, step=50.0, key="exchange_rate"
    )
    st.markdown(f'<div class="rate-pill">💡 Standard Rate: 1 USD = {exchange_rate:,.0f} ៛</div>',
                unsafe_allow_html=True)

    st.markdown("""
    <div class="sidebar-footer-text">
        🔒 Powered by Neon PostgreSQL<br>
        <span style="font-size:10px;opacity:.8;">Personal Finance Pro · v3.0</span>
    </div>
    """, unsafe_allow_html=True)

page = st.session_state.page

# ==========================================
# PAGE 1: DASHBOARD
# ==========================================
if page == "Dashboard":
    st.markdown(f"""
    <div class="hero">
      <div style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:12px;margin-bottom:6px;">
        <h1 style="margin:0;">Good day, {current_user['full_name']} 👋</h1>
        <div style="background:linear-gradient(135deg,rgba(244,63,94,.2),rgba(236,72,153,.28));border:1px solid rgba(251,113,133,.45);padding:6px 15px;border-radius:999px;font-size:13px;font-weight:600;color:#ffe4e6;display:inline-flex;align-items:center;gap:5px;box-shadow:0 4px 14px rgba(225,29,72,.2);">
          <span>Developed by <strong style="color:#fff;">RADA KIM</strong>, For <strong style="color:#fff;">Porn Srey Nit</strong> ❤️</span>
        </div>
      </div>
      <p>{today.strftime("%A, %d %B %Y")} · Financial overview · Live monitoring enabled</p>
    </div>
    """, unsafe_allow_html=True)

    # ── Quick Expense Button ──────────────────────────────────────
    qe_col, _ = st.columns([1, 4])
    with qe_col:
        if st.button("➕ Quick Add Expense", type="primary", use_container_width=True, key="quick_exp_btn"):
            st.session_state.show_quick_expense = not st.session_state.show_quick_expense

    if st.session_state.show_quick_expense:
        with st.expander("💸 Quick Expense Entry", expanded=True):
            with st.form("quick_expense_form", clear_on_submit=True):
                qc1, qc2, qc3 = st.columns(3)
                q_date     = qc1.date_input("Date", value=today, key="qe_date")
                q_amount   = qc2.number_input("Amount ($ USD)", min_value=0.01, step=1.0, format="%.2f", key="qe_amount")
                q_category = qc3.selectbox("Category", CATEGORIES, key="qe_cat")
                qc4, qc5, qc6 = st.columns(3)
                q_etype    = qc4.selectbox("Expense Type", EXPENSE_TYPES, key="qe_type")
                q_payment  = qc5.selectbox("Payment Method", PAYMENTS, key="qe_pay")
                q_merchant = qc6.text_input("Merchant / Place", key="qe_merch")
                q_note     = st.text_input("Note (optional)", key="qe_note")
                sub_q = st.form_submit_button("✅ Save Expense", use_container_width=True, type="primary")
                if sub_q:
                    if q_amount <= 0:
                        st.error("Enter an amount greater than zero.")
                    else:
                        execute("""
                            INSERT INTO transactions
                            (owner,txn_date,kind,category,expense_type,amount,payment_method,merchant,note,updated_at)
                            VALUES (%s,%s,'expense',%s,%s,%s,%s,%s,%s,NOW())
                        """, (OWNER, str(q_date), q_category, q_etype, q_amount, q_payment, q_merchant, q_note))
                        execute(
                            "UPDATE accounts SET current_balance=current_balance-%s WHERE owner=%s AND name=%s",
                            (q_amount, OWNER, q_payment)
                        )
                        st.success(f"Expense of {money(q_amount)} saved!")
                        st.session_state.show_quick_expense = False
                        st.rerun()
    st.markdown("---")

    # Month filter
    months_list = []
    for i in range(12):
        m = today.month - i; y = today.year
        while m <= 0: m += 12; y -= 1
        months_list.append(f"{y}-{m:02d}")

    f_col1, f_col2 = st.columns([1.5, 2])
    with f_col1:
        period_choice = st.selectbox("📅 Period Filter:", ["Current Month","All Time","Custom Range"] + months_list)

    if period_choice == "Current Month":
        start_date, end_date = get_month_range(today.year, today.month)
    elif period_choice == "All Time":
        start_date, end_date = date(2020,1,1), date(2035,12,31)
    elif period_choice == "Custom Range":
        with f_col2:
            dr = st.date_input("Select Range", (today.replace(day=1), mend))
            start_date, end_date = dr if len(dr)==2 else get_month_range(today.year, today.month)
    else:
        y_val, m_val = map(int, period_choice.split("-"))
        start_date, end_date = get_month_range(y_val, m_val)

    df = load_txns(start_date, end_date)
    income, expense = monthly_metrics(df)
    balance = income - expense
    saving_rate = (balance / income * 100) if income else 0

    total_days = (end_date - start_date).days + 1
    elapsed    = min(max(today.day, 1), total_days) if period_choice == "Current Month" else total_days
    daily_avg  = (expense / elapsed) if elapsed > 0 else 0.0
    forecast   = daily_avg * total_days

    budget_df    = query("SELECT category, percent FROM budgets WHERE owner=%s AND month=%s", (OWNER, month))
    total_bpct   = budget_df["percent"].sum() if not budget_df.empty else 0
    budget_amount= income * total_bpct / 100
    budget_used  = expense / budget_amount * 100 if budget_amount else 0

    cols = st.columns(4)
    cards = [
        ("Income",      money(income),             "positive"),
        ("Expenses",    money(expense),            "negative"),
        ("Balance",     money(balance),            "positive" if balance >= 0 else "negative"),
        ("Saving rate", f"{saving_rate:.1f}%",     "positive" if saving_rate >= 0 else "negative"),
    ]
    for c, (lab, val, cl) in zip(cols, cards):
        c.markdown(f'<div class="card"><div class="label">{lab}</div><div class="value {cl}">{val}</div><div class="muted">Selected Period</div></div>', unsafe_allow_html=True)

    st.markdown('<div class="section-title">Live spending monitor</div>', unsafe_allow_html=True)
    col_fc1, col_fc2 = st.columns([2,1])
    with col_fc1:
        st.info(
            f"💡 **Month-end Forecast:** Average spending is **{money(daily_avg)}/day** "
            f"(day {elapsed}/{total_days}). Projected month-end total: **{money(forecast)}**."
        )
        if income > 0 and forecast > income:
            st.error(f"⚠️ **Overspending Warning:** Projected {money(forecast)} exceeds income {money(income)} by {money(forecast-income)}!")
    with col_fc2:
        st.caption("Pace Status")
        st.progress(min(elapsed / total_days, 1.0))
        st.caption(f"{elapsed} of {total_days} days elapsed ({elapsed/total_days*100:.0f}% of month)")

    c1, c2, c3 = st.columns(3)
    c1.metric("Forecast month expense", money(forecast))
    c2.metric("Budget used", f"{budget_used:.1f}%" if budget_amount else "Set budget")
    c3.metric("Transactions", len(df))

    st.markdown('<div class="section-title">Visual Analytics</div>', unsafe_allow_html=True)
    expdf = df[df.kind=="expense"] if not df.empty else pd.DataFrame()

    left, right = st.columns([1.2,1])
    with left:
        st.subheader("🥧 Spending by Category")
        if not expdf.empty:
            cat_sum = expdf.groupby("category", as_index=False)["amount"].sum().sort_values("amount", ascending=False)
            fig_pie = px.pie(cat_sum, values="amount", names="category", hole=0.45,
                             color_discrete_sequence=px.colors.qualitative.Safe)
            fig_pie.update_layout(height=350, margin=dict(l=5,r=5,t=10,b=5))
            st.plotly_chart(fig_pie, use_container_width=True, config={"displayModeBar":False})
        else:
            st.info("No expenses recorded in this period.")
    with right:
        st.subheader("⚖️ Expense Classification")
        if not expdf.empty:
            type_sum = expdf.groupby("expense_type", as_index=False)["amount"].sum()
            fig_bar  = px.bar(type_sum, x="expense_type", y="amount", color="expense_type", text_auto=".2s",
                              color_discrete_map={"Need":"#3b82f6","Want":"#f97316","Bill":"#ef4444",
                                                  "Debt":"#a855f7","Saving":"#10b981","Investment":"#06b6d4"})
            fig_bar.update_layout(showlegend=False, height=350, margin=dict(l=5,r=5,t=10,b=5),
                                  xaxis_title="", yaxis_title="Amount ($)")
            st.plotly_chart(fig_bar, use_container_width=True, config={"displayModeBar":False})
        else:
            st.info("No expense categories found.")

    st.subheader("📊 Daily Cashflow (Income vs Expense)")
    if not df.empty:
        daily_flow = df.groupby(["txn_date","kind"], as_index=False)["amount"].sum()
        fig_flow = px.bar(daily_flow, x="txn_date", y="amount", color="kind", barmode="group",
                          color_discrete_map={"income":"#22c55e","expense":"#ef4444"})
        fig_flow.update_layout(height=300, margin=dict(l=5,r=5,t=10,b=5),
                               xaxis_title="Date", yaxis_title="Amount ($)", hovermode="x unified")
        st.plotly_chart(fig_flow, use_container_width=True, config={"displayModeBar":False})
    else:
        st.caption("No cashflow transactions recorded.")

    st.divider()
    col_big, col_recent = st.columns([1.2,1])
    with col_big:
        st.subheader("🔥 Largest Expenses")
        if not expdf.empty:
            for _, r in expdf.sort_values("amount", ascending=False).head(5).iterrows():
                m_txt = r['merchant'] if r['merchant'] else r['category']
                st.markdown(f"""
                <div class="budget-item">
                    <b>{m_txt}</b> <span class="khr-tag">{r['category']}</span>
                    <span style="float:right;font-weight:bold;color:#dc2626;">{money(r['amount'])}</span><br>
                    <small style="color:#64748b;">{r['txn_date']} • {r['payment_method']} • {r['note'] or 'No note'}</small>
                </div>
                """, unsafe_allow_html=True)
        else:
            st.caption("No expenses recorded.")
    with col_recent:
        st.subheader("🕒 Recent Activity")
        recent_all = query("SELECT * FROM transactions WHERE owner=%s ORDER BY txn_date DESC, id DESC LIMIT 5", (OWNER,))
        if not recent_all.empty:
            for _, r in recent_all.iterrows():
                icon = "🟢" if r['kind']=="income" else "🔴"
                col  = "#16a34a" if r['kind']=="income" else "#dc2626"
                st.markdown(f"""
                <div class="budget-item">
                    {icon} <b>{r['category']}</b>
                    <span style="float:right;font-weight:bold;color:{col};">{money(r['amount'])}</span><br>
                    <small style="color:#64748b;">{r['txn_date']} • {r['merchant'] or r['payment_method'] or ''} • {r['note'] or ''}</small>
                </div>
                """, unsafe_allow_html=True)
        else:
            st.caption("No recent activity.")

# ==========================================
# PAGE 2: DAILY ENTRY
# ==========================================
elif page == "Daily Entry":
    st.markdown('<div class="hero"><h1>Daily Entry</h1><p>Record today\'s income or payment in seconds.</p></div>', unsafe_allow_html=True)

    tab1, tab2 = st.tabs(["💸 Expense", "💵 Income"])
    with tab1:
        with st.form("expense_form", clear_on_submit=True):
            col_a, col_b, col_c = st.columns(3)
            txn_date_val = col_a.date_input("Payment date", value=today)
            amount       = col_b.number_input("Amount ($ USD)", min_value=0.0, step=1.0, format="%.2f")
            category     = col_c.selectbox("Category", CATEGORIES)
            col_d, col_e, col_f = st.columns(3)
            etype    = col_d.selectbox("Expense type", EXPENSE_TYPES)
            payment  = col_e.selectbox("Payment method", PAYMENTS)
            merchant = col_f.text_input("Merchant / place")
            note     = st.text_area("Note", placeholder="What was this payment for?", height=90)
            if st.form_submit_button("➕ Save expense", use_container_width=True, type="primary"):
                if amount <= 0:
                    st.error("Enter an amount greater than zero.")
                else:
                    execute("""INSERT INTO transactions
                        (owner,txn_date,kind,category,expense_type,amount,payment_method,merchant,note,updated_at)
                        VALUES (%s,%s,'expense',%s,%s,%s,%s,%s,%s,NOW())""",
                        (OWNER, str(txn_date_val), category, etype, amount, payment, merchant, note))
                    execute("UPDATE accounts SET current_balance=current_balance-%s WHERE owner=%s AND name=%s",
                            (amount, OWNER, payment))
                    st.success("Expense saved. Dashboard updated.")
                    st.rerun()

    with tab2:
        with st.form("income_form", clear_on_submit=True):
            col_a, col_b, col_c = st.columns(3)
            txn_date_val_inc = col_a.date_input("Income date", value=today, key="income_date")
            amount           = col_b.number_input("Amount ($ USD)", min_value=0.0, step=50.0, format="%.2f", key="income_amount")
            category         = col_c.selectbox("Income type", INCOME_CATEGORIES)
            col_d, col_e = st.columns(2)
            inc_payment = col_d.selectbox("Deposit Account", PAYMENTS, key="inc_payment")
            inc_source  = col_e.text_input("Source / Employer", key="inc_source")
            note = st.text_area("Note", placeholder="Salary, bonus, side income...", height=90)
            if st.form_submit_button("➕ Save income", use_container_width=True, type="primary"):
                if amount <= 0:
                    st.error("Enter an amount greater than zero.")
                else:
                    execute("""INSERT INTO transactions
                        (owner,txn_date,kind,category,expense_type,amount,payment_method,merchant,note,updated_at)
                        VALUES (%s,%s,'income',%s,'Income',%s,%s,%s,%s,NOW())""",
                        (OWNER, str(txn_date_val_inc), category, amount, inc_payment, inc_source, note))
                    execute("UPDATE accounts SET current_balance=current_balance+%s WHERE owner=%s AND name=%s",
                            (amount, OWNER, inc_payment))
                    st.success("Income saved.")
                    st.rerun()

    # Monitoring table
    st.markdown('<div class="section-title">📋 Monitor &amp; Manage Transactions</div>', unsafe_allow_html=True)
    m_c1, m_c2, m_c3 = st.columns([1.5,1,1.5])
    with m_c1:
        mon_range = st.date_input("Date Range", (mstart, mend), key="mon_dates")
    with m_c2:
        mon_kind = st.selectbox("Type", ["All","expense","income"], key="mon_kind")
    with m_c3:
        mon_search = st.text_input("Search (merchant, note, category)", key="mon_search")

    sql_mon = "SELECT * FROM transactions WHERE owner=%s"; params_mon = [OWNER]
    if len(mon_range) == 2:
        sql_mon += " AND txn_date >= %s AND txn_date <= %s"
        params_mon += [str(mon_range[0]), str(mon_range[1])]
    if mon_kind != "All":
        sql_mon += " AND kind=%s"; params_mon.append(mon_kind)
    if mon_search:
        sql_mon += " AND (merchant ILIKE %s OR note ILIKE %s OR category ILIKE %s OR payment_method ILIKE %s)"
        params_mon += [f"%{mon_search}%"] * 4
    sql_mon += " ORDER BY txn_date DESC, id DESC"
    df_mon = query(sql_mon, tuple(params_mon))

    if not df_mon.empty:
        df_display = df_mon.copy()
        df_display["USD Equivalent"] = df_display["amount"].apply(format_usd)
        df_display["KHR Equivalent"] = df_display["amount"].apply(format_khr)
        st.dataframe(
            df_display[["id","txn_date","kind","category","expense_type","USD Equivalent","KHR Equivalent","payment_method","merchant","note"]],
            use_container_width=True, hide_index=True
        )
        with st.expander("✏️ Edit or 🚨 Delete Selected Transaction", expanded=False):
            options = [
                f"ID {r['id']} | {r['txn_date']} | {r['kind'].upper()} | {r['category']} | {money(r['amount'])} ({r['merchant'] or r['note'] or 'No note'})"
                for _, r in df_mon.iterrows()
            ]
            sel = st.selectbox("Select Transaction:", options, key="sel_tx_mod")
            sel_id  = int(sel.split(" | ")[0].replace("ID ",""))
            tx_item = df_mon[df_mon.id == sel_id].iloc[0]

            col_ed, col_del = st.columns([2,1])
            with col_ed:
                with st.form("edit_tx_form", clear_on_submit=True):
                    st.subheader("Update Details")
                    ed_c1, ed_c2 = st.columns(2)
                    with ed_c1:
                        ed_date = st.date_input("Date", value=datetime.strptime(str(tx_item['txn_date'])[:10], "%Y-%m-%d").date())
                        ed_kind = st.selectbox("Kind", ["expense","income"], index=0 if tx_item['kind']=='expense' else 1)
                        all_c   = CATEGORIES if ed_kind=='expense' else INCOME_CATEGORIES
                        ed_cat  = st.selectbox("Category", all_c, index=all_c.index(tx_item['category']) if tx_item['category'] in all_c else 0)
                    with ed_c2:
                        ed_amt   = st.number_input("Amount ($ USD)", min_value=0.01, value=float(tx_item['amount']), format="%0.2f")
                        ed_pay   = st.selectbox("Payment Method", PAYMENTS, index=PAYMENTS.index(tx_item['payment_method']) if tx_item['payment_method'] in PAYMENTS else 0)
                        ed_merch = st.text_input("Merchant / Source", value=str(tx_item['merchant']) if tx_item['merchant'] else "")
                    ed_note = st.text_input("Note", value=str(tx_item['note']) if tx_item['note'] else "")
                    if st.form_submit_button("💾 Update Transaction", use_container_width=True):
                        old_mult = 1.0 if tx_item['kind']=='expense' else -1.0
                        execute("UPDATE accounts SET current_balance=current_balance+%s WHERE owner=%s AND name=%s",
                                (float(tx_item['amount'])*old_mult, OWNER, tx_item['payment_method']))
                        new_mult = -1.0 if ed_kind=='expense' else 1.0
                        execute("UPDATE accounts SET current_balance=current_balance+%s WHERE owner=%s AND name=%s",
                                (ed_amt*new_mult, OWNER, ed_pay))
                        execute("""UPDATE transactions
                            SET txn_date=%s, kind=%s, category=%s, amount=%s,
                                payment_method=%s, merchant=%s, note=%s, updated_at=NOW()
                            WHERE id=%s AND owner=%s""",
                            (str(ed_date), ed_kind, ed_cat, ed_amt, ed_pay, ed_merch, ed_note, sel_id, OWNER))
                        st.success("Transaction updated!"); st.rerun()
            with col_del:
                st.subheader("Delete Transaction")
                st.warning("Permanently removes this transaction.")
                if st.button("🚨 Delete Transaction", use_container_width=True, key=f"del_{sel_id}"):
                    del_mult = 1.0 if tx_item['kind']=='expense' else -1.0
                    execute("UPDATE accounts SET current_balance=current_balance+%s WHERE owner=%s AND name=%s",
                            (float(tx_item['amount'])*del_mult, OWNER, tx_item['payment_method']))
                    execute("DELETE FROM transactions WHERE id=%s AND owner=%s", (sel_id, OWNER))
                    st.success("Deleted!"); st.rerun()
    else:
        st.info("No transactions found for the specified filters.")

# ==========================================
# PAGE 3: TRANSACTIONS
# ==========================================
elif page == "Transactions":
    st.markdown('<div class="hero"><h1>Transactions</h1><p>Search, review and remove your financial records.</p></div>', unsafe_allow_html=True)
    c1, c2, c3 = st.columns(3)
    start = c1.date_input("From", value=mstart)
    end   = c2.date_input("To",   value=mend)
    kind  = c3.selectbox("Type", ["All","income","expense"])
    df    = load_txns(start, end)
    if kind != "All": df = df[df.kind == kind]
    search = st.text_input("Search merchant, category or note")
    if search:
        mask = df.astype(str).apply(lambda col: col.str.contains(search, case=False, na=False))
        df   = df[mask.any(axis=1)]
    st.write(f"**{len(df)} records**")
    df_show = df[["id","txn_date","kind","category","expense_type","amount","payment_method","merchant","note"]].copy()
    df_show["USD"] = df_show["amount"].apply(format_usd)
    df_show["KHR"] = df_show["amount"].apply(format_khr)
    st.dataframe(df_show[["id","txn_date","kind","category","expense_type","USD","KHR","payment_method","merchant","note"]],
                 use_container_width=True, hide_index=True)
    with st.expander("Delete a transaction"):
        tid = st.number_input("Transaction ID", min_value=1, step=1)
        if st.button("Delete transaction", type="secondary"):
            execute("DELETE FROM transactions WHERE id=%s AND owner=%s", (int(tid), OWNER))
            st.success("Deleted."); st.rerun()

# ==========================================
# PAGE 4: ANALYTICS
# ==========================================
elif page == "Analytics":
    st.markdown('<div class="hero"><h1>Analytics</h1><p>Understand where your money actually goes.</p></div>', unsafe_allow_html=True)
    an_c1, an_c2 = st.columns([1.5,2])
    with an_c1:
        an_preset = st.selectbox("Quick Period:", ["Current Month","Last Month","Year-to-Date","All Time","Custom Range"])
    if an_preset == "Current Month":
        a_start, a_end = mstart, mend
    elif an_preset == "Last Month":
        lm = today.month-1 if today.month>1 else 12
        ly = today.year if today.month>1 else today.year-1
        a_start, a_end = get_month_range(ly, lm)
    elif an_preset == "Year-to-Date":
        a_start, a_end = date(today.year,1,1), today
    elif an_preset == "All Time":
        a_start, a_end = date(2020,1,1), date(2035,12,31)
    else:
        with an_c2:
            a_range = st.date_input("Select Analytics Range", (mstart, mend), key="an_custom")
            a_start, a_end = a_range if len(a_range)==2 else (mstart, mend)

    df  = load_txns(a_start, a_end)
    exp = df[df.kind=="expense"].copy()
    inc = df[df.kind=="income"].copy()

    if exp.empty and inc.empty:
        st.info("Add transactions first to see analytics.")
    else:
        a, b, c, d = st.columns(4)
        total_exp = exp.amount.sum() if not exp.empty else 0.0
        total_inc = inc.amount.sum() if not inc.empty else 0.0
        a.metric("Total Income",  money(total_inc))
        b.metric("Total Expense", money(total_exp))
        c.metric("Net Flow",      money(total_inc-total_exp))
        d.metric("Transactions",  len(df))

        st.markdown("---")
        left, right = st.columns(2)
        with left:
            st.subheader("Spending by Category")
            if not exp.empty:
                g = exp.groupby("category", as_index=False).amount.sum().sort_values("amount", ascending=False)
                fig = px.pie(g, names="category", values="amount", hole=.5)
                fig.update_layout(height=400, margin=dict(l=5,r=5,t=10,b=5))
                st.plotly_chart(fig, use_container_width=True, config={"displayModeBar":False})
        with right:
            st.subheader("Expense by Classification")
            if not exp.empty:
                t = exp.groupby("expense_type", as_index=False).amount.sum()
                fig = px.bar(t, x="expense_type", y="amount", text_auto=".2f")
                fig.update_layout(height=400, margin=dict(l=5,r=5,t=10,b=5), xaxis_title="", yaxis_title="Amount ($)")
                st.plotly_chart(fig, use_container_width=True, config={"displayModeBar":False})

        st.subheader("Daily Spending Trend")
        if not exp.empty:
            daily = exp.groupby("txn_date", as_index=False).amount.sum()
            fig   = px.line(daily, x="txn_date", y="amount", markers=True)
            fig.update_layout(height=320, margin=dict(l=5,r=5,t=10,b=5), xaxis_title="", yaxis_title="Amount ($)")
            st.plotly_chart(fig, use_container_width=True, config={"displayModeBar":False})

        st.subheader("Top Payments")
        if not exp.empty:
            top10 = exp.sort_values("amount", ascending=False).head(10)[["txn_date","category","amount","payment_method","merchant","note"]].copy()
            top10["amount"] = top10["amount"].apply(money)
            st.dataframe(top10, use_container_width=True, hide_index=True)

# ==========================================
# PAGE 5: BUDGET
# ==========================================
elif page == "Budget":
    st.markdown('<div class="hero"><h1>Monthly Budget Planner</h1><p>Set and track category budgets by % or real amount.</p></div>', unsafe_allow_html=True)
    b_months  = [f"{today.year}-{m:02d}" for m in range(1,13)]
    sel_month = st.selectbox("📅 Select Target Month:", b_months, index=today.month-1)

    y_m_parts = sel_month.split("-")
    m_st_b, m_en_b = get_month_range(int(y_m_parts[0]), int(y_m_parts[1]))
    inc_df = query(
        "SELECT COALESCE(SUM(amount),0) AS inc FROM transactions WHERE owner=%s AND kind='income' AND txn_date>=%s AND txn_date<=%s",
        (OWNER, str(m_st_b), str(m_en_b))
    )
    recorded_income = float(inc_df['inc'].iloc[0]) if not inc_df.empty else 0.0

    b_kpi1, b_kpi2 = st.columns([1.5,2])
    with b_kpi1:
        st.markdown(f"**Recorded Income for {sel_month}:**")
        st.markdown(f"<h2 style='margin:0;color:#16a34a;'>{money(recorded_income)}</h2>", unsafe_allow_html=True)
        base_income = recorded_income if recorded_income > 0 else 1000.0
        if recorded_income == 0:
            st.caption("💡 Using $1,000 baseline because no income recorded yet.")

    cur_b_df = query("SELECT category, percent, amount FROM budgets WHERE owner=%s AND month=%s", (OWNER, sel_month))
    b_dict   = {r['category']:(r['percent'],r['amount']) for _, r in cur_b_df.iterrows()}

    st.markdown("---"); st.subheader("Set Category Budgets")
    input_mode = st.radio("Input Mode:", ["Input by Percentage (%)","Input by Real Amount ($ USD)"], horizontal=True)

    with st.form("budget_form"):
        col_list = st.columns(3)
        new_budgets = {}
        for i, cat in enumerate(CATEGORIES):
            cur_p, cur_a = b_dict.get(cat, (0.0, 0.0))
            if cur_a == 0.0 and cur_p > 0.0:
                cur_a = base_income * (cur_p / 100.0)
            with col_list[i % 3]:
                if input_mode == "Input by Percentage (%)":
                    p_val    = st.number_input(f"{cat} (%)", min_value=0.0, max_value=100.0, value=float(cur_p), step=1.0, key=f"bp_{cat}")
                    calc_amt = base_income * (p_val/100.0)
                    st.caption(f"Calculated: **{money(calc_amt)}**")
                    new_budgets[cat] = (p_val, calc_amt)
                else:
                    a_val    = st.number_input(f"{cat} ($ Amount)", min_value=0.0, value=float(cur_a), step=10.0, key=f"ba_{cat}")
                    calc_pct = (a_val / base_income * 100.0) if base_income > 0 else 0.0
                    st.caption(f"Calculated: **{calc_pct:.1f}%** of income")
                    new_budgets[cat] = (calc_pct, a_val)

        total_alloc_pct = sum(v[0] for v in new_budgets.values())
        total_alloc_amt = sum(v[1] for v in new_budgets.values())
        with b_kpi2:
            st.markdown("**Total Budget Allocated:**")
            st.markdown(f"<h2 style='margin:0;color:#2563eb;'>{money(total_alloc_amt)} <span style='font-size:18px;color:#64748b;'>({total_alloc_pct:.1f}%)</span></h2>", unsafe_allow_html=True)
            st.progress(min(total_alloc_pct/100.0, 1.0))

        if st.form_submit_button(f"💾 Save Budget Plan for {sel_month}", use_container_width=True, type="primary"):
            conn = _pg()
            cur  = conn.cursor()
            for cat, (p_num, a_num) in new_budgets.items():
                cur.execute("""
                    INSERT INTO budgets (owner,month,category,percent,amount)
                    VALUES (%s,%s,%s,%s,%s)
                    ON CONFLICT(owner,month,category) DO UPDATE SET percent=EXCLUDED.percent, amount=EXCLUDED.amount
                """, (OWNER, sel_month, cat, p_num, a_num))
            conn.commit(); conn.close()
            st.success(f"Budget plan for {sel_month} saved!"); st.rerun()

    st.markdown(f'<div class="section-title">🎯 Budget vs. Actual Tracker ({sel_month})</div>', unsafe_allow_html=True)
    month_exp_df = query(
        "SELECT category, SUM(amount) AS spent FROM transactions WHERE owner=%s AND kind='expense' AND txn_date>=%s AND txn_date<=%s GROUP BY category",
        (OWNER, str(m_st_b), str(m_en_b))
    )
    spent_dict = {r['category']:float(r['spent']) for _, r in month_exp_df.iterrows()}

    b_track_cols = st.columns(2)
    t_idx = 0
    for cat in CATEGORIES:
        cur_p, cur_a = b_dict.get(cat, (0.0, 0.0))
        target_budget = cur_a if cur_a > 0 else (base_income*(cur_p/100.0))
        actual_spent  = spent_dict.get(cat, 0.0)
        if target_budget > 0 or actual_spent > 0:
            with b_track_cols[t_idx % 2]:
                rem  = target_budget - actual_spent
                prog = min(actual_spent/target_budget, 1.0) if target_budget > 0 else 1.0
                badge = "🚨 Over Budget" if actual_spent > target_budget else ("⚠️ Near Limit" if prog > 0.8 else "✅ On Track")
                st.markdown(f"**{cat}** — <span class='khr-tag'>{badge}</span>", unsafe_allow_html=True)
                st.progress(prog)
                rem_str = f"Remaining: {money(rem)}" if rem >= 0 else f"Over by: {money(-rem)}"
                st.caption(f"Spent: **{money(actual_spent)}** / Budget: **{money(target_budget)}** • {rem_str}")
            t_idx += 1

# ==========================================
# PAGE 6: SETTINGS
# ==========================================
elif page == "Settings":
    st.markdown('<div class="hero"><h1>Settings &amp; Backup</h1><p>Export your personal financial data.</p></div>', unsafe_allow_html=True)
    df  = load_txns()
    csv = df.to_csv(index=False).encode()
    st.download_button("⬇️ Download Transactions CSV", csv,
                       f"transactions_{current_user['username']}.csv", "text/csv",
                       use_container_width=True)
    st.divider()
    st.subheader("Account Info")
    st.write(f"User: **{current_user['full_name']} (@{current_user['username']})**")
    st.write(f"Transactions: **{len(df)}**")
    st.info("🔒 Your data is stored in a private Neon PostgreSQL database, isolated by your username.")
    st.warning("Download the CSV regularly to keep a personal backup of your financial history.")
