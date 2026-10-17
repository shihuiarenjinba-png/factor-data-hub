# Commercial Data Boundary / 商用データ接続ポリシー
Reviewed: 2026-10-18. Status: HOLD (not release approval).

## 商品の範囲
- 販売価値はCSV/XLSX取込、品質検査、分析、表示、レポート等のソフトウェア機能。
- 第三者の株価・マクロ時系列、非公開トークン、APIキー、研究DBは販売物に同梱しない。
- 利用者の権利が確認できるBYODファイルは取込可能とする。

## 実装上の境界
- FACTOR_DATA_HUB_MODEは既定でcommercial。未知の値は起動を拒否する。
- commercialでは専用のアップロード画面に分岐し、研究用の外部データ取得処理には進まない。
- yfinanceはresearchモードを明示した場合のみimportする。
- 商用依存はrequirements-commercial.txtのみを使う。
- 出典、利用条件、権利確認が必須。ローカルファイルを製品側で再配布しない。
- commercialでは既存の共有ストレージへアップロード内容を保存しない。
- 入力上限: 10MiB、100,000行、100列。XLSX展開サイズにも制限。
- Streamlitをクラウドホストするとユーザーデータが当該サーバーへ送信される。ローカル処理と誤表示しない。
- 利用権の自己申告は法的な許諾を証明するものではない。

## 既存研究モードへの警告
既存の研究モードは変更していないが、販売・一般公開を承認するものではない。
特に旧 username -> private GitHub users.json -> FRED API key の方式は、ユーザー名だけで本人を確認している設計であり、利用者間のキー分離が保証されていない。
商用公開・不特定多数への利用は禁止し、認証方式を別途改修すること。
研究DB、機密データ、トークンは販売用コードにコピーしない。

## 提供元別ゲート
| Provider | 商用モードでの自動接続 | 判定 |
| --- | --- | --- |
| Yahoo Finance / yfinance | 無効 | Yahooは再配布を制限。yfinanceもYahoo APIを個人利用向けとしている。 |
| FRED | 無効 | 系列ごとに著作権が異なり、APIおよびAI/ML/キャッシュ利用制限もある。個別審査要。 |
| Bank of Japan | 無効 | 商用複製等は事前許可が必要な場合がある。 |
| Kenneth French Data Library | 無効 | 商用再配布の条件・許諾を確認するまで保留。 |
| e-Stat | 未実装 | 利用規約に従う商用利用は可能。APIアプリID・出典・第三者権利等を個別確認。 |
| BYOD CSV / XLSX | 有効 | 利用者の権利宣言を前提とする。真正性は未検証。 |

Official sources:
- https://fred.stlouisfed.org/docs/api/terms_of_use.html
- https://fred.stlouisfed.org/legal/fraud-awareness
- https://help.yahoo.com/kb/SLN2352.html
- https://github.com/ranaroussi/yfinance
- https://www.stat-search.boj.or.jp/info/notice_en.html
- https://www.e-stat.go.jp/terms-of-use
- https://www.e-stat.go.jp/api/api-dev/faq

## Release gate
1. Windows実機でのCSV/XLSX破損、文字コード、巨大ファイル、日付処理、権限確認。
2. Streamlitローカル実行のアクセス境界、セッション・ログ・一時ファイルの取扱い確認。
3. 利用許諾、第三者ライセンス、プライバシー表示、販売説明の整合。
4. MSIXビルド、署名、インストール、更新、アンインストール、Store審査確認。
5. 外部APIは接続ごとに許諾と出典の確認が終わるまで製品UIから提供しない。
6. 未実施の回帰試験・実機試験をPASSと報告しない。
