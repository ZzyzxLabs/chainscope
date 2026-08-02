# chainscope

**開源鏈上鑑識工具 —— 用公開資料能做到的,全部做到。**

[English](README.md)

> ⚠️ **Alpha 階段**,API 仍會變動。設計文件見 [ARCHITECTURE.md](ARCHITECTURE.md)(英文)。

---

## 為什麼做這個

商業鏈上分析平台(Chainalysis、Elliptic、TRM)的護城河是我們**複製不了、也不該假裝能複製**的東西:十年累積的專有標註資料庫,來自傳票、交易所合作與臥底購買。

但它們四項核心能力當中,有三項完全不需要專有資料:

| 能力 | 公開資料可複製? | 原因 |
|---|---|---|
| 跨鏈配對 | ✅ | 本質是時間 × 金額 × 行為特徵的搜尋問題 |
| 聚類與歸集推論 | ✅ | 演算法都是公開的 |
| 資金流追蹤 | ✅ | 圖遍歷 |
| 實體歸屬 | ⚠️ 部分 | 只能靠公開標籤 + 有文件的啟發式方法 |

chainscope 誠實地實作可複製的那些部分,給那些沒有六位數授權費的人:**記者、研究者、小型執法單位、被駭的專案方、CTF 選手**。

## 最重要的設計決定

鏈上鑑識有一個反覆出現的失敗模式:**一個啟發式的猜測被寫了下來,經過三個工具的傳遞,最後看起來像事實。而有人會因此被指控。**

chainscope 讓這件事在結構上難以發生。每一則歸屬都必須攜帶它的來源與證據強度,型別系統不允許你省略:

```python
Attribution(
    address="bc1q...",                 # 某兌換服務的比特幣熱錢包
    label="Instant-swap service (BTC side)",
    category=Category.CEX,
    confidence=Confidence.LOW,         # ← 行為推論,不是標籤
    method=Method.INFERENCE,
    source="analyst",
    rationale="存款後 5–45 分鐘內出款,折價率穩定,"
              "且總是付給全新地址、找零回到本錢包。",
)
```

低信心度的主張**漏寫 `rationale` 會直接建構失敗**。`Confidence.HIGH` 以下一律以「主張」而非「標籤」呈現。合併時,制裁名單的命中**永遠不會**被較友善的標籤蓋掉。

這不是防禦性的樣板程式碼。**這是這個領域的專業倫理,只是改由編譯器來執行,而不是仰賴讀輸出的人自己記得。**

## 信心度分級

```
CERTAIN      權威名單,或合約在鏈上自述
HIGH         第三方公開發布的標籤(區塊瀏覽器 nametag)
MEDIUM       結構性啟發式(共同輸入聚類、存款地址歸集)
LOW          行為推論(時間、金額、費率模式)
SPECULATIVE  單一巧合
```

## 安裝

```bash
pip install chainscope            # 核心
pip install "chainscope[all]"     # + EVM、Bitcoin、Solana、美化輸出
```

## 目前進度

v0.1 建置中。已完成:

- [x] `core.units` —— `Amount` 精確整數運算,永不使用 float
- [x] `core.chainid` —— CAIP-2 鏈識別
- [x] `core.attribution` —— 來源、信心度、非破壞性合併
- [x] `transport` —— 內容定址快取、finality 推導 TTL、節流、稽核日誌
- [x] `providers` —— 以「能力」路由的資料源,含失敗轉移
- [x] `chains` —— EVM 與 Bitcoin 轉接器
- [x] `attribution` —— OFAC、瀏覽器標籤、本地標註、衝突解析
- [ ] `analysis` —— 歸集、跨鏈配對、剝離鏈、聚類
- [ ] `cli` 與 renderers

## 擴充

新增一條鏈、一個資料源、一個分析器或一個標註來源,**應該只需要一個檔案加一份測試 cassette**。這是這套架構被檢驗的標準。全部透過 entry points 註冊,不需要改動核心:

```toml
[project.entry-points."chainscope.analyzers"]
my_analyzer = "my_package:MyAnalyzer"
```

詳見 [docs/extending.md](docs/extending.md)(英文)。

你的擴充**可以住在自己的 repo**,不需要合併進本專案。

## 架構上就是唯讀

取數層在型別上沒有簽名能力 —— 不是靠規範,是靠型別。`Query` 是唯讀操作的封閉聯集,傳輸層另外阻擋 `eth_send*`、`eth_sign*`、`personal_*`。

**能動用資金的鑑識工具是使用者的風險。這一個做不到。**

## ⚠️ 啟發式結果不是證據

聚類、跨鏈配對、找零判定產出的是**帶分數的假設**,不是結論。`Confidence.HIGH` 以下的結果,**未經獨立驗證前不得用於指控任何個人或實體**。

如果你正在建立一個會影響某人自由或生計的案件:這個工具幫你找線索,**不幫你結案**。

## 貢獻

歡迎。請先讀 [CONTRIBUTING.md](CONTRIBUTING.md)(英文)—— 特別是三條不可妥協的規則:測試不得連網、歸屬必須帶來源、新增標註來源必須同步更新 `docs/data-sources.md`。

程式碼與註解請用英文(為了國際貢獻者),但**翻譯版 README 非常歡迎**。

## 授權

Apache-2.0。標註資料集依其各自條款另行散布,見 [docs/data-sources.md](docs/data-sources.md)。
