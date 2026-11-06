# 配方 01：可选配置拆包 + 跨分片有状态串行折叠（`unwrap` + `for_each.state`）

## 一、 场景与需求

给定包含可选背景设定 `document.context_notes`（类型为 `[string, "null"]`）与有序分片列表 `document.sections` 的文档结构体，需执行两阶段处理：

1. **可选上下文条件初始化**：若 `document.context_notes` 非空，将其拆包并提炼为初始全局摘要 `rolling_summary`；若为 `null`，则保留 `graph.variables` 中的默认初始摘要。
2. **跨分片串行累积折叠**：按顺序逐个处理 `document.sections`，使第 $i$ 个分片读取并更新第 $i-1$ 个分片产出的 `rolling_summary`，同时收集每个分片独立产出的审查笔记至 `section_notes` 列表。

---

## 二、 核心原语组合要点

1. **条件拆包（[`builtin:unwrap`](../../reference/builtins/unwrap.md) 模式 A）**：省略 `default` 参数调用 `builtin:unwrap`，暴露 `.some`（产出非空 `valid_context_text`）与 `.none` 两个分支；`.some` 分支经 `init_context_summary` 覆写 `rolling_summary`，`.none` 分支直接汇合至下游。
2. **有状态串行折叠与单次收集并用（`for_each.state` + `for_each.collect`）**：在 `read_sections` 节点上同时声明 `state: [rolling_summary]`（强制 `concurrency: 1`）与 `collect: { single_note: section_notes }`：

```yaml
read_sections:
  for_each:
    source: document.sections
    item: current_section
    index: section_idx
    concurrency: 1                # 声明 state 时必须为 1
    state: [rolling_summary]      # 跨分片串行读写更新
    collect:
      single_note: section_notes  # 按分片顺序收集每次迭代产出的 single_note
```

---

## 三、 完整工作流定义

```yaml
# 1. 根节点输入契约：context_notes 为显式可空类型 [string, "null"]
input:
  document:
    type: object
    properties:
      title: { type: string }
      context_notes: { type: [string, "null"] }
      sections:
        type: array
        items: { type: string }
    required: [title, context_notes, sections]

# 2. 根节点输出契约
output:
  rolling_summary:
    type: string
  section_notes:
    type: array
    items: { type: string }

graph:
  # 3. 为 rolling_summary 提供默认初值（当 check_context 走 .none 分支时沿用该值）
  variables:
    rolling_summary: "（初始状态：无背景设定）"

  nodes:
    # 步骤 1：拆包可选字段 document.context_notes ([string, "null"] -> 非空 string)
    check_context:
      use: builtin:unwrap
      with:
        input:
          source: document.context_notes
        output:
          value: valid_context_text

    # 步骤 2（仅在 .some 分支下执行）：基于非空 valid_context_text 覆写初始 rolling_summary
    init_context_summary:
      input: [document, valid_context_text]
      output:
        rolling_summary: { type: string }
      run: "结合标题 variables.document.title 与背景资料 variables.valid_context_text 生成初始摘要，写入 output.rolling_summary。"

    # 步骤 3（汇合后执行）：有状态串行折叠 document.sections，更新 rolling_summary 并收集 section_notes
    read_sections:
      for_each:
        source: document.sections
        item: current_section
        index: section_idx
        concurrency: 1
        state: [rolling_summary]
        collect:
          single_note: section_notes
      input: [document, section_idx, current_section, rolling_summary]
      output:
        rolling_summary: { type: string }
        single_note: { type: string }
      run:
        - "处理《variables.document.title》的第 variables.section_idx 分片：variables.current_section。"
        - "结合上一分片累积摘要 variables.rolling_summary 更新全局摘要写入 output.rolling_summary。"
        - "生成当前分片的独立审查记录写入 output.single_note。"

  # 4. 控制流连边：.some 与 .none 两条路径在 read_sections 处汇合
  edges:
    - start -> check_context
    - check_context.some -> init_context_summary
    - init_context_summary -> read_sections
    - check_context.none -> read_sections
    - read_sections -> end
```
