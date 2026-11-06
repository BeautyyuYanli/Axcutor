# 配方 01：可选配置拆包 + 跨分片有状态串行折叠（`unwrap` + `for_each.state`）

## 场景与问题

给定一个包含可选背景设定 `document.context_notes`（类型为 `[string, "null"]`）和有序分片列表 `document.sections` 的文档结构体：
1. 若 `document.context_notes` 非空，先将其拆包并提炼为初始全局摘要 `rolling_summary`；若为 `null`，则直接使用 `graph.variables` 中的默认初始状态。
2. 按分片顺序逐个处理 `document.sections`：第 $i$ 个分片的处理需要读取第 $i-1$ 个分片更新后的 `rolling_summary` 并原地演进它，同时收集每个分片产出的独立审查笔记到 `section_notes` 列表中。

## 解决方案要点

* 使用 [`builtin:unwrap`](../../reference/builtins/unwrap.md)（不带 `default` 的双分支模式）检查 `document.context_notes` 是否非空；
* 在 `read_sections` 节点上同时声明 `for_each.state: [rolling_summary]`（强制 `concurrency: 1` 串行折叠）和 `for_each.collect: { single_note: section_notes }`。

---

## 完整工作流定义

```yaml
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
output:
  rolling_summary:
    type: string
  section_notes:
    type: array
    items: { type: string }

graph:
  variables:
    rolling_summary: "（初始状态：无背景设定）"

  nodes:
    # 1. 拆包可选的 document.context_notes ([string, "null"] -> 非空 string)
    check_context:
      use: builtin:unwrap
      with:
        input:
          source: document.context_notes
        output:
          value: valid_context_text

    # 2. 若存在背景设定，初始化 rolling_summary
    init_context_summary:
      input: [document, valid_context_text]
      output:
        rolling_summary: { type: string }
      run: "结合标题 variables.document.title 与背景资料 variables.valid_context_text 生成初始摘要，写入 output.rolling_summary。"

    # 3. 有状态串行折叠：跨分片演进 rolling_summary，同时收集 single_note
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

  edges:
    - start -> check_context
    - check_context.some -> init_context_summary
    - init_context_summary -> read_sections
    - check_context.none -> read_sections
    - read_sections -> end
```
