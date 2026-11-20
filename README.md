# visual-parity

給 [Claude Code](https://claude.com/claude-code) 用的 skill：比對兩個已渲染網頁的樣式落差、跑互動 E2E 驗收、檢查 RWD 破版。

改版驗收時最常見的兩個問題是「新頁面跟舊的長得不一樣，但講不出哪裡不一樣」和「agent 改一項就截圖一次，把 context 燒光」。這個 skill 一次跑完全部比對，把細節寫進報告檔案，只回傳一張壓縮過的落差表。

---

## 安裝

### 1. Clone 到 skills 目錄

個人全域可用：

```bash
git clone https://github.com/KWYeh-cl/visual-parity.git ~/.claude/skills/visual-parity
```

只想在單一專案生效的話，改 clone 到專案的 `.claude/skills/visual-parity`。

### 2. 裝相依

```bash
pip install playwright pillow && playwright install chromium
```

`pillow` 只有 `snapshot` 會用到。axe-core 已放在 `scripts/vendor/`，不用另外裝。

Windows 上 `python` 若指向 Microsoft Store 的空殼，改用 `py`（例如 `py -m pip install playwright pillow`、`py scripts\parity.py selftest`）。

錄影要輸出 MP4 才需要 ffmpeg，選配：

```bash
brew install ffmpeg            # macOS
winget install Gyan.FFmpeg     # Windows（或 choco install ffmpeg）
```

Windows 裝完要重開終端機讓 `ffmpeg` 進 PATH。

沒裝也不會壞，錄影會自動退回 `.webm` 並在 stdout 提示。

### 3. 驗證

```bash
python ~/.claude/skills/visual-parity/scripts/parity.py selftest
```

印出 `selftest OK` 就成功了。

---

## 怎麼觸發

裝好之後在 Claude Code 裡用白話講就會自動載入，不用記指令。這些說法都會觸發：

> 「新舊頁面長得不一樣」「跟測試環境比對」「像素比對」「樣式對不上」「visual diff」「RWD 檢查」「開 dev server 驗收」「E2E 驗收」

也可以直接跑 CLI，下面三個子指令都是。

---

## 子指令

| 子指令 | 用途 | Exit code |
|---|---|---|
| `diff` | 比對兩個 URL 的 computed style 落差，含沒有文字的圖片與 icon | 有落差或缺漏 `1`，完全一致 `0` |
| `responsive` | 在 375 / 768 / 1440 各跑一次 diff，另查水平溢出 | 同上 |
| `flow` | 依 JSON 腳本跑互動流程（含樣式斷言、axe），蒐集 console / network 錯誤，可錄影 | 有步驟失敗 `1` |
| `snapshot` | 把頁面或區塊截圖，跟設計稿 PNG（Figma 匯出）做像素比對 | 落差超過門檻 `1` |

完整用法見 [SKILL.md](SKILL.md)。

### diff — 新舊頁面樣式落差

```bash
python ~/.claude/skills/visual-parity/scripts/parity.py diff \
  --ref   https://staging.example.com/faq/content?id=28473 \
  --local https://localhost:3000/faq/content?id=28473 \
  --out   parity-diff.md
```

stdout 會回五個數字：

- `matched` — 成功配對並比對的元素數
- `diffs` — 屬性落差總數
- `missing-locally` — 舊頁有、新頁完全沒有的內容（含圖片與 icon），**抓漏掉的區塊看這個**
- `skipped-ambiguous` — 文字或圖檔在頁面上重複、無法 1:1 配對而略過。數字大不代表有問題，但代表覆蓋率沒滿
- `skipped-textless` — 沒有文字也沒有識別屬性的末端元素（CSS icon、分隔線），未被比對

元素配對順序：先套 `--map` 的手動對照 → 「標籤 + 文字」自動配對 → 「純文字」補配。最後這步是為了吸收 SPA→SSR 造成的標籤變化（例如 `<p>` 變成 `<div>`）。

DOM 結構差太多時（舊 iframe vs 新 SSR），用 `--map` 指定 JSON 對照表補：

```json
{
  "#faqAnswer": ".faq-body",
  ".uform_search": "[data-testid=search-hero]"
}
```

左邊是 ref 的 selector、右邊是 local 的，支援尾綴比對，不必寫完整路徑。

其他參數：`--width` / `--widths` / `--height` / `--wait-selector` / `--extra-wait`。

### responsive — RWD 破版

```bash
python ~/.claude/skills/visual-parity/scripts/parity.py responsive \
  --ref <URL> --local <URL> --out parity-responsive.md
```

等同在三種寬度各跑一次 diff，另外檢查 local 是否出現 `scrollWidth > clientWidth`——這是 RWD 破版最常見的徵兆。

### flow — 互動驗收

用 JSON 描述步驟，不用每次現寫 Playwright：

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
python ~/.claude/skills/visual-parity/scripts/parity.py flow \
  --steps steps.json --out parity-flow.md
```

可用 action：

| action | 用途 |
|---|---|
| `goto` `click` `fill` `press` `hover` | 操作 |
| `wait` `wait_animations` | 等 selector／毫秒數；等動畫跑完 |
| `emulate` | 切換 reduced motion、color scheme、media |
| `expect_text` `expect_visible` `expect_focused` | 內容與 focus 斷言 |
| `expect_style` | computed style 斷言（含 pseudo、transition、動畫結束值） |
| `save_box` `expect_box` | 記下位置大小，之後斷言沒有位移 |
| `axe` | 無障礙檢查 |
| `screenshot` | 存檔 |

每個 action 的參數見 [SKILL.md](SKILL.md)。

`wait` 吃 `selector`，或改吃 `{"action": "wait", "ms": 800}` 單純停一段時間——動畫沒有對應 selector 可等時用這個。

預設第一個失敗就停，`--keep-going` 可跑完全部。console error / warning 與失敗的 request 一律蒐集，這類問題不會反映在樣式比對裡。`--headed` 可開有頭瀏覽器讓人工旁觀。

**把 flow 的 JSON 存進專案（例如 `e2e/`），下次改版直接重跑，就是回歸測試。**

### flow --video — 錄下整段互動

截圖看不出 transition、loading、hover 這類會動的東西：

```bash
python ~/.claude/skills/visual-parity/scripts/parity.py flow \
  --steps steps.json --out parity-flow.md --video parity-flow.mp4
```

| 參數 | 預設 | 說明 |
|---|---|---|
| `--video PATH` | 關閉 | 副檔名 `.mp4` 會用 ffmpeg 轉 H.264 並刪除中間的 webm；寫 `.webm` 則不轉檔 |
| `--video-size WxH` | `1280x720` | 同時決定錄影解析度與 viewport |
| `--video-tail 秒` | `1` | 最後一步跑完後繼續錄的時間，避免收尾動畫被切掉 |
| `--no-cursor` | 游標預設開啟 | 關掉合成游標。開啟時 `click`／`fill` 前先讓指標走到目標中心，按下時有藍色擴散環 |

影格率是 Playwright screencast 的 **25fps**。判斷「動畫有沒有做對」夠用；要判斷「掉不掉幀」不夠，要另外錄 60fps：macOS 用內建的 `screencapture -v`；Windows 沒有內建等效指令，用 OBS Studio 或 Xbox Game Bar（`Win+Alt+R`，影格率要在設定調成 60），都沒有就把它列為未驗。

### flow --mutate — 確認斷言真的會失敗

```bash
python ~/.claude/skills/visual-parity/scripts/parity.py flow --steps steps.json --mutate
```

每個通過的 `expect_*` 會在刻意弄壞的頁面（移除 CSS、清空文字、隱藏元素、移走 focus）上重跑；還是通過的標為 `INVALID` 並讓整次 exit 1。

影片路徑會同時寫進報告開頭與 stdout。

---

## 專案驗證入口：scaffold_verify.py

在 app repo 產生 `.claude/skills/verify-<app>/`：啟動／檢查／清理 dev server 的腳本、可搜尋的功能地圖（`features/`），以及每個功能一份 flow（`flows/<id>.json`，底層跑 `parity.py flow`，報告自帶 commit）。

```bash
python ~/.claude/skills/visual-parity/scripts/scaffold_verify.py --repo <app repo> \
  [--host auth.example.test --entry /auth/login --port 8443 --start "npm run dev -- --port $PORT"]
```

沒給的值會從 `package.json`、`vite.config.*`、`next.config.*` 偵測，猜的會標 `GUESSED`。產生後：

- `scripts/launch.sh` → `scripts/doctor.sh` → `scripts/verify.sh <feature-id> [--mutate]` → `scripts/cleanup.sh`
- `scripts/check-map.sh [--live]` 檢查地圖格式、索引、每個功能都有 flow，`--live` 再確認地圖裡每個路由都回 200。新增或改名頁面時跟著更新地圖，地圖才不會過時。

## 報告來源欄位

每份報告開頭與 stdout 都記錄 `commit`、`working tree`（clean／dirty）、比對的 `url` 與執行時間；不在 git repo 內執行時寫 `(not a git repo)`。PR 的 Evidence 用這個 commit 證明「驗的就是要交付的版本」。

## 給 agent 的使用規範

SKILL.md 裡有四條鐵則，直接影響這個 skill 划不划算，摘要在這裡讓人也知道：

1. **不要 Read `scripts/parity.py`。** 它是黑箱，`--help` 就夠。
2. **不要把報告全文讀進 context。** stdout 摘要已含「最常出現的落差屬性」，要細節用 `grep` 撈特定屬性或 selector。
3. **不要逐項修。** 落差通常是少數幾條系統性規則（一條 `margin-bottom`、一組 token）造成的，不是幾十個獨立問題。
4. **改完重跑一次全量比對**，而不是只驗剛改的那一項。

第 2 條是這個 skill 存在的主因：改一項就截圖一次，會把每張截圖與每份 computed style 永久留在 context 裡，同一份工作在 600k context 下做，成本是 100k 時的六倍。

---

## dev server

server 生命週期交給既有的 `webapp-testing` skill，不要自己寫：

```bash
python ~/.claude/skills/webapp-testing/scripts/with_server.py \
  --server "npm run dev -- --experimental-https" --port 3000 \
  -- python ~/.claude/skills/visual-parity/scripts/parity.py diff --ref <URL> --local <URL> --out report.md
```

server 已經在跑就直接呼叫 `parity.py`。自簽憑證預設接受（`ignore_https_errors`）。

---

## 更新與維護

```bash
cd ~/.claude/skills/visual-parity && git pull
```

改過 `scripts/parity.py` 後一定要跑 selftest，它會用內建 fixture 驗證配對與 diff 邏輯仍正確：

```bash
python scripts/parity.py selftest
```
