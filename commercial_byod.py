"""Commercial-mode bring-your-own-data import and quality inspection.

This module has no network clients, credential store, database or persistent
upload cache. Users must have rights to process the files they submit.
"""
from __future__ import annotations

import hashlib
import io
import json
import zipfile
from pathlib import Path
from typing import Any

import pandas as pd

MAX_UPLOAD_BYTES = 10 * 1024 * 1024
MAX_UNCOMPRESSED_XLSX_BYTES = 80 * 1024 * 1024
MAX_XLSX_ENTRIES = 500
MAX_ROWS = 100_000
MAX_COLUMNS = 100
DATE_NAMES = {"date", "datetime", "timestamp", "日付", "年月", "年月日"}


class DataImportError(ValueError):
    """User-facing validation failure."""


def _safe_xlsx(raw: bytes) -> None:
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            files = archive.infolist()
            if len(files) > MAX_XLSX_ENTRIES:
                raise DataImportError("Excelファイル内の項目数が上限を超えています。")
            if sum(item.file_size for item in files) > MAX_UNCOMPRESSED_XLSX_BYTES:
                raise DataImportError("Excel展開後のサイズが上限を超えています。")
            if any(item.flag_bits & 1 for item in files):
                raise DataImportError("暗号化されたExcelファイルは非対応です。")
            if not any(item.filename == "xl/workbook.xml" for item in files):
                raise DataImportError("有効なExcelブックではありません。")
    except zipfile.BadZipFile as exc:
        raise DataImportError("破損または非対応のExcelファイルです。") from exc


def inspect_upload(
    raw: bytes,
    filename: str,
    *,
    source: str,
    license_note: str,
    rights_confirmed: bool,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Load one CSV/XLSX and return a non-persistent dataframe and QA report."""
    if not rights_confirmed:
        raise DataImportError("データを利用する権利の確認が必要です。")
    if not isinstance(raw, bytes) or not raw:
        raise DataImportError("空のデータは取り込めません。")
    if len(raw) > MAX_UPLOAD_BYTES:
        raise DataImportError("ファイルサイズが10MiBを超えています。")
    extension = Path(filename).suffix.lower()
    if extension not in {".csv", ".xlsx"}:
        raise DataImportError("対応形式はCSVとXLSXのみです。")
    if not source.strip():
        raise DataImportError("データの出典を入力してください。")
    if not license_note.strip():
        raise DataImportError("利用条件・権利の根拠を入力してください。")

    try:
        if extension == ".csv":
            df = pd.read_csv(io.BytesIO(raw), dtype=str, nrows=MAX_ROWS + 1,
                             encoding="utf-8-sig")
        else:
            _safe_xlsx(raw)
            df = pd.read_excel(io.BytesIO(raw), sheet_name=0, dtype=str,
                               nrows=MAX_ROWS + 1, engine="openpyxl")
    except (UnicodeError, pd.errors.ParserError, ValueError, OSError, zipfile.BadZipFile) as exc:
        raise DataImportError("読込に失敗しました。UTF-8 CSVまたは正常なXLSXを指定してください。") from exc

    if df.empty:
        raise DataImportError("データ行がありません。")
    if len(df) > MAX_ROWS:
        raise DataImportError("行数が上限（100,000行）を超えています。")
    if df.shape[1] > MAX_COLUMNS:
        raise DataImportError("列数が上限（100列）を超えています。")
    column_names = [str(c).strip() for c in df.columns]
    if any(not col or col.lower().startswith("unnamed:") for col in column_names):
        raise DataImportError("空欄の列名があります。見出しを設定してください。")
    if len(set(column_names)) != len(column_names):
        raise DataImportError("重複した列名があります。")
    df.columns = column_names

    report: dict[str, Any] = {
        "format": "commercial_byod_quality_v1",
        "input_sha256": hashlib.sha256(raw).hexdigest(),
        "file_type": extension.lstrip("."),
        "source_declared_by_user": source.strip()[:300],
        "license_note_declared_by_user": license_note.strip()[:500],
        "rights_declared_by_user": True,
        "rows": int(df.shape[0]),
        "columns": int(df.shape[1]),
        "missing_by_column": {str(col): int(df[col].isna().sum()) for col in df.columns},
        "duplicate_rows": int(df.duplicated().sum()),
        "warnings": [],
    }
    date_columns = [col for col in df.columns if col.lower() in DATE_NAMES]
    if date_columns:
        col = date_columns[0]
        dates = pd.to_datetime(df[col], errors="coerce")
        invalid = int(dates.isna().sum())
        duplicate_dates = int(dates[dates.notna()].duplicated().sum())
        report["date_column"] = col
        report["invalid_dates"] = invalid
        report["duplicate_dates"] = duplicate_dates
        if invalid:
            report["warnings"].append(f"日付形式の不正・欠損: {invalid}行")
        if duplicate_dates:
            report["warnings"].append(f"日付の重複: {duplicate_dates}行")
        if not dates.dropna().is_monotonic_increasing:
            report["warnings"].append("日付が昇順ではありません。")
    if report["duplicate_rows"]:
        report["warnings"].append(f"完全重複行: {report['duplicate_rows']}行")
    if any(report["missing_by_column"].values()):
        report["warnings"].append("欠損値があります。")
    report["status"] = "REVIEW" if report["warnings"] else "STRUCTURE_CHECKED"
    # This does NOT attest to licensing, ownership, accuracy or financial fitness.
    return df, report


def render_commercial_app() -> None:
    """Commercial mode: strictly upload-only, without external data downloads."""
    import streamlit as st

    st.title("データ接続・品質チェック（持ち込みデータ版）")
    st.caption("CSV / Excelを取り込み、日付・欠損・重複を確認するためのツールです。")
    st.warning(
        "外部の株価・マクロデータを同梱せず、未承認のデータAPIへの接続もしません。"
        "利用者自身が利用権を持つデータだけを指定してください。"
        "ファイルはこのStreamlitアプリが動作する環境で処理されます。"
        "クラウド上で実行する場合、アップロード内容はそのサーバーへ送信されます。"
    )
    source = st.text_input("出典・提供元", placeholder="例：自社データ / 契約先プロバイダー名")
    license_note = st.text_input(
        "利用条件・権利の根拠", placeholder="例：社内利用を許可された契約データ"
    )
    rights = st.checkbox("このデータを取り込み、分析する権利を確認しました")
    upload = st.file_uploader("CSV / Excelを選択（最大10MiB）", type=["csv", "xlsx"])
    if st.button("データ品質を検査", type="primary"):
        if upload is None:
            st.error("ファイルを選択してください。")
            return
        try:
            frame, report = inspect_upload(
                upload.getvalue(), upload.name,
                source=source, license_note=license_note, rights_confirmed=rights
            )
        except DataImportError as exc:
            st.error(str(exc))
            return
        st.metric("行数", report["rows"])
        st.metric("列数", report["columns"])
        st.write("検査結果:", report["status"])
        for warning in report["warnings"]:
            st.warning(warning)
        st.dataframe(frame.head(30), use_container_width=True)
        st.download_button(
            "品質レポート（JSON）を保存",
            json.dumps(report, ensure_ascii=False, indent=2),
            file_name="data_quality_report.json",
            mime="application/json",
        )
        st.info(
            "この検査は形式・一部のデータ品質のみを扱います。"
            "出典の正当性、商用利用許可、値の正確性は証明しません。"
        )
