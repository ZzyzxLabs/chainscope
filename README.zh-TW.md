# chainscope

**一套用來「蓋自己的鏈上分析」的框架 —— 取數、標籤、儲存、過濾、警報,可重現性的紀律已經內建。**

[English](README.md)

> ⚠️ **Alpha 階段**,`0.x` 期間 API 仍會變動。第一條端到端路徑已通(Etherscan → 歸集分析),但**尚無錄製的 cassette**,provider 抽象還沒見過真實 API 回應。詳見[目前進度](#目前進度)。

---

## 這是什麼

**不是**一個做完的鑑識產品,而是**一副讓你在上面蓋自己東西的骨架**。

資料來源、標籤、以及你所在領域特有的問題,由你帶進來。chainscope 提供的是那些**寫起來瑣碎、容易微妙地寫錯、而且每個同類專案幾乎都一樣**的部分:

| 你需要 | 你會拿到 |
|---|---|
| 取鏈上資料 | 依能力路由的 provider,含失敗轉移、快取、節流、稽核 |
| 正規化 | 橫跨 EVM / UTXO / Solana / Tron 的同一組 `Transfer` / `Address` / `Amount` |
| 標註地址 | 多來源解析器,合併衝突但不隱藏衝突 |
| 儲存與查詢 | 可重建的 SQLite store,附型別化過濾 |
| 分析 | `Analyzer` 協定;四個技術作為可運作的參考實作 |
| 警報 | *(規劃中)* Watch 是區塊範圍的純函式 —— 不用跑 daemon |
| 產出報告 | 終端機、Markdown、JSON —— 全都保留信心度 |
| 舉證 | 任何人都能離線重播的 case bundle |

以上每一項都是外掛點,而且**你的擴充住在你自己的 repo** —— 新增一條鏈、provider、store 後端、分析器或標籤來源,應該只需要一個檔案加一份測試 cassette,**不需要 fork 這個專案**。

### 天花板在哪

商業平台(Chainalysis、Elliptic、TRM)的護城河是十年累積的專有標註資料,來自傳票、交易所合作與臥底購買。那個資料庫**用公開資料複製不出來**,而假裝可以會是這個專案能做的最有害的事。

它們另外三項能力則完全不需要專有資料:

| 能力 | 公開資料可複製? | 原因 |
|---|---|---|
| 跨鏈配對 | ✅ | 本質是時間 × 金額 × 行為特徵的搜尋問題 |
| 聚類與歸集推論 | ✅ | 演算法都是公開的 |
| 資金流追蹤 | ✅ | 圖遍歷 |
| 實體歸屬 | ⚠️ 部分 | 只能靠公開標籤 + 有文件的啟發式方法 |

## 這到底改進了什麼

真正的競爭對手不是 Chainalysis,而是今天大家實際在做的兩件事。

**相對於「自己寫一支腳本」。** 多數鏈上追蹤就是幾百行 `requests` 加 `json`。這行得通,而且它失敗的方式就那幾種,每次辦案都重演一次:

| 失敗模式 | 長什麼樣 | chainscope 怎麼處理 |
|---|---|---|
| 浮點運算 | 總額錯得很細微,而且看起來很正常 | `Amount` 是精確整數;混用 symbol 或 decimals 直接拋錯 |
| 靜默截斷 | 資料源少給幾筆、照樣回 `200`,你的集合少一個地址 | 遍歷上限會出現在 `Result.warnings`;nonce 檢查可證明歷史沒被截斷 |
| API 失敗變空陣列 | 某個地址從分析裡無聲消失 | provider 是拒絕而不是回空 —— 「不支援」和「查無資料」是不同的型別 |
| 手工組區塊號 | 一個 hex 位數寫錯,四個時間戳全歪,一個很有自信的錯答案 | 型別化查詢,區塊號就是整數 |
| 來源佚失 | 半年後沒人說得出那個數字哪來的 | 每一筆回應都記錄下來、內容定址、可重播 |
| 猜測變成事實 | 「應該是同一個實體」被複述到最後被當成引用來源 | `Confidence` 與 `rationale` 是必填,不是選填 |

這些都不是難題。它們只是**你必須先踩過才會知道要防**的問題 —— 這也正是同一支腳本每次都被重寫、每次都寫壞的原因。

**相對於「託管平台」。** 這個東西是你自己跑的。資料在你的硬碟上、provider key 是你的,沒有帳號、沒有配額,也沒有任何人可以收回。而且託管平台無法把它答案底下的資料交給你,所以它的輸出本質上是**「相信我們」**;你的可以被查驗。

**真正新的地方:一個案子是一個你可以寄給別人的檔案。**

```bash
chainscope bundle theft.chainscope        # 裡面有什麼、能不能離線重播
```
```python
cache = Bundle.open("theft.chainscope").replay_cache()    # 離線,不需要 API key
```

一個 bundle 同時帶著分析結果**與產生它的每一筆原始回應**。審閱的人離線重跑,得到 byte-identical 的輸出 —— 於是爭論從「我不相信你」變成**「你那個 log 查詢漏掉了 block 20011451」**。同一套機制也讓測試維持離線,並讓一份調查在「當初回答它的那個資料源倒閉之後」仍然完整。商業平台在結構上做不到這件事:它們的資料不被允許離開平台。

## 自己架、自己跑

chainscope 是工具箱,不是服務。它期望的終局是:**你在它上面建自己的系統,然後不需要我們也能一直跑下去。**

| 你想要 | 你會拿到 |
|---|---|
| 自己的資料庫 | `Store` 協定 —— 預設 SQLite,Postgres / DuckDB / 圖資料庫都是插件。可從快取重建,所以改 schema 是「重建」而不是「重爬一次」 |
| 自己的前端 | 每個 `Result` 都有穩定 JSON、圖可匯出到 Neo4j / Gephi / Cytoscape、另有唯讀的本機 API。**不綁一個你只能將就用的 UI** |
| 自己的警報 | `Watch` + `evaluate(watch, since, until)`,對區塊範圍的純函式。用 cron、CI 或你自己的 daemon 驅動。沒有排程器、沒有訊息佇列 —— 而且因為它是純函式,**「這條警報為什麼會觸發?」可以原地重播回答** |
| 自己的分析 | 分析器、資料源、鏈、儲存後端、標註來源全是 entry point。你的擴充住在你自己的 repo |
| 自己的標籤 | `Attribution` 帶著來源、方法、信心度與理由,合併時不破壞衝突 —— 一份**可以被質疑**、而不是只能被相信的共享標記集 |

資料抓取是**沿著調查路徑長出來的**,不是先索引整條鏈 —— 這是讓它維持成「筆電工具」而不是「叢集」的關鍵。每一條的理由見 [ARCHITECTURE.md](ARCHITECTURE.md) §4.8–4.11(英文)。

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
pip install "chainscope[all]"     # + EVM、Bitcoin、Solana、Tron、美化輸出
```

## 目前進度

v0.1 建置中。已完成:

- [x] `core.units` —— `Amount` 精確整數運算,永不使用 float
- [x] `core.chainid` —— CAIP-2 鏈識別
- [x] `core.attribution` —— 來源、信心度、非破壞性合併
- [x] `transport` —— 內容定址快取、finality 推導 TTL、節流、稽核日誌
- [x] `providers` —— 以「能力」路由的資料源,含失敗轉移
- [x] `chains` —— EVM、Bitcoin、Solana、Tron 轉接器
- [x] `attribution` —— OFAC、瀏覽器標籤、本地標註、衝突解析
- [x] `analysis` —— 歸集、跨鏈配對、剝離鏈、共同輸入聚類
- [x] `cli` 與 renderers —— 終端機、Markdown、JSON
- [x] `case` —— 可離線重播的案件包
- [x] `store` —— 實體儲存 + 型別化過濾,可從 cache 完整重建([§4.8](ARCHITECTURE.md))
- [x] `providers.etherscan` —— explorer 等級的 `ADDRESS_HISTORY` 與 `ASSET_TRANSFERS`,一把 key 通吃 60+ 條 EVM 鏈。**這是讓內建分析器能端到端運行的關鍵**

**已設計、尚未實作。** 理由先寫下來是刻意的:這幾項一旦有人依賴就很難反悔。

- [ ] 錄製的 cassette —— provider 抽象尚未見過真實 API 回應,介面形狀未經驗證
- [ ] 標註來源的 fetcher —— 目前只讀你自己準備的本地檔案
- [ ] `watch` —— 對區塊範圍求值的 `evaluate()`([§4.10](ARCHITECTURE.md))
- [ ] 外掛協定版本化與穩定度分級([§4.11](ARCHITECTURE.md))
- [ ] `analyze --bundle` —— 一行指令重播;目前只能用 `Bundle.replay_cache()`
- [ ] 圖匯出、本機唯讀 API

## 擴充

新增一條鏈、provider、**store 後端**、分析器或標註來源,**應該只需要一個檔案加一份測試 cassette,而且不需要 fork 這個 repo**。這是這套架構被檢驗的標準。全部透過 entry points 註冊,不需要改動核心:

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
