import { defineConfig } from 'vitepress'
import { withMermaid } from 'vitepress-plugin-mermaid'

export default withMermaid(
  defineConfig({
    title: 'Axcutor Manual',
    description: '结构化并发控制流图（Structured CFG）工作流 DSL 文档与形式化规范',
    lang: 'zh-CN',

    // 支持通过环境变量 VITEPRESS_BASE 自动适配 GitHub Pages 子路径（如 /Axcutor/），本地开发默认 /
    base: process.env.VITEPRESS_BASE || '/',

    // 将各目录下的 README.md 映射为对应目录的 index.md，既保留仓库直接阅读习惯又支持站点目录首页
    rewrites(id) {
      return id.replace(/(^|\/)README\.md$/, '$1index.md')
    },

    markdown: {
      math: true
    },

    themeConfig: {
      search: {
        provider: 'local'
      },

      outline: {
        level: [2, 3],
        label: '本页目录'
      },

      nav: [
        { text: '首页', link: '/' },
        { text: '渐进式教程 (Guide)', link: '/docs/guide/', activeMatch: '^/docs/guide/' },
        { text: '实战配方 (Cookbook)', link: '/docs/cookbook/', activeMatch: '^/docs/cookbook/' },
        { text: '速查字典 (Reference)', link: '/docs/reference/', activeMatch: '^/docs/reference/' },
        { text: '引擎规范 (Spec)', link: '/spec/', activeMatch: '^/spec/' },
        { text: '早期原型 (Prototype)', link: '/prototype/', activeMatch: '^/prototype/' }
      ],

      sidebar: {
        '/docs/guide/': [
          {
            text: '渐进式教程导读',
            items: [
              { text: '教程总览与阅读路线', link: '/docs/guide/' }
            ]
          },
          {
            text: '01. 基础构建块与模块化',
            collapsed: false,
            items: [
              { text: '01. Basics：基础概念与最小工作流', link: '/docs/guide/01-foundations/01-basics' },
              { text: '02. Use：跨文件调用子工作流', link: '/docs/guide/01-foundations/02-use-subworkflows' },
              { text: '03. Inline Graph：内联块级子图', link: '/docs/guide/01-foundations/03-inline-graphs' }
            ]
          },
          {
            text: '02. 条件分支与动态循环',
            collapsed: false,
            items: [
              { text: '01. Branches：排他分支与按需产出', link: '/docs/guide/02-control-flow/01-branches' },
              { text: '02. 成环回边：动态条件循环 (While)', link: '/docs/guide/02-control-flow/02-cycles-and-back-edges' }
            ]
          },
          {
            text: '03. 结构化并发编排',
            collapsed: false,
            items: [
              { text: '01. 同构并发：集合遍历 (for_each)', link: '/docs/guide/03-concurrency/01-for-each' },
              { text: '02. 异构并发：Fork-Join 并发组', link: '/docs/guide/03-concurrency/02-fork-join' }
            ]
          },
          {
            text: '04. 作用域、执行护栏与容错',
            collapsed: false,
            items: [
              { text: '01. 图栈帧与命名空间', link: '/docs/guide/04-state-and-resilience/01-frames-and-scoping' },
              { text: '02. 执行护栏：max_steps 与 timeout_ms', link: '/docs/guide/04-state-and-resilience/02-guardrails-and-timeouts' },
              { text: '03. 错误处理：catch 与 fail 回滚', link: '/docs/guide/04-state-and-resilience/03-error-handling' }
            ]
          }
        ],

        '/docs/cookbook/': [
          {
            text: '实战组合配方索引',
            items: [
              { text: 'Cookbook 场景总览', link: '/docs/cookbook/' }
            ]
          },
          {
            text: '01. 循环收敛与动态队列',
            collapsed: false,
            items: [
              { text: '01. 生成-校验-修正闭环', link: '/docs/cookbook/01-loops-and-queues/01-generate-verify-refine-loop' },
              { text: '02. 候选节点短路早退回退链', link: '/docs/cookbook/01-loops-and-queues/02-short-circuit-fallback-chain' }
            ]
          },
          {
            text: '02. 集合流水线与嵌套并发',
            collapsed: false,
            items: [
              { text: '01. 可选配置拆包 + 有状态串行折叠', link: '/docs/cookbook/02-collection-and-concurrency/01-stateful-sequential-fold' },
              { text: '02. 并发多路分流、条件收集与扁平化', link: '/docs/cookbook/02-collection-and-concurrency/02-multi-way-triage-and-flatmap' },
              { text: '03. 外层并发遍历 + 内层子图循环', link: '/docs/cookbook/02-collection-and-concurrency/03-concurrent-map-with-inner-loop' }
            ]
          },
          {
            text: '03. 事务回滚与批处理容错',
            collapsed: false,
            items: [
              { text: '01. 内联子图多步原子事务 try-catch', link: '/docs/cookbook/03-transactions-and-resilience/01-multi-step-atomic-transaction' },
              { text: '02. 批处理异常隔离与死信队列 (DLQ)', link: '/docs/cookbook/03-transactions-and-resilience/02-batch-isolation-and-dead-letter-queue' }
            ]
          }
        ],

        '/docs/reference/': [
          {
            text: '用户速查字典',
            items: [
              { text: '速查字典总览', link: '/docs/reference/' },
              { text: '01. 节点与图字段速查表', link: '/docs/reference/01-node-and-graph-fields' },
              { text: '02. 6 种控制边语法速查表', link: '/docs/reference/02-edge-syntax-cheatsheet' },
              { text: '03. TypeSpec 类型写法速查表', link: '/docs/reference/03-typespec-cheatsheet' }
            ]
          },
          {
            text: '运行时内建节点字典 (builtin:*)',
            collapsed: false,
            items: [
              { text: '内建节点总览与通用约定', link: '/docs/reference/builtins/' },
              { text: 'builtin:unwrap (可空拆包/默认值)', link: '/docs/reference/builtins/unwrap' },
              { text: 'builtin:switch (布尔/枚举路由)', link: '/docs/reference/builtins/switch' },
              { text: 'builtin:array_shift (首元素弹出)', link: '/docs/reference/builtins/array-shift' },
              { text: 'builtin:array_append (末尾追加)', link: '/docs/reference/builtins/array-append' },
              { text: 'builtin:map_set (有序映射写入)', link: '/docs/reference/builtins/map-set' }
            ]
          }
        ],

        '/spec/': [
          {
            text: '语言与运行时形式化规范',
            items: [
              { text: '规范总纲与子系统架构', link: '/spec/' }
            ]
          },
          {
            text: '01. 语法结构与 AST 规范',
            collapsed: false,
            items: [
              { text: '01. 文档根结构与三类节点文法', link: '/spec/01-grammar-and-ast/01-document-and-node-grammar' },
              { text: '02. 数据绑定、with 与字段路径文法', link: '/spec/01-grammar-and-ast/02-binding-and-projection-grammar' },
              { text: '03. 控制边表达式与 CFG 文法', link: '/spec/01-grammar-and-ast/03-edge-and-cfg-grammar' }
            ]
          },
          {
            text: '02. 静态类型系统与符号表决议',
            collapsed: false,
            items: [
              { text: '01. TypeSpec 良构性公理', link: '/spec/02-type-system-and-symbols/01-typespec-well-formedness' },
              { text: '02. 类型等价判定与实例兼容性', link: '/spec/02-type-system-and-symbols/02-type-equivalence-and-compatibility' },
              { text: '03. 静态符号表两阶段决议算法', link: '/spec/02-type-system-and-symbols/03-two-phase-symbol-resolution' },
              { text: '04. 静态字段路径投影类型推导', link: '/spec/02-type-system-and-symbols/04-static-field-projection-typing' },
              { text: '05. 内建算子编译期泛型推导规则', link: '/spec/02-type-system-and-symbols/05-builtin-generic-inference' }
            ]
          },
          {
            text: '03. 控制流拓扑与静态数据流分析',
            collapsed: false,
            items: [
              { text: '01. CFG 拓扑穷尽、SESE 与可达性', link: '/spec/03-static-flow-analysis/01-cfg-topology-and-sese-validation' },
              { text: '02. 宿主初值互斥与局部帧无冲突', link: '/spec/03-static-flow-analysis/02-scoping-and-collision-rules' },
              { text: '03. 全路径分支敏感必达定值分析', link: '/spec/03-static-flow-analysis/03-branch-sensitive-definite-assignment' },
              { text: '04. 并发写集合两两互斥分析', link: '/spec/03-static-flow-analysis/04-concurrent-disjoint-write-analysis' }
            ]
          },
          {
            text: '04. 运行时状态机与异常展开模型',
            collapsed: false,
            items: [
              { text: '01. 栈帧生命周期与内存模型', link: '/spec/04-runtime-execution-model/01-frame-memory-model' },
              { text: '02. Fork-Join 与 for_each 调度状态机', link: '/spec/04-runtime-execution-model/02-fork-join-and-for-each-scheduler' },
              { text: '03. 运行护栏精确计量规则', link: '/spec/04-runtime-execution-model/03-guardrails-and-step-accounting' },
              { text: '04. 三级异常、retry/catch/fail 与栈展开', link: '/spec/04-runtime-execution-model/04-exception-handling-and-unwinding' }
            ]
          },
          {
            text: '05. 业务执行器 (run) ABI 契约',
            collapsed: false,
            items: [
              { text: '01. run 业务执行器标准 ABI 契约', link: '/spec/05-executor-abi/01-run-executor-contract' }
            ]
          }
        ],

        '/prototype/': [
          {
            text: '早期原型设计稿归档',
            items: [
              { text: '原型归档索引', link: '/prototype/' },
              { text: 'DSL.md (初始总体规范)', link: '/prototype/DSL' },
              { text: 'builtins.md (内建算子初稿)', link: '/prototype/builtins' },
              { text: 'error_handling.md (容错模型初稿)', link: '/prototype/error_handling' },
              { text: 'while_and_iter.md (循环与遍历初稿)', link: '/prototype/while_and_iter' },
              { text: 'while_plus_iter.md (嵌套子图初稿)', link: '/prototype/while_plus_iter' }
            ]
          }
        ]
      }
    }
  })
)
