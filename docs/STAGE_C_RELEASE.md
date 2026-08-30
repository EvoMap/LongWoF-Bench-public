# 阶段 C：开源代码与测试集发布决策

本文件是 Stage C 的发布规范。机器可读版本位于 `release/stage_c_release.v1.json`；资产级公开/私有分类仍以 `release/asset_policy.v1.json` 为准。

## 已定决策

| 项目 | 决策 |
|---|---|
| 名称 | **LongWoF-Bench** |
| 代码仓库 | `https://github.com/EvoMap/LongWoF-Bench-public` |
| 代码许可证 | **Apache-2.0** |
| 公开数据许可证 | **CC BY 4.0** |
| 官方评测 | **公开独立的非计分 dev 示例；778 题 test judge 全部隐藏并通过评测服务执行** |
| 逐题派生指标 | **公开**，仅限 `results/task_metrics.csv` 的白名单字段 |
| 数据版本 | `taskgenome-bench-public-data-v1.0.1.tar.gz` |
| 完整性与身份认证 | SHA-256 sidecar + Sigstore keyless bundle |
| 长期存档 | GitHub Release 资产为唯一渠道；v1.0.1 不使用 Zenodo |
| 当前渠道状态 | `pending_publication`；外部上传前 `anonymous_download: false` |

许可证选择是发布决策，但不是对当前 authoring 工作树的整体授权。发布前必须由权利人确认代码版权，并完成公开数据逐资产来源、第三方条款和 NOTICE 审计。私有资产不获得公开分发许可。

## 三类发布物

1. **干净代码仓库**：runner、公开单元测试、schemas、文档、脱敏研究结果和 `D0001` 小型公开 dev 示例。它由显式白名单导出，不包含 `tasks_final`、`_runs`、judge、gold、oracle 或 trace。
2. **独立公开数据包**：4,590 条公开资产，资产净字节数 576,791,843，固定 release ID 为 `4be80d65f0fd195aa11b168e`。公开包只含最终测试用的 legacy Skill 与 Gemini/Opus Gene；Agent Skill、rewritten Skill 变体和生成轨迹留在私有归档。归档不进入普通 Git 历史。
3. **永不公开的私有包**：judge、gold、reference、oracle、trace、provenance 和历史原始运行。私有 scenario 资产与历史 raw runs 可分成两个加密归档，但必须处于同一访问控制和保留策略下。

## 为什么 778 题 judge 全部隐藏

Research v1 的分母已经冻结为 778。如果从其中抽题公开 judge，再把剩余题称为官方 test，会改变分母并使既有结果与新服务口径混淆。因此公开 dev 使用独立 `D` 标识的合成示例，不占用任何官方 `T` 题；官方 778 题继续由隐藏 judge 服务评分。服务只返回通过/失败、协议版本和可公开的有限诊断类别，不返回 judge stdout、断言、gold 或差分细节。

## 逐题指标公开边界

可以公开 task ID、模型/条件、通过位、token 计数、协议、子集、资产摘要和脱敏来源运行 ID。不得公开 raw response、完整 prompt、候选代码、judge stdout/stderr、traceback、逐断言详情、gold、reference 或任何能反推隐藏检查的失败差分。

## 构建流程

```bash
# 1. 重新审计 778 题公开/私有边界。
python -B tools/release_assets.py audit --report release/quality_report.v1.json

# 2. 只构建公开资产；该目录不会生成 private/。
python -B tools/release_assets.py build-public --out /tmp/taskgenome-public-build

# 3. 导出代码白名单；最终发布时加 --require-clean。
python -B tools/stage_c_release.py export-code \
  --out /tmp/taskgenome-code

# 4. 打包公开数据并记录外层归档摘要。
python -B tools/stage_c_release.py package-data \
  --public /tmp/taskgenome-public-build/public \
  --out /tmp/taskgenome-artifacts \
  --record /tmp/taskgenome-artifacts/public_data_artifact.v1.json

# 5. 独立校验两个公开边界。
python -B tools/stage_c_release.py verify-code --root /tmp/taskgenome-code
python -B tools/stage_c_release.py verify-package \
  --archive /tmp/taskgenome-artifacts/taskgenome-bench-public-data-v1.0.1.tar.gz \
  --sha256-file /tmp/taskgenome-artifacts/taskgenome-bench-public-data-v1.0.1.tar.gz.sha256 \
  --record /tmp/taskgenome-artifacts/public_data_artifact.v1.json

# 6. 解包后验证公开 bundle 可被无密钥 dry-run 读取，并报告 778 题。
mkdir -p /tmp/taskgenome-artifacts/extracted
tar -xzf /tmp/taskgenome-artifacts/taskgenome-bench-public-data-v1.0.1.tar.gz \
  -C /tmp/taskgenome-artifacts/extracted
python -B tools/public_data_smoke.py \
  --bundle-root /tmp/taskgenome-artifacts/extracted/taskgenome-bench-public-data-v1.0.1 \
  --ids T0499 --models gemini_flash \
  --conditions no_context,with_skill,with_gene_opus

# 7. 将通过校验的代码树导入全新、单提交的 main 历史。
python -B tools/stage_c_release.py init-code-repo \
  --root /tmp/taskgenome-code \
  --out /tmp/taskgenome-public-repo \
  --policy /tmp/taskgenome-code/release/stage_c_release.v1.json

# 8. 在发布候选仓库中复核 refs、manifest、对象路径和完整历史。
python -B tools/stage_c_release.py verify-history \
  --root /tmp/taskgenome-public-repo
```

只有在摘要与已冻结记录一致、且发布审批完成后，才将临时记录提升为仓库中的
`release/public_data_artifact.v1.json`；上述命令不会覆盖已有记录。

私有包必须写到另一个访问控制目录，不能与公开 artifacts 共用输出根：

```bash
python -B tools/release_assets.py build-private --out /secure/taskgenome-private-v1
```

## 建立全新 Git 历史

导出的 `/tmp/taskgenome-code` 不带 `.git`。`init-code-repo` 会把它复制到空目录，使用策略中固定的组织 noreply 身份、提交日期和 `main` 分支，创建唯一的初始提交及 `v1.0.1` tag，然后运行 `verify-history`。相同的导出字节和策略会得到相同的提交树与提交摘要；源工作区的绝对路径不会进入 Git 对象。不要从 authoring 仓库复制 `.git`，不要把 filter-repo 当成唯一泄漏保证，也不要对当前远端进行未经备份的强推。

`verify-history` 除了再次执行 `verify-code`，还要求候选仓库只有 `main` 和预期版本 tag、历史恰好一个无父提交、提交作者/提交者为组织 noreply 身份，并将 `git rev-list --all --objects` 的路径与 `PUBLIC_CODE_MANIFEST.json`（含其祖先目录）逐项比对。它会扫描所有可达 blob、commit 和 tag 对象中的历史敏感字符串，拒绝旧报告/博客目录、`_runs` 目录以及旧个人邮箱/账号/仓库 URL，并用 `git fsck --unreachable` 确认没有残留不可达对象。审计报告只包含相对路径、摘要和计数，不记录 authoring 工作区路径。

本次发布选择了新建仓库 `EvoMap/LongWoF-Bench-public`，而不是让干净仓库占用 authoring 远端已有的 `EvoMap/LongWoF-Bench` URL。原因是该远端除发布分支外还带有 `main`、`codex/research-v1-release` 等分支，一旦转为公开会连同这些分支的全部对象一并暴露；而仓库可见性变更是外部状态变更，不能由本地构建脚本代替，也无法在事后撤回。authoring 远端因此保持非公开且名称不变。

若日后要让公开仓库改用不带 `-public` 后缀的名称，顺序必须是：先将 authoring 远端改名或迁入私有存档腾出该 URL，再重命名公开仓库；重命名后本文件、双语 README、`CITATION.cff` 与 `release/public_data_artifact.v1.json` 中钉住的地址都需要重新导出一轮。

## 签名与存档

发布归档后执行：

签名由 `.github/workflows/sign-release.yml` 执行，不在维护者的机器上进行：

```bash
gh workflow run sign-release.yml --repo EvoMap/LongWoF-Bench-public \
  -f tag=v1.0.1 -f archive=taskgenome-bench-public-data-v1.0.1.tar.gz
```

工作流会从 Release 下载归档、先核对 sha256 再签名、随后用
`cosign verify-blob` 自证 bundle 可验，最后把 bundle 传回同一个 Release。

之所以不用本机 `cosign sign-blob`：Sigstore 是 keyless 签名，会把签名者的 OIDC
身份写入证书并提交到 **Rekor 公开透明日志**，该日志不可删除。用个人账号在本机
签名会把个人邮箱永久公开，与本发布刻意采用组织 noreply 提交身份、并从代码与
历史中清除个人标识的做法相矛盾。工作流身份形如
`https://github.com/EvoMap/LongWoF-Bench-public/.github/workflows/sign-release.yml@refs/heads/main`，
不含个人信息，且指向一个任何人都能读到的文件。

下游验证时应同时固定身份与签发者：

```bash
cosign verify-blob --bundle "$ARCHIVE.sigstore.json" \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com \
  --certificate-identity-regexp '^https://github\.com/EvoMap/LongWoF-Bench-public/\.github/workflows/sign-release\.yml@' \
  "$ARCHIVE"
```

注意 `verify-package` 不校验 Sigstore bundle，它只负责归档字节与摘要；bundle
的可验性由上面的 `verify-blob` 保证。

当前已验证归档大小为 **321,889,608 bytes**，SHA-256 为 `9c80cad56a35a662eb339fc93d17a6954826b3b1d325aeaca9ede9ea10a11f70`；这是受限 Anthropic Skill 目录隔离后重新打包的归档（4,420 条资产），机器记录见 `release/public_data_artifact.v1.json`。签名前请先用 `verify-package` 确认本地归档摘要与该值一致，再对确认过的文件签名。

`release/public_data_artifact.v1.json` 中的 `publication` 对象是唯一的渠道
真相源：GitHub Release 的 tag、归档/sidecar/Sigstore URL 都由 `v1.0.1` 和文件名
固定生成。策略未声明的渠道整块省略而不是输出 `null`，因此某个渠道键缺失表示
“本版本不使用该渠道”，绝不表示“待创建”。仓库已有 `v1.0.1` tag，但尚未创建 GitHub Release、也尚未上传任何资产，因此仍不能把这些
确定性 URL 描述成已经可匿名下载的地址，也不能把已下线的 HF 包重新标作官方来源。

发布管理员完成外部上传后，必须先用匿名 HTTP 客户端下载以下三个 GitHub Release
资产，再执行校验：

```bash
BASE_URL=https://github.com/EvoMap/LongWoF-Bench-public/releases/download/v1.0.1
ARCHIVE=taskgenome-bench-public-data-v1.0.1.tar.gz
curl --fail --location "$BASE_URL/$ARCHIVE" --output "$ARCHIVE"
curl --fail --location "$BASE_URL/$ARCHIVE.sha256" --output "$ARCHIVE.sha256"
curl --fail --location "$BASE_URL/$ARCHIVE.sigstore.json" --output "$ARCHIVE.sigstore.json"
sha256sum --check "$ARCHIVE.sha256"
python -B tools/stage_c_release.py verify-package \
  --archive "$ARCHIVE" --sha256-file "$ARCHIVE.sha256" \
  --record release/public_data_artifact.v1.json
```

上传后才可将 `publication.status` 改为 `published`、将
`anonymous_download` 会随之变为 `true`；修改任何归档字节必须
重新生成 sidecar、Sigstore bundle 和 artifact 记录，且必须增加数据版本，不能覆写
`v1.0.1` 的摘要。

Sigstore bundle、SHA-256 sidecar 和 GitHub Release 必须指向同一字节流。任何文件变化都创建新的数据版本；不得覆盖 `v1.0.0`。论文与精确复现应引用 `v1.0.1` 这个不可变 tag 及归档的 SHA-256。

## 发布闸门

- 权利人批准 Apache-2.0 / CC BY 4.0，并完成第三方 NOTICE。
- `audit`、`build-public`、`verify-code`、`verify-package` 和完整测试全部通过。
- 对代码导出和解包后的数据各做一次独立 secret scan / malware scan。
- 随机抽查任务，确认 prompt/context/runtime 可用且 judge/gold/reference/oracle/trace 不存在。
- 在隔离环境演练评测服务，确认不会返回隐藏诊断。
- 保存 authoring 仓库和私有包的加密备份后，才处理最终 GitHub URL。
