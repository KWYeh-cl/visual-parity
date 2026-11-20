---
name: visual-parity
description: 比對兩個已渲染網頁的樣式落差（新舊版對照 / 改版驗收）、把畫面跟 Figma 設計稿做截圖比對，跑互動 E2E 流程（含 hover／動畫狀態的樣式斷言與 axe 無障礙檢查）並蒐集 console 與 network 錯誤，以及在 375/768/1440 三種寬度檢查 RWD 與水平溢出。當使用者提到「新舊頁面長得不一樣」「跟測試環境比對」「跟 Figma 比對」「設計稿驗收」「像素比對」「樣式對不上」「visual diff」「RWD 檢查」「無障礙檢查」「開 dev server 驗收」「E2E 驗收」時使用。與 webapp-testing 互補：webapp-testing 負責 server 生命週期與臨時 Playwright 腳本，本 skill 負責可重複的比對與驗收。
---

# Visual Parity

一次跑完、產出完整落差清單。**禁止**「改一項 → 截圖 → 再改一項」的來回迴圈。

## 為什麼存在

改一項就截圖一次，會把每張截圖與每份 computed style 永久留在 context 裡。同一份工作在 600k context 下做，成本是 100k 時的六倍。本 skill 把比對結果寫進檔案，只回傳一張壓縮過的落差表。

## 鐵則

1. **不要 Read `scripts/parity.py`。** 它是黑箱，用 `--help` 就夠。
2. **不要把報告全文讀進 context。** stdout 的摘要已含「最常出現的落差屬性」；需要細節時用 `grep` 撈特定屬性或 selector，不要 `Read` 整份。
3. **不要逐項修。** 先看 stdout 的 top differing properties——落差通常是少數幾條系統性規則（一條 `margin-bottom`、一組 token）造成的，不是幾十個獨立問題。
4. **改完重跑一次全量比對**，而不是只驗剛改的那一項。

## 前置：dev server

server 生命週期交給既有的 webapp-testing skill，不要自己寫：

```bash
python ~/.claude/skills/webapp-testing/scripts/with_server.py \
  --server "npm run dev -- --experimental-https" --port 3000 \
  -- python ~/.claude/skills/visual-parity/scripts/parity.py diff --ref <URL> --local <URL> --out report.md
```

server 已經在跑就直接呼叫 `parity.py`。自簽憑證已預設接受（`ignore_https_errors`）。

## diff — 新舊頁面樣式落差

```bash
python ~/.claude/skills/visual-parity/scripts/parity.py diff \
  --ref   https://staging.example.com/faq/content?id=28473 \
  --local https://localhost:3000/faq/content?id=28473 \
  --out   parity-diff.md
```

元素配對：先套 `--map` 的手動對照，再以「標籤+文字」自動配對，接著以「純文字」補配（吸收 SPA→SSR 的標籤變化，例如 `<p>` 變 `<div>`），最後用「視覺識別」配對沒有文字的元素：

| 元素 | 配對依據 |
|---|---|
| `img`／`video`／`iframe` | 檔名。會去掉網域、query、內容 hash（`logo.3f2a9c1d.png`、Strapi 的 `banner_95de82a84f.webp`），並解開 `next/image` 的 `?url=` |
| `svg` | `<use href>` → `aria-label` → `<title>` → 第一個 `path` 的 `d` |
| `input`／`textarea`／`select` | `type`、`name`、`placeholder`、`aria-label` |
| 其他 | `aria-label` → `title` → `background-image` 檔名 |

stdout 會回報五個數字：

- `matched` — 成功配對並比對的元素數
- `diffs` — 屬性落差總數
- `missing-locally` — 舊頁有、新頁完全沒有的內容，包含圖片與 icon（**抓漏掉的區塊用這個**）
- `skipped-ambiguous` — 文字或圖檔在頁面上重複、無法 1:1 配對，**未被比對**。數字大不代表有問題，但代表覆蓋率沒滿；關鍵區塊落在這裡就用 `--map` 補。
- `skipped-textless` — 沒有文字、也沒有任何識別屬性的末端元素（CSS 畫的 icon、分隔線、裝飾色塊），**未被比對**。報告會列出它們的 selector；重要的用 `--map` 補，或改用 `snapshot` 涵蓋。

其他參數：`--width` / `--widths`、`--wait-selector`（等內容渲染完）、`--extra-wait`（動畫或延遲載入）。
Exit code 1 表示有屬性落差或有 `missing-locally`，0 表示完全一致。

### --map：手動指定對照

DOM 結構差太多時（舊 iframe vs 新 SSR），用 JSON 補：

```json
{
  "#faqAnswer": ".faq-body",
  ".uform_search": "[data-testid=search-hero]",
  "#footer .crumb": "nav[aria-label=breadcrumb]"
}
```

左邊是 ref 的 selector、右邊是 local 的。支援尾綴比對，不必寫完整路徑。

## responsive — 三種寬度 + 水平溢出

```bash
python ~/.claude/skills/visual-parity/scripts/parity.py responsive \
  --ref <URL> --local <URL> --out parity-responsive.md
```

等同在 375 / 768 / 1440 各跑一次 diff，另外檢查 local 是否出現水平溢出（`scrollWidth > clientWidth`），這是 RWD 破版最常見的徵兆。

## flow — 互動驗收 + console/network 錯誤

用 JSON 描述步驟，不要每次現寫 Playwright：

```json
[
  {"action": "goto", "url": "https://localhost:3000/products"},
  {"action": "click", "selector": ".ba-free-download", "desc": "點 Free Download"},
  {"action": "wait", "selector": "[role=dialog]"},
  {"action": "expect_focused", "selector": "[role=dialog] input[type=email]", "desc": "focus 應落在 input 而非 close button"},
  {"action": "fill", "selector": "input[type=email]", "value": "a@b.com"},
  {"action": "click", "selector": "button[type=submit]"},
  {"action": "expect_text", "selector": "[role=dialog]", "value": "Check your email"}
]
```

```bash
python ~/.claude/skills/visual-parity/scripts/parity.py flow --steps steps.json --out parity-flow.md
```

可用 action：

| action | 用途 |
|---|---|
| `goto` `click` `fill` `press` `hover` | 操作 |
| `wait` | 等 `selector`，或 `{"ms":800}` 單純停一段時間 |
| `wait_animations` | 等 `selector`（省略則整頁）底下所有有限次的 CSS／Web 動畫跑完 |
| `emulate` | 切換 `reduced_motion`（`reduce`／`no-preference`）、`color_scheme`、`media` |
| `expect_text` `expect_visible` `expect_focused` | 內容與 focus 斷言 |
| `expect_style` | computed style 斷言，見下方 |
| `save_box` `expect_box` | 記下元素位置大小，之後斷言沒有位移，或直接給 `x`／`y`／`width`／`height` |
| `axe` | 無障礙檢查，見下方 |
| `screenshot` | 存檔 |

### expect_style：狀態與動畫的定量斷言

```json
[
  {"action": "hover", "selector": ".cta"},
  {"action": "wait_animations", "selector": ".cta"},
  {"action": "expect_style", "selector": ".cta", "styles": {"background-color": "#1565c0", "transition-duration": "200ms"}},
  {"action": "expect_style", "selector": ".cta", "pseudo": "::after", "styles": {"opacity": "1"}},
  {"action": "save_box", "selector": ".card", "as": "before"},
  {"action": "click", "selector": ".card .expand"},
  {"action": "wait_animations"},
  {"action": "expect_box", "selector": ".card", "same_as": "before", "tolerance": 2},
  {"action": "emulate", "reduced_motion": "reduce"},
  {"action": "expect_style", "selector": ".cta", "styles": {"transition-duration": "0s"}}
]
```

- 期望值照設計稿的寫法就好（`#fff`、`300ms`、`1rem`），會先換算成瀏覽器的 computed 格式再比。
- 單一數值（px、s、ms）容許 `tolerance`（預設 0.5，時間以 ms 計）；`expect_box` 預設容許 2px。
- 斷言「動畫結束後的狀態」一律先 `wait_animations`，不要用 `wait ms` 猜時間。

### axe：無障礙檢查

```json
{"action": "axe", "selector": "main", "exclude": [".third-party-widget"], "fail_on": "serious"}
```

- 預設規則集 `wcag2a`、`wcag2aa`、`wcag21a`、`wcag21aa`，可用 `tags` 覆寫。
- `fail_on`（`minor`／`moderate`／`serious`／`critical`，預設 `serious`）以上的違規會讓這一步失敗；所有違規都會寫進報告。
- 在每個要驗的狀態各跑一次（例如 dialog 打開後），axe 只看得到當下的 DOM。
- axe-core 4.14.0 已 vendor 在 `scripts/vendor/`，不需要網路。
預設第一個失敗就停（`--keep-going` 可跑完全部）。console error/warning 與失敗的 request 一律蒐集，這類問題不會反映在樣式比對裡。
`--headed` 可開有頭瀏覽器讓人工旁觀。

### --video：錄整段互動

截圖看不出 transition、loading、hover 這類會動的東西，用錄影：

```bash
python ~/.claude/skills/visual-parity/scripts/parity.py flow \
  --steps steps.json --out parity-flow.md --video parity-flow.mp4
```

- 副檔名 `.mp4` 會在錄完後用 ffmpeg 轉成 H.264（QuickTime 直接開），中間的 webm 自動刪除。**ffmpeg 不存在時不會失敗**，保留 `.webm` 並在 stdout 提示。副檔名寫 `.webm` 則完全不轉檔。
- `--video-size WxH`（預設 `1280x720`）同時決定錄影解析度與 viewport。
- `--video-tail <秒>`（預設 `1`）最後一步跑完後繼續錄的時間，避免收尾的動畫被切掉。
- **游標預設會畫**（`--no-cursor` 關掉）。Playwright 的 click 是直接派發事件、沒有指標移動，錄出來看不出點了哪裡；開了之後每個 `click`／`fill` 前會先讓游標走到目標中心（約 0.4 秒），按下時有藍色擴散環。要讓 reviewer 看懂互動的是哪個元件就加這個。
- 影格率是 Playwright screencast 的 **25fps**，判斷「動畫有沒有做對」夠用；要判斷「掉不掉幀」不夠，那種要錄 60fps：macOS 用 `screencapture -v`；Windows 沒有內建等效指令，用 OBS Studio（設定 60 FPS）或 Xbox Game Bar（`Win+Alt+R`，需在設定把影格率調成 60），兩者都沒有就標為「未驗：本機無法錄 60fps」。
- 影片路徑會同時寫進報告開頭與 stdout。

### --mutate：確認斷言真的會失敗

新寫的 `expect_*` 沒看過它失敗就不算數——它可能在壞掉的頁面上也會過（例如對不存在的 `::after` 斷言 `content: none`）。加 `--mutate`，每個通過的斷言會在一個刻意弄壞的頁面上重跑一次：

| action | 怎麼弄壞 |
|---|---|
| `expect_style` `expect_box` | 移除所有 CSS |
| `expect_text` | 清空元素文字 |
| `expect_visible` | 隱藏元素 |
| `expect_focused` | 把 focus 移走 |

弄壞後仍然通過的斷言標為 `INVALID`，寫進報告的 `mutation check` 段落並讓整次執行 exit 1。重跑會從頭執行到該斷言前一步，表單送出之類有副作用的步驟也會再跑一次。交付驗收證據前，新寫的 flow 至少用 `--mutate` 跑一次。

把 flow 的 JSON 存進專案（例如 `e2e/`），下次改版直接重跑，就是回歸測試。

## snapshot — 跟 Figma 設計稿截圖比對

Figma 的數值（`get_design_context` 給的字級、間距）跟瀏覽器實際 render 出來的結果常有落差：字體 metrics、行高計算、auto layout 換成 flex 後的取整。所以設計稿驗收看的是**畫面**，不是數值。

1. 用 Figma MCP 的 `get_screenshot` 取得該 frame 的 PNG（它回傳短期 URL，照它給的 curl 指令下載）。記下回應裡的 `original_width`，這是 frame 的 CSS 寬度。`maxDimension` 設成 `original_width × 2` 以上，比對精度才夠。
2. 截本機同一區塊來比：

```bash
python ~/.claude/skills/visual-parity/scripts/parity.py snapshot \
  --ref-image figma-hero.png \
  --url https://localhost:3000/products/photodirector \
  --selector "section.hero" --width 1440 \
  --out parity-snapshot.md --diff-image a2-hero-vs-figma-1440.png
```

- `--selector` 對應 Figma frame 的那個區塊；省略則截整頁。`--width` 給 `original_width`。
- 本機截圖會縮放到設計稿的寬度再比。高度不同時只比重疊的部分，並把高度差列出來（超過 `--max-height-diff`，預設 8px，就算失敗）。
- **預設容忍反鋸齒**：先輕微模糊、再去掉孤立的像素，字體與曲線的 rasterize 差異不會被算進去，位置、尺寸、顏色、缺漏的變化會留下來。要逐像素比對就加 `--strict`。
- 預設凍結動畫、transition 與游標閃爍，並等 webfont 載入完成（`--no-freeze` 關掉凍結）。webfont 沒載入、退回系統字型，會直接顯示成大片落差。
- stdout 回報落差比例，以及前 5 個落差區域的 CSS 座標與該位置的本機元素；完整清單在報告裡。超過 `--max-mismatch`（預設 1%）就 exit 1。
- diff 圖是三格並排：設計稿｜本機｜落差（紅色是落差像素，藍框是編號區域）。**這張圖要人看**：agent 先讀報告的區域表，只在需要判斷「這是設計稿用了假資料，還是真的做錯」時才打開圖。

設計稿常用假文案或假圖，這類區域一定會有落差。判斷方式：落差區域對到的元素如果是 CMS 內容，記為「內容不同」而不是「做錯」，並在回報裡分開列。

## 驗收交付物規範

被 `/pm-task` 之類的流程當成驗收證據時，除了跑得動，還要符合這些：

- **檔名** `<編號>-<驗收目標英文 slug>[-<寬度>].<png|mp4>`，一律小寫 kebab-case（中文與空白在 CLI／git／PR 連結會出事）。檔名要說「這在驗什麼」不是「這是什麼畫面」：`a3-faq-empty-state-no-results-375.png` ✅；`faq-page-375.png`、`after.png` ❌。
- **截圖逐張讀過才算檢查**，存檔不算。每張查五件事：元素重疊、內容溢出容器、非預期空白、文字截斷、水平溢出。
- **動畫與轉場不靠肉眼判**，要有定量斷言，用 flow 的 `wait_animations` ＋ `expect_style`／`expect_box`／`emulate` 寫：動畫結束後的最終 computed style 與可見性、`transition-duration`／`animation-name` 確實掛上、`prefers-reduced-motion: reduce` 下確實關閉、轉場前後 bounding box 無非預期位移。
  > 判斷測試：這條斷言重跑一百次結果一樣嗎？不一樣 → 它驗的是手感，不能拿來決定通過或失敗。
- **錄影**一段只涵蓋一條互動路徑、15 秒內，用 `flow --video`（見上節）。**不要用 `screencapture` 錄互動**——Playwright 派發的是合成事件，macOS 真實指標不會動，錄到的游標停在原地；`screencapture -v` 只在需要判斷掉幀時才用。agent 讀不了影片，唯一觀眾是人。

## 報告開頭的來源欄位

每份 `.md` 報告開頭與 stdout 都會寫：`commit`（`git rev-parse HEAD`，不在 git repo 裡是 `(not a git repo)`）、`working tree`（clean 或 dirty 與檔案數）、`url`、`run at`。PR 的 `**Verified at:**` 填這裡的 commit；`working tree` 是 dirty 時，證據對應的不是任何一個 commit，要在 `Not verified` 註明。

## 驗收回報格式

跑完 diff／responsive／flow／snapshot 後，回報**以驗收條件（AC）為主軸，不以工具為主軸**。讀者要知道的是「哪些條件過了、哪些沒過、哪些沒驗到」，不是「flow 跑了幾步」。各子指令的 `.md` 報告是證據，不要整份貼進回報。

```markdown
## 驗收結果：<功能或頁面名稱>

**結論**：<一句話。例如「3 項通過、1 項失敗、1 項內容不同待確認」>

| AC | 結果 | 證據 | 備註 |
|---|---|---|---|
| A1 hero 區塊符合設計稿 | ⚠️ 內容不同 | `snapshot` 2.1%，`a1-hero-vs-figma-1440.png` | 落差都在 CMS 標題，設計稿是假文案 |
| A2 hover 時 CTA 變色，reduced motion 下不做動畫 | ✅ | `flow` 步驟 3–6（`expect_style`） | |
| A3 dialog 打開後無 serious 以上無障礙違規 | ❌ | `flow` axe：`color-contrast` ×2 | `.dialog .hint` 對比 3.1:1 |

### 覆蓋率缺口
- diff 1440px：`skipped-ambiguous` 159、`skipped-textless` 7。<其中有沒有關鍵區塊；有的話補了 `--map` 沒有>

### 沒驗到的
- <例如：Figma 只有桌機版，375／768 只做了 diff 沒有設計稿比對>

### 附檔
- `a1-hero-vs-figma-1440.png`、`a2-cta-hover-reduced-motion.mp4`、`parity-flow.md`
```

規則：

- **結果只有四種**，不要自創：
  - ✅ 通過：有一個重跑會得到相同結果的證據。
  - ❌ 失敗：附上失敗的值，例如「expected `#1565c0`, got `rgb(21, 101, 191)`」，不要只寫「顏色不對」。
  - ⚠️ 內容不同：落差來自資料而不是實作（設計稿假文案、假圖，stage 與 local 的 CMS 資料不同）。必須寫出落差落在哪個元素、為什麼判斷是資料。
  - ⏭️ 未驗：寫原因，並同時列進「沒驗到的」。
  > 判斷測試：把這筆落差的資料換成一樣的，落差會消失嗎？會 → ⚠️ 內容不同；不會 → ❌ 失敗。判斷不了 → ❌，並在備註寫出疑點。
- **證據要能重跑**：寫出子指令、步驟編號或檔名。「看起來沒問題」「截圖確認過」不算證據，除非附上那張截圖，並寫出檢查了哪五件事（見上節）。
- **覆蓋率缺口不可省略**：`skipped-ambiguous` 或 `skipped-textless` 不是 0 時要列出來。數字是 0 也寫一行「無」，讓讀者知道有看過。
- **console 錯誤與失敗的 request** 有出現就列在對應 AC 的備註；不屬於任何 AC 的，另列在「沒驗到的」之後，標明「與本次改動無關」或「待確認」。
- AC 從 spec、issue 或 PLAN.md 抄過來，**編號沿用來源**。沒有正式 AC 時，用「頁面區塊＋狀態」自己編，並在結論註明是自編的。

## 維護

改過 `parity.py` 後跑 `python scripts/parity.py selftest`，會用內建 fixture 驗證配對、diff、flow、`--mutate`、snapshot 與報告來源欄位仍正確，並檢查每個子指令與 flow action 都有寫進 README.md 與 SKILL.md（新增 action 卻沒寫文件就會失敗）。
