# factor-data-hub — ファクター/マーケットデータ取得ハブ

Kenneth French Library + FRED + yfinance + BOJ から Japan 5 / US 5 ファクターと
マクロ系列を取得し、.shared/factor_data/ 配下に Excel 形式で保存する Streamlit アプリ。

カテゴリ: **Data** ・ launcher の pp-registry/factor-data-hub.json から参照。

## ローカル起動

```bash
pip install -r requirements.txt
streamlit run app.py
```

## Streamlit Cloud 用設定

このアプリは FRED API key を必要とします。Streamlit Cloud にデプロイする場合:

1. **Streamlit Cloud の Settings → Secrets** に以下を追加:
   ```toml
   GITHUB_DATA_TOKEN = "<読み取り PAT — factor-data-secrets repo にアクセス可能>"
   ```

2. アプリ起動後、サイドバーの「👤 登録ユーザー」に **username** を入力 → 「登録キーを取得」
   - Private repo shihuiarenjinba-png/factor-data-secrets/users.json から
     `{"<username>": "<FRED_API_KEY>"}` をルックアップし、セッション内にキャッシュ
   - 以後そのセッションでは毎回入力不要

3. (代替) FRED API key 直接入力もサイドバーから可能。
   または環境変数 FRED_API_KEY でも上書き可。