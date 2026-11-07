# 配方 01：可选配置拆包 + 跨分片有状态串行折叠（`unwrap` + `array_shift` + 成环回边）

## 一、 场景与需求

给定包含可选背景设定 `document.context_notes`（类型为 `[string, "null"]`）与有序分片列表 `document.sections` 的文档结构体，需执行两阶段处理：

1. **可选上下文条件初始化**：若 `document.context_notes` 非空，将其拆包并提炼为初始全局摘要 `rolling_summary`；若为 `null`，则保留 `graph.variables` 中的默认初始摘要。
2. **跨分片串行累积折叠**：按顺序逐个处理 `document.sections`，使每个有效分片读取并更新上一分片产出的 `rolling_summary`、将有效分片的独立审查笔记追加至 `section_notes` 列表，并在遇到无关分片时直接跳过（保持 `rolling_summary` 与 `section_notes` 原值不变）。

---

## 二、 核心原语组合要点

1. **条件拆包（[`builtin:unwrap`](../../reference/builtins/unwrap.md) 模式 A）**：省略 `default` 参数调用 `builtin:unwrap`，暴露 `.some`（产出非空 `valid_context_text`）与 `.none` 两个分支；`.some` 分支经 `init_context_summary` 覆写 `rolling_summary`，`.none` 分支直接汇合至队列初始化节点。
2. **工作队列消费与有状态回边折叠（[`builtin:assign`](../../reference/builtins/assign.md) + [`builtin:array_shift`](../../reference/builtins/array-shift.md) + [`builtin:array_append`](../../reference/builtins/array-append.md) + CFG 回边）**：
   * 入环前通过 `builtin:assign` 将 `document.sections` 绑定至工作队列 `pending_sections`。
   * 循环头 `next_section` 使用 `builtin:array_shift` 弹出首项 `current_section`，并绑定剩余切片副本至同名变量 `pending_sections`。
   * `read_single_section.updated` 分支覆写更新 `rolling_summary` 变量绑定，并经 `record_note` 追加 `single_note` 后回边至 `next_section`；`skipped` 分支直接声明 `output: []` 连回 `next_section`，无需重抄未修改的状态变量。

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
  # 3. 为跨轮状态提供默认初值，并为仅由内建算子写入的局部中间变量声明 TypeSpec
  variables:
    rolling_summary:
      type: string
      default: "（初始状态：无背景设定）"
    section_notes:
      type: array
      items: { type: string }
      default: []
    valid_context_text:
      type: string
    pending_sections:
      type: array
      items: { type: string }
    current_section:
      type: string

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

    # 步骤 3（汇合后执行）：将结构体字段 document.sections 初始化至工作队列 pending_sections
    init_queue:
      use: builtin:assign
      with:
        input:
          source: document.sections
        output:
          value: pending_sections

    # 步骤 4（循环头）：弹出首分片 current_section，绑定剩余切片副本至同名变量 pending_sections
    next_section:
      use: builtin:array_shift
      with:
        input:
          source: pending_sections
        output:
          item: current_section
          rest: pending_sections

    # 步骤 5：处理当前分片；skipped 分支直接声明 output: []，无需重抄 rolling_summary
    read_single_section:
      input: [document, current_section, rolling_summary]
      output:
        rolling_summary: { type: string }
        single_note: { type: string }
      branches:
        updated:
          description: "有效分片，覆写更新 rolling_summary 变量绑定并产出 single_note"
          output: [rolling_summary, single_note]
        skipped:
          description: "无关分片，不修改任何变量，直接进入下一轮"
          output: []
      run: |
        阅读《variables.document.title》的分片内容：variables.current_section。
        若分片有效，结合上一分片累积摘要 variables.rolling_summary 更新全局摘要写入 output.rolling_summary，生成当前分片的独立审查记录写入 output.single_note，并选择 updated 分支。
        若分片无关，直接选择 skipped 分支。

    # 步骤 6：仅在 updated 分支下将 single_note 追加至 section_notes
    record_note:
      use: builtin:array_append
      with:
        input:
          source: section_notes
          item: single_note
        output:
          result: section_notes

  # 4. 控制流连边：条件拆包汇合后进入 array_shift 成环回边折叠
  edges:
    - start -> check_context
    - check_context.some -> init_context_summary
    - init_context_summary -> init_queue
    - check_context.none -> init_queue
    - init_queue -> next_section
    - next_section.some -> read_single_section
    - read_single_section.updated -> record_note
    - record_note -> next_section                # 更新并记录后回边继续下一分片
    - read_single_section.skipped -> next_section # 跳过时直接回边，rolling_summary 保持原值不变
    - next_section.empty -> end                  # 队列消费完毕，退出循环
```
