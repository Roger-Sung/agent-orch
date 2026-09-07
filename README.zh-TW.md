# agent-orch（繁體中文摘要）

> 這是英文 [README](README.md) 的摘要版，只放定位、來由、證據與邊界。完整說明、架構圖、生命週期與操作手冊請看英文版與 [`docs/`](docs/)。

[![CI](https://github.com/Roger-Sung/agent-orch/actions/workflows/ci.yml/badge.svg)](https://github.com/Roger-Sung/agent-orch/actions/workflows/ci.yml)

## 這是什麼

一個有狀態的 AI agent 任務編排／派工服務，用來執行 Claude Code 與 Codex CLI 長時間運行的多 agent 工作流。它可在明確的人機協作停點之間無人看管地執行，到了停點才交由人裁決（human-in-the-loop）。核心機制包括 SQLite 狀態機、單一寫入者的常駐 daemon、收斂與停止策略、跨供應商的 review gate，以及每個已提交階段執行的封存證據鏈。

它為 Claude／Codex 長流程的持久、可續跑執行而建，用在事後必須能稽核每次重試與副作用的場合。公開目的是給人讀，不是給人用——見英文版的 Project status。引擎沒有第三方 Python 依賴，demo 不需要任何設定。

## 給作品集讀者

這份作品展示的是**如何控制可能犯錯的 agent 流程**，不是訓練模型，也不是宣稱兩個模型一定比一個好。重點在三個工程問題：哪份輸出才有權威、誰能推進狀態、什麼證據足以支持安全重試。

先跑下方離線 demo，再選一條閱讀路線：

- 失敗後是否會重做副作用：[controller](orchestrator/controller.py) 與 [daemon](orchestrator/daemon.py)。
- 舊 PASS 或顯示文字能否冒充有效結果：[輸出邊界決策](docs/decisions/provider-output-boundary.md) 與 [測試](orchestrator/tests/test_provider_output.py)。
- 同一 reviewer 如何記住脈絡，卻不能繼承修改權限：[流程契約](docs/astra-fable-opt-in.md)、[session registry](orchestrator/review_session.py) 與 [流程測試](orchestrator/tests/test_review_flow.py)。
- 安全邊界究竟防得住什麼：[threat model](docs/threat-model.md) 與 [L1/L2 驗收測試](orchestrator/tests/test_containment_layers.py)。

公開的是程式與可重現的測試，不包括私人部署、帳號憑證、對話逐字稿或 runtime state。

## 為什麼是服務，不是一個迴圈

一個反覆呼叫 agent 的 shell 迴圈，在中途失敗之前都很好用。失敗之後問題就來了，而迴圈答不出任何一題：當時跑到哪個階段、已經燒了幾次嘗試、產出有沒有被審過、現在能不能安全地續跑還是會重做一個副作用已經落地的步驟。

這些答案必須住在只有一個寫入者的持久狀態裡。四個性質由此而來：

- **型別化的結果。** 階段只能印出一行 `ORCHESTRATOR_OUTCOME`，狀態機從不猜 agent 的意思。
- **有停止策略，不無限重試。** 舊 profile 保留 attempt／edge／transition 上限；支援收斂判斷的 envelope 流程用進展、停滯與振盪證據決定停點。新的單次 review 模式不新增硬輪數，也不自動派 repair；非 ready 就交協調者。timeout 等安全限制仍保留。
- **回收而非遺棄。** daemon 中途死掉，重啟時會把仍標記執行中的 run 停為 blocked 並附理由，隔離無法對帳的部分。
- **證據封存。** 每次已提交的執行都封存 manifest，內含 log hash、輸出 hash、結果、模型、token 用量與 lease token。

## 它怎麼來的

起點是不想守在 agent 旁邊接力：啟動、走開、回來驗收一個可以查證的結果。這裡每一個機制，都是這個承諾在某個具體情境下破掉時加上去的：

- 執行無法安全續跑，於是有了單一寫入者的持久狀態。
- 同一家模型的審查只是在確認執行者的假設，而不是在檢驗它，於是有了跨供應商的 review gate。
- 一個階段無視自己的工作區，改寫了機器上另一處的正式資料還回報成功，是人讀結果時才發現的，於是有了 L1 預防與 L2 偵測。

這個系統的形狀是出過什麼事的紀錄，不是預先畫好的設計。

後來的真實整合測試也抓到：Codex 內層沙箱與既有 macOS 外層沙箱衝突，連合法工具都無法執行。修正不是靜默放寬，而是先取得明確決策，再讓 opt-in executor 強制使用 orch 外層 L1、不重複建立內層沙箱。這段取捨也保留在[流程文件](docs/astra-fable-opt-in.md)。

## 原協調者＋同一位 reviewer

新的 opt-in 流程讓原對話串負責 explore／spec、RD 的模型與 effort、進度與仲裁；外部草案直接交單一 Fable 審查，不另跑提案作者。後續由 Codex 實作，沿用同系列 Fable session 審實作。技術審查通過不等於使用者授權。

文中的 Astra 指原協調對話串，在參考部署裡是 Codex session。

- 每階段模型／effort 解析後封存並綁定 digest，不靠改全域環境變數派工。
- Reviewer 沒有工具，只讀傳入的 spec／candidate／evidence；PASS 綁定精確 hash，candidate 變動就失效。
- 呼叫前留下 pending；只有資料庫已提交的封存結果能清除。未知中斷不盲目重播；重建 context 保留 predecessor 與決策，不假裝仍是原 session。

非 ready 先停給原協調者；反駁與規格仲裁仍由人／原對話串協調，不是全自動法庭。跨家族是減少共同盲點的設計選擇，不是模型品質 benchmark；session 延續也不保證 cache 命中或省費。

## 證據

- 引擎測試由 CI 在 Linux 與 macOS 上執行，涵蓋狀態機與各種上限、lease 回收、intake 風險分類與 interpretation envelope、propose 階段的收斂、runner 生命週期一致性，以及圍堵層的驗收測試（L1 寫入阻擋、L2 逃逸偵測）。L1 測試需要 macOS `sandbox-exec`，其他主機會跳過。去識別化掃描器有自己的 fixture 測試。
- 每個已提交的階段執行都留下封存的 manifest（daemon 中途死掉的執行只會標記 blocked 並補 log，不封存）；`python3 -m orchestrator containment-inspect TASK_ID` 以唯讀連線重新驗證保留的證據。
- CI 只跑部分去識別化掃描——嚴格規則需要的站點字串刻意不進 repo。綠色 badge 代表測試通過且 repo 端規則沒有發現問題；嚴格掃描是發佈者的責任，見 [`docs/operating.md`](docs/operating.md)。

## 有做的與沒做的

**有做**：狀態機、單一寫入者 daemon、型別化結果、各種上限、lease 回收、封存 manifest、跨供應商 review gate、git 出口封鎖、L1 寫入預防（macOS）、L2 寫入偵測、假 agent demo、去識別化掃描器與 fail-closed 的 pre-commit hook。

另有 opt-in 的外部草案 review、每階段 execution config、同系列 reviewer session 延續／恢復，以及綁定 candidate 的三軸 review。這是程式能力，不代表所有部署都已升級。

**沒做，且在程式碼與文件裡明說，不留給人自己踩到：**

- L3 隔離。階段仍讀得到使用者讀得到的任何東西，也攔不住它往外送。
- 通用的跨供應商身分證明。舊流程信任 owner 槽位；opt-in 會檢查原生命令形狀與 reviewer 回報身分，但不能認證任意 wrapper。
- 全自動仲裁、跨主機 session 共用，或保證收斂／cache／省費。
- 通用的 CLI adapter，讓其他 agent CLI 也能當 owner。
- 探測供應商 CLI 支援哪些能力。
- Windows 支援。

圍堵層的邊界與未解問題寫在 [`docs/threat-model.md`](docs/threat-model.md)。

L1 限制的是寫入 allowlist，不是只准寫工作區：CLI state 與 temp 也在其中；讀取和網路不限。舊流程可用 `--allow-unsandboxed` 或 `ORCH_ALLOW_UNSANDBOXED` 明確接受無 L1 執行；opt-in Codex 則拒絕這兩種繞過方式，必須有外層 L1。Reviewer 維持無工具模式。

## 三十秒 demo

```sh
python3 -m orchestrator.demo                        # 合成的端對端執行
python3 -m unittest discover -s orchestrator/tests  # 引擎測試
python3 -m unittest discover -s tools/tests         # 掃描器測試
```

demo 用一個故意跟自己意見不合的假 agent 跑一個合成任務，讓 review 邊的上限被觸發：任務停在 `waiting_user`，每次執行都封存，歷史完整保留。不呼叫任何供應商 CLI，沒有東西離開這台機器。

## 授權與定位

保留所有權利，僅供閱讀與作品集評估；詳見 [LICENSE](LICENSE) 與英文版 Project status。不是開源，不接受 issue 與 pull request，不承諾維護。
