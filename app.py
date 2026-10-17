"""ファクター/マーケットデータ取得ハブ（Excel 3ファイル形式版）。

ユーザー指定の出力フォーマット:

    japan_factors.xlsx
      ├── シート "monthly" : date, market_excess, smb, hml, rmw, cma, mom, rf
      └── シート "daily"   : date, market_excess, smb, hml, rmw, cma, mom, rf

    us_factors.xlsx
      ├── シート "monthly" : 同上
      └── シート "daily"   : 同上

    industry_10.xlsx
      ├── シート "monthly" : date, nodur, durbl, manuf, enrgy, hitec, telcm, shops, hlth, utils, other
      └── シート "daily"   : 同上

    macro_us.xlsx  (オプション)
      └── シート "monthly" : date, cpi, indpro, gs10, ...

列名は小文字統一。"Mkt-RF" は "market_excess"（市場リターン − 無リスクリターン）に改名。
出力先: apps/.shared/factor_data/
"""
from __future__ import annotations

import io
import os
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from pathlib import Path

import pandas as pd
import requests
import streamlit as st
# The commercial edition never imports a Yahoo/yfinance connector.
# Yahoo's upstream data terms must not be assumed to permit resale.
if os.getenv("FACTOR_DATA_HUB_MODE", "research").strip().lower() != "commercial":
    import yfinance as yf

# BOJ サーバは TLS 中間 CA を送らないため、Windows / macOS のシステム証明書ストアを
# 使うよう truststore で切替える。インストールされていない環境では BOJ 取得のみが
# SSL エラーになる（他の取得には影響しない）。
try:
    import truststore as _truststore
    _truststore.inject_into_ssl()
    _BOJ_TRUSTSTORE_OK = True
except ImportError:
    _BOJ_TRUSTSTORE_OK = False


st.set_page_config(page_title="ファクター/マーケットデータ取得", page_icon="📈", layout="wide")

# A separate, non-network BYOD entry point is mandatory for commercial builds.
# Keep the existing research-only flows unchanged and unavailable in this mode.
if os.getenv("FACTOR_DATA_HUB_MODE", "research").strip().lower() == "commercial":
    from commercial_byod import render_commercial_app
    render_commercial_app()
    st.stop()

APP_DIR = Path(__file__).resolve().parent
APPS_DIR = APP_DIR.parent
SHARED_DIR = APPS_DIR / ".shared" / "factor_data"
SHARED_DIR.mkdir(parents=True, exist_ok=True)

KF_BASE = "https://mba.tuck.dartmouth.edu/pages/faculty/ken.french/ftp"

# ============================================================
# FRED API キー
#   優先順位:
#     1. st.session_state["fred_api_key"]            # サイドバーの直接入力
#     2. st.session_state["_fred_key_cached"]        # username 経由でキャッシュ済み
#     3. username × private-repo lookup              # GitHub をバックエンド化
#     4. os.environ.get("FRED_API_KEY")              # 環境変数 (ローカル用)
#   ハードコード既定値は廃止。username 方式は Streamlit Cloud Secrets に
#   GITHUB_DATA_TOKEN (private repo へのリードトークン) を入れて有効化する。
# ============================================================
SECRETS_REPO = "shihuiarenjinba-png/factor-data-secrets"
SECRETS_FILE = "users.json"


@st.cache_data(ttl=300, show_spinner=False)
def _fetch_user_keys_from_github(token: str) -> dict:
    """`users.json` を private repo から取得して {"username": "fred_key"} を返す。

    取得失敗 / 認証失敗 / パース失敗時は {} を返す(ユーザー入力フォールバック)。
    キャッシュは 5 分。
    """
    if not token:
        return {}
    try:
        resp = requests.get(
            f"https://api.github.com/repos/{SECRETS_REPO}/contents/{SECRETS_FILE}",
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github.raw",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "factor-data-hub",
            },
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        return data if isinstance(data, dict) else {}
    except (requests.RequestException, ValueError):
        return {}


def _get_github_data_token() -> str:
    """Streamlit Cloud Secrets から GITHUB_DATA_TOKEN を取得。ローカルでは空文字。"""
    try:
        return str(st.secrets.get("GITHUB_DATA_TOKEN", "") or "")
    except (FileNotFoundError, AttributeError, KeyError):
        return ""


def get_fred_api_key() -> str:
    """現在有効な FRED API キーを返す(直接入力 > キャッシュ > GitHub > env)。"""
    direct = st.session_state.get("fred_api_key")
    if direct:
        return direct
    cached = st.session_state.get("_fred_key_cached")
    if cached:
        return cached
    username = (st.session_state.get("fred_username") or "").strip()
    if username:
        users = _fetch_user_keys_from_github(_get_github_data_token())
        key = users.get(username) or ""
        if key:
            st.session_state["_fred_key_cached"] = key
            return key
    return os.environ.get("FRED_API_KEY", "") or ""

# =========================================================
# データセット定義（地域 → 月次/日次のFF5とMom URL）
# =========================================================
FACTOR_FILES = {
    "us_factors.xlsx": {
        "label": "🇺🇸 US 6ファクター",
        "monthly": {
            "ff5": f"{KF_BASE}/F-F_Research_Data_5_Factors_2x3_CSV.zip",
            "mom": f"{KF_BASE}/F-F_Momentum_Factor_CSV.zip",
        },
        "daily": {
            "ff5": f"{KF_BASE}/F-F_Research_Data_5_Factors_2x3_daily_CSV.zip",
            "mom": f"{KF_BASE}/F-F_Momentum_Factor_daily_CSV.zip",
        },
    },
    "japan_factors.xlsx": {
        "label": "🇯🇵 Japan 6ファクター",
        "monthly": {
            "ff5": f"{KF_BASE}/Japan_5_Factors_CSV.zip",
            "mom": f"{KF_BASE}/Japan_MOM_Factor_CSV.zip",
        },
        "daily": {
            "ff5": f"{KF_BASE}/Japan_5_Factors_Daily_CSV.zip",
            "mom": f"{KF_BASE}/Japan_MOM_Factor_Daily_CSV.zip",
        },
    },
}

INDUSTRY_FILE = {
    "industry_10.xlsx": {
        "label": "🏭 US 10業種ポートフォリオ",
        "monthly": f"{KF_BASE}/10_Industry_Portfolios_CSV.zip",
        "daily": f"{KF_BASE}/10_Industry_Portfolios_daily_CSV.zip",
    },
}

# =========================================================
# BOJ (日本銀行) Research Data セット
# Excel 多段ヘッダのため、raw bytes をそのまま保存し、整形は分析側に任せる方針。
# URL は https://www.boj.or.jp/research/research_data/ から手動取得済み。
# =========================================================
BOJ_BASE = "https://www.boj.or.jp"
BOJ_DATASETS: dict[str, dict[str, str]] = {
    "boj_cpi.xlsx": {
        "label": "🏦 BOJ 消費者物価指数（特殊要因除く）",
        "url": BOJ_BASE + "/research/research_data/cpi/cpirev.xlsx",
    },
    "boj_gap.xlsx": {
        "label": "🏦 BOJ 需給ギャップ・潜在成長率",
        "url": BOJ_BASE + "/research/research_data/gap/gap.xlsx",
    },
    "boj_reri.xlsx": {
        "label": "🏦 BOJ 実質輸出入指数",
        "url": BOJ_BASE + "/research/research_data/reri/rs_reri.xlsx",
    },
    "boj_cai.xlsx": {
        "label": "🏦 BOJ 消費活動指数",
        "url": BOJ_BASE + "/research/research_data/cai/cai.xlsx",
    },
}

# =========================================================
# FRED 系列マップ（カテゴリ別）
#   注: FRED API キーを使用するため API キー必須（既定キー埋込済）
# =========================================================

# --- US コア（伝統的マクロ）---
FRED_SERIES_US_CORE = {
    "CPIAUCSL": "cpi",
    "INDPRO": "indpro",
    "UNRATE": "unrate",
    "GS10": "gs10",
    "TB3MS": "tb3ms",
    "BAA": "baa",
    "AAA": "aaa",
    "DTWEXBGS": "dxy",
    "VIXCLS": "vix",
    "DCOILWTICO": "wti",
}

# --- US 金利スプレッド・信用ストレス（Phase 1 追加）---
FRED_SERIES_US_STRESS = {
    "T10Y2Y": "yield_curve",          # 10Y - 2Y 国債利回りスプレッド
    "BAA10Y": "credit_spread_baa",     # BAA社債 - 10Y国債スプレッド
    "BAMLH0A0HYM2": "credit_spread_hy",  # 米ハイイールドOAS
    "NFCI": "financial_stress",         # シカゴ連銀金融状況指数（週次→月末）
}

# --- US センチメント（Phase 3 追加）---
# 提供元: ミシガン大学 (UMCSENT, MICH) + OECD (CSCICP03..., BSCICP03...)
# いずれも研究機関の無償公開データを FRED 経由で取得
FRED_SERIES_US_SENTIMENT = {
    "UMCSENT": "umich_sentiment",          # 消費者信頼感（ミシガン大学）
    "MICH": "umich_inflation_exp",         # 1年先インフレ期待（ミシガン大学）
    "CSCICP03USM665S": "consumer_conf_us",  # OECD 消費者信頼感（米国）
    "BSCICP03USM665S": "business_conf_us",  # OECD 企業信頼感（米国）
}

# --- JP コア ---
FRED_SERIES_JP_CORE = {
    "JPNCPIALLMINMEI": "cpi_jp",
    "JPNPROINDMISMEI": "indpro_jp",
    "LRHUTTTTJPM156S": "unrate_jp",
    "IRLTLT01JPM156N": "gov10y_jp",
    "INTDSRJPM193N": "discount_jp",
    "DEXJPUS": "usdjpy",
    "JPNLOLITOAASMEI": "leading_jp",
    "MANMM101JPM189S": "m1_jp",
}

# --- JP 追加（Phase 1）---
FRED_SERIES_JP_EXT = {
    "MYAGM2JPM189S": "m2_jp",          # 日本 M2 マネーサプライ
}

# --- JP センチメント（Phase 3 追加）---
# 提供元: OECD（短観の元データを OECD が指数化したもの・研究機関の無償公開）
FRED_SERIES_JP_SENTIMENT = {
    "BSCICP03JPM665S": "business_conf_jp",  # OECD 企業信頼感（日本・短観の代替）
    "CSCICP03JPM665S": "consumer_conf_jp",  # OECD 消費者信頼感（日本）
}

# --- 統合（外部から参照される名前は維持・後方互換）---
FRED_SERIES_US = {**FRED_SERIES_US_CORE, **FRED_SERIES_US_STRESS, **FRED_SERIES_US_SENTIMENT}
FRED_SERIES_JP = {**FRED_SERIES_JP_CORE, **FRED_SERIES_JP_EXT, **FRED_SERIES_JP_SENTIMENT}
FRED_SERIES_MONTHLY = FRED_SERIES_US  # 後方互換

# --- カテゴリラベル（UI 表示用）---
SERIES_CATEGORY_LABEL = {
    "FRED_SERIES_US_CORE":      "🇺🇸 US コア",
    "FRED_SERIES_US_STRESS":    "🇺🇸 US 金利・信用ストレス",
    "FRED_SERIES_US_SENTIMENT": "🇺🇸 US センチメント（ミシガン大/OECD）",
    "FRED_SERIES_JP_CORE":      "🇯🇵 JP コア",
    "FRED_SERIES_JP_EXT":       "🇯🇵 JP 追加",
    "FRED_SERIES_JP_SENTIMENT": "🇯🇵 JP センチメント（OECD/短観代替）",
}

# --- 派生系列定義（取得後に計算で生成）---
# キー: 出力列名, 値: (依存列1, 依存列2, ラムダ)
DERIVED_SERIES = {
    "us_jp_10y_diff": (
        "gs10", "gov10y_jp",
        lambda us, jp: us - jp,
        "日米10年金利差 (US 10Y - JP 10Y)",
    ),
}


# =========================================================
# KF CSV パーサ（堅牢版）
# =========================================================
def _is_header_line(line: str) -> bool:
    s = line.strip()
    return s.startswith(",") and any(c.isalpha() for c in s)


def _is_data_line(line: str) -> bool:
    s = line.strip()
    if not s:
        return False
    first = s.split(",", 1)[0].strip()
    return first.isdigit() and len(first) in (4, 6, 8)


def parse_kf_csv_robust(text: str) -> list[pd.DataFrame]:
    """KF CSV を「ヘッダ行 → 連続するN桁データ行」で複数表に分解。
    順番通りのリストを返す（最初の表が通常 "Average Value Weighted Returns" 等）。
    """
    out: list[pd.DataFrame] = []
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        if _is_header_line(lines[i]):
            header_line = lines[i]
            j = i + 1
            data_lines: list[str] = []
            while j < len(lines):
                if _is_data_line(lines[j]):
                    data_lines.append(lines[j])
                    j += 1
                else:
                    break
            if data_lines:
                csv_text = header_line + "\n" + "\n".join(data_lines)
                try:
                    df = pd.read_csv(io.StringIO(csv_text), index_col=0)
                    df.index = df.index.astype(str).str.strip()
                    for c in df.columns:
                        df[c] = pd.to_numeric(df[c], errors="coerce")
                    out.append(df)
                except Exception:
                    pass
            i = j
        else:
            i += 1
    return out


def first_main_table(tables: list[pd.DataFrame], expected_freq: str) -> pd.DataFrame | None:
    """期待する周波数(monthly/daily)のうち、最大行数の表を返す。
    通常 KF の最初の表が Value Weighted Returns。
    """
    expected_len = {"monthly": 6, "daily": 8}.get(expected_freq)
    candidates = []
    for df in tables:
        if df.empty:
            continue
        idx_len = len(str(df.index[0]))
        if idx_len == expected_len:
            candidates.append(df)
    if not candidates:
        # フォールバック: 周波数判定失敗時は最大表
        candidates = [df for df in tables if not df.empty]
        if not candidates:
            return None
    return max(candidates, key=len)


# 列名マッピング（KF原形 → 内部標準・小文字）
FACTOR_RENAME = {
    "mkt-rf": "market_excess",
    "smb": "smb",
    "hml": "hml",
    "rmw": "rmw",
    "cma": "cma",
    "mom": "mom",
    "wml": "mom",       # 一部の地域で WML 表記
    "momentum": "mom",
    "rf": "rf",
}


def normalize_factor_columns(df: pd.DataFrame) -> pd.DataFrame:
    """KF列名（大小混在）を小文字標準名にリネーム。"""
    new_cols = {}
    for c in df.columns:
        key = c.lower().strip()
        if key in FACTOR_RENAME:
            new_cols[c] = FACTOR_RENAME[key]
        else:
            new_cols[c] = key.replace(" ", "_")
    return df.rename(columns=new_cols)


def reorder_factor_columns(df: pd.DataFrame, drop_rf: bool = True) -> pd.DataFrame:
    """理想順: market_excess, smb, hml, rmw, cma, mom
    drop_rf=True なら rf 列を除外（market_excess に既に差し引かれているため不要）。
    """
    desired = ["market_excess", "smb", "hml", "rmw", "cma", "mom"]
    if not drop_rf:
        desired = desired + ["rf"]
    present_desired = [c for c in desired if c in df.columns]
    others = [c for c in df.columns if c not in present_desired and c != "rf"]
    if not drop_rf and "rf" in df.columns:
        others = [c for c in df.columns if c not in present_desired]
    return df[present_desired + others]


def normalize_industry_columns(df: pd.DataFrame) -> pd.DataFrame:
    """産業ポートフォリオ列名を全部小文字化（NoDur→nodur, HiTec→hitec 等）。"""
    return df.rename(columns={c: c.lower().strip().replace(" ", "_") for c in df.columns})


# =========================================================
# 取得パイプライン
# =========================================================
@st.cache_data(show_spinner=False, ttl=24 * 3600)
def fetch_zip(url: str) -> bytes:
    r = requests.get(url, timeout=60, headers={"User-Agent": "Mozilla/5.0"})
    r.raise_for_status()
    return r.content


def zip_to_csv_text(zip_blob: bytes) -> str | None:
    with zipfile.ZipFile(io.BytesIO(zip_blob)) as zf:
        names = [n for n in zf.namelist() if n.lower().endswith(".csv")]
        if not names:
            return None
        return zf.read(names[0]).decode("utf-8", errors="replace")


def fetch_ff5_with_mom(ff5_url: str, mom_url: str, freq: str) -> tuple[pd.DataFrame, list[str]]:
    """FF5 + Momentum を取得し、内部標準名 + 理想順で返す。
    取得失敗時は notes に詳細を記録して空 DataFrame を返す（例外は最小限）。
    """
    notes: list[str] = []

    # --- FF5 ---
    try:
        blob = fetch_zip(ff5_url)
        notes.append(f"FF5 ZIP DL: {len(blob):,} B")
    except requests.HTTPError as e:
        notes.append(f"❌ FF5 ZIP HTTP エラー ({e.response.status_code}): {ff5_url}")
        return pd.DataFrame(), notes
    except Exception as e:
        notes.append(f"❌ FF5 ZIP 取得失敗: {type(e).__name__}: {e}")
        return pd.DataFrame(), notes

    text = zip_to_csv_text(blob)
    if text is None:
        notes.append(f"❌ ZIP 内に CSV なし: {ff5_url}")
        return pd.DataFrame(), notes
    tables = parse_kf_csv_robust(text)
    notes.append(f"パース完了: {len(tables)} 表検出")
    ff5 = first_main_table(tables, freq)
    if ff5 is None or ff5.empty:
        # フォールバック: 周波数判定失敗時は最大表を採用
        if tables:
            ff5 = max(tables, key=len)
            notes.append(f"⚠️ {freq} の主表抽出失敗 → 最大表を採用 ({len(ff5)} 行)")
        else:
            notes.append(f"❌ FF5 表ゼロ")
            return pd.DataFrame(), notes
    ff5 = normalize_factor_columns(ff5)
    notes.append(f"FF5 メイン表: {len(ff5)} 行 × {len(ff5.columns)} 列, 列={list(ff5.columns)}")

    # --- Mom ---
    try:
        mom_blob = fetch_zip(mom_url)
        notes.append(f"Mom ZIP DL: {len(mom_blob):,} B")
        mom_text = zip_to_csv_text(mom_blob)
        mom_tables = parse_kf_csv_robust(mom_text) if mom_text else []
        mom = first_main_table(mom_tables, freq) if mom_tables else None
        if mom is None or mom.empty:
            notes.append("⚠️ Mom 表抽出失敗 — Mom 列なしで継続")
            merged = ff5
        else:
            mom = normalize_factor_columns(mom)
            if "mom" in mom.columns:
                merged = ff5.join(mom[["mom"]], how="left")
                matched = int(merged["mom"].notna().sum()) if "mom" in merged.columns else 0
                notes.append(f"Mom 結合: {matched} 件マッチ")
            else:
                notes.append(f"⚠️ Mom 列が認識できず（列={list(mom.columns)}）")
                merged = ff5
    except requests.HTTPError as e:
        notes.append(f"⚠️ Mom URL 404 等: {e.response.status_code} - Mom 列なしで継続")
        merged = ff5
    except Exception as e:
        notes.append(f"⚠️ Mom 取得失敗: {type(e).__name__}: {e}")
        merged = ff5

    merged = reorder_factor_columns(merged)
    merged.index.name = "date"
    return merged, notes


def fetch_industry(url: str, freq: str) -> tuple[pd.DataFrame, list[str]]:
    """産業ポートフォリオ取得（先頭の Average Value Weighted Returns 表を採用）。"""
    notes: list[str] = []
    try:
        blob = fetch_zip(url)
        notes.append(f"ZIP DL: {len(blob):,} B")
    except requests.HTTPError as e:
        notes.append(f"❌ HTTP {e.response.status_code}: {url}")
        return pd.DataFrame(), notes
    except Exception as e:
        notes.append(f"❌ ZIP 取得失敗: {type(e).__name__}: {e}")
        return pd.DataFrame(), notes

    text = zip_to_csv_text(blob)
    if text is None:
        notes.append("❌ ZIP 内に CSV なし")
        return pd.DataFrame(), notes
    tables = parse_kf_csv_robust(text)
    notes.append(f"パース完了: {len(tables)} 表検出")
    expected_len = {"monthly": 6, "daily": 8}.get(freq)
    candidates = [df for df in tables if not df.empty and len(str(df.index[0]).strip()) == expected_len]
    if not candidates:
        # フォールバック: 最大表を採用
        non_empty = [df for df in tables if not df.empty]
        if not non_empty:
            notes.append("❌ 産業データの表ゼロ")
            return pd.DataFrame(), notes
        df = max(non_empty, key=len)
        notes.append(f"⚠️ {freq} 表抽出失敗 → 最大表を採用 ({len(df)} 行)")
    else:
        df = candidates[0]  # 先頭 = VW Returns
    df = normalize_industry_columns(df)
    df.index.name = "date"
    notes.append(f"{len(df)} 行 × {len(df.columns)} 列 (Value Weighted Returns)")
    return df, notes


# =========================================================
# Excel 保存
# =========================================================
def save_xlsx_two_sheets(path: Path, monthly: pd.DataFrame, daily: pd.DataFrame) -> dict:
    """monthly + daily を1つの Excel に2シートで保存（列幅自動調整あり）。"""
    if monthly.empty and daily.empty:
        return {"error": "monthly / daily 両方とも空のため保存スキップ"}
    with pd.ExcelWriter(path, engine="openpyxl") as writer:
        if not monthly.empty:
            monthly_out = monthly.reset_index()
            if monthly_out.columns[0] != "date":
                monthly_out = monthly_out.rename(columns={monthly_out.columns[0]: "date"})
            monthly_out.to_excel(writer, sheet_name="monthly", index=False)
            _autosize_excel_columns(writer, "monthly", monthly_out)
        if not daily.empty:
            daily_out = daily.reset_index()
            if daily_out.columns[0] != "date":
                daily_out = daily_out.rename(columns={daily_out.columns[0]: "date"})
            daily_out.to_excel(writer, sheet_name="daily", index=False)
            _autosize_excel_columns(writer, "daily", daily_out)
    return {
        "monthly_rows": len(monthly),
        "daily_rows": len(daily),
        "path": str(path),
        "size": path.stat().st_size,
    }


# =========================================================
# FRED 取得
# =========================================================
def _format_mtime_relative(path: Path) -> str:
    """ファイル mtime を「本日 (14:30)」「3日前 (2026-05-22)」「なし」形式に整形。
    キャッシュの取得日時表示専用ヘルパー（軽量）。
    """
    if not path.exists():
        return "なし"
    try:
        from datetime import datetime as _dt
        mtime = _dt.fromtimestamp(path.stat().st_mtime)
        now = _dt.now()
        delta_days = (now.date() - mtime.date()).days
        if delta_days == 0:
            return f"本日 ({mtime.strftime('%H:%M')})"
        if delta_days == 1:
            return f"昨日 ({mtime.strftime('%Y-%m-%d')})"
        return f"{delta_days}日前 ({mtime.strftime('%Y-%m-%d')})"
    except Exception:
        return "不明"


@st.cache_data(show_spinner=False, ttl=24 * 3600)
def fetch_boj_xlsx(url: str) -> bytes:
    """BOJ から xlsx を取得してバイト列で返す。
    truststore を inject 済みなので、Windows / macOS のシステム証明書ストアで
    SSL 検証が通る。失敗した場合は呼び出し側で例外を捕捉する。
    """
    r = requests.get(url, timeout=60, headers={"User-Agent": "Mozilla/5.0"})
    r.raise_for_status()
    return r.content


def save_boj_raw_xlsx(path: Path, payload: bytes) -> dict:
    """BOJ Excel の raw bytes を SHARED_DIR に保存（同名は上書き＝最新のみ保持）。
    多段ヘッダ整形は分析側に任せ、ここではバイトをそのまま書く。
    """
    if not payload:
        return {"error": "受信ペイロードが空です"}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return {
        "path": str(path),
        "size": path.stat().st_size,
        "bytes_written": len(payload),
    }


def preview_saved_xlsx(path: Path, max_rows: int = 10, key_prefix: str = "preview"):
    """保存済み Excel ファイルをサイドバー/インラインで確認するUIブロック。
    シート一覧、各シートの行数、先頭/末尾の数行プレビュー、DLボタンを表示。
    """
    if not path.exists():
        st.warning(f"ファイルがありません: {path}")
        return
    try:
        xls = pd.ExcelFile(path)
    except Exception as e:
        st.error(f"Excel として読めません: {e}")
        return

    c1, c2, c3 = st.columns([2, 2, 2])
    c1.metric("📄 ファイル", path.name)
    c2.metric("📑 シート数", len(xls.sheet_names))
    c3.metric("💾 サイズ", f"{path.stat().st_size:,} B")

    for sheet in xls.sheet_names:
        try:
            df = xls.parse(sheet)
            with st.expander(f"シート `{sheet}` — {len(df)} 行 × {len(df.columns)} 列", expanded=False):
                st.markdown("**列構造**")
                st.write(list(df.columns))
                st.markdown("**先頭5行**")
                st.dataframe(df.head(5), use_container_width=True)
                st.markdown("**末尾5行**")
                st.dataframe(df.tail(5), use_container_width=True)
        except Exception as e:
            st.error(f"シート `{sheet}` 読込失敗: {e}")

    with open(path, "rb") as f:
        st.download_button(
            f"📥 {path.name} をダウンロード",
            f.read(),
            file_name=path.name,
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            key=f"{key_prefix}_dl_{path.name}",
        )


def _fetch_fred_via_api(series_id: str, api_key: str, timeout: int) -> pd.Series:
    """FRED 公式 API（api.stlouisfed.org）経由で系列を取得（JSON 形式）。

    エンドポイント: /fred/series/observations
    Docs: https://fred.stlouisfed.org/docs/api/fred/series_observations.html
    """
    url = "https://api.stlouisfed.org/fred/series/observations"
    params = {
        "series_id": series_id,
        "api_key": api_key,
        "file_type": "json",
    }
    r = requests.get(
        url, params=params, timeout=(10, timeout),
        headers={"User-Agent": "factor-data-hub"},
    )
    r.raise_for_status()
    data = r.json()
    obs = data.get("observations", [])
    if not obs:
        raise RuntimeError(f"observations が空: {series_id}")
    df = pd.DataFrame(obs)
    df["date"] = pd.to_datetime(df["date"])
    df["value"] = pd.to_numeric(df["value"], errors="coerce")
    return df.set_index("date")["value"]


def _fetch_fred_via_csv(series_id: str, timeout: int) -> pd.Series:
    """フォールバック: 旧来の CSV エンドポイント（API失敗時の保険）。"""
    urls = [
        f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={series_id}",
        f"https://fred.stlouisfed.org/series/{series_id}/downloaddata/{series_id}.csv",
    ]
    last_exc: Exception | None = None
    for url in urls:
        try:
            with requests.get(
                url, timeout=(15, timeout),
                headers={
                    "User-Agent": "Mozilla/5.0 (factor-data-hub)",
                    "Accept-Encoding": "identity",
                },
                stream=True,
            ) as r:
                r.raise_for_status()
                text = b"".join(r.iter_content(chunk_size=16384)).decode("utf-8", errors="replace")
            df = pd.read_csv(io.StringIO(text))
            date_col = next(
                (c for c in df.columns if c.lower() in ("date", "observation_date")),
                df.columns[0],
            )
            val_col = [c for c in df.columns if c != date_col][0]
            df[date_col] = pd.to_datetime(df[date_col])
            df[val_col] = pd.to_numeric(df[val_col], errors="coerce")
            return df.set_index(date_col)[val_col]
        except Exception as e:
            last_exc = e
            continue
    raise last_exc if last_exc else RuntimeError(f"CSV fallback失敗: {series_id}")


@st.cache_data(show_spinner=False, ttl=12 * 3600)
def fetch_fred_series(series_id: str, api_key: str | None = None,
                       timeout: int = 60, retries: int = 2) -> pd.Series:
    """FRED の単一系列を取得。**API 経由を優先**、失敗時のみ CSV にフォールバック。

    Args:
        series_id: FRED の系列ID（例: 'CPIAUCSL'）
        api_key: 明示指定。None なら get_fred_api_key() を使用
        timeout: 1リクエストの読込タイムアウト（秒）
        retries: API でのリトライ回数
    """
    key = api_key or get_fred_api_key()
    last_exc: Exception | None = None

    # === Phase 1: API（推奨） ===
    for attempt in range(retries + 1):
        try:
            return _fetch_fred_via_api(series_id, key, timeout)
        except requests.HTTPError as e:
            # APIキー不正(400/403)は即諦め → CSV fallbackへ
            if e.response is not None and e.response.status_code in (400, 401, 403, 404):
                last_exc = e
                break
            last_exc = e
            continue
        except (requests.Timeout, requests.ConnectionError) as e:
            last_exc = e
            if attempt < retries:
                continue
            break
        except Exception as e:
            last_exc = e
            break

    # === Phase 2: CSV fallback ===
    try:
        return _fetch_fred_via_csv(series_id, timeout)
    except Exception as csv_exc:
        # 両方失敗 → API側のエラーを優先（より診断的）
        raise RuntimeError(f"API失敗: {last_exc} / CSV失敗: {csv_exc}") from last_exc


def build_macro_monthly(series_map: dict | None = None, max_workers: int = 6) -> tuple[pd.DataFrame, list[str]]:
    """FRED の複数系列を取得し、月次共通インデックスで統合。

    **API 優先化により並列度を 6 に**:
      公式 API は JSON で軽量・高速・安定（CSV版より大幅に速い）。
      並列度6でも問題なく動作する。
      内部で fetch_fred_series が API → CSV の順にフォールバック。
    """
    if series_map is None:
        series_map = FRED_SERIES_US
    if not series_map:
        return pd.DataFrame(), ["（系列未選択）"]

    notes: list[str] = []
    series_results: dict[str, pd.Series] = {}

    def fetch_one(sid: str, label: str):
        try:
            s = fetch_fred_series(sid).rename(label)
            # ★ 重要: 必ず月末に正規化する
            # FRED は系列によって「月初日」「月中」「月末」混在で返すため、
            # 条件付き resample だと US/JP 結合時に inner join が空になることがある。
            # 例: 米CPI が 2024-01-01、日CPI が 2024-01-31 → join できない
            s = s.resample("ME").last()
            return sid, label, s, None
        except Exception as e:
            return sid, label, None, e

    if max_workers <= 1:
        # 直列実行（安定優先）
        for sid, label in series_map.items():
            sid_r, label_r, s, err = fetch_one(sid, label)
            if err is None:
                series_results[label_r] = s
                notes.append(f"✅ {label_r} ({sid_r}): {len(s)} ポイント")
            else:
                notes.append(f"❌ {label_r} ({sid_r}): {err}")
    else:
        with ThreadPoolExecutor(max_workers=max_workers) as ex:
            futures = [ex.submit(fetch_one, sid, label) for sid, label in series_map.items()]
            for fut in as_completed(futures):
                sid, label, s, err = fut.result()
                if err is None:
                    series_results[label] = s
                    notes.append(f"✅ {label} ({sid}): {len(s)} ポイント")
                else:
                    notes.append(f"❌ {label} ({sid}): {err}")

    if not series_results:
        return pd.DataFrame(), notes
    # 並列の取得順は不定なので、series_map の順に並べ直して結合
    ordered_labels = [label for sid, label in series_map.items() if label in series_results]
    merged = pd.concat([series_results[lab] for lab in ordered_labels], axis=1)
    merged.index.name = "date"
    return merged, notes


def _compute_derived_series(combined: pd.DataFrame) -> tuple[pd.DataFrame, list[str]]:
    """combined DataFrame に派生系列を追加（計算可能なものだけ）。

    戻り値: (拡張後DF, 追加された列名リスト)
    """
    added: list[str] = []
    out = combined.copy()
    for new_col, spec in DERIVED_SERIES.items():
        dep_a, dep_b, fn, _label = spec
        if dep_a in out.columns and dep_b in out.columns:
            try:
                out[new_col] = fn(out[dep_a], out[dep_b])
                added.append(new_col)
            except Exception:
                pass
    return out, added


def _align_month_end(df: pd.DataFrame) -> pd.DataFrame:
    """DatetimeIndex を月末に正規化（既に月末なら何もしない・保険処理）。"""
    if df.empty or not isinstance(df.index, pd.DatetimeIndex):
        return df
    try:
        return df.resample("ME").last()
    except Exception:
        return df


def _autosize_excel_columns(writer, sheet_name: str, df: pd.DataFrame) -> None:
    """openpyxl ワークシートの列幅を内容に応じて自動調整。

    Excel デフォルトの列幅 (~8文字) では、長い列名（例: 'business_conf_us'）や
    大きな数値（例: 3.71E+12）が `###` 表示になる。データに合わせた幅を設定する。
    """
    try:
        from openpyxl.utils import get_column_letter
        ws = writer.sheets[sheet_name]
        # date 列含めて列を取得
        cols = list(df.columns)
        for col_idx, col_name in enumerate(cols, 1):
            col_str = str(col_name)
            header_len = len(col_str)
            # サンプル100行で最長値を見る（巨大DF対策）
            sample = df[col_name].head(100) if len(df) > 0 else None
            data_max = 0
            if sample is not None and len(sample) > 0:
                try:
                    data_max = int(sample.astype(str).str.len().max())
                except Exception:
                    data_max = 12
            # 全角文字考慮（マルチバイトは1.3倍幅）
            max_len = max(header_len, data_max if pd.notna(data_max) else 0)
            width = min(max(max_len * 1.15 + 2, 10), 40)  # 10〜40 にクランプ
            ws.column_dimensions[get_column_letter(col_idx)].width = width
        # date 列だけはやや広めに（datetime 表示用）
        if "date" in cols:
            date_col_idx = cols.index("date") + 1
            ws.column_dimensions[get_column_letter(date_col_idx)].width = max(
                ws.column_dimensions[get_column_letter(date_col_idx)].width, 14
            )
    except Exception:
        # 列幅調整は失敗しても致命的でないので握りつぶす
        pass


def save_macro_xlsx(us_df: pd.DataFrame, jp_df: pd.DataFrame, path: Path) -> dict:
    """マクロ統合 Excel (.xlsx) を作成。シート: us, jp, combined。

    combined シートでは、両方の列が揃っていれば 派生系列（日米金利差 等）を自動追加。

    戻り値: {"us_rows": N, "jp_rows": M, "combined_rows": K, "derived": [..], "path": str, "size": bytes}
    両方空なら空 dict を返す。
    """
    if us_df.empty and jp_df.empty:
        return {"error": "us / jp 両方とも空のため保存スキップ"}
    derived_added: list[str] = []
    # ★ 念のため両側を月末に整列（fetch_one で既に行っているが保険）
    us_df = _align_month_end(us_df)
    jp_df = _align_month_end(jp_df)

    with pd.ExcelWriter(path, engine="openpyxl") as writer:
        if not us_df.empty:
            us_out = us_df.reset_index()
            if us_out.columns[0] != "date":
                us_out = us_out.rename(columns={us_out.columns[0]: "date"})
            us_out.to_excel(writer, sheet_name="us", index=False)
            _autosize_excel_columns(writer, "us", us_out)
        if not jp_df.empty:
            jp_out = jp_df.reset_index()
            if jp_out.columns[0] != "date":
                jp_out = jp_out.rename(columns={jp_out.columns[0]: "date"})
            jp_out.to_excel(writer, sheet_name="jp", index=False)
            _autosize_excel_columns(writer, "jp", jp_out)
        combined_rows = 0
        if not us_df.empty and not jp_df.empty:
            # outer join → forward fill で全期間カバー、共通月だけ欲しい場合は dropna 不要なら残す
            # ここでは inner で共通月のみ（分析の都合上）
            combined = us_df.join(jp_df, how="inner")
            combined, derived_added = _compute_derived_series(combined)
            combined_out = combined.reset_index()
            if combined_out.columns[0] != "date":
                combined_out = combined_out.rename(columns={combined_out.columns[0]: "date"})
            combined_out.to_excel(writer, sheet_name="combined", index=False)
            _autosize_excel_columns(writer, "combined", combined_out)
            combined_rows = len(combined)
    return {
        "us_rows": len(us_df), "jp_rows": len(jp_df),
        "combined_rows": combined_rows,
        "derived": derived_added,
        "path": str(path), "size": path.stat().st_size,
    }


# =========================================================
# UI
# =========================================================
st.title("📈 ファクター / マーケットデータ取得ハブ")
st.caption("**理想形式**: Excel 3ファイル（各 monthly+daily の2シート） + マクロ統合")

with st.sidebar:
    st.header("📂 共有ストレージ")
    st.caption(f"`{SHARED_DIR}`")
    existing = sorted(SHARED_DIR.glob("*"))
    if existing:
        st.caption(f"📦 {len(existing)} ファイル — **取得状況に関係なくいつでも DL 可能**")
        for f in existing:
            c1, c2, c3 = st.columns([3, 1, 1])
            c1.caption(
                f"・{f.name}\n　({f.stat().st_size:,} B)"
                f"\n　最終取得: {_format_mtime_relative(f)}"
            )
            c2.download_button(
                "📥", f.read_bytes(),
                file_name=f.name,
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
                     if f.suffix.lower() == ".xlsx" else "application/octet-stream",
                key=f"sidebar_dl_{f.name}",
                help=f"{f.name} をダウンロード",
            )
            if c3.button("🗑", key=f"del_{f.name}", help=f"{f.name} を削除"):
                try:
                    f.unlink()
                    st.rerun()
                except OSError as e:
                    st.error(str(e))
    else:
        st.info("まだ何も取得していません。")

    st.markdown("---")
    with st.expander("👤 登録ユーザー (GitHub 経由でキー自動取得)", expanded=False):
        st.caption(
            "private repo `shihuiarenjinba-png/factor-data-secrets/users.json` に"
            "登録済みの username を入れると、Streamlit Cloud Secrets の "
            "`GITHUB_DATA_TOKEN` 経由でその人の FRED API キーを取得します。"
            "一度取れたキーはセッション内でキャッシュされ、再入力不要です。"
        )
        st.text_input("ユーザー名", key="fred_username", placeholder="aaron")
        if st.button("登録キーを取得", key="fetch_user_key"):
            st.session_state.pop("_fred_key_cached", None)
            _fetch_user_keys_from_github.clear()
            key = get_fred_api_key()
            if key and st.session_state.get("_fred_key_cached"):
                masked = key[:4] + "..." + key[-4:] if len(key) > 8 else "(短い)"
                st.success(f"登録キーを取得しました ({masked})")
                st.rerun()
            elif not _get_github_data_token():
                st.error("Streamlit Cloud Secrets に `GITHUB_DATA_TOKEN` が未設定です")
            else:
                st.error("該当ユーザーが users.json に見つかりません")

    st.markdown("---")
    with st.expander("🔑 FRED API キー設定", expanded=False):
        current_key = get_fred_api_key()
        masked = current_key[:4] + "..." + current_key[-4:] if len(current_key) > 8 else "(未設定)"
        st.caption(f"現在のキー: `{masked}`")
        new_key = st.text_input(
            "API キーを上書き（空欄でデフォルトに戻す）",
            value="", type="password", key="fred_key_input",
            help="https://fred.stlouisfed.org/docs/api/api_key.html で無料取得可。",
        )
        c1, c2 = st.columns(2)
        if c1.button("保存", key="save_key"):
            if new_key.strip():
                st.session_state["fred_api_key"] = new_key.strip()
                st.success("キーを更新しました（このセッション中）")
                st.rerun()
            else:
                st.warning("空欄なので変更なし")
        if c2.button("デフォルトに戻す", key="reset_key"):
            st.session_state.pop("fred_api_key", None)
            st.success("デフォルトキーに戻しました")
            st.rerun()
        st.caption("💡 環境変数 `FRED_API_KEY` でも上書き可能。")

    st.markdown("---")
    st.markdown(
        """
        **列構造（理想形）**

        ファクターファイル (`*_factors.xlsx`):
        `date | market_excess | smb | hml | rmw | cma | mom`

        🔑 **`market_excess` = 市場リターン − 無リスクリターン**
        （= 市場超過リターン。回帰の被説明変数に使用。
        `rf` は差し引き済みなので出力に含めない）

        産業ファイル (`industry_10.xlsx`):
        `date | nodur | durbl | manuf | enrgy | hitec | telcm | shops | hlth | utils | other`

        マクロファイル (`macro.xlsx`):
        シート `us` / `jp` / `combined`
        """
    )

tabs = st.tabs([
    "🚀 ワンショット取得（推奨）",
    "📂 保存ファイルを確認・プレビュー",
    "🔧 個別取得",
    "📊 マクロ統合（Excel）",
    "📤 マクロ手動アップロード（FRED不通時）",
])

# ---------- Tab 1: ワンショット ----------
with tabs[0]:
    st.subheader("🚀 必要なファイルを細かく選んで一括取得")
    st.caption("ファイル × 周波数 × 系列 を自由に組み合わせて取得できます。")

    with st.expander("📋 出力ファイルの構造（参考）", expanded=False):
        st.markdown(
            """
            | ファイル | シート | 列構成 |
            |---|---|---|
            | `us_factors.xlsx` | monthly / daily | `date, market_excess, smb, hml, rmw, cma, mom` |
            | `japan_factors.xlsx` | monthly / daily | 同上 |
            | `industry_10.xlsx` | monthly / daily | `date, nodur, durbl, manuf, enrgy, hitec, telcm, shops, hlth, utils, other` |
            | `macro.xlsx` | us / jp / combined | 選択した系列のみ |

            ※ `market_excess` は **市場リターン − 無リスクリターン**（スプレッド値）。`rf` は出力に含めません。
            """
        )

    # ========== Section A: ファクター ==========
    st.markdown("### 🏭 ファクター取得")
    st.caption("各ファイルごとに **monthly / daily** を個別チェック。両方OFFならそのファイルは作成しません。")

    factor_choices: dict[str, dict[str, bool]] = {}
    for fname, cfg in {**FACTOR_FILES, **INDUSTRY_FILE}.items():
        c1, c2, c3, c4 = st.columns([3, 1, 1, 1])
        c1.write(f"**{cfg['label']}** → `{fname}`")
        m_on = c2.checkbox("Monthly", value=True, key=f"sel_m_{fname}")
        d_on = c3.checkbox("Daily", value=True, key=f"sel_d_{fname}")
        active = c4.checkbox("取得する", value=True, key=f"sel_act_{fname}")
        factor_choices[fname] = {"monthly": m_on, "daily": d_on, "active": active}

    # ========== Section B: マクロ ==========
    st.markdown("---")
    st.markdown("### 📊 マクロ取得（FRED・系列を個別選択可）")
    st.caption("US / JP それぞれ系列を個別チェック可能。**チェックなしならその地域はシート作成しません**。")

    col_us, col_jp = st.columns(2)
    with col_us:
        st.markdown("**🇺🇸 US 系列**")
        get_us_macro = st.checkbox("US マクロを取得", value=False, key="get_us_macro")
        us_series_selected: list[str] = []
        if get_us_macro:
            c_all1, c_all2 = st.columns(2)
            if c_all1.button("全選択", key="us_all"):
                for sid in FRED_SERIES_US:
                    st.session_state[f"us_ser_{sid}"] = True
            if c_all2.button("全解除", key="us_none"):
                for sid in FRED_SERIES_US:
                    st.session_state[f"us_ser_{sid}"] = False
            # カテゴリ別表示
            st.caption("**コア**")
            for sid, label in FRED_SERIES_US_CORE.items():
                if st.checkbox(f"{label} ({sid})", value=True, key=f"us_ser_{sid}"):
                    us_series_selected.append(sid)
            st.caption("**金利・信用ストレス（Phase 1）**")
            for sid, label in FRED_SERIES_US_STRESS.items():
                if st.checkbox(f"{label} ({sid})", value=True, key=f"us_ser_{sid}"):
                    us_series_selected.append(sid)
            st.caption("**センチメント（Phase 3 / ミシガン大・OECD）**")
            for sid, label in FRED_SERIES_US_SENTIMENT.items():
                if st.checkbox(f"{label} ({sid})", value=True, key=f"us_ser_{sid}"):
                    us_series_selected.append(sid)
    with col_jp:
        st.markdown("**🇯🇵 JP 系列**")
        get_jp_macro = st.checkbox("JP マクロを取得", value=False, key="get_jp_macro")
        jp_series_selected: list[str] = []
        if get_jp_macro:
            c_all1, c_all2 = st.columns(2)
            if c_all1.button("全選択", key="jp_all"):
                for sid in FRED_SERIES_JP:
                    st.session_state[f"jp_ser_{sid}"] = True
            if c_all2.button("全解除", key="jp_none"):
                for sid in FRED_SERIES_JP:
                    st.session_state[f"jp_ser_{sid}"] = False
            # カテゴリ別表示
            st.caption("**コア**")
            for sid, label in FRED_SERIES_JP_CORE.items():
                if st.checkbox(f"{label} ({sid})", value=True, key=f"jp_ser_{sid}"):
                    jp_series_selected.append(sid)
            st.caption("**追加（Phase 1）**")
            for sid, label in FRED_SERIES_JP_EXT.items():
                if st.checkbox(f"{label} ({sid})", value=True, key=f"jp_ser_{sid}"):
                    jp_series_selected.append(sid)
            st.caption("**センチメント（Phase 3 / OECD・短観代替）**")
            for sid, label in FRED_SERIES_JP_SENTIMENT.items():
                if st.checkbox(f"{label} ({sid})", value=True, key=f"jp_ser_{sid}"):
                    jp_series_selected.append(sid)

    # 派生系列の説明
    if (get_us_macro or get_jp_macro):
        with st.expander("🔢 自動計算される派生系列", expanded=False):
            for col, spec in DERIVED_SERIES.items():
                _a, _b, _fn, label = spec
                st.caption(f"・**{col}** ← `{_a}` − `{_b}`  : {label}")
            st.caption("⚠️ 依存列が両方取得できた場合のみ combined シートに自動追加されます。")

    # ========== Section C: BOJ Research Data ==========
    st.markdown("---")
    st.markdown("### 🏦 BOJ Research Data 取得")
    st.caption(
        "日本銀行の研究データ（CPI / 需給ギャップ / 実輸出入 / 消費活動指数）を取得します。"
        "Excel の多段ヘッダはそのまま保存（整形は分析側で実施）。"
    )
    if not _BOJ_TRUSTSTORE_OK:
        st.warning(
            "⚠️ `truststore` が未インストールです。BOJ サーバは TLS 中間 CA を送らないため、"
            "SSL 検証で失敗する可能性があります。対処: ターミナルで `pip install truststore` を実行してください。"
        )
    boj_choices: dict[str, bool] = {}
    for fname, cfg in BOJ_DATASETS.items():
        c1, c2 = st.columns([5, 1])
        c1.write(f"**{cfg['label']}** → `{fname}`")
        boj_choices[fname] = c2.checkbox("取得", value=False, key=f"sel_boj_{fname}")

    st.markdown("---")
    include_macro = (get_us_macro and us_series_selected) or (get_jp_macro and jp_series_selected)
    include_boj = any(boj_choices.values())

    # === FRED 接続テスト ===
    if include_macro:
        with st.expander("🔧 FRED 接続テスト（推奨：マクロ取得前に1回実行）", expanded=False):
            st.caption(
                "マクロ取得前に FRED API が応答するか軽い系列1つで確認します。"
                "API 経由でも失敗するなら、CSV fallback が試され、それでもダメならネット環境の問題です。"
            )
            test_col1, test_col2 = st.columns(2)
            if test_col1.button("🔌 API テスト (UNRATE)", key="fred_test_api"):
                import time as _time
                t0 = _time.time()
                try:
                    s = _fetch_fred_via_api("UNRATE", get_fred_api_key(), timeout=30)
                    dt = _time.time() - t0
                    st.success(f"✅ FRED API OK: UNRATE 取得成功 ({len(s)} ポイント, {dt:.2f}s)")
                except Exception as e:
                    dt = _time.time() - t0
                    st.error(f"❌ API 失敗 ({dt:.2f}s): {type(e).__name__}: {e}")
            if test_col2.button("🔌 CSV テスト (UNRATE)", key="fred_test_csv"):
                import time as _time
                t0 = _time.time()
                try:
                    s = _fetch_fred_via_csv("UNRATE", timeout=30)
                    dt = _time.time() - t0
                    st.success(f"✅ FRED CSV OK: UNRATE 取得成功 ({len(s)} ポイント, {dt:.2f}s)")
                except Exception as e:
                    dt = _time.time() - t0
                    st.error(f"❌ CSV 失敗 ({dt:.2f}s): {type(e).__name__}: {e}")

    if st.button("🚀 選択した内容で一括取得", type="primary", key="oneshot"):
        # 旧コードとの互換: targets を生成
        targets = [fname for fname, cfg in factor_choices.items() if cfg["active"] and (cfg["monthly"] or cfg["daily"])]
        results: list[dict] = []
        saved_paths: list[Path] = []

        for fname in targets:
            sel = factor_choices.get(fname, {"monthly": True, "daily": True})
            want_m = sel.get("monthly", True)
            want_d = sel.get("daily", True)
            if fname in FACTOR_FILES:
                cfg = FACTOR_FILES[fname]
                freq_tag = f"({'M' if want_m else ''}{'D' if want_d else ''})"
                with st.status(f"取得中: {cfg['label']} {freq_tag}", expanded=True) as status:
                    m_df, d_df = pd.DataFrame(), pd.DataFrame()
                    try:
                        if want_m:
                            st.write("📅 Monthly 取得...")
                            try:
                                m_df, m_notes = fetch_ff5_with_mom(
                                    cfg["monthly"]["ff5"], cfg["monthly"]["mom"], "monthly")
                                for n in m_notes: st.write(f"   {n}")
                                st.write(f"   ✅ Monthly: **{len(m_df)} 行**")
                            except Exception as e:
                                st.write(f"   ❌ Monthly 失敗: {e}")
                        else:
                            st.write("⏭ Monthly スキップ（未選択）")

                        if want_d:
                            st.write("📅 Daily 取得...")
                            try:
                                d_df, d_notes = fetch_ff5_with_mom(
                                    cfg["daily"]["ff5"], cfg["daily"]["mom"], "daily")
                                for n in d_notes: st.write(f"   {n}")
                                st.write(f"   ✅ Daily: **{len(d_df)} 行**")
                            except Exception as e:
                                st.write(f"   ❌ Daily 失敗: {e}")
                        else:
                            st.write("⏭ Daily スキップ（未選択）")

                        out_path = SHARED_DIR / fname
                        save_info = save_xlsx_two_sheets(out_path, m_df, d_df)
                        if "error" in save_info:
                            raise RuntimeError(save_info["error"])
                        saved_paths.append(out_path)
                        results.append({
                            "file": fname,
                            "monthly_rows": save_info["monthly_rows"],
                            "daily_rows": save_info["daily_rows"],
                            "size_B": save_info["size"],
                            "cols": ", ".join(m_df.columns if not m_df.empty else d_df.columns),
                            "status": "✅",
                        })
                        status.update(label=f"✅ {cfg['label']} 保存完了 (M:{save_info['monthly_rows']}行 / D:{save_info['daily_rows']}行)", state="complete")
                    except Exception as e:
                        _cache_hint = "（キャッシュ保持）" if (SHARED_DIR / fname).exists() else ""
                        results.append({
                            "file": fname, "monthly_rows": 0, "daily_rows": 0,
                            "size_B": 0, "cols": "-", "status": f"❌ {e}{_cache_hint}",
                        })
                        status.update(label=f"❌ {cfg['label']} 失敗", state="error")
                # status 外側に常時表示の DL 行（保存できていた時のみ）
                if (SHARED_DIR / fname).exists():
                    _p = SHARED_DIR / fname
                    cc1, cc2 = st.columns([5, 1])
                    cc1.markdown(
                        f"📄 **{fname}** ({_p.stat().st_size:,} B) — 保存済 / "
                        f"最終取得: {_format_mtime_relative(_p)}"
                    )
                    cc2.download_button("📥 DL", _p.read_bytes(), file_name=fname,
                                        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                                        key=f"inline_dl_{fname}")

            elif fname in INDUSTRY_FILE:
                cfg = INDUSTRY_FILE[fname]
                freq_tag = f"({'M' if want_m else ''}{'D' if want_d else ''})"
                with st.status(f"取得中: {cfg['label']} {freq_tag}", expanded=True) as status:
                    m_df, d_df = pd.DataFrame(), pd.DataFrame()
                    try:
                        if want_m:
                            st.write("📅 Monthly 取得...")
                            try:
                                m_df, m_notes = fetch_industry(cfg["monthly"], "monthly")
                                for n in m_notes: st.write(f"   {n}")
                                st.write(f"   ✅ Monthly: **{len(m_df)} 行**")
                            except Exception as e:
                                st.write(f"   ❌ Monthly 失敗: {e}")
                        else:
                            st.write("⏭ Monthly スキップ（未選択）")

                        if want_d:
                            st.write("📅 Daily 取得...")
                            try:
                                d_df, d_notes = fetch_industry(cfg["daily"], "daily")
                                for n in d_notes: st.write(f"   {n}")
                                st.write(f"   ✅ Daily: **{len(d_df)} 行**")
                            except Exception as e:
                                st.write(f"   ❌ Daily 失敗: {e}")
                        else:
                            st.write("⏭ Daily スキップ（未選択）")

                        out_path = SHARED_DIR / fname
                        save_info = save_xlsx_two_sheets(out_path, m_df, d_df)
                        if "error" in save_info:
                            raise RuntimeError(save_info["error"])
                        saved_paths.append(out_path)
                        results.append({
                            "file": fname,
                            "monthly_rows": save_info["monthly_rows"],
                            "daily_rows": save_info["daily_rows"],
                            "size_B": save_info["size"],
                            "cols": ", ".join(m_df.columns if not m_df.empty else d_df.columns),
                            "status": "✅",
                        })
                        status.update(label=f"✅ {cfg['label']} 保存完了 (M:{save_info['monthly_rows']}行 / D:{save_info['daily_rows']}行)", state="complete")
                    except Exception as e:
                        _cache_hint = "（キャッシュ保持）" if (SHARED_DIR / fname).exists() else ""
                        results.append({
                            "file": fname, "monthly_rows": 0, "daily_rows": 0,
                            "size_B": 0, "cols": "-", "status": f"❌ {e}{_cache_hint}",
                        })
                        status.update(label=f"❌ {cfg['label']} 失敗", state="error")
                # 常時表示の DL 行（status 外）
                if (SHARED_DIR / fname).exists():
                    _p = SHARED_DIR / fname
                    cc1, cc2 = st.columns([5, 1])
                    cc1.markdown(
                        f"📄 **{fname}** ({_p.stat().st_size:,} B) — 保存済 / "
                        f"最終取得: {_format_mtime_relative(_p)}"
                    )
                    cc2.download_button("📥 DL", _p.read_bytes(), file_name=fname,
                                        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                                        key=f"inline_dl_{fname}")

        # ========================================================
        # ファクター取得完了直後の prominent DL セクション
        # （マクロ処理に入る前に確実に表示・たとえマクロが失敗しても残る）
        # ========================================================
        if saved_paths:
            st.markdown("---")
            st.markdown("### ✅ ファクター取得完了 — ここからすぐ DL 可能")
            st.caption(
                "🔽 マクロ取得がこの後始まりますが、上記ファクターはこのセクションから **マクロを待たずに DL** できます。"
            )
            dl_cols = st.columns(min(len(saved_paths), 4))
            for i, sp in enumerate(saved_paths):
                with dl_cols[i % len(dl_cols)]:
                    with st.container(border=True):
                        st.markdown(f"📄 **{sp.name}**")
                        st.caption(f"{sp.stat().st_size:,} B")
                        st.download_button(
                            f"📥 {sp.stem} をDL",
                            sp.read_bytes(),
                            file_name=sp.name,
                            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                            key=f"prominent_dl_{sp.name}",
                            use_container_width=True,
                        )
            st.markdown("---")

        if include_macro:
            # 選択された系列だけサブセットを作る
            us_subset = {sid: FRED_SERIES_US[sid] for sid in us_series_selected if sid in FRED_SERIES_US} if get_us_macro else {}
            jp_subset = {sid: FRED_SERIES_JP[sid] for sid in jp_series_selected if sid in FRED_SERIES_JP} if get_jp_macro else {}

            # 状態診断（バグ追跡用）
            st.markdown(
                f"### 🔍 マクロ取得の事前診断\n"
                f"- US マクロ取得: **{'✅有効' if get_us_macro else '❌無効'}** "
                f"/ 選択系列数: **{len(us_subset)}** / 系列: `{', '.join(us_subset.values()) if us_subset else '(なし)'}`\n"
                f"- JP マクロ取得: **{'✅有効' if get_jp_macro else '❌無効'}** "
                f"/ 選択系列数: **{len(jp_subset)}** / 系列: `{', '.join(jp_subset.values()) if jp_subset else '(なし)'}`"
            )

            with st.status(f"取得中: マクロ統合 (US:{len(us_subset)}系列 / JP:{len(jp_subset)}系列) → macro.xlsx", expanded=True) as status:
                us_macro, jp_macro = pd.DataFrame(), pd.DataFrame()
                if us_subset:
                    st.write(f"🇺🇸 **US 取得開始** ({len(us_subset)} 系列)...")
                    us_macro, us_notes = build_macro_monthly(us_subset)
                    for n in us_notes: st.write(f"   {n}")
                    if not us_macro.empty:
                        st.write(f"   ✅ US 完了: **{len(us_macro)} 行 × {len(us_macro.columns)} 列** "
                                 f"({us_macro.index.min().date()} 〜 {us_macro.index.max().date()})")
                    else:
                        st.write(f"   ❌ US: 取得結果が空")
                else:
                    st.write("⏭ US スキップ（取得対象なし）")

                if jp_subset:
                    st.write(f"🇯🇵 **JP 取得開始** ({len(jp_subset)} 系列)...")
                    jp_macro, jp_notes = build_macro_monthly(jp_subset)
                    for n in jp_notes: st.write(f"   {n}")
                    if not jp_macro.empty:
                        st.write(f"   ✅ JP 完了: **{len(jp_macro)} 行 × {len(jp_macro.columns)} 列** "
                                 f"({jp_macro.index.min().date()} 〜 {jp_macro.index.max().date()})")
                    else:
                        st.write(f"   ❌ JP: 取得結果が空")
                else:
                    st.write("⏭ JP スキップ（取得対象なし）")

                # 結合プレビュー
                if not us_macro.empty and not jp_macro.empty:
                    common_dates = us_macro.index.intersection(jp_macro.index)
                    st.write(f"   🔀 共通月数（inner join 結果予測）: **{len(common_dates)}** 月")
                    if len(common_dates) == 0:
                        st.warning(
                            "⚠️ 共通月がゼロです！ US と JP の日付インデックスが一致していません。"
                            "→ combined シートが空になります。"
                        )

                if not us_macro.empty or not jp_macro.empty:
                    out_path = SHARED_DIR / "macro.xlsx"
                    save_info = save_macro_xlsx(us_macro, jp_macro, out_path)
                    if "error" in save_info:
                        status.update(label=f"❌ マクロ保存失敗: {save_info['error']}", state="error")
                    else:
                        saved_paths.append(out_path)
                        sheets_made = []
                        if not us_macro.empty: sheets_made.append("us")
                        if not jp_macro.empty: sheets_made.append("jp")
                        if not us_macro.empty and not jp_macro.empty: sheets_made.append("combined")
                        derived_info = ""
                        if save_info.get("derived"):
                            derived_info = f" + 派生 {len(save_info['derived'])} 列 ({', '.join(save_info['derived'])})"
                        results.append({
                            "file": "macro.xlsx",
                            "monthly_rows": f"US={save_info['us_rows']} JP={save_info['jp_rows']} combined={save_info['combined_rows']}{derived_info}",
                            "daily_rows": 0,
                            "size_B": save_info["size"],
                            "cols": f"シート: {', '.join(sheets_made)}",
                            "status": "✅",
                        })
                        status.update(label=f"✅ macro.xlsx 保存完了 (Excel形式・{len(sheets_made)}シート{derived_info})", state="complete")
                else:
                    status.update(label="❌ マクロ統合 失敗（FREDタイムアウト）", state="error")
                    st.error(
                        "📡 **FRED への接続が全系列タイムアウトしました**\n\n"
                        "対処方法:\n"
                        "1. しばらく時間をおいて再試行（FRED 側が混雑している可能性）\n"
                        "2. 上の「🔧 FRED 接続テスト」で軽い系列1つで疎通確認\n"
                        "3. VPN/プロキシ使用中なら一時的にOFF\n"
                        "4. **ファクター3ファイルは保存済みなので、上の DL ボタンから取得可能**"
                    )

            # マクロも常時表示の DL 行
            macro_path = SHARED_DIR / "macro.xlsx"
            if macro_path.exists():
                cc1, cc2 = st.columns([5, 1])
                cc1.markdown(
                    f"📄 **macro.xlsx** ({macro_path.stat().st_size:,} B) — 保存済 / "
                    f"最終取得: {_format_mtime_relative(macro_path)}"
                )
                cc2.download_button("📥 DL", macro_path.read_bytes(), file_name="macro.xlsx",
                                    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                                    key="inline_dl_macro")

        # ========================================================
        # BOJ 取得処理（選択されたデータセットを順に取得・保存）
        # 既存パターン（factor / industry）を踏襲。raw xlsx をそのまま保存。
        # ========================================================
        boj_targets = [fname for fname, on in boj_choices.items() if on] if include_boj else []
        for fname in boj_targets:
            cfg = BOJ_DATASETS[fname]
            with st.status(f"取得中: {cfg['label']}", expanded=True) as status:
                try:
                    st.write(f"📥 BOJ から取得中: `{cfg['url']}`")
                    payload = fetch_boj_xlsx(cfg["url"])
                    st.write(f"   ✅ 受信: **{len(payload):,} bytes**")
                    out_path = SHARED_DIR / fname
                    save_info = save_boj_raw_xlsx(out_path, payload)
                    if "error" in save_info:
                        raise RuntimeError(save_info["error"])
                    saved_paths.append(out_path)
                    results.append({
                        "file": fname,
                        "monthly_rows": "-",
                        "daily_rows": "-",
                        "size_B": save_info["size"],
                        "cols": "raw xlsx（多段ヘッダ・分析側で整形）",
                        "status": "✅",
                    })
                    status.update(label=f"✅ {cfg['label']} 保存完了 ({save_info['size']:,} B)", state="complete")
                except Exception as e:
                    _cache_hint = "（キャッシュ保持）" if (SHARED_DIR / fname).exists() else ""
                    results.append({
                        "file": fname, "monthly_rows": 0, "daily_rows": 0,
                        "size_B": 0, "cols": "-", "status": f"❌ {e}{_cache_hint}",
                    })
                    status.update(label=f"❌ {cfg['label']} 失敗", state="error")
            # status 外側の常時表示行
            if (SHARED_DIR / fname).exists():
                _p = SHARED_DIR / fname
                cc1, cc2 = st.columns([5, 1])
                cc1.markdown(
                    f"📄 **{fname}** ({_p.stat().st_size:,} B) — 保存済 / "
                    f"最終取得: {_format_mtime_relative(_p)}"
                )
                cc2.download_button("📥 DL", _p.read_bytes(), file_name=fname,
                                    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                                    key=f"inline_dl_{fname}")

        st.markdown("---")
        st.markdown("### 📊 取得結果サマリ")
        st.dataframe(pd.DataFrame(results), use_container_width=True)
        st.success(f"📂 保存先: `{SHARED_DIR}`")

        # === 全保存ファイルの一覧 DL（マクロ含む / 失敗してもファクターは残る）===
        all_saved_now = sorted(SHARED_DIR.glob("*.xlsx"))
        if all_saved_now:
            st.markdown("### 📥 ダウンロード一覧（保存できたファイル全て）")
            st.caption("⚠️ 一部取得に失敗しても、成功したファイルはここから DL できます。")
            dl_cols2 = st.columns(min(len(all_saved_now), 4))
            for i, sp in enumerate(all_saved_now):
                with dl_cols2[i % len(dl_cols2)]:
                    with st.container(border=True):
                        st.markdown(f"📄 **{sp.name}**")
                        st.caption(f"{sp.stat().st_size:,} B")
                        st.download_button(
                            f"📥 {sp.stem} をDL",
                            sp.read_bytes(),
                            file_name=sp.name,
                            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                            key=f"final_dl_{sp.name}",
                            use_container_width=True,
                        )

        # === 取得直後のインラインプレビュー ===
        if saved_paths:
            st.markdown("---")
            st.markdown("### 👀 保存ファイルのインラインプレビュー")
            st.caption("中身を確認してから次のアプリへ進めます。")
            for sp in saved_paths:
                with st.container(border=True):
                    preview_saved_xlsx(sp, key_prefix=f"oneshot_{sp.stem}")

# ---------- Tab 2: 保存ファイル プレビュー ----------
with tabs[1]:
    st.subheader("📂 保存済みファイルを確認")
    st.caption(
        "factor-data-hub が共有ストレージに作った Excel ファイルの中身を、シート別にプレビューします。"
        "ダウンロード前に内容確認できます。"
    )
    existing_xlsx = sorted(SHARED_DIR.glob("*.xlsx"))
    if not existing_xlsx:
        st.info(
            "📭 まだ保存ファイルがありません。\n\n"
            "「🚀 ワンショット取得」タブで取得すると、ここで内容を確認できます。"
        )
    else:
        chosen = st.selectbox(
            "プレビューするファイル",
            existing_xlsx,
            format_func=lambda p: f"{p.name} ({p.stat().st_size:,} B)",
        )
        if chosen is not None:
            preview_saved_xlsx(chosen, key_prefix="standalone")


# ---------- Tab 3: 個別取得（プレビュー） ----------
with tabs[2]:
    st.subheader("🔧 個別取得（プレビュー付き）")
    st.caption("特定の1ファイル・1シートだけ、または特定のマクロ系列1本だけ取得する用。")

    mode = st.radio(
        "種別",
        ["📈 ファクター/産業（KFから）", "📊 マクロ系列（FREDから・1本指定）"],
        horizontal=True, key="single_mode",
    )

    # ----- ファクター/産業 -----
    if mode.startswith("📈"):
        fname = st.selectbox(
            "ファイル",
            options=list(FACTOR_FILES) + list(INDUSTRY_FILE),
            format_func=lambda k: (FACTOR_FILES.get(k) or INDUSTRY_FILE.get(k))["label"] + f" → {k}",
            key="single_fname",
        )
        sheet = st.radio("周波数", ["monthly", "daily"], horizontal=True, key="single_freq")

        if st.button("🚀 取得 & プレビュー", type="primary", key="single_fetch"):
            try:
                if fname in FACTOR_FILES:
                    cfg = FACTOR_FILES[fname][sheet]
                    with st.spinner("取得中..."):
                        df, notes = fetch_ff5_with_mom(cfg["ff5"], cfg["mom"], sheet)
                else:
                    cfg_url = INDUSTRY_FILE[fname][sheet]
                    with st.spinner("取得中..."):
                        df, notes = fetch_industry(cfg_url, sheet)

                for n in notes: st.write(n)

                if df.empty:
                    st.error("取得結果が空です")
                else:
                    c1, c2, c3 = st.columns(3)
                    c1.metric("行数", len(df))
                    c2.metric("列数", len(df.columns))
                    c3.metric("期間", f"{df.index[0]} 〜 {df.index[-1]}")

                    st.markdown("**列構造**")
                    st.write(list(df.columns))
                    st.dataframe(df.head(20), use_container_width=True)
                    st.dataframe(df.tail(20), use_container_width=True)

                    buf = io.BytesIO()
                    df_out = df.reset_index().rename(columns={df.index.name or "index": "date"})
                    with pd.ExcelWriter(buf, engine="openpyxl") as w:
                        df_out.to_excel(w, sheet_name=sheet, index=False)
                    st.download_button(
                        f"📥 単体Excel ({sheet}シートのみ)",
                        buf.getvalue(),
                        file_name=f"{fname.replace('.xlsx','')}_{sheet}.xlsx",
                        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    )
            except Exception as e:
                st.error(f"取得失敗: {type(e).__name__}: {e}")

    # ----- マクロ系列 1本指定 -----
    else:
        all_series = {}
        for sid, lab in FRED_SERIES_US_CORE.items():
            all_series[f"🇺🇸 [コア] {lab} — {sid}"] = (sid, lab, "us")
        for sid, lab in FRED_SERIES_US_STRESS.items():
            all_series[f"🇺🇸 [ストレス] {lab} — {sid}"] = (sid, lab, "us")
        for sid, lab in FRED_SERIES_US_SENTIMENT.items():
            all_series[f"🇺🇸 [センチメント] {lab} — {sid}"] = (sid, lab, "us")
        for sid, lab in FRED_SERIES_JP_CORE.items():
            all_series[f"🇯🇵 [コア] {lab} — {sid}"] = (sid, lab, "jp")
        for sid, lab in FRED_SERIES_JP_EXT.items():
            all_series[f"🇯🇵 [追加] {lab} — {sid}"] = (sid, lab, "jp")
        for sid, lab in FRED_SERIES_JP_SENTIMENT.items():
            all_series[f"🇯🇵 [センチメント] {lab} — {sid}"] = (sid, lab, "jp")
        all_series["🔧 任意のFRED系列ID（手入力）"] = ("__custom__", "custom", "?")

        choice = st.selectbox("取得する系列", list(all_series.keys()), key="single_macro")
        sid, label, region = all_series[choice]
        if sid == "__custom__":
            sid = st.text_input("FRED 系列ID（例: GDP, M2SL）", value="", key="custom_sid").strip()
            label = st.text_input("列名（保存時の名前）", value=sid.lower() if sid else "", key="custom_lab").strip()

        if st.button("🚀 単一系列を取得", type="primary", key="single_macro_fetch", disabled=not sid):
            with st.spinner(f"FRED から {sid} 取得中..."):
                try:
                    s = fetch_fred_series(sid).rename(label or sid.lower())
                    df = s.to_frame()
                    df.index.name = "date"
                    c1, c2, c3 = st.columns(3)
                    c1.metric("ポイント数", len(df))
                    c2.metric("系列ID", sid)
                    if len(df):
                        c3.metric("期間", f"{df.index.min().date()} 〜 {df.index.max().date()}")

                    st.dataframe(df.head(15), use_container_width=True)
                    st.dataframe(df.tail(15), use_container_width=True)
                    st.line_chart(df)

                    # 単一系列のExcel保存
                    buf = io.BytesIO()
                    df_out = df.reset_index()
                    with pd.ExcelWriter(buf, engine="openpyxl") as w:
                        df_out.to_excel(w, sheet_name="data", index=False)
                    st.download_button(
                        f"📥 {sid} の単一系列Excel",
                        buf.getvalue(),
                        file_name=f"{sid}.xlsx",
                        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    )
                    # 共有ストレージ保存オプション
                    if st.checkbox("📂 共有ストレージにも保存", key="save_single_shared"):
                        out_path = SHARED_DIR / f"macro_{sid}.xlsx"
                        with pd.ExcelWriter(out_path, engine="openpyxl") as w:
                            df_out.to_excel(w, sheet_name="data", index=False)
                        st.success(f"保存: {out_path}")
                except Exception as e:
                    st.error(f"取得失敗: {type(e).__name__}: {e}")

# ---------- Tab 4: マクロ ----------
with tabs[3]:
    st.subheader("📊 マクロ統合 (`macro.xlsx` — Excel形式)")
    st.caption("US と JP の系列を **個別チェック**で柔軟に選択 → `macro.xlsx`（シート: `us`/`jp`/`combined`）。")
    st.info("📌 このタブは **マクロ専用** です。ファクター（factors / industry）は出力されません。")

    col1, col2 = st.columns(2)
    with col1:
        st.markdown("**🇺🇸 US 系列**")
        get_us = st.checkbox("US を取得", value=True, key="mtab_get_us")
        us_selected_sids: list[str] = []
        if get_us:
            c1, c2 = st.columns(2)
            if c1.button("全選択", key="mtab_us_all"):
                for sid in FRED_SERIES_US:
                    st.session_state[f"mtab_us_{sid}"] = True
            if c2.button("全解除", key="mtab_us_none"):
                for sid in FRED_SERIES_US:
                    st.session_state[f"mtab_us_{sid}"] = False
            st.caption("**コア**")
            for sid, label in FRED_SERIES_US_CORE.items():
                if st.checkbox(f"{label} ({sid})", value=True, key=f"mtab_us_{sid}"):
                    us_selected_sids.append(sid)
            st.caption("**金利・信用ストレス**")
            for sid, label in FRED_SERIES_US_STRESS.items():
                if st.checkbox(f"{label} ({sid})", value=True, key=f"mtab_us_{sid}"):
                    us_selected_sids.append(sid)
            st.caption("**センチメント（ミシガン大・OECD）**")
            for sid, label in FRED_SERIES_US_SENTIMENT.items():
                if st.checkbox(f"{label} ({sid})", value=True, key=f"mtab_us_{sid}"):
                    us_selected_sids.append(sid)
    with col2:
        st.markdown("**🇯🇵 JP 系列**")
        get_jp = st.checkbox("JP を取得", value=True, key="mtab_get_jp")
        jp_selected_sids: list[str] = []
        if get_jp:
            c1, c2 = st.columns(2)
            if c1.button("全選択", key="mtab_jp_all"):
                for sid in FRED_SERIES_JP:
                    st.session_state[f"mtab_jp_{sid}"] = True
            if c2.button("全解除", key="mtab_jp_none"):
                for sid in FRED_SERIES_JP:
                    st.session_state[f"mtab_jp_{sid}"] = False
            st.caption("**コア**")
            for sid, label in FRED_SERIES_JP_CORE.items():
                if st.checkbox(f"{label} ({sid})", value=True, key=f"mtab_jp_{sid}"):
                    jp_selected_sids.append(sid)
            st.caption("**追加**")
            for sid, label in FRED_SERIES_JP_EXT.items():
                if st.checkbox(f"{label} ({sid})", value=True, key=f"mtab_jp_{sid}"):
                    jp_selected_sids.append(sid)
            st.caption("**センチメント（OECD・短観代替）**")
            for sid, label in FRED_SERIES_JP_SENTIMENT.items():
                if st.checkbox(f"{label} ({sid})", value=True, key=f"mtab_jp_{sid}"):
                    jp_selected_sids.append(sid)

    if st.button("🚀 取得", type="primary", key="macro_run"):
        us_df = pd.DataFrame()
        jp_df = pd.DataFrame()

        if get_us and us_selected_sids:
            us_subset = {sid: FRED_SERIES_US[sid] for sid in us_selected_sids}
            with st.spinner(f"🇺🇸 US 取得中（{len(us_subset)} 系列）..."):
                us_df, us_notes = build_macro_monthly(us_subset)
            for n in us_notes: st.write(n)
        if get_jp and jp_selected_sids:
            jp_subset = {sid: FRED_SERIES_JP[sid] for sid in jp_selected_sids}
            with st.spinner(f"🇯🇵 JP 取得中（{len(jp_subset)} 系列）..."):
                jp_df, jp_notes = build_macro_monthly(jp_subset)
            for n in jp_notes: st.write(n)

        if us_df.empty and jp_df.empty:
            st.error("取得失敗：両方とも0件でした。系列を1つ以上選択してください。")
        else:
            out_path = SHARED_DIR / "macro.xlsx"
            save_info = save_macro_xlsx(us_df, jp_df, out_path)
            if "error" in save_info:
                st.error(f"保存失敗: {save_info['error']}")
            else:
                st.success(
                    f"✅ `macro.xlsx` 保存完了（Excel形式・{save_info['size']:,} B）"
                    f"\n\nUS: {save_info['us_rows']} 行 / JP: {save_info['jp_rows']} 行 / combined: {save_info['combined_rows']} 行"
                    f"\n\n📂 `{out_path}`"
                )
                st.markdown("### 👀 保存ファイルのインラインプレビュー")
                preview_saved_xlsx(out_path, key_prefix="macro_tab")


# ---------- Tab 5: マクロ手動アップロード（FRED不通時のfallback） ----------
with tabs[4]:
    st.subheader("📤 マクロ手動アップロード（FRED 不通時の救済策）")
    st.caption(
        "FRED への接続が継続的に失敗する環境向けの fallback。"
        "FRED のサイトから手動 DL した CSV (1系列1ファイル) を複数まとめてアップロード → `macro.xlsx` を組み立てます。"
    )

    with st.expander("📖 FRED から CSV を手動 DL する手順", expanded=False):
        st.markdown(
            """
            1. https://fred.stlouisfed.org/ にブラウザでアクセス
            2. 検索窓に系列ID（例: `CPIAUCSL`, `GS10`, `JPNCPIALLMINMEI`）を入力
            3. ページ右上の「📥 Download」→ **CSV** を選択
            4. 各系列について繰り返し → 全 CSV をここにまとめてアップロード

            ファイル名は **系列ID.csv**（例: `CPIAUCSL.csv`）にしておくとラベル付けが自動。
            """
        )

    uploaded_csvs = st.file_uploader(
        "📥 FRED CSV を複数アップロード（系列IDがファイル名）",
        type=["csv"], accept_multiple_files=True, key="manual_fred",
    )

    region = st.radio(
        "保存先シート",
        ["us", "jp", "両方（自動振り分け）"],
        horizontal=True, key="manual_region",
    )

    if uploaded_csvs and st.button("🚀 手動アップロードから macro.xlsx 構築", type="primary"):
        # 既知の US/JP 系列IDから自動振り分け
        us_ids = set(FRED_SERIES_US.keys())
        jp_ids = set(FRED_SERIES_JP.keys())
        us_series = {}
        jp_series = {}
        unknown = []
        for f in uploaded_csvs:
            stem = Path(f.name).stem
            try:
                df = pd.read_csv(f)
                date_col = next((c for c in df.columns if c.lower() in ("date", "observation_date")), df.columns[0])
                val_col = [c for c in df.columns if c != date_col][0]
                df[date_col] = pd.to_datetime(df[date_col])
                df[val_col] = pd.to_numeric(df[val_col], errors="coerce")
                s = df.set_index(date_col)[val_col].rename(stem.lower())
                # 月次再サンプル
                if s.index.freqstr != "M":
                    s = s.resample("ME").last()
                # 振り分け
                if region == "us":
                    us_series[stem.lower()] = s
                elif region == "jp":
                    jp_series[stem.lower()] = s
                else:
                    if stem in us_ids:
                        label = FRED_SERIES_US[stem]
                        us_series[label] = s.rename(label)
                    elif stem in jp_ids:
                        label = FRED_SERIES_JP[stem]
                        jp_series[label] = s.rename(label)
                    else:
                        unknown.append(stem)
                        us_series[stem.lower()] = s  # デフォルト us 扱い
                st.write(f"✅ {f.name}: {len(s)} ポイント")
            except Exception as e:
                st.error(f"❌ {f.name}: {e}")

        if unknown:
            st.warning(f"⚠️ 自動振り分けできないID（US側に格納）: {', '.join(unknown)}")

        us_df = pd.concat(us_series.values(), axis=1) if us_series else pd.DataFrame()
        jp_df = pd.concat(jp_series.values(), axis=1) if jp_series else pd.DataFrame()
        if not us_df.empty: us_df.index.name = "date"
        if not jp_df.empty: jp_df.index.name = "date"

        if us_df.empty and jp_df.empty:
            st.error("有効なデータがありませんでした")
        else:
            out_path = SHARED_DIR / "macro.xlsx"
            save_info = save_macro_xlsx(us_df, jp_df, out_path)
            if "error" in save_info:
                st.error(f"保存失敗: {save_info['error']}")
            else:
                st.success(
                    f"✅ macro.xlsx 保存完了（手動アップロードから構築）"
                    f"\nUS: {save_info['us_rows']} 行 / JP: {save_info['jp_rows']} 行 / combined: {save_info['combined_rows']} 行"
                )
                preview_saved_xlsx(out_path, key_prefix="manual_macro")
