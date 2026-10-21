param([string]$DataDir = (Join-Path $PSScriptRoot '..\..\.tmp-ui-check\data'))
$ErrorActionPreference = 'Stop'
# 造一份"用来点开看"的测试 vault（**只写这个临时数据目录，不碰真实 data/**）。
$root = Join-Path (Resolve-Path -LiteralPath (New-Item -ItemType Directory -Force -Path $DataDir)).Path 'vault'
New-Item -ItemType Directory -Force -Path (Join-Path $root 'entities') | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $root 'sections') | Out-Null

function W([string]$name, [string]$text) {
  $path = Join-Path $root $name
  [System.IO.File]::WriteAllText($path, $text, (New-Object System.Text.UTF8Encoding($false)))
}

W 'README.md' @'
---
title: 背景档案
kind: index
updated: 2026-10-12
---

# 背景档案索引

这里是**索引**，每一轮都会进模型上下文，所以只放指针：

- [[小林]]：用户本人
- [[小张]]：同事
- 还有 [[某位还没写的人]] 只被提到过

细节去 sections/ 里翻。
'@

W 'entities\小林.md' @'
---
title: 小林
kind: person
relation: 用户本人
status: 活跃
updated: 2026-10-12
tags: [用户, 前端]
aliases: [林, 小林同学]
sources: [20261010-101010-aaaa]
mood: 偏谨慎
channel: web
---

# 小林

用户本人，做**前端**，住在上海。

- 偏好：先看结论，再看理由
- 忌讳：`innerHTML` 直接塞模型输出

相关：[[小张]]、[[某个还不存在的名字]]

> 记于 2026-10-12
'@

W 'entities\小张.md' @'
---
title: 小张
kind: person
updated: 2026-10-11
tags:
  - 同事
aliases:
  - 张工
---

小张是同事，负责后端。

他和 [[小林]] 一起做那个项目。

1. 先约时间
2. 再对接口
'@

W 'entities\项目A.md' @'
---
title: 项目A
kind: project
status: 进行中
updated: 2026-10-09
tags: [项目]
---

项目 A，参与的还有 [[小张]]。
'@

W 'sections\偏好.md' @'
---
title: 偏好
kind: section
updated: 2026-10-12
tags: [习惯]
---

## 沟通偏好

- 结论先行
- 不要寒暄

涉及的人：[[小林]]
'@

W 'sections\奇怪的正文.md' @'
---
title: 奇怪的正文
kind: section
updated: 2026-10-12
---

# 里面塞了攻击载荷

<script>window.__PWNED__ = 1; alert('xss')</script>

<img src=x onerror="window.__PWNED__ = 2">

**粗体**、`行内代码`，还有一个指向 [[小张]] 的双链和一个不存在的 [[幽灵档案]]。

```js
const a = 1 < 2 && "</div>";
```
'@

# 很长的档案：故意超过界面查看上限（_VAULT_VIEW_MAX_CHARS = 200000）
$sb = New-Object System.Text.StringBuilder
[void]$sb.AppendLine('---')
[void]$sb.AppendLine('title: 长档案')
[void]$sb.AppendLine('kind: section')
[void]$sb.AppendLine('status: 参考')
[void]$sb.AppendLine('updated: 2026-10-01')
[void]$sb.AppendLine('---')
[void]$sb.AppendLine('')
$line = '- 这一行是为了把文件撑到二十万字符以上，用来触发查看器的截断提示。' + ('呀' * 8)
for ($i = 0; $i -lt 4500; $i++) { [void]$sb.AppendLine($line) }
W 'sections\长档案.md' $sb.ToString()

Get-ChildItem -Recurse -File $root | Select-Object FullName, Length | Format-Table -AutoSize
