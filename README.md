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
## 商用向けの権利確認型BYODモード（レビュー中）

この作業ブランチでは、商用モードを既定にしました。既存の研究用API取得機能は、明示的にresearchを選択した場合のみ使用できます。新しい商用モードは外部データ提供元に接続せず、ユーザー自身が利用権を持つCSV/XLSXの取込・品質検査のみを行います。

商用モードで使用する依存関係と起動コマンド（ローカル環境）：

~~~powershell
python -m pip install -r requirements-commercial.txt
$env:FACTOR_DATA_HUB_MODE = "commercial"
python -m streamlit run app.py --server.address localhost
~~~

研究用の旧機能が必要な場合は、明示的にresearchに切り替える必要があります（販売版で使用しないこと）：

~~~powershell
$env:FACTOR_DATA_HUB_MODE = "research"
python -m pip install -r requirements.txt
python -m streamlit run app.py
~~~

**重要:** 旧研究モードには、ユーザー名によりGitHub内のAPIキーを参照する方式や、第三者の時系列を取得・保存する処理が残ります。第三者に公開する認証・データ提供方式として認可されていません。商用利用の許諾を個別に確認せず流用しないでください。Streamlitをクラウド上に置いた場合は、アップロードしたファイルはクラウドサーバーへ送信されます。品質レポートはデータ利用権の証明書ではありません。

- 権利・接続方針：COMMERCIAL_DATA_POLICY.md
- 品質検査：commercial_byod.py
- オフライン回帰テスト（実行検証待ち）：python -m unittest discover -s tests -p "test_commercial_byod.py" -v
- **リリース判定：HOLD**。商用データ接続の許諾、Windows実機試験、MSIXなどが未完了です。
