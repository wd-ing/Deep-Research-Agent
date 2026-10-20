/** 报告 Markdown → .docx（纯前端，不经过后端）。
 *
 *  为什么自己转而不是引 pandoc/服务端转换：
 *  - 报告 md 的方言很窄（标题/段落/列表/表格/引用/代码/链接/强调），
 *    一套 ~200 行的 mdast 遍历就覆盖全；
 *  - 服务端转换要多一条请求路径和部署依赖，与本项目「本地优先」相悖。
 *
 *  输入口径：与「下载 .md」同一份原始 report——**不**套 `cleanReportMd`
 *  以外的第二层处理；唯一例外是 `# secN` 脚手架行（模型留的内部标记，
 *  报告渲染同样会去掉，导出 Word 变成大标题更不该留），规则与渲染层一致。
 *
 *  字体：eastAsia 只能写一个名字——主写 PingFang SC（macOS 原生），
 *  Windows 上没有这个名字时 Word 会回落到文档默认东亚字体（雅黑等），
 *  不会变成 tofu。正文 11pt、行距 1.4 倍。
 */
import { unified } from 'unified'
import remarkParse from 'remark-parse'
import remarkGfm from 'remark-gfm'
import {
  AlignmentType, BorderStyle, Document, ExternalHyperlink, HeadingLevel,
  LevelFormat, Packer, Paragraph, ShadingType, Table, TableCell, TableRow,
  TextRun, WidthType,
} from 'docx'
import type { IParagraphOptions } from 'docx'
import type { List, ListItem, PhrasingContent, Root, RootContent, Table as MdTable } from 'mdast'

const HEADING: Record<number, (typeof HeadingLevel)[keyof typeof HeadingLevel]> = {
  1: HeadingLevel.HEADING_1,
  2: HeadingLevel.HEADING_2,
  3: HeadingLevel.HEADING_3,
  4: HeadingLevel.HEADING_4,
  5: HeadingLevel.HEADING_5,
  6: HeadingLevel.HEADING_6,
}

/** 正文东亚字体。ascii/hAnsi 给西文一套干净的无衬线。 */
const FONT = { ascii: 'Helvetica Neue', hAnsi: 'Helvetica Neue', eastAsia: 'PingFang SC' }
const MONO = { ascii: 'Menlo', hAnsi: 'Menlo', eastAsia: 'PingFang SC' }

/** 模型偶尔留的 `# sec1` 脚手架标题（ReportView.cleanReportMd 的同一规则） */
const SEC_HDR = /^#\s*sec\d/

type InlineEl = TextRun | ExternalHyperlink
type InlineStyle = { bold?: boolean; italics?: boolean; strike?: boolean }

/** 行内节点 → TextRun/ExternalHyperlink。样式旗标往下传而不是事后改 run——
 *  TextRun 实例不是可展开的 options 对象，`**粗*斜***` 这类嵌套只有
 *  在构造时合并旗标才正确。未知节点降级取其文本内容，不丢字。 */
function inlines(children: readonly PhrasingContent[], st: InlineStyle = {}): InlineEl[] {
  const out: InlineEl[] = []
  for (const n of children) {
    switch (n.type) {
      case 'text':
        out.push(new TextRun({ text: n.value, ...st }))
        break
      case 'strong':
        out.push(...inlines(n.children, { ...st, bold: true }))
        break
      case 'emphasis':
        out.push(...inlines(n.children, { ...st, italics: true }))
        break
      case 'delete':
        out.push(...inlines(n.children, { ...st, strike: true }))
        break
      case 'inlineCode':
        out.push(new TextRun({ text: n.value, font: MONO, highlight: 'lightGray' }))
        break
      case 'link': {
        const inner = inlines(n.children, st)
        out.push(new ExternalHyperlink({
          link: n.url,
          children: inner.length
            ? inner
            : [new TextRun({ text: n.url, style: 'Hyperlink' })],
        }))
        break
      }
      case 'break':
        out.push(new TextRun({ break: 1 }))
        break
      case 'image':
        // 报告里没有真实图片；有的话只能留下 alt 文本
        if (n.alt) out.push(new TextRun({ text: `[图：${n.alt}]`, italics: true }))
        break
      default: {
        // html / footnoteReference 等：把字面内容留住，别静默丢字
        const v = (n as { value?: string }).value
        if (v) out.push(new TextRun({ text: v }))
      }
    }
  }
  return out
}

function para(children: readonly PhrasingContent[], opts: IParagraphOptions, st: InlineStyle = {}): Paragraph {
  return new Paragraph({ ...opts, children: inlines(children, st) })
}

/** 有序列表编号定义：每一级 decimal，缩进跟随 docx 默认。 */
const OL_LEVELS = Array.from({ length: 8 }, (_, i) => ({
  level: i,
  format: LevelFormat.DECIMAL,
  text: `%${i + 1}.`,
  alignment: AlignmentType.START,
}))

export async function reportToDocx(md: string, title: string): Promise<Blob> {
  // `# secN` 脚手架行与渲染层同规则去掉——那是内部标记不是内容
  const src = md.split('\n').filter((l) => !SEC_HDR.test(l)).join('\n')
  const tree = unified().use(remarkParse).use(remarkGfm).parse(src) as Root

  const out: (Paragraph | Table)[] = []
  // 每个**顶层**有序列表一个 instance：不然后面所有 ol 会接着
  // 前面那个列表继续数下去（1,2,3, 然后下一张表从 4 开始）。
  let olInstance = 0

  const emitList = (list: List, level: number) => {
    const instance = level === 0 ? ++olInstance : undefined
    for (const item of list.children as ListItem[]) {
      for (const c of item.children) {
        if (c.type === 'paragraph') {
          const listPr = list.ordered
            ? { numbering: { reference: 'ra-ol', level, instance } }
            : { bullet: { level } }
          out.push(para(c.children, {
            ...listPr,
            spacing: { before: 40, after: 40 },
          }))
        } else if (c.type === 'list') {
          emitList(c, level + 1)
        } else {
          // 列表项里的代码块/表格等：正常块级渲染，不挂编号
          emitBlock(c)
        }
      }
    }
  }

  const emitBlock = (n: RootContent) => {
    switch (n.type) {
      case 'heading':
        out.push(new Paragraph({
          heading: HEADING[n.depth] ?? HeadingLevel.HEADING_6,
          children: inlines(n.children),
        }))
        break
      case 'paragraph':
        out.push(para(n.children, { spacing: { after: 160 } }))
        break
      case 'blockquote':
        // 引用块 = 左 2pt 墨线 + 缩进（同前端 blockquote 的语言）
        for (const c of n.children) {
          if (c.type === 'paragraph') {
            out.push(para(c.children, {
              indent: { left: 360 },
              border: { left: { style: BorderStyle.SINGLE, size: 12, space: 8, color: '1a1a1a' } },
              spacing: { before: 60, after: 60 },
            }))
          } else {
            emitBlock(c)
          }
        }
        break
      case 'code': {
        for (const line of n.value.replace(/\n$/, '').split('\n')) {
          out.push(new Paragraph({
            children: [new TextRun({ text: line || ' ', font: MONO, size: 20 })],
            shading: { type: ShadingType.CLEAR, fill: 'F5F5F5', color: 'auto' },
            spacing: { before: 0, after: 0, line: 300 },
          }))
        }
        break
      }
      case 'list':
        emitList(n, 0)
        break
      case 'table':
        emitTable(n)
        break
      case 'thematicBreak':
        out.push(new Paragraph({
          children: [],
          border: { bottom: { style: BorderStyle.SINGLE, size: 4, space: 6, color: 'D4D4D4' } },
          spacing: { before: 160, after: 160 },
        }))
        break
      case 'html':
        break // md 里混的 html 在 Word 里不可渲染，跳过（内容在 html 里没有纯文本时本就不可见）
      default:
        if ((n as { children?: PhrasingContent[] }).children) {
          out.push(para((n as { children: PhrasingContent[] }).children, {}))
        }
    }
  }

  const emitTable = (t: MdTable) => {
    const aligns = t.align ?? []
    const alignOf = (i: number) =>
      aligns[i] === 'center' ? AlignmentType.CENTER
      : aligns[i] === 'right' ? AlignmentType.RIGHT
      : AlignmentType.LEFT
    const border = { style: BorderStyle.SINGLE, size: 2, color: 'B8B4AC' }
    out.push(new Table({
      width: { size: 100, type: WidthType.PERCENTAGE },
      rows: t.children.map((row, ri) => new TableRow({
        tableHeader: ri === 0,
        children: row.children.map((cell, ci) => new TableCell({
          margins: { top: 60, bottom: 60, left: 100, right: 100 },
          borders: { top: border, bottom: border, left: border, right: border },
          children: [para(cell.children as PhrasingContent[], {
            alignment: alignOf(ci),
          }, ri === 0 ? { bold: true } : {})],
        })),
      })),
    }))
  }

  for (const n of tree.children) emitBlock(n)

  const doc = new Document({
    creator: 'Deep Research Agent',
    title,
    styles: {
      default: {
        document: {
          run: { font: FONT, size: 22 }, // 22 半点 = 11pt
          paragraph: { spacing: { line: 336, lineRule: 'auto' } }, // ~1.4 倍行距
        },
      },
    },
    numbering: {
      config: [{ reference: 'ra-ol', levels: OL_LEVELS }],
    },
    sections: [{
      children: [
        new Paragraph({ heading: HeadingLevel.TITLE, children: [new TextRun(title)] }),
        ...out,
      ],
    }],
  })
  return Packer.toBlob(doc)
}
